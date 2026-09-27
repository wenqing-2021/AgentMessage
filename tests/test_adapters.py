from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_message.agents.adapters import CodexAdapter, adapter_for
from agent_message.core.models import AgentKind, ClaimedRun

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


class FakeAdapter(CodexAdapter):
    executable = sys.executable

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        return [
            self.executable,
            "-c",
            "import json; print(json.dumps({'session_id':'thread-1','text':'finished'}))",
        ]


class LargeJsonLineAdapter(CodexAdapter):
    executable = sys.executable

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        script = (
            "import json; "
            "print(json.dumps({'thread_id': 'thread-large'})); "
            "print(json.dumps({'item': {'text': 'x' * (128 * 1024)}})); "
            "print(json.dumps({'text': 'finished after a large event'}))"
        )
        return [self.executable, "-c", script]


class ProgressJsonAdapter(CodexAdapter):
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
    def test_agent_environments_do_not_inherit_feishu_service_variables(self) -> None:
        service_only = {
            "AGENT_MESSAGE_FEISHU_APP_ID": "cli_private",
            "AGENT_MESSAGE_FEISHU_APP_SECRET": "secret_private",
            "AGENT_MESSAGE_FEISHU_FUTURE_SETTING": "future_private",
            "AGENT_MESSAGE_ALLOWED_OPEN_IDS": "ou_private",
        }
        retained = {
            "HOME": "/home/tester",
            "PATH": "/usr/bin:/bin",
            "HTTPS_PROXY": "http://proxy.example",
            "AGENT_MESSAGE_AGENT_SETTING": "retained",
        }
        with patch.dict(os.environ, service_only | retained, clear=True):
            environments = (
                CodexAdapter().environment(),
            )

        for environment in environments:
            for key in service_only:
                self.assertNotIn(key, environment)
            for key, value in retained.items():
                self.assertEqual(environment[key], value)

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

    def test_codex_reasoning_effort_for_new_and_resumed_turns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for session in (None, "thread-1"):
                run = ClaimedRun(**{**make_run(root, session).__dict__, "reasoning_effort": "high"})
                command = CodexAdapter().build_command(run, root / "final")
                self.assertIn('model_reasoning_effort="high"', command)
                if session:
                    self.assertIn(session, command)
                self.assertIn("sandbox_workspace_write.network_access=false", command)

    def test_codex_effort_keeps_all_config_overrides_in_one_cli_scope(self) -> None:
        # Repeating -c at exec/resume scope replaces the root override list in Codex.
        # Cover real combinations; merely checking that flags exist missed this bug.
        for runtime in ("plain", "sandbox", "container"):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config = make_config(root,
                    sandbox_projects=("alpha",) if runtime == "sandbox" else (),
                    container_projects=("alpha",) if runtime == "container" else ())
                adapter = CodexAdapter(app_config=config)
                for session in (None, "thread-1"):
                    run = ClaimedRun(**{**make_run(root, session).__dict__,
                        "model": "gpt-6-astra", "reasoning_effort": "high"})
                    command = adapter.build_command(run, root / "final")
                    boundary = command.index("exec")
                    self.assertNotIn("-c", command[boundary + 1:])
                    overrides = [command[i + 1] for i in range(boundary) if command[i] == "-c"]
                    self.assertIn('model_reasoning_effort="high"', overrides)
                    self.assertIn("sandbox_workspace_write.network_access=false", overrides)
                    if runtime != "plain":
                        self.assertIn(f"mcp_servers.agent_message_{runtime}.required=true", overrides)
                    self.assertEqual(command[command.index("-m") + 1], run.model)
                    if session:
                        self.assertIn(session, command)

    def test_codex_injects_model_flag_when_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selected = ClaimedRun(**{**make_run(root).__dict__, "model": "deepseek/deepseek-v4-pro"})
            command = CodexAdapter().build_command(selected, root / "final")
            self.assertIn("-m", command)
            self.assertEqual(command[command.index("-m") + 1], "deepseek/deepseek-v4-pro")
            resumed = CodexAdapter().build_command(
                ClaimedRun(**{**make_run(root, "thread-1").__dict__, "model": "kimi-code/k3"}),
                root / "final",
            )
            self.assertEqual(resumed[resumed.index("-m") + 1], "kimi-code/k3")
            self.assertGreater(resumed.index("-m"), resumed.index("resume"))
            self.assertIn("thread-1", resumed)
            plain = CodexAdapter().build_command(make_run(root), root / "final")
            self.assertNotIn("-m", plain)

    def test_codex_injects_sandbox_mcp_for_new_and_resumed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(root, sandbox_gpu_projects=("alpha",))
            adapter = CodexAdapter(app_config=config)
            command = adapter.build_command(make_run(root), root / "final")
            joined = "\n".join(command)
            self.assertIn("developer_instructions=", joined)
            self.assertIn("MUST use the sandbox_run MCP tool", joined)
            self.assertIn("mcp_servers.agent_message_sandbox.command", joined)
            self.assertIn("agent_message.runtimes.sandbox.mcp", joined)
            self.assertIn('"sandbox_run"', joined)
            self.assertIn("--run-id", joined)
            resumed = adapter.build_command(make_run(root, "thread-1"), root / "final")
            self.assertIn("resume", resumed)
            self.assertIn("mcp_servers.agent_message_sandbox.required=true", resumed)
            self.assertIn("[AgentMessage sandbox execution policy]", resumed[-1])
            self.assertIn("say hello", resumed[-1])
            self.assertIn("send-to-feishu", resumed[-1])

    def test_codex_injects_container_mcp_for_new_and_resumed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(root, container_projects=("alpha",))
            adapter = CodexAdapter(app_config=config)
            command = adapter.build_command(make_run(root), root / "final")
            joined = "\n".join(command)
            self.assertIn("developer_instructions=", joined)
            self.assertIn("MUST use the container_run MCP tool", joined)
            self.assertIn("mcp_servers.agent_message_container.command", joined)
            self.assertIn("agent_message.runtimes.container.mcp", joined)
            self.assertIn('"container_run"', joined)
            self.assertIn("--run-id", joined)
            resumed = adapter.build_command(make_run(root, "thread-1"), root / "final")
            self.assertIn("resume", resumed)
            self.assertIn("mcp_servers.agent_message_container.required=true", resumed)
            self.assertIn("[AgentMessage container execution policy]", resumed[-1])
            self.assertIn("say hello", resumed[-1])
            self.assertIn("send-to-feishu", resumed[-1])


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
