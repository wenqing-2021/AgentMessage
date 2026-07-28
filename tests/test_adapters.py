from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from agent_message.adapters import AgentAdapter, CodexAdapter, QoderAdapter
from agent_message.models import AgentKind, ClaimedRun

from tests.helpers import make_config


def make_run(root: Path, session_id: str | None = None) -> ClaimedRun:
    config = make_config(root)
    return ClaimedRun(
        run_id=1,
        task_id="task-1",
        message_id=1,
        project_alias="alpha",
        project_path=config.projects["alpha"].path,
        agent=AgentKind.CODEX,
        session_id=session_id,
        prompt="say hello",
        chat_id="chat-1",
        log_path=root / "logs" / "task-1" / "run-1.jsonl",
    )


class FakeAdapter(AgentAdapter):
    kind = AgentKind.CODEX
    executable = sys.executable

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        return [
            self.executable,
            "-c",
            "import json; print(json.dumps({'session_id':'thread-1','text':'finished'}))",
        ]


class LargeJsonLineAdapter(AgentAdapter):
    kind = AgentKind.CODEX
    executable = sys.executable

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        script = (
            "import json; "
            "print(json.dumps({'thread_id': 'thread-large'})); "
            "print(json.dumps({'item': {'text': 'x' * (128 * 1024)}})); "
            "print(json.dumps({'text': 'finished after a large event'}))"
        )
        return [self.executable, "-c", script]


class ProgressJsonAdapter(AgentAdapter):
    kind = AgentKind.CODEX
    executable = sys.executable

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        events = [
            {"type": "thread.started", "thread_id": "thread-progress"},
            {"type": "turn.started"},
            {"type": "item.started", "item": {"type": "reasoning"}},
            {"type": "item.started", "item": {"type": "command_execution"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "已检查配置。"}},
        ]
        return [self.executable, "-c", f"import json; print(*map(json.dumps, {events!r}), sep='\\n')"]


class AdapterTests(unittest.TestCase):
    def test_codex_uses_workspace_write_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            command = CodexAdapter().build_command(make_run(root), root / "final")
            self.assertIn("workspace-write", command)
            self.assertIn("--skip-git-repo-check", command)
            self.assertIn("sandbox_workspace_write.network_access=false", command)
            network_command = CodexAdapter(tool_network_enabled=True).build_command(
                make_run(root), root / "final"
            )
            self.assertIn("sandbox_workspace_write.network_access=true", network_command)
            resumed = CodexAdapter().build_command(make_run(root, "thread-1"), root / "final")
            self.assertIn("resume", resumed)
            self.assertIn("--skip-git-repo-check", resumed)
            self.assertNotIn("--ask-for-approval", command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", resumed)

    def test_qoder_has_no_yolo_and_blocks_network_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            command = QoderAdapter().build_command(make_run(Path(temp)), Path(temp) / "final")
            self.assertNotIn("--yolo", command)
            blocked = command[command.index("--disallowed-tools") + 1]
            self.assertIn("WebFetch", blocked)
            self.assertIn("Bash(git push:*)", blocked)

    def test_json_process_result_is_logged_and_session_is_captured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = make_run(Path(temp))

            async def execute() -> tuple[int, str | None, str]:
                result = await FakeAdapter().execute(run, lambda _: None)
                return result.exit_code, result.session_id, result.final_message

            exit_code, session_id, message = asyncio.run(execute())
            self.assertEqual(exit_code, 0)
            self.assertEqual(session_id, "thread-1")
            self.assertEqual(message, "finished")
            self.assertTrue(run.log_path.is_file())

    def test_large_json_event_does_not_break_stream_reading(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = make_run(Path(temp))

            async def execute() -> tuple[int, str | None, str]:
                result = await LargeJsonLineAdapter().execute(run, lambda _: None)
                return result.exit_code, result.session_id, result.final_message

            exit_code, session_id, message = asyncio.run(execute())
            self.assertEqual(exit_code, 0)
            self.assertEqual(session_id, "thread-large")
            self.assertEqual(message, "finished after a large event")
            self.assertGreater(run.log_path.stat().st_size, 128 * 1024)

    def test_progress_forwards_safe_events_but_not_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = make_run(Path(temp))
            updates: list[str] = []

            async def execute() -> tuple[int, str | None, str]:
                result = await ProgressJsonAdapter().execute(run, lambda _: None, on_progress=updates.append)
                return result.exit_code, result.session_id, result.final_message

            exit_code, session_id, message = asyncio.run(execute())
            self.assertEqual(exit_code, 0)
            self.assertEqual(session_id, "thread-progress")
            self.assertEqual(message, "已检查配置。")
            self.assertEqual(
                updates,
                [
                    "Codex 会话已建立。",
                    "Codex 正在分析任务。",
                    "Codex 正在执行本地命令。",
                    "Codex：已检查配置。",
                ],
            )
