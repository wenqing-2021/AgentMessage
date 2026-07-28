from __future__ import annotations

import io
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from agent_message.core.models import AgentKind
from agent_message.core.state import StateStore
from agent_message.orchestration.commands import CommandError, parse_command
from agent_message.runtimes.container.mcp import ContainerMcpServer, INSTRUCTIONS
from agent_message.runtimes.container.policy import CONTAINER_INSTRUCTIONS
from agent_message.runtimes.container.runner import (
    ContainerRunResult,
    ContainerRunnerError,
    ContainerTarget,
    _container_project_path,
    build_container_command,
    resolve_container_target,
    resolve_project_cwd,
    terminate_container_process,
)

from tests.helpers import make_config


def inspect_payload(
    source: Path,
    destination: str,
    *,
    status: str = "running",
    writable: bool = True,
    extra_mounts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    mounts: list[dict[str, object]] = [
        {
            "Type": "bind",
            "Source": str(source),
            "Destination": destination,
            "RW": writable,
        }
    ]
    mounts.extend(extra_mounts or [])
    return {"State": {"Status": status}, "Mounts": mounts}


class ContainerRunnerTests(unittest.TestCase):
    def test_logs_command_accepts_container_source(self) -> None:
        command = parse_command("/logs a1b2c3d4 50 container")
        assert command is not None
        self.assertEqual(command.log_lines, 50)
        self.assertEqual(command.log_source, "container")
        with self.assertRaises(CommandError):
            parse_command("/logs a1b2c3d4 50 docker")

    def test_mount_mapping_supports_exact_parent_and_deepest_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            project = workspace / "group" / "project"
            project.mkdir(parents=True)
            parent = inspect_payload(workspace, "/workspace")
            self.assertEqual(
                _container_project_path(project, parent),
                PurePosixPath("/workspace/group/project"),
            )
            deepest = inspect_payload(
                workspace,
                "/workspace",
                extra_mounts=[
                    {
                        "Type": "bind",
                        "Source": str(project),
                        "Destination": "/project",
                        "RW": True,
                    }
                ],
            )
            self.assertEqual(
                _container_project_path(project, deepest), PurePosixPath("/project")
            )

    def test_mount_mapping_rejects_missing_and_deepest_read_only_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            project = workspace / "project"
            project.mkdir(parents=True)
            with self.assertRaisesRegex(ContainerRunnerError, "no bind mount"):
                _container_project_path(
                    project,
                    inspect_payload(Path(temp) / "other", "/other"),
                )
            payload = inspect_payload(
                workspace,
                "/workspace",
                extra_mounts=[
                    {
                        "Type": "bind",
                        "Source": str(project),
                        "Destination": "/project",
                        "RW": False,
                    }
                ],
            )
            with self.assertRaisesRegex(ContainerRunnerError, "read-only"):
                _container_project_path(project, payload)

    def test_explicit_container_path_selects_and_validates_bind_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            payload = inspect_payload(
                project,
                "/inferred",
                extra_mounts=[
                    {
                        "Type": "bind",
                        "Source": str(project),
                        "Destination": "/configured",
                        "RW": True,
                    }
                ],
            )
            self.assertEqual(
                _container_project_path(
                    project, payload, PurePosixPath("/configured")
                ),
                PurePosixPath("/configured"),
            )
            with self.assertRaisesRegex(ContainerRunnerError, "configured container_path"):
                _container_project_path(
                    project, payload, PurePosixPath("/not-mounted-here")
                )

            payload["Mounts"][1]["RW"] = False  # type: ignore[index]
            with self.assertRaisesRegex(ContainerRunnerError, "read-only"):
                _container_project_path(
                    project, payload, PurePosixPath("/configured")
                )

    def test_cwd_rejects_absolute_parent_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            outside = root / "outside"
            project.mkdir()
            outside.mkdir()
            (project / "nested").mkdir()
            (project / "escape").symlink_to(outside, target_is_directory=True)
            _, relative = resolve_project_cwd(project, "nested")
            self.assertEqual(relative, Path("nested"))
            for invalid in (str(outside), "..", "escape"):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ContainerRunnerError):
                        resolve_project_cwd(project, invalid)

    def test_build_command_keeps_argv_separate_and_maps_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), container_projects=("alpha",))
            project = config.projects["alpha"]
            (project.path / "nested").mkdir()
            target = ContainerTarget(
                "alpha-container", "running", PurePosixPath("/workspace/alpha")
            )
            with patch(
                "agent_message.runtimes.container.runner.resolve_container_target",
                return_value=target,
            ), patch(
                "agent_message.runtimes.container.runner._docker_path",
                return_value="/usr/bin/docker",
            ):
                command, resolved = build_container_command(
                    project,
                    ["python", "-c", "print('a && b')"],
                    "nested",
                )
            self.assertEqual(resolved, target)
            self.assertEqual(
                command[:4],
                ["/usr/bin/docker", "exec", "--workdir", "/workspace/alpha/nested"],
            )
            self.assertEqual(command[-3:], ["python", "-c", "print('a && b')"])
            self.assertIn("setsid", command)
            self.assertIn("--wait", command)

    def test_termination_scans_session_and_signals_positive_pids(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with patch(
            "agent_message.runtimes.container.runner._docker_path", return_value="docker"
        ), patch(
            "agent_message.runtimes.container.runner.subprocess.run", return_value=completed
        ) as run:
            stopped = terminate_container_process("alpha-container", 123, signal.SIGTERM)
        self.assertTrue(stopped)
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["docker", "exec", "alpha-container", "sh"])
        self.assertIn("/proc/[0-9]*/stat", "\n".join(command))
        self.assertEqual(command[-2:], ["-TERM", "123"])

    def test_resolve_target_only_starts_allowed_stopped_container(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), container_projects=("alpha",))
            project = config.projects["alpha"]
            stopped = inspect_payload(project.path, "/project", status="exited")
            running = inspect_payload(project.path, "/project", status="running")
            with patch(
                "agent_message.runtimes.container.runner._inspect", return_value=stopped
            ), patch(
                "agent_message.runtimes.container.runner._start", return_value=running
            ) as start:
                target = resolve_container_target(project, allow_start=True)
            start.assert_called_once_with(project)
            self.assertEqual(target.status, "running")
            with patch(
                "agent_message.runtimes.container.runner._inspect", return_value=stopped
            ):
                with self.assertRaisesRegex(ContainerRunnerError, "start it first"):
                    resolve_container_target(project, allow_start=False)

    def test_container_job_restart_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), container_projects=("alpha",))
            state = StateStore(config)
            try:
                task = state.create_task(
                    project_alias="alpha",
                    agent=AgentKind.CODEX,
                    chat_id="chat-1",
                    owner_open_id="ou-1",
                    prompt="run",
                )
                claimed = state.claimed_run()
                assert claimed is not None
                job = state.create_container_job(
                    task_id=task.id,
                    run_id=claimed.run_id,
                    container_name="alpha-container",
                    argv=["sleep", "30"],
                    cwd=".",
                )
                state.set_container_job_pid(job.id, 123)
                state.set_container_job_inner_pid(job.id, 456)
                state.recover_interrupted()
                recovered = state.latest_container_job(task.id)
                assert recovered is not None
                self.assertEqual(recovered.status, "interrupted")
                self.assertEqual(recovered.error, "bridge restarted")
            finally:
                state.close()


