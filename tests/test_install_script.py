from __future__ import annotations

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
                ["bash", "-c", shell, "bash", str(INSTALLER), temp],
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
