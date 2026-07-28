from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_message.agents.adapters import CodexAdapter, QoderAdapter, adapter_for
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


class QoderScriptAdapter(QoderAdapter):
    executable = sys.executable

    def __init__(self, script: str) -> None:
        super().__init__()
        self.script = script

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        return [self.executable, "-c", self.script]


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
                QoderAdapter().environment(),
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

    def test_codex_injects_gpu_mcp_for_new_and_resumed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(root, gpu_projects=("alpha",))
            adapter = CodexAdapter(app_config=config)
            command = adapter.build_command(make_run(root), root / "final")
            joined = "\n".join(command)
            self.assertIn("developer_instructions=", joined)
            self.assertIn("MUST use the gpu_run MCP tool", joined)
            self.assertIn("mcp_servers.agent_message_bwrap_gpu.command", joined)
            self.assertIn("agent_message.runtimes.gpu.mcp", joined)
            self.assertIn('"gpu_run"', joined)
            self.assertIn("--run-id", joined)
            resumed = adapter.build_command(make_run(root, "thread-1"), root / "final")
            self.assertIn("resume", resumed)
            self.assertIn("mcp_servers.agent_message_bwrap_gpu.required=true", resumed)
            self.assertIn("[AgentMessage GPU execution policy]", resumed[-1])
            self.assertTrue(resumed[-1].endswith("say hello"))

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
            self.assertTrue(resumed[-1].endswith("say hello"))

    def test_qoder_ordinary_command_allows_bash_network_and_is_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = make_run(root)
            malicious_prompt = "inspect; $(touch should-not-run)"
            run = ClaimedRun(**{**run.__dict__, "agent": AgentKind.QODER, "prompt": malicious_prompt})
            command = QoderAdapter().build_command(run, root / "final")
            self.assertNotIn("--yolo", command)
            self.assertNotIn("--dangerously-skip-permissions", command)
            self.assertEqual(command[command.index("--permission-mode") + 1], "auto")
            self.assertEqual(command[command.index("--setting-sources") + 1], "")
            self.assertIn("--strict-mcp-config", command)
            mcp = json.loads(command[command.index("--mcp-config") + 1])
            self.assertEqual(mcp, {"mcpServers": {}})
            tools = command[command.index("--tools") + 1].split(",")
            self.assertIn("Bash", tools)
            blocked = command[command.index("--disallowed-tools") + 1]
            self.assertIn("WebFetch", blocked)
            self.assertIn("Bash(git push:*)", blocked)
            self.assertNotIn("Bash(curl:*)", blocked)
            self.assertNotIn("Bash(wget:*)", blocked)
            self.assertNotIn("Bash(ssh:*)", blocked)
            self.assertIn("mcp__*", blocked)
            self.assertEqual(command[-1], malicious_prompt)
            self.assertEqual(command.count(malicious_prompt), 1)

            resumed = QoderAdapter().build_command(make_run(root, "qoder-session"), root / "final")
            self.assertEqual(resumed[resumed.index("-r") + 1], "qoder-session")

            inherited = {
                "QODER_SHELL_PREFIX": "offline-wrapper",
                "QODERCN_SHELL_PREFIX": "offline-wrapper",
                "CLAUDE_CODE_SHELL_PREFIX": "offline-wrapper",
            }
            with patch.dict(os.environ, inherited):
                environment = QoderAdapter().environment()
            for key in inherited:
                self.assertNotIn(key, environment)

    def test_qoder_gpu_command_exposes_only_approved_gpu_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(
                root,
                gpu_projects=("alpha",),
                qoder_only_projects=("alpha",),
            )
            command = QoderAdapter(config).build_command(make_run(root), root / "final")
            tools = command[command.index("--tools") + 1].split(",")
            self.assertIn("Bash", tools)
            self.assertIn("mcp__agent_message_bwrap_gpu__gpu_run", tools)
            self.assertEqual(
                command[command.index("--allowed-mcp-server-names") + 1],
                "agent_message_bwrap_gpu",
            )
            self.assertEqual(
                command[command.index("--allowed-tools") + 1],
                "mcp__agent_message_bwrap_gpu__gpu_run",
            )
            payload = json.loads(command[command.index("--mcp-config") + 1])
            server = payload["mcpServers"]["agent_message_bwrap_gpu"]
            self.assertEqual(server["type"], "stdio")
            self.assertIn("agent_message.runtimes.gpu.mcp", server["args"])
            self.assertIn("--run-id", server["args"])
            self.assertNotIn("mcp__*", command[command.index("--disallowed-tools") + 1])
            self.assertIn("MUST use the gpu_run MCP tool", "\n".join(command))
            self.assertIn("[AgentMessage GPU execution policy]", command[-1])

    def test_qoder_container_command_omits_host_bash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(
                root,
                container_projects=("alpha",),
                qoder_only_projects=("alpha",),
            )
            command = QoderAdapter(config).build_command(make_run(root), root / "final")
            tools = command[command.index("--tools") + 1].split(",")
            self.assertNotIn("Bash", tools)
            self.assertIn("mcp__agent_message_container__container_run", tools)
            payload = json.loads(command[command.index("--mcp-config") + 1])
            server = payload["mcpServers"]["agent_message_container"]
            self.assertIn("agent_message.runtimes.container.mcp", server["args"])
            self.assertIn("MUST use the container_run MCP tool", "\n".join(command))
            self.assertIn("[AgentMessage container execution policy]", command[-1])

    def test_adapter_factory_passes_config_to_qoder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp))
            adapter = adapter_for(AgentKind.QODER, app_config=config)
            self.assertIsInstance(adapter, QoderAdapter)
            self.assertIs(adapter.app_config, config)

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

    def test_qoder_stream_uses_result_and_never_forwards_thinking_or_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = make_run(Path(temp))
            events = [
                {
                    "type": "system",
                    "subtype": "hook_response",
                    "output": "private hook output",
                    "exit_code": 1,
                    "session_id": "qoder-1",
                },
                {"type": "system", "subtype": "init", "session_id": "qoder-1"},
                {
                    "type": "assistant",
                    "session_id": "qoder-1",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "private reasoning"},
                            {"type": "text", "text": "intermediate text"},
                            {"type": "tool_use", "name": "Edit", "input": {"secret": "x"}},
                        ]
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "final answer",
                    "session_id": "qoder-1",
                },
            ]
            payload = json.dumps(events, ensure_ascii=False)
            script = (
                "import json; events=json.loads(" + repr(payload) + "); "
                "print(*(json.dumps(item, ensure_ascii=False) for item in events), sep='\\n')"
            )
            updates: list[str] = []

            async def execute():  # type: ignore[no-untyped-def]
                return await QoderScriptAdapter(script).execute(
                    run, lambda _: None, on_progress=updates.append
                )

            result = asyncio.run(execute())
            self.assertEqual(result.session_id, "qoder-1")
            self.assertEqual(result.final_message, "final answer")
            self.assertIsNone(result.error)
            self.assertEqual(updates, ["Qoder 会话已建立。", "Qoder 正在修改项目文件。"])
            self.assertNotIn("private reasoning", "\n".join(updates))
            self.assertNotIn("private hook output", "\n".join(updates))

    def test_qoder_result_error_overrides_assistant_text_even_with_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = make_run(Path(temp))
            events = [
                {"type": "system", "subtype": "init", "session_id": "qoder-error"},
                {
                    "type": "assistant",
                    "session_id": "qoder-error",
                    "message": {"content": [{"type": "text", "text": "looks successful"}]},
                },
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "errors": ["Network attempt failed\nwith details"],
                    "permission_denials": [{"tool": "secret", "input": "never forward"}],
                    "session_id": "qoder-error",
                },
            ]
            payload = json.dumps(events)
            script = (
                "import json; events=json.loads(" + repr(payload) + "); "
                "print(*(json.dumps(item) for item in events), sep='\\n')"
            )

            async def execute():  # type: ignore[no-untyped-def]
                return await QoderScriptAdapter(script).execute(run, lambda _: None)

            result = asyncio.run(execute())
            self.assertEqual(result.exit_code, 0)
            self.assertIn("error_during_execution", result.error or "")
            self.assertIn("1 项工具权限被拒绝", result.error or "")
            self.assertNotIn("never forward", result.error or "")
            self.assertNotEqual(result.final_message, "looks successful")

    def test_qoder_rejects_missing_result_and_conflicting_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cases = (
                (
                    "print('{\"type\":\"system\",\"subtype\":\"init\","
                    "\"session_id\":\"one\"}')",
                    "未返回 result",
                ),
                (
                    "print('{\"type\":\"system\",\"subtype\":\"init\","
                    "\"session_id\":\"one\"}'); print('{\"type\":\"result\","
                    "\"subtype\":\"success\",\"is_error\":false,\"result\":\"ok\","
                    "\"session_id\":\"two\"}')",
                    "不同的会话 ID",
                ),
            )
            for index, (script, error) in enumerate(cases):
                with self.subTest(error=error):
                    run = make_run(root / str(index))

                    async def execute():  # type: ignore[no-untyped-def]
                        return await QoderScriptAdapter(script).execute(run, lambda _: None)

                    result = asyncio.run(execute())
                    self.assertIn(error, result.error or "")

    def test_qoder_ignores_malformed_unknown_and_oversized_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run = make_run(Path(temp))
            script = (
                "import json; print('not-json'); "
                "print(json.dumps({'type':'future.event','payload':'x'})); "
                "print(json.dumps({'type':'assistant','message':{'content':["
                "{'type':'thinking','thinking':'x'*(1024*1024+50)}]}})); "
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
                "'result':'still works','session_id':'qoder-large'}))"
            )

            async def execute():  # type: ignore[no-untyped-def]
                return await QoderScriptAdapter(script).execute(run, lambda _: None)

            result = asyncio.run(execute())
            self.assertEqual(result.final_message, "still works")
            self.assertEqual(result.session_id, "qoder-large")
            self.assertIsNone(result.error)
            self.assertGreater(run.log_path.stat().st_size, 1024 * 1024)
