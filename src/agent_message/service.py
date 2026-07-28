from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .commands import SimpleCommand, parse_command
from .config import AppConfig
from .feishu import FeishuGateway
from .models import InboundMessage
from .router import MessageRouter
from .scheduler import Scheduler
from .state import StateStore

LOGGER = logging.getLogger(__name__)
SendText = Callable[[str, str], None]


class BridgeService:
    """Owns the synchronous Feishu ingress and asynchronous local worker."""

    def __init__(self, config: AppConfig, send_text: SendText) -> None:
        self.config = config
        self.state = StateStore(config)
        self.router = MessageRouter(config, self.state)
        self._send_text = send_text
        self.loop: asyncio.AbstractEventLoop | None = None
        self.scheduler = Scheduler(config, self.state, self.enqueue_reply)
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
                await asyncio.to_thread(self._send_text, message.chat_id, message.content)
            except Exception as exc:
                LOGGER.warning("Feishu send failed for outbox %s: %s", message.id, exc)
                self.state.mark_outbox_failed(message.id, str(exc))
                await asyncio.sleep(2)
            else:
                self.state.mark_outbox_sent(message.id)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        interrupted = self.state.recover_interrupted()
        for task in interrupted:
            self.state.enqueue_outbox(task.chat_id, f"任务 {task.id} 因服务重启而中断；发送普通文本可恢复会话。")
        self._outbox_wake.set()
        outbox_task = asyncio.create_task(self._outbox_loop(), name="agent-message-outbox")
        scheduler_task = asyncio.create_task(self.scheduler.run(), name="agent-message-scheduler")
        try:
            await asyncio.gather(outbox_task, scheduler_task)
        finally:
            self._closing = True
            await self.scheduler.shutdown()
            outbox_task.cancel()
            scheduler_task.cancel()
            await asyncio.gather(outbox_task, scheduler_task, return_exceptions=True)
            self.state.close()


async def run_feishu_bridge(config: AppConfig) -> None:
    # The gateway must exist before the event loop starts so credential failures are immediate.
    holder: dict[str, BridgeService] = {}

    def on_message(message: InboundMessage) -> None:
        holder["service"].receive(message)

    gateway = FeishuGateway(config, on_message)
    service = BridgeService(config, gateway.send_text)
    holder["service"] = service
    gateway.start_in_thread()
    await service.run()
