from __future__ import annotations

import asyncio
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_message.agents.adapters import AgentAdapter
from agent_message.core.models import AgentKind, AgentResult, ClaimedRun, TaskStatus
from agent_message.core.state import StateStore
from agent_message.orchestration.scheduler import Scheduler

from tests.helpers import make_config


class FinishedAdapter(AgentAdapter):
    kind = AgentKind.CODEX
    executable = "unused"

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        raise AssertionError("execute is overridden")

    async def execute(self, run: ClaimedRun, on_pid, on_session=None, on_progress=None):  # type: ignore[no-untyped-def]
        callback = on_pid(12345)
        if asyncio.iscoroutine(callback):
            await callback
        return AgentResult(0, "thread-1", "all done")


class ProgressAdapter(FinishedAdapter):
    async def execute(self, run: ClaimedRun, on_pid, on_session=None, on_progress=None):  # type: ignore[no-untyped-def]
        result = await super().execute(run, on_pid, on_session, on_progress)
        if on_progress:
            callback = on_progress("Codex 正在执行本地命令。")
            if asyncio.iscoroutine(callback):
                await callback
        return result


class QoderFinishedAdapter(FinishedAdapter):
    kind = AgentKind.QODER

    async def execute(self, run: ClaimedRun, on_pid, on_session=None, on_progress=None):  # type: ignore[no-untyped-def]
        callback = on_pid(23456)
        if asyncio.iscoroutine(callback):
            await callback
        if on_session:
            session_callback = on_session("qoder-thread")
            if asyncio.iscoroutine(session_callback):
                await session_callback
        return AgentResult(0, "qoder-thread", "qoder done")


