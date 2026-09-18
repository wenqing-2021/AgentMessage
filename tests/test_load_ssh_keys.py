import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('load_ssh_keys',
    Path(__file__).resolve().parents[1] / 'scripts/load_ssh_keys.py')
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


class LoadSshKeysTests(unittest.TestCase):
    def key(self, root, name, header=b'-----BEGIN OPENSSH PRIVATE KEY-----'):
        path = root / name
        path.write_bytes(header + b'\nTEST-ONLY\n')
        path.chmod(0o600)
        return path

    def test_scans_custom_filenames_and_skips_non_keys_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.key(root, 'custom_key')
            second = self.key(root, 'id_rsa', b'-----BEGIN RSA PRIVATE KEY-----')
            self.key(root, 'known_hosts', b'example.invalid ssh-ed25519 public')
            self.key(root, 'custom_key.pub', b'ssh-ed25519 public')
            self.key(root, 'config', b'Host example.invalid')
            unsafe = self.key(root, 'unsafe'); unsafe.chmod(0o644)
            (root / 'link').symlink_to(first)
            (root / 'nested').mkdir()
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                self.assertEqual(kwargs['env']['SSH_ASKPASS_REQUIRE'], 'never')
                self.assertNotIn('DISPLAY', kwargs['env'])
                self.assertNotIn('SSH_ASKPASS', kwargs['env'])
                self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
                self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
                self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
                return SimpleNamespace(returncode=1 if argv == ['ssh-add', '-l'] else 0)
            with patch.dict(os.environ, {'SSH_AUTH_SOCK': '/test/agent.sock', 'DISPLAY': ':0',
                                         'SSH_ASKPASS': '/test/prompt'}), patch.object(loader.subprocess, 'run', run):
                self.assertEqual(loader.load_keys(root), (2, 0))
            self.assertEqual([a[-1] for a in calls if a[:2] == ['ssh-add', '-q']],
                             [str(first), str(second)])

    def test_encrypted_invalid_and_timeout_keys_do_not_block_valid_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('encrypted', 'invalid', 'timeout', 'valid'):
                self.key(root, name)
            added = []
            def run(argv, **kwargs):
                if argv == ['ssh-add', '-l']:
                    return SimpleNamespace(returncode=1)
                name = Path(argv[-1]).name
                if name == 'timeout':
                    raise subprocess.TimeoutExpired(argv, 5)
                if argv[0] == 'ssh-keygen':
                    return SimpleNamespace(returncode=1 if name in ('encrypted', 'invalid') else 0)
                added.append(name)
                return SimpleNamespace(returncode=0)
            with patch.dict(os.environ, {'SSH_AUTH_SOCK': '/test/agent.sock'}), patch.object(loader.subprocess, 'run', run):
                self.assertEqual(loader.load_keys(root), (1, 3))
            self.assertEqual(added, ['valid'])

    def test_missing_agent_fails_and_empty_directory_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    loader.load_keys(Path(directory))
            with patch.dict(os.environ, {'SSH_AUTH_SOCK': '/test/agent.sock'}), patch.object(
                loader.subprocess, 'run', return_value=SimpleNamespace(returncode=2)
            ):
                with self.assertRaises(RuntimeError):
                    loader.load_keys(Path(directory))
            with patch.dict(os.environ, {'SSH_AUTH_SOCK': '/test/agent.sock'}), patch.object(
                loader.subprocess, 'run', return_value=SimpleNamespace(returncode=1)
            ):
                self.assertEqual(loader.load_keys(Path(directory)), (0, 0))

    def test_budget_limits_startup_and_foreign_owner_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = self.key(root, 'key')
            with patch.object(loader.os, 'getuid', return_value=os.getuid() + 1):
                self.assertFalse(loader.private_key_candidate(key))
            with patch.dict(os.environ, {'SSH_AUTH_SOCK': '/test/agent.sock'}), patch.object(
                loader.subprocess, 'run', return_value=SimpleNamespace(returncode=1)
            ) as run:
                self.assertEqual(loader.load_keys(root, budget_seconds=0), (0, 1))
                run.assert_called_once()
