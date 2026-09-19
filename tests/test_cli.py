from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_message.cli import (
    _ssh_agent_keys,
    _systemd_user_services,
    _container_project_for_selector,
    _qoder_login_status,
    command_resume,
)
from agent_message.core.models import AgentKind
from agent_message.core.state import StateStore

from tests.helpers import make_config


class CliTests(unittest.TestCase):
    def test_systemd_service_summary_reports_inactive_units(self) -> None:
        with patch("agent_message.cli._command_output", return_value=(0, "active\nactive\n")):
            summary, inactive = _systemd_user_services()
        self.assertEqual(summary, "本体 active，SSH agent active")
        self.assertEqual(inactive, [])

        with patch(
            "agent_message.cli._command_output",
            return_value=(3, "active\ninactive\n"),
        ):
            summary, inactive = _systemd_user_services()
        self.assertIn("SSH agent inactive", summary)
        self.assertEqual(inactive, ["agent-message-ssh-agent.service"])

    def test_systemd_service_summary_degrades_when_bus_is_unreachable(self) -> None:
        with patch(
            "agent_message.cli._command_output",
            return_value=(1, "Failed to connect to bus: Operation not permitted\n"),
        ):
            summary, inactive = _systemd_user_services()
        self.assertIn("无法确认", summary)
        self.assertEqual(inactive, [])

    def test_ssh_agent_keys_counts_loaded_keys_and_reports_empty_agent(self) -> None:
        with patch(
            "agent_message.cli._command_output",
            return_value=(0, "256 SHA256:aaa one (ED25519)\n3072 SHA256:bbb two (RSA)\n"),
        ):
            self.assertEqual(_ssh_agent_keys(), "已加载 2 把")

        with patch(
            "agent_message.cli._command_output",
            return_value=(1, "The agent has no identities.\n"),
        ):
            self.assertIn("未加载密钥", _ssh_agent_keys())

        with patch(
            "agent_message.cli._command_output",
            return_value=(2, "Error connecting to agent: No such file or directory\n"),
        ):
            self.assertIn("无法确认", _ssh_agent_keys())

    def test_ssh_agent_keys_uses_the_dedicated_socket(self) -> None:
        with patch("agent_message.cli._command_output", return_value=(0, "key\n")) as probe:
            _ssh_agent_keys()
        environment = probe.call_args.kwargs["env"]
        self.assertTrue(environment["SSH_AUTH_SOCK"].endswith("/agent-message-ssh/agent.sock"))

    def test_qoder_login_status_returns_only_boolean(self) -> None:
        payload = (
            '{"logged_in":true,"username":"private name",'
            '"email":"private@example.com"}'
        )
        with patch("agent_message.cli._command_output", return_value=(0, payload)):
            self.assertIs(_qoder_login_status(), True)

    def test_container_doctor_selector_accepts_alias_or_container_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(
                Path(temp),
                projects=("alpha", "beta"),
                container_projects=("alpha", "beta"),
            )
            self.assertEqual(
                _container_project_for_selector(config, "alpha").alias, "alpha"
            )
            self.assertEqual(
                _container_project_for_selector(config, "beta-container").alias,
                "beta",
            )
            with self.assertRaisesRegex(ValueError, "未知项目或容器"):
                _container_project_for_selector(config, "missing")

            beta = config.projects["beta"]
            assert beta.container is not None
            config.projects["beta"] = replace(
                beta,
                container=replace(beta.container, name="alpha-container"),
            )
            with self.assertRaisesRegex(ValueError, "同时用于多个项目"):
                _container_project_for_selector(config, "alpha-container")

    def test_resume_opens_persisted_codex_session_in_terminal(self) -> None:
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
                state.finish_run(
                    run_id=claimed.run_id,
                    task_id=task.id,
                    exit_code=0,
                    session_id="thread-1",
                    final_message="done",
                    error=None,
                )
            finally:
                state.close()

            with patch("agent_message.cli.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                exit_code = command_resume(str(config.config_path), task.id)

            self.assertEqual(exit_code, 0)
            command = run.call_args.args[0]
            self.assertEqual(command[0], "codex")
            self.assertIn("resume", command)
            self.assertIn("--include-non-interactive", command)
            self.assertIn("sandbox_workspace_write.network_access=false", command)
            self.assertEqual(command[-1], "thread-1")

    def test_resume_injects_sandbox_mcp_for_sandbox_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), sandbox_gpu_projects=("alpha",))
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
                state.finish_run(
                    run_id=claimed.run_id,
                    task_id=task.id,
                    exit_code=0,
                    session_id="thread-sandbox",
                    final_message="done",
                    error=None,
                )
            finally:
                state.close()

            with patch(
                "agent_message.cli.subprocess.run", return_value=SimpleNamespace(returncode=0)
            ) as run:
                exit_code = command_resume(str(config.config_path), task.id)

            self.assertEqual(exit_code, 0)
            command = run.call_args.args[0]
            joined = "\n".join(command)
            self.assertIn("mcp_servers.agent_message_sandbox.command", joined)
            self.assertIn("agent_message.runtimes.sandbox.mcp", joined)

    def test_resume_injects_container_mcp_for_container_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp), container_projects=("alpha",))
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
                state.finish_run(
                    run_id=claimed.run_id,
                    task_id=task.id,
                    exit_code=0,
                    session_id="thread-container",
                    final_message="done",
                    error=None,
                )
            finally:
                state.close()

            with patch(
                "agent_message.cli.subprocess.run", return_value=SimpleNamespace(returncode=0)
            ) as run:
                exit_code = command_resume(str(config.config_path), task.id)

            self.assertEqual(exit_code, 0)
            command = run.call_args.args[0]
            joined = "\n".join(command)
            self.assertIn("mcp_servers.agent_message_container.command", joined)
            self.assertIn("agent_message.runtimes.container.mcp", joined)

    def test_resume_opens_qoder_with_same_security_and_session(self) -> None:
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
                state.finish_run(
                    run_id=claimed.run_id,
                    task_id=task.id,
                    exit_code=0,
                    session_id="qoder-thread-1",
                    final_message="done",
                    error=None,
                )
            finally:
                state.close()

            with patch(
                "agent_message.cli.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run:
                exit_code = command_resume(str(config.config_path), task.id)

            self.assertEqual(exit_code, 0)
            command = run.call_args.args[0]
            self.assertEqual(command[0], "qodercli")
            self.assertEqual(command[command.index("--permission-mode") + 1], "auto")
            self.assertEqual(command[command.index("--setting-sources") + 1], "")
            self.assertIn("--strict-mcp-config", command)
            self.assertEqual(command[command.index("-r") + 1], "qoder-thread-1")
            self.assertNotIn("-p", command)
            self.assertEqual(run.call_args.kwargs["cwd"], config.projects["alpha"].path)

    def test_resume_qoder_container_uses_only_container_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(
                Path(temp),
                container_projects=("alpha",),
                qoder_only_projects=("alpha",),
            )
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
                state.finish_run(
                    run_id=claimed.run_id,
                    task_id=task.id,
                    exit_code=0,
                    session_id="qoder-container",
                    final_message="done",
                    error=None,
                )
            finally:
                state.close()

            with patch(
                "agent_message.cli.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run:
                self.assertEqual(command_resume(str(config.config_path), task.id), 0)

            command = run.call_args.args[0]
            tools = command[command.index("--tools") + 1].split(",")
            self.assertNotIn("Bash", tools)
            self.assertIn("mcp__agent_message_container__container_run", tools)
