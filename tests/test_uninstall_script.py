from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


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
            self.assertIn("删除 AgentMessage 及其全部本地数据", result.stdout)
            self.assertIn("Uninstall cancelled", result.stdout)
            self.assertTrue(install_dir.exists())


if __name__ == "__main__":
    unittest.main()
