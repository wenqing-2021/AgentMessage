from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_message.core.config import GpuConfig
from agent_message.core.models import AgentKind
from agent_message.core.state import StateStore
from agent_message.orchestration.commands import CommandError, parse_command
from agent_message.runtimes.gpu.mcp import GpuMcpServer, INSTRUCTIONS
from agent_message.runtimes.gpu.policy import GPU_INSTRUCTIONS
from agent_message.runtimes.gpu.runner import (
    GpuRunResult,
    GpuRunnerError,
    build_bwrap_command,
    resolve_project_cwd,
)

from tests.helpers import make_config


class GpuRunnerTests(unittest.TestCase):
    def test_logs_command_accepts_gpu_source(self) -> None:
        command = parse_command("/logs a1b2c3d4 50 gpu")
        assert command is not None
        self.assertEqual(command.log_lines, 50)
        self.assertEqual(command.log_source, "gpu")
        with self.assertRaises(CommandError):
            parse_command("/logs a1b2c3d4 50 other")

    def test_bwrap_command_has_fixed_gpu_and_filesystem_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(root, gpu_projects=("alpha",))
            project = config.projects["alpha"]
            (project.path / ".git").mkdir()
            (project.path / "nested").mkdir()
            original_exists = Path.exists
            original_is_dir = Path.is_dir

            def fake_exists(path: Path) -> bool:
                return str(path) == "/dev/dxg" or original_exists(path)

            def fake_is_dir(path: Path) -> bool:
                return str(path) == "/usr/lib/wsl/lib" or original_is_dir(path)

            with patch.object(Path, "exists", fake_exists), patch.object(
                Path, "is_dir", fake_is_dir
            ):
                command = build_bwrap_command(
                    project,
                    ["uv", "run", "python", "train.py"],
                    "nested",
                    bwrap_path="/usr/bin/bwrap",
                    uv_path="/usr/bin/true",
                )

            self.assertIn("--unshare-net", command)
            self.assertIn("--dev-bind", command)
            device_index = command.index("--dev-bind")
            self.assertEqual(command[device_index + 1 : device_index + 3], ["/dev/dxg", "/dev/dxg"])
            self.assertIn("--clearenv", command)
            self.assertIn("--cap-drop", command)
            bind_index = command.index("--bind")
            self.assertEqual(command[bind_index + 1], str(project.path.resolve()))
            git_index = command.index(str(project.path.resolve() / ".git"))
            self.assertEqual(command[git_index - 1], "--ro-bind")
            self.assertNotIn("/mnt/c", command)
            self.assertNotIn("/run", command)
            self.assertEqual(command[-4:], ["uv", "run", "python", "train.py"])

    def test_network_can_only_be_enabled_by_project_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), gpu_projects=("alpha",))
            project = config.projects["alpha"]
            online = replace(
                project,
                gpu=GpuConfig(enabled=True, network=True, timeout_seconds=3600),
            )
            command = build_bwrap_command(
                online,
                ["/usr/bin/true"],
                bwrap_path="/usr/bin/bwrap",
                require_gpu_device=False,
            )
            self.assertNotIn("--unshare-net", command)

    def test_cwd_rejects_absolute_parent_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            (project / "escape").symlink_to(outside, target_is_directory=True)
            self.assertEqual(resolve_project_cwd(project, "."), project.resolve())
            for invalid in (str(outside), "..", "escape"):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(GpuRunnerError):
                        resolve_project_cwd(project, invalid)

    def test_gpu_job_state_and_restart_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), gpu_projects=("alpha",))
            state = StateStore(config)
            try:
                task = state.create_task(
                    project_alias="alpha",
                    agent=AgentKind.CODEX,
                    chat_id="chat-1",
                    owner_open_id="ou-1",
                    prompt="train",
                )
                run = state.claimed_run()
                assert run is not None
                job = state.create_gpu_job(
                    task_id=task.id,
                    run_id=run.run_id,
                    argv=["python", "train.py"],
                    cwd=".",
                )
                state.set_gpu_job_pid(job.id, 123)
                active = state.latest_gpu_job(task.id)
                assert active is not None
                self.assertEqual(active.pid, 123)
                state.recover_interrupted()
                recovered = state.latest_gpu_job(task.id)
                assert recovered is not None
                self.assertEqual(recovered.status, "interrupted")
            finally:
                state.close()


class GpuMcpTests(unittest.TestCase):
    def _server(self, root: Path) -> tuple[GpuMcpServer, str]:
        config = make_config(root, gpu_projects=("alpha",))
        state = StateStore(config)
        try:
            task = state.create_task(
                project_alias="alpha",
                agent=AgentKind.CODEX,
                chat_id="chat-1",
                owner_open_id="ou-1",
                prompt="gpu",
            )
        finally:
            state.close()
        return GpuMcpServer(str(config.config_path), task.id, None), task.id

    def test_initialize_and_tool_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server, _ = self._server(Path(temp))
            output = io.StringIO()
            try:
                with patch("agent_message.runtimes.gpu.mcp.sys.stdout", output):
                    server._dispatch(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26"},
                        }
                    )
                    server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                messages = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(messages[0]["result"]["instructions"], INSTRUCTIONS)
                self.assertEqual(INSTRUCTIONS, GPU_INSTRUCTIONS)
                self.assertIn("MUST use the gpu_run MCP tool", INSTRUCTIONS)
                tool = messages[1]["result"]["tools"][0]
                self.assertEqual(tool["name"], "gpu_run")
                self.assertEqual(tool["inputSchema"]["required"], ["argv"])
                self.assertNotIn("project_path", tool["inputSchema"]["properties"])
            finally:
                server.state.close()

    def test_gpu_tool_records_job_and_returns_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server, task_id = self._server(Path(temp))
            output = io.StringIO()
            fake = GpuRunResult(0, 1.25, "backend=gpu\ncuda:0")
            try:
                with patch("agent_message.runtimes.gpu.mcp.sys.stdout", output), patch(
                    "agent_message.runtimes.gpu.mcp.run_bubblewrap", return_value=fake
                ):
                    server._run_tool(7, {"argv": ["python", "train.py"], "cwd": "."})
                message = json.loads(output.getvalue())
                self.assertFalse(message["result"]["isError"])
                self.assertEqual(message["result"]["structuredContent"]["exit_code"], 0)
                job = server.state.latest_gpu_job(task_id)
                assert job is not None
                self.assertEqual(job.status, "succeeded")
            finally:
                server.state.close()

    def test_stdio_server_negotiates_and_lists_only_gpu_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server, task_id = self._server(root)
            config_path = server.config.config_path
            server.state.close()
            requests = "\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26"},
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                            "params": {},
                        }
                    ),
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
                    ),
                    "",
                ]
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_message.runtimes.gpu.mcp",
                    "--config",
                    str(config_path),
                    "--task-id",
                    task_id,
                ],
                input=requests,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "agent-message-bwrap-gpu")
            self.assertEqual(
                [tool["name"] for tool in responses[1]["result"]["tools"]],
                ["gpu_run"],
            )


if __name__ == "__main__":
    unittest.main()
