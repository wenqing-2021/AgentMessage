"""Coordinate queued agent tasks and report their progress."""

from __future__ import annotations

import asyncio
import os
import re
import signal
from collections.abc import Awaitable, Callable

from ..agents.adapters import adapter_for
from ..core.config import AppConfig
from ..core.models import AgentResult, ClaimedRun, ContainerJob
from ..core.state import StateStore
from ..runtimes.container.runner import terminate_container_process


Notify = Callable[[str, str], Awaitable[None] | None]
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_GPU_POLL_SECONDS = 2.0
_GPU_PROGRESS_SECONDS = 30.0


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
            claimed.agent,
            codex_tool_network=self.config.service.codex_tool_network,
            app_config=self.config,
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

        gpu_monitor_stop = asyncio.Event()
        gpu_monitor = asyncio.create_task(
            self._monitor_gpu_progress(claimed, gpu_monitor_stop),
            name=f"agent-message-gpu-progress-{claimed.task_id}",
        )
        container_monitor = asyncio.create_task(
            self._monitor_container_progress(claimed, gpu_monitor_stop),
            name=f"agent-message-container-progress-{claimed.task_id}",
        )
        try:
            try:
                result = await adapter.execute(claimed, on_pid, on_session, on_progress)
            except Exception as exc:  # The worker must survive adapter implementation errors.
                result = AgentResult(1, claimed.session_id, "", f"启动 agent 失败：{exc}")
        finally:
            gpu_monitor_stop.set()
            await asyncio.gather(gpu_monitor, container_monitor)
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

    async def _monitor_gpu_progress(
        self, claimed: ClaimedRun, stop_event: asyncio.Event
    ) -> None:
        current_job_id: int | None = None
        current_status: str | None = None
        last_output = ""
        last_sent_at = 0.0
        while True:
            job = self.state.latest_gpu_job(claimed.task_id)
            now = asyncio.get_running_loop().time()
            if job is not None:
                if job.id != current_job_id:
                    current_job_id = job.id
                    current_status = None
                    last_output = ""
                    last_sent_at = 0.0
                    await self._send(
                        claimed.chat_id,
                        f"任务 {claimed.task_id} GPU 作业 {job.id} 已启动。",
                    )
                if job.status in {"running", "stopping"}:
                    raw = "\n".join(self.state.tail_gpu_log(claimed.task_id, 12))
                    output = _ANSI_ESCAPE.sub("", raw).strip()[-1000:]
                    if (
                        output
                        and output != last_output
                        and now - last_sent_at >= _GPU_PROGRESS_SECONDS
                    ):
                        last_output = output
                        last_sent_at = now
                        await self._send(
                            claimed.chat_id,
                            f"任务 {claimed.task_id} GPU 进度：\n{output}",
                        )
                if job.status != current_status:
                    current_status = job.status
                    if job.status in {"succeeded", "failed", "stopped", "interrupted"}:
                        labels = {
                            "succeeded": "已完成",
                            "failed": "失败",
                            "stopped": "已停止",
                            "interrupted": "已中断",
                        }
                        suffix = f"：{job.error}" if job.error else "。"
                        await self._send(
                            claimed.chat_id,
                            f"任务 {claimed.task_id} GPU 作业 {job.id} "
                            f"{labels[job.status]}{suffix}",
                        )
            if stop_event.is_set():
                return
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_GPU_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _monitor_container_progress(
        self, claimed: ClaimedRun, stop_event: asyncio.Event
    ) -> None:
        current_job_id: int | None = None
        current_status: str | None = None
        last_output = ""
        last_sent_at = 0.0
        while True:
            job = self.state.latest_container_job(claimed.task_id)
            now = asyncio.get_running_loop().time()
            if job is not None:
                if job.id != current_job_id:
                    current_job_id = job.id
                    current_status = None
                    last_output = ""
                    last_sent_at = 0.0
                    await self._send(
                        claimed.chat_id,
                        f"任务 {claimed.task_id} 容器作业 {job.id} "
                        f"已启动（{job.container_name}）。",
                    )
                if job.status in {"running", "stopping"}:
                    raw = "\n".join(self.state.tail_container_log(claimed.task_id, 12))
                    output = _ANSI_ESCAPE.sub("", raw).strip()[-1000:]
                    if (
                        output
                        and output != last_output
                        and now - last_sent_at >= _GPU_PROGRESS_SECONDS
                    ):
                        last_output = output
                        last_sent_at = now
                        await self._send(
                            claimed.chat_id,
                            f"任务 {claimed.task_id} 容器进度：\n{output}",
                        )
                if job.status != current_status:
                    current_status = job.status
                    if job.status in {"succeeded", "failed", "stopped", "interrupted"}:
                        labels = {
                            "succeeded": "已完成",
                            "failed": "失败",
                            "stopped": "已停止",
                            "interrupted": "已中断",
                        }
                        suffix = f"：{job.error}" if job.error else "。"
                        await self._send(
                            claimed.chat_id,
                            f"任务 {claimed.task_id} 容器作业 {job.id} "
                            f"{labels[job.status]}{suffix}",
                        )
            if stop_event.is_set():
                return
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_GPU_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _signal_process_groups(pids: list[int], sig: signal.Signals) -> None:
        for pid in pids:
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                pass

    async def stop(self, task_id: str) -> bool:
        pid = self.state.running_pid(task_id)
        gpu_pids = self.state.mark_gpu_jobs_stopping(task_id)
        container_jobs = self.state.mark_container_jobs_stopping(task_id)
        if pid is None and not gpu_pids and not container_jobs:
            return False
        pids = ([pid] if pid is not None else []) + gpu_pids + [
            job.pid for job in container_jobs if job.pid is not None
        ]
        await self._signal_container_jobs(container_jobs, signal.SIGTERM)
        self._signal_process_groups(pids, signal.SIGTERM)
        await asyncio.sleep(self.config.service.stop_grace_seconds)
        await self._signal_container_jobs(container_jobs, signal.SIGKILL)
        self._signal_process_groups(pids, signal.SIGKILL)
        return True

    @staticmethod
    async def _signal_container_jobs(
        jobs: list[ContainerJob], sig: signal.Signals
    ) -> None:
        # `/stop` is already a serialized, bounded control path. Keep this synchronous so it also
        # works on the Python 3.10/WSL event-loop combinations that motivated the child watcher fix.
        for job in jobs:
            if job.container_pid is not None:
                terminate_container_process(job.container_name, job.container_pid, sig)

    async def shutdown(self) -> None:
        self._stopping = True
        for task_id in list(self._running):
            await self.stop(task_id)
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
