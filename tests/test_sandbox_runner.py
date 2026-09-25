from __future__ import annotations

import io
import json
import os
import stat
from types import SimpleNamespace
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_message.core.config import SandboxConfig
from agent_message.core.models import AgentKind
from agent_message.core.state import StateStore
from agent_message.orchestration.commands import CommandError, parse_command
from agent_message.runtimes.sandbox.mcp import SandboxMcpServer
from agent_message.runtimes.sandbox.policy import SANDBOX_INSTRUCTIONS, sandbox_instructions
from agent_message.runtimes.sandbox.runner import (
    SandboxRunResult,
    SandboxRunnerError,
    build_bwrap_command,
    resolve_project_cwd,
)

from tests.helpers import make_config


class SandboxRunnerTests(unittest.TestCase):
    def test_logs_command_accepts_sandbox_source_and_legacy_alias(self) -> None:
        for word in ("sandbox", "gpu"):
            command = parse_command(f"/logs a1b2c3d4 50 {word}")
            assert command is not None
            self.assertEqual(command.log_lines, 50)
            self.assertEqual(command.log_source, "sandbox")
        with self.assertRaises(CommandError):
            parse_command("/logs a1b2c3d4 50 other")

    def test_bwrap_command_has_fixed_sandbox_and_filesystem_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = make_config(root, sandbox_gpu_projects=("alpha",))
            project = config.projects["alpha"]
            (project.path / ".git").mkdir()
            (project.path / "nested").mkdir()
            original_exists = Path.exists
            original_is_dir = Path.is_dir

            def fake_exists(path: Path) -> bool:
                return str(path) in ("/dev/dxg", "/etc/alternatives") or original_exists(path)

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
            self.assertNotIn(str(project.path.resolve() / ".git"), command)
            self.assertIn("Git commits and pushes only work through sandbox_run", SANDBOX_INSTRUCTIONS)
            self.assertNotIn("/mnt/c", command)
            self.assertNotIn("/run", command)
            alternatives_index = command.index("/etc/alternatives")
            self.assertEqual(
                command[alternatives_index - 1 : alternatives_index + 2],
                ["--ro-bind", "/etc/alternatives", "/etc/alternatives"],
            )
            self.assertEqual(command[-4:], ["uv", "run", "python", "train.py"])

    def test_sandbox_without_gpu_needs_no_device_or_wsl_libraries(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = make_config(root, sandbox_projects=("alpha",)).projects["alpha"]
            original_is_dir = Path.is_dir
            original_exists = Path.exists

            def fake_exists(path: Path) -> bool:
                # Simulate a host with no WSL GPU device at all.
                return False if str(path) == "/dev/dxg" else original_exists(path)

            def fake_is_dir(path: Path) -> bool:
                return False if str(path) == "/usr/lib/wsl/lib" else original_is_dir(path)

            with patch.object(Path, "exists", fake_exists), patch.object(
                Path, "is_dir", fake_is_dir
            ):
                command = build_bwrap_command(
                    project,
                    ["/usr/bin/true"],
                    bwrap_path="/usr/bin/bwrap",
                    uv_path="/usr/bin/true",
                )

            self.assertNotIn("--dev-bind", command)
            self.assertNotIn("/usr/lib/wsl/lib", command)
            self.assertNotIn("LD_LIBRARY_PATH", command)
            self.assertIn("--clearenv", command)
            self.assertIn("--unshare-net", command)
            self.assertIn(SANDBOX_INSTRUCTIONS, sandbox_instructions(False))
            self.assertNotIn("GPU passthrough", sandbox_instructions(False))
            self.assertIn("GPU passthrough", sandbox_instructions(True))

    def test_configured_host_paths_are_mounted_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = make_config(root, sandbox_projects=("alpha",)).projects["alpha"]
            toolchain = root / "toolchain"
            toolchain.mkdir()
            fonts = root / "fonts"
            fonts.mkdir()
            configured = replace(
                project, sandbox=replace(project.sandbox, readonly_paths=(toolchain, fonts))
            )

            def build(target):
                return build_bwrap_command(
                    target,
                    ["/usr/bin/true"],
                    bwrap_path="/usr/bin/bwrap",
                    uv_path="/usr/bin/true",
                )

            command = build(configured)
            for source in (toolchain, fonts):
                index = command.index(str(source))
                self.assertEqual(
                    command[index - 1 : index + 2], ["--ro-bind", str(source), str(source)]
                )
            # Host toolchains stay hidden unless the operator lists them.
            self.assertNotIn("/opt/quarto", command)
            self.assertNotIn("/etc/fonts", command)

            broken = replace(
                configured,
                sandbox=replace(configured.sandbox, readonly_paths=(toolchain, root / "gone")),
            )
            with self.assertRaisesRegex(SandboxRunnerError, "not available on the host"):
                build(broken)

    def test_network_can_only_be_enabled_by_project_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), sandbox_gpu_projects=("alpha",))
            project = config.projects["alpha"]
            online = replace(
                project,
                sandbox=SandboxConfig(enabled=True, gpu=True, network=True, timeout_seconds=3600),
            )
            command = build_bwrap_command(
                online,
                ["/usr/bin/true"],
                bwrap_path="/usr/bin/bwrap",
                require_gpu_device=False,
            )
            self.assertNotIn("--unshare-net", command)

    def test_git_identity_makes_commit_without_host_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = make_config(root, sandbox_gpu_projects=("alpha",)).projects["alpha"]
            project = replace(project, sandbox=replace(project.sandbox,
                git_user_name="Example User", git_user_email="example@example.com"))
            command = build_bwrap_command(project, ["git", "status"],
                bwrap_path="bwrap", require_gpu_device=False)
            environment = {"PATH": os.defpath, "HOME": str(root / "empty-home"),
                           "GIT_CONFIG_NOSYSTEM": "1"}
            for i, arg in enumerate(command):
                if arg == "--setenv" and command[i + 1].startswith("GIT_"):
                    environment[command[i + 1]] = command[i + 2]
            for args in (["init"], ["commit", "--allow-empty", "-m", "test identity"]):
                result = subprocess.run(["git", *args], cwd=project.path, env=environment,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"],
                cwd=project.path, env=environment, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "Example User <example@example.com>")

    def test_ssh_forwarding_is_opt_in_and_exposes_only_socket_and_known_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "agent.sock"
            path.touch()
            original_stat = Path.stat

            def fake_stat(candidate, *args, **kwargs):
                if candidate == path:
                    return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.getuid())
                return original_stat(candidate, *args, **kwargs)

            socket_patch = patch.object(Path, "stat", fake_stat)
            socket_patch.start()
            self.addCleanup(socket_patch.stop)
            hosts = root / "known_hosts"
            hosts.write_text("example host key\n")
            project = make_config(root, sandbox_gpu_projects=("alpha",)).projects["alpha"]
            def build(target):
                return build_bwrap_command(target, ["git", "status"], bwrap_path="bwrap",
                                           require_gpu_device=False)
            with patch.dict(os.environ, {"SSH_AUTH_SOCK": str(path),
                                         "AGENT_MESSAGE_FEISHU_APP_SECRET": "test-secret"}):
                disabled = build(project)
                self.assertNotIn(str(path), disabled)
                self.assertNotIn("SSH_AUTH_SOCK", disabled)
                enabled = replace(project, sandbox=replace(project.sandbox,
                    ssh_agent_socket=path, ssh_known_hosts=hosts))
                command = build(enabled)
            self.assertIn("SSH_AUTH_SOCK", command)
            self.assertIn("--clearenv", command)
            self.assertNotIn("test-secret", command)
            self.assertNotIn(str(Path.home() / ".ssh"), command)
            for source, target in ((path, "/tmp/agent-message-ssh-agent"),
                                   (hosts, "/tmp/agent-message-known-hosts")):
                index = command.index(str(source))
                self.assertEqual(command[index - 1:index + 2], ["--ro-bind", str(source), target])
            ssh = command[command.index("GIT_SSH_COMMAND") + 1]
            self.assertIn("StrictHostKeyChecking=yes", ssh)
            self.assertIn("BatchMode=yes", ssh)
            with patch("agent_message.runtimes.sandbox.runner.os.getuid", return_value=123456), patch.object(
                Path, "stat", lambda candidate, *args, **kwargs:
                    SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=123455)
                    if candidate == path else original_stat(candidate, *args, **kwargs)
            ):
                with self.assertRaisesRegex(SandboxRunnerError, "owned by the service user"):
                    build(enabled)
            invalid = replace(enabled, sandbox=replace(enabled.sandbox, ssh_agent_socket=hosts))
            with self.assertRaisesRegex(SandboxRunnerError, "Unix socket"):
                build(invalid)
            hosts.unlink()
            with self.assertRaisesRegex(SandboxRunnerError, "unavailable"):
                build(enabled)

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
                    with self.assertRaises(SandboxRunnerError):
                        resolve_project_cwd(project, invalid)

    def test_sandbox_job_state_and_restart_recovery(self) -> None:
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
                run = state.claimed_run()
                assert run is not None
                job = state.create_sandbox_job(
                    task_id=task.id,
                    run_id=run.run_id,
                    argv=["python", "train.py"],
                    cwd=".",
                )
                state.set_sandbox_job_pid(job.id, 123)
                active = state.latest_sandbox_job(task.id)
                assert active is not None
                self.assertEqual(active.pid, 123)
                state.recover_interrupted()
                recovered = state.latest_sandbox_job(task.id)
                assert recovered is not None
                self.assertEqual(recovered.status, "interrupted")
            finally:
                state.close()


    def test_legacy_gpu_jobs_table_is_migrated_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), sandbox_projects=("alpha",))
            state = StateStore(config)
            task = state.create_task(
                project_alias="alpha",
                agent=AgentKind.CODEX,
                chat_id="chat-1",
                owner_open_id="ou-1",
                prompt="train",
            )
            state.close()
            # Recreate the pre-rename layout an upgraded install would still have.
            connection = sqlite3.connect(state.path)
            connection.executescript(
                "DROP TABLE sandbox_jobs;"
                "CREATE TABLE gpu_jobs ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,"
                " run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,"
                " status TEXT NOT NULL, pid INTEGER, command_summary TEXT NOT NULL,"
                " argv_json TEXT NOT NULL, cwd TEXT NOT NULL, log_path TEXT NOT NULL,"
                " started_at TEXT NOT NULL, finished_at TEXT, exit_code INTEGER, error TEXT);"
            )
            connection.execute(
                "INSERT INTO gpu_jobs(task_id, status, command_summary, argv_json, cwd,"
                " log_path, started_at) VALUES (?, 'succeeded', 'python train.py', "
                "'[]', '.', '/tmp/legacy.log', '2026-01-01T00:00:00+00:00')",
                (task.id,),
            )
            connection.commit()
            connection.close()

            reopened = StateStore(config)
            try:
                job = reopened.latest_sandbox_job(task.id)
                assert job is not None
                self.assertEqual(job.command_summary, "python train.py")
                self.assertEqual(job.status, "succeeded")
                tables = {
                    str(row[0])
                    for row in reopened._connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("sandbox_jobs", tables)
                self.assertNotIn("gpu_jobs", tables)
            finally:
                reopened.close()


class SandboxMcpTests(unittest.TestCase):
    def _server(self, root: Path) -> tuple[SandboxMcpServer, str]:
        config = make_config(root, sandbox_gpu_projects=("alpha",))
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
        return SandboxMcpServer(str(config.config_path), task.id, None), task.id

    def test_initialize_and_tool_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server, _ = self._server(Path(temp))
            output = io.StringIO()
            try:
                with patch("agent_message.runtimes.sandbox.mcp.sys.stdout", output):
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
                instructions = messages[0]["result"]["instructions"]
                self.assertEqual(instructions, server.instructions)
                self.assertEqual(instructions, sandbox_instructions(True))
                self.assertIn(SANDBOX_INSTRUCTIONS, instructions)
                self.assertIn("MUST use the sandbox_run MCP tool", instructions)
                tool = messages[1]["result"]["tools"][0]
                self.assertEqual(tool["name"], "sandbox_run")
                self.assertEqual(tool["inputSchema"]["required"], ["argv"])
                self.assertNotIn("project_path", tool["inputSchema"]["properties"])
            finally:
                server.state.close()

    def test_sandbox_tool_records_job_and_returns_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server, task_id = self._server(Path(temp))
            output = io.StringIO()
            fake = SandboxRunResult(0, 1.25, "backend=gpu\ncuda:0")
            try:
                with patch("agent_message.runtimes.sandbox.mcp.sys.stdout", output), patch(
                    "agent_message.runtimes.sandbox.mcp.run_bubblewrap", return_value=fake
                ):
                    server._run_tool(7, {"argv": ["python", "train.py"], "cwd": "."})
                message = json.loads(output.getvalue())
                self.assertFalse(message["result"]["isError"])
                self.assertEqual(message["result"]["structuredContent"]["exit_code"], 0)
                job = server.state.latest_sandbox_job(task_id)
                assert job is not None
                self.assertEqual(job.status, "succeeded")
            finally:
                server.state.close()

    def test_stdio_server_negotiates_and_lists_only_sandbox_run(self) -> None:
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
                    "agent_message.runtimes.sandbox.mcp",
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
            self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "agent-message-sandbox")
            self.assertEqual(
                [tool["name"] for tool in responses[1]["result"]["tools"]],
                ["sandbox_run"],
            )


if __name__ == "__main__":
    unittest.main()
