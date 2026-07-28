from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable

from .adapters import adapter_for
from .config import AppConfig
from .models import AgentResult, ClaimedRun
from .state import StateStore


Notify = Callable[[str, str], Awaitable[None] | None]


class Scheduler:
    def __init__(self, config: AppConfig, state: StateStore, notify: Notify) -> None:
        self.config = config
        self.state = state
        self.notify = notify
        self.wake = asyncio.Event()
        self._stopping = False
        self._running: dict[str, asyncio.Task[None]] = {}

    def notify_work(self) -> None:
        self.wake.set()

    async def run(self) -> None:
        self.wake.set()
        while not self._stopping:
            self._reap_finished()
            claimed = self.state.claimed_run()
            if claimed is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            task = asyncio.create_task(self._run_claim(claimed), name=f"agent-message-{claimed.task_id}")
            self._running[claimed.task_id] = task

    def _reap_finished(self) -> None:
        for task_id, task in list(self._running.items()):
            if task.done():
                self._running.pop(task_id, None)
                task.result()

    async def _send(self, chat_id: str, text: str) -> None:
        result = self.notify(chat_id, text)
        if asyncio.iscoroutine(result):
            await result

    async def _run_claim(self, claimed: ClaimedRun) -> None:
        await self._send(claimed.chat_id, f"任务 {claimed.task_id} 已启动（{claimed.agent.value}）。")
        adapter = adapter_for(
            claimed.agent, codex_tool_network=self.config.service.codex_tool_network
        )
        last_progress_at = 0.0
        last_progress = ""

        async def on_pid(pid: int) -> None:
            self.state.set_run_pid(claimed.run_id, pid)

        async def on_session(session_id: str) -> None:
            self.state.set_task_session(claimed.task_id, session_id)

        async def on_progress(text: str) -> None:
            nonlocal last_progress_at, last_progress
            message = text.strip()[:800]
            now = asyncio.get_running_loop().time()
            if not message or message == last_progress or now - last_progress_at < 2.0:
                return
            last_progress_at = now
            last_progress = message
            await self._send(claimed.chat_id, f"任务 {claimed.task_id} 进度：{message}")

        try:
            result = await adapter.execute(claimed, on_pid, on_session, on_progress)
        except Exception as exc:  # The worker must survive adapter implementation errors.
            result = AgentResult(1, claimed.session_id, "", f"启动 agent 失败：{exc}")
        task = self.state.finish_run(
            run_id=claimed.run_id,
            task_id=claimed.task_id,
            exit_code=result.exit_code,
            session_id=result.session_id,
            final_message=result.final_message,
            error=result.error,
        )
        limit = self.config.service.final_message_limit
        summary = result.final_message[:limit]
        if result.error:
            await self._send(claimed.chat_id, f"任务 {task.id} 失败：{result.error}\n\n{summary}")
        else:
            await self._send(claimed.chat_id, f"任务 {task.id} 已完成。\n\n{summary}")
        self.wake.set()

    async def stop(self, task_id: str) -> bool:
        pid = self.state.running_pid(task_id)
        if pid is None:
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        await asyncio.sleep(self.config.service.stop_grace_seconds)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return True

    async def shutdown(self) -> None:
        self._stopping = True
        for task_id in list(self._running):
            await self.stop(task_id)
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
