from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_message.cli import command_resume
from agent_message.models import AgentKind
from agent_message.state import StateStore

from tests.helpers import make_config


class CliTests(unittest.TestCase):
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
