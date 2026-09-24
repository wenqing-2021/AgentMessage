"""Compose the Feishu gateway, router, scheduler, and durable outbox."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from .commands import SimpleCommand, parse_command
from .router import MessageRouter
from .scheduler import Scheduler
from .spool import SpoolWatcher
from .sync_spool import SyncSpoolWatcher
from ..channels.feishu import FeishuError, FeishuGateway
from ..core.config import AppConfig
from ..core.models import InboundMessage, OutboxMessage
from ..core.state import StateStore

LOGGER = logging.getLogger(__name__)
SendText = Callable[[str, str], None]
# Sends an outbound attachment: (chat_id, kind, path).
SendMedia = Callable[[str, str, Path], None]
# Downloads an inbound attachment: (message_id, file_key, kind, dest, max_bytes).
DownloadResource = Callable[[str, str, str, Path, int], None]

# The outbox is strictly ordered, so one entry that can never succeed would block
# every later reply. Give up after a few backed-off attempts and move on.
_MAX_SEND_ATTEMPTS = 5
_RETRY_BASE_SECONDS = 2.0
_RETRY_MAX_SECONDS = 30.0


class BridgeService:
    """Owns the synchronous Feishu ingress and asynchronous local worker."""

    def __init__(
        self,
        config: AppConfig,
        send_text: SendText,
        send_media: SendMedia | None = None,
        downloader: DownloadResource | None = None,
    ) -> None:
        self.config = config
        self.state = StateStore(config)
        self.router = MessageRouter(config, self.state, downloader=downloader)
        self._send_text = send_text
        self._send_media = send_media
        self.loop: asyncio.AbstractEventLoop | None = None
        self.scheduler = Scheduler(config, self.state, self.enqueue_reply)
        self.watcher = SpoolWatcher(config, self.state, self._send_media_now) if send_media else None
        self._outbox_wake = asyncio.Event()
        self._closing = False

    def receive(self, inbound: InboundMessage) -> None:
        """Called by the Feishu SDK thread; it persists before returning its event ack."""
        replies = self.router.handle(inbound)
        for reply in replies:
            self.state.enqueue_outbox(inbound.chat_id, reply)
        if self.state.is_authorized(inbound.sender_open_id):
            try:
                command = parse_command(inbound.text)
            except ValueError:
                command = None
            if isinstance(command, SimpleCommand) and command.name == "stop" and command.task_id:
                task = self.state.get_task(command.task_id, inbound.chat_id)
                if self.loop and task and task.owner_open_id == inbound.sender_open_id:
                    asyncio.run_coroutine_threadsafe(self.scheduler.stop(command.task_id), self.loop)
        if self.loop:
            self.loop.call_soon_threadsafe(self.scheduler.notify_work)
            self.loop.call_soon_threadsafe(self._outbox_wake.set)

    async def enqueue_reply(self, chat_id: str, text: str) -> None:
        self.state.enqueue_outbox(chat_id, text)
        self._outbox_wake.set()

    async def _send_media_now(self, chat_id: str, kind: str, path: Path) -> None:
        """Offload one transfer to a worker thread; the spool watcher stays async."""
        if self._send_media is None:
            raise FeishuError("media sender is not configured")
        await asyncio.to_thread(self._send_media, chat_id, kind, path)

    async def _deliver(self, message: OutboxMessage) -> bool:
        """Deliver one outbox entry; False means it was terminally marked failed."""
        if message.kind == "text":
            await asyncio.to_thread(self._send_text, message.chat_id, message.content)
            return True
        if self._send_media is None:
            raise FeishuError("media sender is not configured")
        path = Path(message.file_path or "")
        if not path.is_file():
            await asyncio.to_thread(
                self._send_text,
                message.chat_id,
                f"{message.content}（未发送：文件已不存在）",
            )
            self.state.mark_outbox_terminal(message.id, "file missing before send")
            return False
        await asyncio.to_thread(self._send_media, message.chat_id, message.kind, path)
        return True

    async def _outbox_loop(self) -> None:
        while not self._closing:
            message = self.state.next_outbox()
            if message is None:
                self._outbox_wake.clear()
                try:
                    await asyncio.wait_for(self._outbox_wake.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                delivered = await self._deliver(message)
            except Exception as exc:
                LOGGER.warning("Feishu send failed for outbox %s: %s", message.id, exc)
                attempts = self.state.mark_outbox_failed(message.id, str(exc))
                if self._give_up(message, attempts, str(exc)):
                    continue
                await asyncio.sleep(
                    min(_RETRY_BASE_SECONDS * (2 ** (attempts - 1)), _RETRY_MAX_SECONDS)
                )
            else:
                if delivered:
                    self.state.mark_outbox_sent(message.id)

    def _give_up(self, message: OutboxMessage, attempts: int, error: str) -> bool:
        """Drop an entry that keeps failing so it cannot block later replies.

        Attachments report the failure back to the chat; text entries only reach
        the log, because a notice is itself a text entry.
        """
        if attempts < _MAX_SEND_ATTEMPTS:
            return False
        self.state.mark_outbox_terminal(message.id, error)
        LOGGER.error(
            "Dropping outbox %s after %s attempts (%s): %s",
            message.id,
            attempts,
            message.kind,
            error,
        )
        if message.kind != "text":
            self.state.enqueue_outbox(message.chat_id, self._failure_notice(message, error))
        return True

    @staticmethod
    def _failure_notice(message: OutboxMessage, error: str) -> str:
        return (
            f"{message.content} 发送失败，已跳过（共尝试 {_MAX_SEND_ATTEMPTS} 次）。\n"
            f"原因：{error[:300]}"
        )

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        for run_id in self.state.unfinished_agent_sync_runs():
            SyncSpoolWatcher(self.state).capture_run(run_id)
        interrupted = self.state.recover_interrupted()
        for task in interrupted:
            self.state.enqueue_outbox(task.chat_id, f"任务 {task.id} 因服务重启而中断；发送普通文本可恢复会话。")
        self._outbox_wake.set()
        outbox_task = asyncio.create_task(self._outbox_loop(), name="agent-message-outbox")
        scheduler_task = asyncio.create_task(self.scheduler.run(), name="agent-message-scheduler")
        sync_task = asyncio.create_task(SyncSpoolWatcher(self.state).run(), name="agent-message-sync")
        watcher_task = (
            asyncio.create_task(self.watcher.run(), name="agent-message-spool")
            if self.watcher is not None
            else None
        )
        try:
            tasks = [outbox_task, scheduler_task, sync_task] + ([watcher_task] if watcher_task else [])
            await asyncio.gather(*tasks)
        finally:
            self._closing = True
            if self.watcher is not None:
                self.watcher.stop()
            await self.scheduler.shutdown()
            outbox_task.cancel()
            scheduler_task.cancel()
            sync_task.cancel()
            if watcher_task is not None:
                watcher_task.cancel()
            await asyncio.gather(
                outbox_task,
                scheduler_task,
                sync_task,
                *([watcher_task] if watcher_task is not None else []),
                return_exceptions=True,
            )
            self.state.close()


async def run_feishu_bridge(config: AppConfig) -> None:
    # The gateway must exist before the event loop starts so credential failures are immediate.
    holder: dict[str, BridgeService] = {}

    def on_message(message: InboundMessage) -> None:
        holder["service"].receive(message)

    gateway = FeishuGateway(config, on_message)
    service = BridgeService(
        config,
        gateway.send_text,
        send_media=gateway.send_media,
        downloader=gateway.download_resource,
    )
    holder["service"] = service
    gateway.start_in_thread()
    await service.run()
