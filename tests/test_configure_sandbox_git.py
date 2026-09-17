import importlib.util
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "configure_sandbox_git", Path(__file__).resolve().parents[1] / "scripts/configure_sandbox_git.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class ConfigureSandboxGitTests(unittest.TestCase):
    def test_preserves_other_settings_and_is_repeatable(self):
        original = ('# keep comment\n[service]\nstate_dir = "var"\n'
                    'sandbox_git_user_name = "Old"\n'
                    'sandbox_ssh_agent_socket = "/tmp/old.sock"\n'
                    'sandbox_ssh_known_hosts = "/tmp/old.hosts"\n'
                    '[projects.example]\npath = "/tmp/example"\n')
        values = {setup.KEYS[0]: 'Example "Author" \\ Name',
                  setup.KEYS[1]: "example@example.com"}
        updated = setup.update_text(original, values)
        self.assertTrue(updated.startswith("# keep comment"))
        parsed = setup.tomllib.loads(updated)
        self.assertEqual(parsed["projects"], setup.tomllib.loads(original)["projects"])
        self.assertEqual(parsed["service"], {"state_dir": "var", **values})
        self.assertEqual(setup.update_text(updated, values), updated)

    def test_unusual_layout_fails_without_writing(self):
        with self.assertRaises(ValueError):
            setup.update_text('[service]\n"sandbox_git_user_name" = "Old"\n',
                              {setup.KEYS[0]: "New"})

    def test_save_mode_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projects.toml"
            path.write_text("original")
            setup.save(path, "original", "replacement")
            self.assertEqual(path.read_text(), "replacement")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                setup.save(path, "original", "lost update")
            self.assertEqual(path.read_text(), "replacement")
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                setup.save(link, "replacement", "bad")

    def test_ssh_resources_and_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            sock_path = Path(directory) / "agent.sock"
            hosts = Path(directory) / "known_hosts"
            hosts.write_text("example.invalid ssh-ed25519 EXAMPLE\n")
            real_stat = Path.stat

            def fake_stat(path, *args, **kwargs):
                if path == sock_path:
                    return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600,
                                           st_uid=owner)
                return real_stat(path, *args, **kwargs)

            owner = os.getuid()
            with patch.object(Path, "stat", fake_stat):
                self.assertEqual(setup.check_ssh(str(sock_path), str(hosts)),
                                 (str(sock_path), str(hosts)))
                owner += 1
                with self.assertRaises(ValueError):
                    setup.check_ssh(str(sock_path), str(hosts))
            with self.assertRaises(ValueError):
                setup.check_ssh(str(hosts), str(hosts))
            with self.assertRaises(ValueError):
                setup.check_ssh("relative.sock", str(hosts))

    def test_prompt_does_not_display_detected_identity(self):
        with patch.object(setup.getpass, "getpass", return_value="") as prompt:
            self.assertEqual(setup.ask("Name", "private-value"), "private-value")
            self.assertNotIn("private-value", prompt.call_args.args[0])
        with patch.object(setup.getpass, "getpass", return_value="bad\nvalue"):
            with self.assertRaises(ValueError):
                setup.ask("Name")


if __name__ == "__main__":
    unittest.main()