class SchedulerTests(unittest.TestCase):
    def test_idle_timeout_is_normal_in_python_310(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp))
            state = StateStore(config)
            try:
                scheduler = Scheduler(config, state, lambda _chat, _text: None)

                async def timeout_once(awaitable, **_kwargs) -> None:  # type: ignore[no-untyped-def]
                    awaitable.close()
                    scheduler._stopping = True
                    raise asyncio.TimeoutError

                with patch("agent_message.orchestration.scheduler.asyncio.wait_for", timeout_once):
                    asyncio.run(scheduler.run())
            finally:
                state.close()

    def test_run_finishes_task_and_enqueues_lifecycle_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp))
            state = StateStore(config)
            try:
                task = state.create_task(
                    project_alias="alpha",
                    agent=AgentKind.CODEX,
                    chat_id="chat-1",
                    owner_open_id="ou-1",
                    prompt="work",
                )
                claimed = state.claimed_run()
                assert claimed is not None
                replies: list[tuple[str, str]] = []

                async def notify(chat_id: str, text: str) -> None:
                    replies.append((chat_id, text))

                scheduler = Scheduler(config, state, notify)
                with patch(
                    "agent_message.orchestration.scheduler.adapter_for",
                    return_value=FinishedAdapter(),
                ):
                    asyncio.run(scheduler._run_claim(claimed))
                completed = state.get_task(task.id)
                assert completed is not None
                self.assertEqual(completed.status, TaskStatus.SUCCEEDED)
                self.assertEqual(completed.session_id, "thread-1")
                self.assertEqual(len(replies), 2)
                self.assertIn("已启动", replies[0][1])
                self.assertIn("已完成", replies[1][1])
            finally:
                state.close()

    def test_qoder_run_persists_session_and_finishes_normally(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), qoder_only_projects=("alpha",))
            state = StateStore(config)
            try:
                task = state.create_task(
                    project_alias="alpha",
                    agent=AgentKind.QODER,
                    chat_id="chat-1",
                    owner_open_id="ou-1",
                    prompt="work",
                )
                claimed = state.claimed_run()
                assert claimed is not None
                replies: list[str] = []
                scheduler = Scheduler(config, state, lambda _chat, text: replies.append(text))
                with patch(
                    "agent_message.orchestration.scheduler.adapter_for",
                    return_value=QoderFinishedAdapter(),
                ):
                    asyncio.run(scheduler._run_claim(claimed))
                completed = state.get_task(task.id)
                assert completed is not None
                self.assertEqual(completed.status, TaskStatus.SUCCEEDED)
                self.assertEqual(completed.session_id, "qoder-thread")
                self.assertTrue(any("qoder" in text for text in replies))
            finally:
                state.close()

    def test_run_forwards_safe_progress_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp))
            state = StateStore(config)
            try:
                task = state.create_task(
                    project_alias="alpha",
                    agent=AgentKind.CODEX,
                    chat_id="chat-1",
                    owner_open_id="ou-1",
                    prompt="work",
                )
                claimed = state.claimed_run()
                assert claimed is not None
                replies: list[tuple[str, str]] = []

                async def notify(chat_id: str, text: str) -> None:
                    replies.append((chat_id, text))

                scheduler = Scheduler(config, state, notify)
                with patch(
                    "agent_message.orchestration.scheduler.adapter_for",
                    return_value=ProgressAdapter(),
                ):
                    asyncio.run(scheduler._run_claim(claimed))
                self.assertTrue(any("进度：Codex 正在执行本地命令。" in text for _, text in replies))
            finally:
                state.close()

    def test_sandbox_monitor_forwards_sanitized_bounded_log_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), sandbox_gpu_projects=("alpha",))
            state = StateStore(config)
            try:
                task = state.create_task(
                    project_alias="alpha",
                    agent=AgentKind.CODEX,
                    chat_id="chat-1",
                    owner_open_id="ou-1",
                    prompt="train",
                )
                claimed = state.claimed_run()
                assert claimed is not None
                job = state.create_sandbox_job(
                    task_id=task.id,
                    run_id=claimed.run_id,
                    argv=["python", "train.py"],
                    cwd=".",
                )
                job.log_path.parent.mkdir(parents=True)
                job.log_path.write_text(
                    "\x1b[31msecret-color\x1b[0m\n" + ("x" * 1500),
                    encoding="utf-8",
                )
                replies: list[str] = []
                scheduler = Scheduler(config, state, lambda _chat, text: replies.append(text))

                async def monitor_once() -> None:
                    stop = asyncio.Event()
                    stop.set()
                    await scheduler._monitor_sandbox_progress(claimed, stop)

                asyncio.run(monitor_once())
                self.assertEqual(len(replies), 2)
                self.assertIn(f"沙箱作业 {job.id} 已启动", replies[0])
                self.assertIn("沙箱进度", replies[1])
                self.assertNotIn("\x1b", replies[1])
                self.assertLessEqual(len(replies[1].split("：\n", 1)[1]), 1000)
            finally:
                state.close()

    def test_stop_signals_codex_and_gpu_process_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), sandbox_gpu_projects=("alpha",))
            state = StateStore(config)
            try:
                task = state.create_task(
                    project_alias="alpha",
                    agent=AgentKind.CODEX,
                    chat_id="chat-1",
                    owner_open_id="ou-1",
                    prompt="train",
                )
                claimed = state.claimed_run()
                assert claimed is not None
                state.set_run_pid(claimed.run_id, 111)
                job = state.create_sandbox_job(
                    task_id=task.id,
                    run_id=claimed.run_id,
                    argv=["python", "train.py"],
                    cwd=".",
                )
                state.set_sandbox_job_pid(job.id, 222)
                scheduler = Scheduler(config, state, lambda _chat, _text: None)

                async def no_sleep(_seconds: float) -> None:
                    return None

                with patch("agent_message.orchestration.scheduler.os.killpg") as killpg, patch(
                    "agent_message.orchestration.scheduler.asyncio.sleep", no_sleep
                ):
                    stopped = asyncio.run(scheduler.stop(task.id))

                self.assertTrue(stopped)
                self.assertEqual(
                    [call.args for call in killpg.call_args_list],
                    [
                        (111, signal.SIGTERM),
                        (222, signal.SIGTERM),
                        (111, signal.SIGKILL),
                        (222, signal.SIGKILL),
                    ],
                )
                latest = state.latest_sandbox_job(task.id)
                assert latest is not None
                self.assertEqual(latest.status, "stopping")
            finally:
                state.close()

    def test_container_monitor_forwards_sanitized_bounded_log_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), container_projects=("alpha",))
            state = StateStore(config)
            try:
                task = state.create_task(
                    project_alias="alpha",
                    agent=AgentKind.CODEX,
                    chat_id="chat-1",
                    owner_open_id="ou-1",
                    prompt="test",
                )
                claimed = state.claimed_run()
                assert claimed is not None
                job = state.create_container_job(
                    task_id=task.id,
                    run_id=claimed.run_id,
                    container_name="alpha-container",
                    argv=["python", "test.py"],
                    cwd=".",
                )
                job.log_path.parent.mkdir(parents=True)
                job.log_path.write_text(
                    "\x1b[31mcontainer-color\x1b[0m\n" + ("x" * 1500),
                    encoding="utf-8",
                )
                replies: list[str] = []
                scheduler = Scheduler(config, state, lambda _chat, text: replies.append(text))

                async def monitor_once() -> None:
                    stop = asyncio.Event()
                    stop.set()
                    await scheduler._monitor_container_progress(claimed, stop)

                asyncio.run(monitor_once())
                self.assertEqual(len(replies), 2)
                self.assertIn(f"容器作业 {job.id} 已启动", replies[0])
                self.assertIn("容器进度", replies[1])
                self.assertNotIn("\x1b", replies[1])
                self.assertLessEqual(len(replies[1].split("：\n", 1)[1]), 1000)
            finally:
                state.close()

    def test_stop_signals_codex_and_container_process_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), container_projects=("alpha",))
            state = StateStore(config)
            try:
                task = state.create_task(
                    project_alias="alpha",
                    agent=AgentKind.CODEX,
                    chat_id="chat-1",
                    owner_open_id="ou-1",
                    prompt="train",
                )
                claimed = state.claimed_run()
                assert claimed is not None
                state.set_run_pid(claimed.run_id, 111)
                job = state.create_container_job(
                    task_id=task.id,
                    run_id=claimed.run_id,
                    container_name="alpha-container",
                    argv=["python", "train.py"],
                    cwd=".",
                )
                state.set_container_job_pid(job.id, 333)
                state.set_container_job_inner_pid(job.id, 444)
                scheduler = Scheduler(config, state, lambda _chat, _text: None)

                async def no_sleep(_seconds: float) -> None:
                    return None

                with patch("agent_message.orchestration.scheduler.os.killpg") as killpg, patch(
                    "agent_message.orchestration.scheduler.terminate_container_process"
                ) as terminate, patch(
                    "agent_message.orchestration.scheduler.asyncio.sleep", no_sleep
                ):
                    stopped = asyncio.run(scheduler.stop(task.id))

                self.assertTrue(stopped)
                self.assertEqual(
                    [call.args for call in terminate.call_args_list],
                    [
                        ("alpha-container", 444, signal.SIGTERM),
                        ("alpha-container", 444, signal.SIGKILL),
                    ],
                )
                self.assertEqual(
                    [call.args for call in killpg.call_args_list],
                    [
                        (111, signal.SIGTERM),
                        (333, signal.SIGTERM),
                        (111, signal.SIGKILL),
                        (333, signal.SIGKILL),
                    ],
                )
                latest = state.latest_container_job(task.id)
                assert latest is not None
                self.assertEqual(latest.status, "stopping")
            finally:
                state.close()
