from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SshAgentServiceTests(unittest.TestCase):
    def test_unit_readiness_and_private_runtime_socket(self):
        unit = (ROOT / 'deploy/agent-message-ssh-agent.service').read_text()
        dependency = (ROOT / 'deploy/agent-message-ssh-agent.conf').read_text()
        # Forking waits for the bound socket before ssh-add and ordered consumers start.
        self.assertIn('Type=forking\n', unit)
        self.assertIn('RuntimeDirectory=agent-message-ssh\n', unit)
        self.assertIn('RuntimeDirectoryMode=0700\n', unit)
        self.assertIn('Environment=SSH_AUTH_SOCK=%t/agent-message-ssh/agent.sock\n', unit)
        self.assertIn('ExecStart=/usr/bin/ssh-agent -a %t/agent-message-ssh/agent.sock\n', unit)
        self.assertIn('ExecStartPost=/usr/bin/python3 %h/.local/libexec/agent-message/load_ssh_keys.py', unit)
        self.assertIn('SSH_ASKPASS_REQUIRE=never', unit)
        self.assertIn('StandardInput=null', unit)
        self.assertIn('Restart=on-failure', unit)
        self.assertIn('After=agent-message-ssh-agent.service', dependency)
        self.assertIn('Wants=agent-message-ssh-agent.service', dependency)
        self.assertNotIn('Requires=', dependency)
        self.assertNotIn('ssh-keygen', unit)

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze unavailable')
    def test_unit_parses_with_systemd(self):
        result = subprocess.run(['systemd-analyze', 'verify',
            str(ROOT / 'deploy/agent-message-ssh-agent.service')],
            capture_output=True, text=True, timeout=10)
        # Ignore unrelated installed system units: only inspect diagnostics for ours.
        errors = [line for line in result.stderr.splitlines()
                  if 'agent-message-ssh-agent.service:' in line]
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
