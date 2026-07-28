from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_message.adapters import AgentAdapter
from agent_message.models import AgentKind, AgentResult, ClaimedRun, TaskStatus
from agent_message.scheduler import Scheduler
from agent_message.state import StateStore

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

                with patch("agent_message.scheduler.asyncio.wait_for", timeout_once):
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
                with patch("agent_message.scheduler.adapter_for", return_value=FinishedAdapter()):
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
                with patch("agent_message.scheduler.adapter_for", return_value=ProgressAdapter()):
                    asyncio.run(scheduler._run_claim(claimed))
                self.assertTrue(any("进度：Codex 正在执行本地命令。" in text for _, text in replies))
            finally:
                state.close()