class ContainerMcpTests(unittest.TestCase):
    def _server(self, root: Path) -> tuple[ContainerMcpServer, str]:
        config = make_config(root, container_projects=("alpha",))
        state = StateStore(config)
        try:
            task = state.create_task(
                project_alias="alpha",
                agent=AgentKind.CODEX,
                chat_id="chat-1",
                owner_open_id="ou-1",
                prompt="test",
            )
        finally:
            state.close()
        return ContainerMcpServer(str(config.config_path), task.id, None), task.id

    def test_initialize_and_tool_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server, _ = self._server(Path(temp))
            output = io.StringIO()
            try:
                with patch("agent_message.runtimes.container.mcp.sys.stdout", output):
                    server._dispatch(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26"},
                        }
                    )
                    server._dispatch(
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
                    )
                messages = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(messages[0]["result"]["instructions"], INSTRUCTIONS)
                self.assertEqual(INSTRUCTIONS, CONTAINER_INSTRUCTIONS)
                tool = messages[1]["result"]["tools"][0]
                self.assertEqual(tool["name"], "container_run")
                self.assertEqual(tool["inputSchema"]["required"], ["argv"])
                self.assertNotIn("container_name", tool["inputSchema"]["properties"])
            finally:
                server.state.close()

    def test_container_tool_records_job_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server, task_id = self._server(Path(temp))
            output = io.StringIO()
            fake = ContainerRunResult(0, 1.25, "/project")
            try:
                with patch("agent_message.runtimes.container.mcp.sys.stdout", output), patch(
                    "agent_message.runtimes.container.mcp.run_container", return_value=fake
                ) as run:
                    server._run_tool(7, {"argv": ["pwd"], "cwd": "."})
                message = json.loads(output.getvalue())
                self.assertFalse(message["result"]["isError"])
                self.assertEqual(message["result"]["structuredContent"]["tail"], "/project")
                job = server.state.latest_container_job(task_id)
                assert job is not None
                self.assertEqual(job.status, "succeeded")
                self.assertEqual(job.container_name, "alpha-container")
                run.assert_called_once()
            finally:
                server.state.close()

    def test_cancelled_zero_exit_is_still_an_mcp_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server, _ = self._server(Path(temp))
            output = io.StringIO()
            fake = ContainerRunResult(
                0,
                0.5,
                "",
                error="container command was cancelled",
                cancelled=True,
            )
            try:
                with patch("agent_message.runtimes.container.mcp.sys.stdout", output), patch(
                    "agent_message.runtimes.container.mcp.run_container", return_value=fake
                ):
                    server._run_tool(9, {"argv": ["sleep", "30"], "cwd": "."})
                message = json.loads(output.getvalue())
                self.assertTrue(message["result"]["isError"])
                self.assertEqual(
                    message["result"]["structuredContent"]["status"], "stopped"
                )
            finally:
                server.state.close()

    def test_stdio_server_lists_only_container_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server, task_id = self._server(Path(temp))
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
                    "agent_message.runtimes.container.mcp",
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
            self.assertEqual(
                responses[0]["result"]["serverInfo"]["name"],
                "agent-message-container",
            )
            self.assertEqual(
                [tool["name"] for tool in responses[1]["result"]["tools"]],
                ["container_run"],
            )


if __name__ == "__main__":
    unittest.main()
