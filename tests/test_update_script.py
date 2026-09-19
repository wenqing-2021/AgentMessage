from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "update.sh"
MANAGED_FILES = (
    (
        "deploy/agent-message-ssh-agent.service",
        ".config/systemd/user/agent-message-ssh-agent.service",
    ),
    (
        "deploy/agent-message-ssh-agent.conf",
        ".config/systemd/user/agent-message.service.d/ssh-agent.conf",
    ),
    ("scripts/load_ssh_keys.py", ".local/libexec/agent-message/load_ssh_keys.py"),
)


class UpdateScriptTests(unittest.TestCase):
    def _fixture(self, root: Path, *, units_current: bool, dirty: bool = False):
        home = root / "home"
        install_dir = root / "install"
        fake_bin = root / "bin"
        log = root / "commands.log"

        for relative, _target in MANAGED_FILES:
            (install_dir / relative).parent.mkdir(parents=True, exist_ok=True)
            (install_dir / relative).write_text(f"template {relative}\n")
        (install_dir / "src" / "agent_message").mkdir(parents=True)
        (install_dir / ".git").mkdir()
        (install_dir / "deploy" / "agent-message.service").write_text("bridge template\n")
        shutil.copy(UPDATER, install_dir / "update.sh")
        updater = install_dir / "update.sh"
        updater.chmod(0o755)

        for relative, target in MANAGED_FILES:
            installed = home / target
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_text(
                (install_dir / relative).read_text() if units_current else "stale copy\n"
            )
        bridge_unit = home / ".config" / "systemd" / "user" / "agent-message.service"
        bridge_unit.parent.mkdir(parents=True, exist_ok=True)
        bridge_unit.write_text("installed bridge unit\n")

        (install_dir / "install.sh").write_text(
            f'#!/usr/bin/env bash\necho "install.sh $*" >> {log}\n'
            f'cp {install_dir}/deploy/agent-message-ssh-agent.service '
            f'{home}/.config/systemd/user/agent-message-ssh-agent.service\n'
            f'cp {install_dir}/deploy/agent-message-ssh-agent.conf '
            f'{home}/.config/systemd/user/agent-message.service.d/ssh-agent.conf\n'
            f'cp {install_dir}/scripts/load_ssh_keys.py {home}/.local/libexec/agent-message/load_ssh_keys.py\n'
            f'touch {bridge_unit}\n'
        )
        (install_dir / "install.sh").chmod(0o755)

        # Keep templates older than the installed bridge unit unless a refresh is expected.
        if units_current:
            os.utime(bridge_unit, (100, 100))
            for path in (install_dir / "deploy" / "agent-message.service", install_dir / "install.sh"):
                os.utime(path, (1, 1))

        fake_bin.mkdir()
        status_output = " M src/agent_message/cli.py\n" if dirty else ""
        (fake_bin / "git").write_text(
            "#!/usr/bin/env bash\n"
            f'echo "git $*" >> {log}\n'
            'if [[ $* == *"status --porcelain"* ]]; then printf "%s" "' + status_output + '"; fi\n'
        )
        (fake_bin / "git").chmod(0o755)
        (fake_bin / "uv").write_text(f'#!/usr/bin/env bash\necho "uv $*" >> {log}\n')
        (fake_bin / "uv").chmod(0o755)
        (fake_bin / "systemctl").write_text(
            "#!/usr/bin/env bash\n"
            f'echo "systemctl $*" >> {log}\n'
            'if [[ $1 == --user && $2 == is-active ]]; then printf "active\nactive\n"; fi\n'
        )
        (fake_bin / "systemctl").chmod(0o755)
        (fake_bin / "ssh-add").write_text(
            "#!/usr/bin/env bash\n"
            f'echo "ssh-add $*" >> {log}\n'
            'printf "256 SHA256:aaa one (ED25519)\n3072 SHA256:bbb two (RSA)\n"\n'
        )
        (fake_bin / "ssh-add").chmod(0o755)

        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_RUNTIME_DIR": str(root / "run"),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
            }
        )
        return updater, log, environment

    def commands(self, log: Path) -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    def test_bash_syntax_and_help(self) -> None:
        syntax = subprocess.run(
            ["bash", "-n", str(UPDATER)], capture_output=True, text=True, check=False
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

        help_result = subprocess.run(
            ["bash", str(UPDATER), "--help"], capture_output=True, text=True, check=False
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--no-pull", help_result.stdout)

    def test_restarts_both_services_in_order_and_prints_key_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            updater, log, environment = self._fixture(Path(temp), units_current=True)
            result = subprocess.run(
                ["bash", str(updater)],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            commands = self.commands(log)
            restarts = [line for line in commands if line.startswith("systemctl --user restart")]
            self.assertEqual(
                restarts,
                [
                    "systemctl --user restart agent-message-ssh-agent.service",
                    "systemctl --user restart agent-message.service",
                ],
            )
            self.assertIn("uv sync --frozen", commands)
            self.assertIn(f"git -C {updater.parent} pull --ff-only", commands)
            self.assertIn("ssh-add ~/.ssh/your_encrypted_key", result.stdout)
            # The script verifies the outcome itself instead of asking for a manual check.
            self.assertIn("服务状态", result.stdout)
            self.assertIn("agent-message-ssh-agent.service: active", result.stdout)
            self.assertIn("agent-message.service: active", result.stdout)
            self.assertIn("已加载密钥：2 把", result.stdout)
            self.assertFalse([line for line in commands if line.startswith("install.sh")])

    def test_refreshes_units_only_when_templates_differ(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            updater, log, environment = self._fixture(Path(temp), units_current=False)
            result = subprocess.run(
                ["bash", str(updater)],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            refresh = [line for line in self.commands(log) if line.startswith("install.sh")]
            self.assertEqual(len(refresh), 1)
            self.assertIn("--refresh-service", refresh[0])

    def test_dirty_checkout_skips_git_pull(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            updater, log, environment = self._fixture(
                Path(temp), units_current=True, dirty=True
            )
            result = subprocess.run(
                ["bash", str(updater)],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("未提交", result.stderr)
            self.assertFalse([line for line in self.commands(log) if "pull" in line])

    def test_no_pull_flag_skips_git_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            updater, log, environment = self._fixture(Path(temp), units_current=True)
            result = subprocess.run(
                ["bash", str(updater), "--no-pull"],
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse([line for line in self.commands(log) if line.startswith("git ")])


if __name__ == "__main__":
    unittest.main()
