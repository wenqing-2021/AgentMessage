from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_message.core.config import load_config


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def test_bash_syntax_and_help(self) -> None:
        syntax = subprocess.run(
            ["bash", "-n", str(INSTALLER)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        help_result = subprocess.run(
            ["bash", str(INSTALLER), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("~/workspace/AgentMessage", help_result.stdout)

    def test_credentials_and_checkpoint_are_written_with_mode_600(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            shell = r'''
source "$1"
CONFIG_DIR="$2/config"
ENV_FILE="$CONFIG_DIR/feishu.env"
INSTALL_DIR="$2/repository"
STATE_FILE="$INSTALL_DIR/.git/agent-message-install-stage"
mkdir -p "$INSTALL_DIR/.git"
write_credentials_file "cli_test" "" "ou_test"
write_stage credentials
[[ $(read_env_value AGENT_MESSAGE_FEISHU_APP_ID) == cli_test ]]
[[ -z $(read_env_value AGENT_MESSAGE_FEISHU_APP_SECRET) ]]
[[ $(read_stage) == credentials ]]
write_credentials_file "cli_test" "secret_test" "ou_test"
[[ $(read_env_value AGENT_MESSAGE_FEISHU_APP_SECRET) == secret_test ]]
[[ $(read_env_value AGENT_MESSAGE_ALLOWED_OPEN_IDS) == ou_test ]]
[[ $(read_stage) == credentials ]]
[[ $(stat -c %a "$ENV_FILE") == 600 ]]
[[ $(stat -c %a "$STATE_FILE") == 600 ]]
'''
            result = subprocess.run(
                ["bash", "-c", shell, "bash", str(INSTALLER), temp],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_agent_service_is_installed_with_mode_600(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = r'''
source "$1"
INSTALL_DIR="$2"
CONFIG_HOME="$3"
SYSTEMD_USER_DIR="$CONFIG_HOME/systemd/user"
SSH_AGENT_UNIT_FILE="$SYSTEMD_USER_DIR/agent-message-ssh-agent.service"
SSH_AGENT_DROPIN_DIR="$SYSTEMD_USER_DIR/agent-message.service.d"
SSH_AGENT_DROPIN_FILE="$SSH_AGENT_DROPIN_DIR/ssh-agent.conf"
SSH_AGENT_LOADER_DIR="$4/.local/libexec/agent-message"
SSH_AGENT_LOADER_FILE="$SSH_AGENT_LOADER_DIR/load_ssh_keys.py"
install_ssh_agent_service
install_ssh_agent_service
[[ $(stat -c %a "$SSH_AGENT_UNIT_FILE") == 600 ]]
[[ $(stat -c %a "$SSH_AGENT_DROPIN_FILE") == 600 ]]
[[ $(stat -c %a "$SSH_AGENT_LOADER_FILE") == 600 ]]
[[ $(stat -c %a "$SSH_AGENT_LOADER_DIR") == 700 ]]
cmp "$2/deploy/agent-message-ssh-agent.service" "$SSH_AGENT_UNIT_FILE"
cmp "$2/deploy/agent-message-ssh-agent.conf" "$SSH_AGENT_DROPIN_FILE"
grep -q '^# Managed by AgentMessage:' "$SSH_AGENT_UNIT_FILE"
grep -q '^# Managed by AgentMessage:' "$SSH_AGENT_DROPIN_FILE"
grep -q '^# Managed by AgentMessage: SSH key loader$' "$SSH_AGENT_LOADER_FILE"
'''
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    shell,
                    "bash",
                    str(INSTALLER),
                    str(ROOT),
                    str(root / "config"),
                    str(root / "home"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_agent_service_requires_its_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shell = r'''
source "$1"
INSTALL_DIR="$2"
SSH_AGENT_UNIT_FILE="$3/agent-message-ssh-agent.service"
SSH_AGENT_DROPIN_DIR="$3/agent-message.service.d"
SSH_AGENT_DROPIN_FILE="$SSH_AGENT_DROPIN_DIR/ssh-agent.conf"
SSH_AGENT_LOADER_DIR="$3/libexec"
SSH_AGENT_LOADER_FILE="$SSH_AGENT_LOADER_DIR/load_ssh_keys.py"
install_ssh_agent_service
'''
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    shell,
                    "bash",
                    str(INSTALLER),
                    str(root / "checkout"),
                    str(root / "config"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Missing SSH agent installation file", result.stderr)

    def test_existing_install_adds_agent_service_on_the_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            install_dir = root / "AgentMessage"
            config_home = home / ".config"
            (install_dir / ".git").mkdir(parents=True)
            (install_dir / ".venv/bin").mkdir(parents=True)
            (install_dir / "config").mkdir()
            (install_dir / "scripts").mkdir()
            (config_home / "agent-message").mkdir(parents=True)
            (config_home / "systemd/user").mkdir(parents=True)
            shutil.copytree(ROOT / "deploy", install_dir / "deploy")
            shutil.copy2(
                ROOT / "scripts/load_ssh_keys.py", install_dir / "scripts/load_ssh_keys.py"
            )
            executable = install_dir / ".venv/bin/agent-message"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            (install_dir / "config/projects.toml").write_text("[service]\n")
            (config_home / "agent-message/feishu.env").write_text(
                "AGENT_MESSAGE_FEISHU_APP_ID=cli_test\n"
                "AGENT_MESSAGE_FEISHU_APP_SECRET=secret_test\n"
            )
            (config_home / "systemd/user/agent-message.service").write_text("stale unit\n")
            (install_dir / ".git/agent-message-install-stage").write_text("complete\n")

            fake_bin = root / "bin"
            fake_bin.mkdir()
            systemctl = fake_bin / "systemctl"
            systemctl.write_text("#!/bin/sh\nexit 0\n")
            systemctl.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "XDG_CONFIG_HOME": str(config_home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )

            result = subprocess.run(
                ["bash", str(INSTALLER), "--install-dir", str(install_dir)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            units = config_home / "systemd/user"
            self.assertTrue((units / "agent-message-ssh-agent.service").exists())
            self.assertTrue((units / "agent-message.service.d/ssh-agent.conf").exists())
            self.assertTrue((home / ".local/libexec/agent-message/load_ssh_keys.py").exists())
            self.assertTrue(
                (units / "agent-message.service").read_text().startswith(
                    "# Managed by AgentMessage install.sh"
                )
            )

    def test_bridge_unit_is_rendered_from_the_deploy_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir = root / "AgentMessage"
            config_home = root / "config"
            (install_dir / "deploy").mkdir(parents=True)
            (install_dir / "deploy/agent-message.service").write_text(
                (ROOT / "deploy/agent-message.service").read_text() + "# template marker\n"
            )
            shell = r'''
source "$1"
INSTALL_DIR="$2"
CONFIG_HOME="$3"
SYSTEMD_USER_DIR="$CONFIG_HOME/systemd/user"
UNIT_FILE="$SYSTEMD_USER_DIR/agent-message.service"
ENV_FILE="$CONFIG_HOME/agent-message/feishu.env"
write_user_service_file
grep -q '^# Managed by AgentMessage install.sh$' "$UNIT_FILE"
grep -q '^# template marker$' "$UNIT_FILE"
if grep -q '@AGENT_MESSAGE_' "$UNIT_FILE"; then exit 3; fi
'''
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    shell,
                    "bash",
                    str(INSTALLER),
                    str(install_dir),
                    str(config_home),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_systemd_unit_uses_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            shell = r'''
source "$1"
INSTALL_DIR="$2/install path"
PROJECT_CONFIG="$INSTALL_DIR/config/projects.toml"
CONFIG_DIR="$2/config"
ENV_FILE="$CONFIG_DIR/feishu.env"
SYSTEMD_USER_DIR="$2/systemd"
UNIT_FILE="$SYSTEMD_USER_DIR/agent-message.service"
mkdir -p "$INSTALL_DIR/.venv/bin" "$INSTALL_DIR/config" "$CONFIG_DIR"
mkdir -p "$INSTALL_DIR/deploy"
cp "$3/deploy/agent-message.service" "$INSTALL_DIR/deploy/agent-message.service"
touch "$INSTALL_DIR/.venv/bin/agent-message" "$PROJECT_CONFIG" "$ENV_FILE"
chmod 755 "$INSTALL_DIR/.venv/bin/agent-message"
write_user_service_file
escaped_install=$(systemd_path_value "$INSTALL_DIR")
escaped_env=$(systemd_path_value "$ENV_FILE")
grep -F "WorkingDirectory=$escaped_install" "$UNIT_FILE"
grep -F "ExecStart=\"$INSTALL_DIR/.venv/bin/agent-message\" run --config \"$PROJECT_CONFIG\"" "$UNIT_FILE"
grep -F "EnvironmentFile=$escaped_env" "$UNIT_FILE"
[[ $(stat -c %a "$UNIT_FILE") == 600 ]]
'''
            result = subprocess.run(
                ["bash", "-c", shell, "bash", str(INSTALLER), temp, str(ROOT)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            analyzer = shutil.which("systemd-analyze")
            if analyzer is not None:
                verify = subprocess.run(
                    [analyzer, "verify", str(Path(temp) / "systemd/agent-message.service")],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotIn(
                    "agent-message.service: Unit configuration has fatal error",
                    verify.stderr,
                )
                self.assertNotIn("WorkingDirectory= path is not absolute", verify.stderr)

    def test_generated_project_config_is_valid_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            install_dir = Path(temp) / "AgentMessage"
            (install_dir / "config").mkdir(parents=True)
            shell = r'''
source "$1"
INSTALL_DIR="$2"
PROJECT_CONFIG="$INSTALL_DIR/config/projects.toml"
create_project_config
[[ $(stat -c %a "$PROJECT_CONFIG") == 600 ]]
'''
            result = subprocess.run(
                ["bash", "-c", shell, "bash", str(INSTALLER), str(install_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            config = load_config(install_dir / "config/projects.toml")
            self.assertEqual(config.service.default_chat_project, "agent_message")
            self.assertEqual(config.projects["agent_message"].path, install_dir)


if __name__ == "__main__":
    unittest.main()
