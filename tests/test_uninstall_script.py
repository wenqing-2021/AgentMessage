from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_message.core.bot_registry import register_bot
from agent_message.core.config import ConfigError, load_config


ROOT = Path(__file__).resolve().parents[1]
UNINSTALLER = ROOT / "uninstall.sh"


class UninstallScriptTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, dict[str, str]]:
        home = root / "home"
        install_dir = home / "workspace" / "AgentMessage"
        config_home = home / ".config"
        data_home = home / ".local" / "share"
        fake_bin = root / "bin"

        (install_dir / "src" / "agent_message").mkdir(parents=True)
        (install_dir / "config").mkdir()
        (install_dir / "var" / "logs").mkdir(parents=True)
        (config_home / "agent-message").mkdir(parents=True)
        (config_home / "systemd" / "user").mkdir(parents=True)
        fake_bin.mkdir()

        (install_dir / "pyproject.toml").write_text("[project]\nname='agent-message'\n")
        (install_dir / "config" / "projects.toml").write_text("project registry\n")
        (install_dir / "var" / "agent-message.sqlite3").write_bytes(b"sqlite state")
        (install_dir / "var" / "logs" / "run.jsonl").write_text("task log\n")
        (config_home / "agent-message" / "feishu.env").write_text(
            "AGENT_MESSAGE_FEISHU_APP_SECRET=test_secret\n"
        )
        (config_home / "systemd" / "user" / "agent-message.service").write_text(
            "[Service]\n"
        )
        systemctl = fake_bin / "systemctl"
        systemctl.write_text("#!/bin/sh\nexit 1\n")
        systemctl.chmod(0o755)

        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(config_home),
                "XDG_DATA_HOME": str(data_home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
            }
        )
        return install_dir, environment

    def test_shared_checkout_stops_additional_bots_before_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            install_dir, environment = self._fixture(Path(temp))
            root = Path(temp)
            log = root / "systemctl.log"
            (root / "bin/systemctl").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
            config_home = Path(environment["XDG_CONFIG_HOME"])
            credentials = config_home / "agent-message/bots"
            credentials.mkdir()
            (credentials / "cli_second.env").write_text("test secret\n")
            unit = config_home / "systemd/user/agent-message-bot-cli_second.service"
            unit.write_text("# Managed by AgentMessage install.sh\n")
            result = subprocess.run(["bash", str(UNINSTALLER), "--install-dir", str(install_dir),
                "--purge-data", "--yes"], env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--user stop agent-message-bot-cli_second.service", log.read_text())
            self.assertFalse(unit.exists())
            self.assertFalse(install_dir.exists())
            self.assertFalse(credentials.exists())

    def _bot_fixture(self, root: Path) -> tuple[Path, dict[str, str]]:
        install_dir, environment = self._fixture(root)
        config_home = Path(environment["XDG_CONFIG_HOME"])
        (config_home / "agent-message" / "feishu.env").write_text(
            "AGENT_MESSAGE_FEISHU_APP_ID=cli_primary\n"
            "AGENT_MESSAGE_FEISHU_APP_SECRET=primary_secret\n"
        )
        bots = config_home / "agent-message" / "bots"
        bots.mkdir()
        (bots / "cli_second.env").write_text(
            "AGENT_MESSAGE_FEISHU_APP_ID=cli_second\n"
            "AGENT_MESSAGE_FEISHU_APP_SECRET=second_secret\n"
        )
        for alias in ("agent_message", "alpha"):
            (root / alias).mkdir()
        config_path = install_dir / "config" / "projects.toml"
        config_path.write_text(
            '[service]\ndefault_chat_project = "agent_message"\n'
            + "".join(
                f'[projects.{alias}]\npath = "{root / alias}"\n'
                for alias in ("agent_message", "alpha")
            )
        )
        register_bot(config_path, "cli_primary", "cli_second", ["agent_message"])
        interpreter = install_dir / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        interpreter.chmod(0o755)
        (install_dir / "var" / "bots" / "cli_second").mkdir(parents=True)
        (install_dir / "var" / "bots" / "cli_second" / "agent-message.sqlite3").write_bytes(
            b"second state"
        )
        (install_dir / "var" / "logs" / "bots" / "cli_second").mkdir(parents=True)
        (install_dir / "var" / "logs" / "bots" / "cli_second" / "run.jsonl").write_text(
            "second log\n"
        )
        units = config_home / "systemd" / "user"
        (units / "agent-message-bot-cli_second.service").write_text(
            "# Managed by AgentMessage install.sh\n[Service]\n"
        )
        dropin = units / "agent-message-bot-cli_second.service.d" / "ssh-agent.conf"
        dropin.parent.mkdir()
        dropin.write_text("# Managed by AgentMessage: dedicated SSH agent dependency\n")
        log = root / "systemctl.log"
        systemctl = root / "bin" / "systemctl"
        systemctl.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 0\n')
        systemctl.chmod(0o755)
        environment["PYTHONPATH"] = str(ROOT / "src")
        return install_dir, environment

    def test_app_id_removes_one_bot_and_keeps_the_shared_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._bot_fixture(root)
            config_home = Path(environment["XDG_CONFIG_HOME"])
            result = subprocess.run(
                ["bash", str(UNINSTALLER), "--install-dir", str(install_dir),
                 "--app-id", "cli_second", "--yes"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(
                (config_home / "systemd/user/agent-message-bot-cli_second.service").exists()
            )
            self.assertFalse((config_home / "agent-message/bots/cli_second.env").exists())
            self.assertFalse((install_dir / "var/bots/cli_second").exists())
            self.assertFalse((install_dir / "var/logs/bots/cli_second").exists())
            self.assertTrue((install_dir / "src/agent_message").is_dir())
            self.assertTrue((config_home / "systemd/user/agent-message.service").exists())
            self.assertTrue((config_home / "agent-message/feishu.env").exists())
            self.assertTrue((install_dir / "var/agent-message.sqlite3").exists())
            primary = load_config(install_dir / "config/projects.toml")
            self.assertEqual(set(primary.projects), {"agent_message", "alpha"})
            with self.assertRaises(ConfigError):
                load_config(install_dir / "config/projects.toml", app_id="cli_second")
            backups = list(
                (Path(environment["XDG_DATA_HOME"]) / "agent-message/backups").glob(
                    "bot-cli_second-*"
                )
            )
            self.assertEqual(len(backups), 1)
            self.assertIn(
                "second_secret", (backups[0] / "credentials/cli_second.env").read_text()
            )
            self.assertEqual(
                (backups[0] / "state/agent-message.sqlite3").read_bytes(), b"second state"
            )
            log = (root / "systemctl.log").read_text()
            self.assertIn("--user stop agent-message-bot-cli_second.service", log)
            self.assertIn("--user restart agent-message.service", log)

    def test_app_id_refuses_primary_unknown_and_unmanaged_bots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._bot_fixture(root)
            config_home = Path(environment["XDG_CONFIG_HOME"])
            for app_id in ("cli_primary", "cli_missing", "../bad"):
                result = subprocess.run(
                    ["bash", str(UNINSTALLER), "--install-dir", str(install_dir),
                     "--app-id", app_id, "--yes"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(result.returncode, 0, app_id)
            self.assertTrue((config_home / "agent-message/bots/cli_second.env").exists())
            self.assertTrue((install_dir / "var/bots/cli_second").is_dir())
            unit = config_home / "systemd/user/agent-message-bot-cli_second.service"
            self.assertTrue(unit.exists())
            self.assertEqual(
                set(
                    load_config(
                        install_dir / "config/projects.toml", app_id="cli_second"
                    ).projects
                ),
                {"agent_message"},
            )
            unit.write_text("[Service]\n")
            result = subprocess.run(
                ["bash", str(UNINSTALLER), "--install-dir", str(install_dir),
                 "--app-id", "cli_second", "--purge-data", "--yes"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unmanaged unit file", result.stderr)
            self.assertTrue((config_home / "agent-message/bots/cli_second.env").exists())
            self.assertTrue((install_dir / "var/bots/cli_second").is_dir())
            self.assertTrue(unit.exists())

    def test_bash_syntax_and_help(self) -> None:
        syntax = subprocess.run(
            ["bash", "-n", str(UNINSTALLER)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        help_result = subprocess.run(
            ["bash", str(UNINSTALLER), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--keep-data", help_result.stdout)
        self.assertIn("--purge-data", help_result.stdout)
        self.assertIn("--app-id", help_result.stdout)
        self.assertIn("Interactive (default)", help_result.stdout)

    @unittest.skipUnless(shutil.which("script"), "requires util-linux script")
    def test_interactive_selection_removes_only_the_chosen_bot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._bot_fixture(root)
            config_home = Path(environment["XDG_CONFIG_HOME"])
            result = self._run_interactive(environment, install_dir, "2\n\ny\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Installed bots:", result.stdout)
            self.assertIn("2) cli_second", result.stdout)
            self.assertIn("Remove 1 bot(s) (cli_second)", result.stdout)
            self.assertFalse(
                (config_home / "systemd/user/agent-message-bot-cli_second.service").exists()
            )
            self.assertFalse((config_home / "agent-message/bots/cli_second.env").exists())
            self.assertFalse((install_dir / "var/bots/cli_second").exists())
            self.assertFalse((install_dir / "var/logs/bots/cli_second").exists())
            self.assertTrue((install_dir / "src/agent_message").is_dir())
            self.assertTrue((config_home / "systemd/user/agent-message.service").exists())
            self.assertTrue((config_home / "agent-message/feishu.env").exists())
            self.assertEqual(set(load_config(install_dir / "config/projects.toml").bots),
                             {"cli_primary"})
            with self.assertRaises(ConfigError):
                load_config(install_dir / "config/projects.toml", app_id="cli_second")
            backups = list(
                (Path(environment["XDG_DATA_HOME"]) / "agent-message/backups").glob(
                    "bot-cli_second-*"
                )
            )
            self.assertEqual(len(backups), 1)
            log = (root / "systemctl.log").read_text()
            self.assertIn("--user stop agent-message-bot-cli_second.service", log)
            self.assertIn("--user restart agent-message.service", log)

    @unittest.skipUnless(shutil.which("script"), "requires util-linux script")
    def test_interactive_selection_all_removes_the_shared_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._bot_fixture(root)
            config_home = Path(environment["XDG_CONFIG_HOME"])
            result = self._run_interactive(environment, install_dir, "a\n\ny\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("a) all: remove the shared installation and every bot", result.stdout)
            self.assertIn("Remove AgentMessage and all its local data", result.stdout)
            self.assertFalse(install_dir.exists())
            self.assertFalse((config_home / "agent-message").exists())

    @unittest.skipUnless(shutil.which("script"), "requires util-linux script")
    def test_interactive_selection_quit_keeps_everything(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._bot_fixture(root)
            config_home = Path(environment["XDG_CONFIG_HOME"])
            result = self._run_interactive(environment, install_dir, "q\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Uninstall cancelled", result.stdout)
            self.assertTrue(install_dir.exists())
            self.assertTrue((config_home / "agent-message/feishu.env").exists())
            self.assertTrue((config_home / "agent-message/bots/cli_second.env").exists())

    @unittest.skipUnless(shutil.which("script"), "requires util-linux script")
    def test_interactive_primary_choice_requires_the_full_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._bot_fixture(root)
            config_home = Path(environment["XDG_CONFIG_HOME"])
            result = self._run_interactive(environment, install_dir, "1\nn\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cli_primary is the primary bot", result.stdout)
            self.assertIn("Nothing was removed", result.stdout)
            self.assertTrue(install_dir.exists())
            self.assertTrue((config_home / "agent-message/bots/cli_second.env").exists())
            self.assertTrue(
                (config_home / "systemd/user/agent-message-bot-cli_second.service").exists()
            )

    @unittest.skipUnless(shutil.which("script"), "requires util-linux script")
    def test_interactive_selection_rejects_an_unknown_number(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._bot_fixture(root)
            result = self._run_interactive(environment, install_dir, "9\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("is not in the list", result.stdout)
            self.assertTrue(install_dir.exists())
            self.assertTrue((install_dir / "var/bots/cli_second").is_dir())

    def _run_interactive(
        self, environment: dict[str, str], install_dir: Path, input_text: str
    ) -> subprocess.CompletedProcess[str]:
        command = shlex.join(["bash", str(UNINSTALLER), "--install-dir", str(install_dir)])
        return subprocess.run(
            ["script", "-qec", command, "/dev/null"],
            input=input_text,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_empty_app_id_never_falls_back_to_a_full_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._bot_fixture(root)
            config_home = Path(environment["XDG_CONFIG_HOME"])
            result = subprocess.run(
                [
                    "bash",
                    str(UNINSTALLER),
                    "--install-dir",
                    str(install_dir),
                    "--app-id",
                    "",
                    "--purge-data",
                    "--yes",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("non-empty App ID", result.stderr)
            self.assertTrue(install_dir.exists())
            self.assertTrue((config_home / "agent-message/feishu.env").exists())
            self.assertTrue((config_home / "agent-message/bots/cli_second.env").exists())

    def test_malformed_registry_reports_a_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._bot_fixture(root)
            config_home = Path(environment["XDG_CONFIG_HOME"])
            (install_dir / "config/projects.toml").write_text("not = [valid toml\n")
            result = subprocess.run(
                [
                    "bash",
                    str(UNINSTALLER),
                    "--install-dir",
                    str(install_dir),
                    "--app-id",
                    "cli_second",
                    "--purge-data",
                    "--yes",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Cannot read the shared project configuration", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertTrue((config_home / "agent-message/bots/cli_second.env").exists())
            self.assertTrue((install_dir / "var/bots/cli_second").is_dir())
            self.assertTrue(
                (config_home / "systemd/user/agent-message-bot-cli_second.service").exists()
            )

    def test_purge_removes_checkout_credentials_and_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._fixture(root)
            result = subprocess.run(
                [
                    "bash",
                    str(UNINSTALLER),
                    "--install-dir",
                    str(install_dir),
                    "--purge-data",
                    "--yes",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(install_dir.exists())
            self.assertFalse((Path(environment["XDG_CONFIG_HOME"]) / "agent-message").exists())
            self.assertFalse(
                (
                    Path(environment["XDG_CONFIG_HOME"])
                    / "systemd/user/agent-message.service"
                ).exists()
            )
            self.assertFalse((Path(environment["XDG_DATA_HOME"]) / "agent-message").exists())

    def test_optional_ssh_service_cleanup_preserves_keys_and_unmanaged_units(self) -> None:
        for managed in (True, False):
            with self.subTest(managed=managed), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                install_dir, environment = self._fixture(root)
                units = Path(environment["XDG_CONFIG_HOME"]) / "systemd/user"
                agent_unit = units / "agent-message-ssh-agent.service"
                dropin = units / "agent-message.service.d/ssh-agent.conf"
                dropin.parent.mkdir()
                header = "# Managed by AgentMessage: dedicated SSH agent\n" if managed else "# custom\n"
                agent_unit.write_text(header + "[Service]\n")
                dropin.write_text(header + "[Unit]\n")
                loader = Path(environment["HOME"]) / ".local/libexec/agent-message/load_ssh_keys.py"
                loader.parent.mkdir(parents=True)
                loader.write_text("# Managed by AgentMessage: SSH key loader\n" if managed else "# custom\n")
                key = Path(environment["HOME"]) / ".ssh/id_ed25519"
                key.parent.mkdir()
                key.write_text("test key sentinel")
                result = subprocess.run(["bash", str(UNINSTALLER), "--install-dir", str(install_dir),
                    "--purge-data", "--yes"], env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(agent_unit.exists(), not managed)
                self.assertEqual(dropin.exists(), not managed)
                self.assertEqual(loader.exists(), not managed)
                self.assertEqual(key.read_text(), "test key sentinel")

    def test_keep_data_backs_up_before_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._fixture(root)
            result = subprocess.run(
                [
                    "bash",
                    str(UNINSTALLER),
                    "--install-dir",
                    str(install_dir),
                    "--keep-data",
                    "--yes",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(install_dir.exists())

            backups = list(
                (Path(environment["XDG_DATA_HOME"]) / "agent-message/backups").glob(
                    "uninstall-*"
                )
            )
            self.assertEqual(len(backups), 1)
            backup = backups[0]
            self.assertEqual(
                (backup / "repository/config/projects.toml").read_text(),
                "project registry\n",
            )
            self.assertEqual(
                (backup / "repository/var/agent-message.sqlite3").read_bytes(),
                b"sqlite state",
            )
            self.assertIn(
                "test_secret",
                (backup / "user-config/feishu.env").read_text(),
            )
            self.assertEqual(backup.stat().st_mode & 0o077, 0)

    def test_refuses_home_as_install_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._fixture(root)
            home = Path(environment["HOME"])
            sentinel = home / "keep-me"
            sentinel.write_text("safe\n")
            result = subprocess.run(
                [
                    "bash",
                    str(UNINSTALLER),
                    "--install-dir",
                    str(home),
                    "--purge-data",
                    "--yes",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(sentinel.exists())
            self.assertTrue(install_dir.exists())

    @unittest.skipUnless(shutil.which("script"), "requires util-linux script")
    def test_interactive_data_choice_defaults_to_purge(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir, environment = self._fixture(root)
            command = shlex.join(
                ["bash", str(UNINSTALLER), "--install-dir", str(install_dir)]
            )
            result = subprocess.run(
                ["script", "-qec", command, "/dev/null"],
                input="\n\n",
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Remove AgentMessage and all its local data", result.stdout)
            self.assertIn("Uninstall cancelled", result.stdout)
            self.assertTrue(install_dir.exists())


if __name__ == "__main__":
    unittest.main()
