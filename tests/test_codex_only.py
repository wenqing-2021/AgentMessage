"""Regression tests for removal of alternate-agent execution and legacy state."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_message.agents.adapters import AdapterError, CodexAdapter, adapter_for
from agent_message.core.config import ConfigError, load_config
from agent_message.core.models import AgentKind, TaskOrigin
from agent_message.core.state import StateStore
from agent_message.orchestration.commands import CommandError, parse_command
from agent_message.orchestration.router import MessageRouter
from tests.helpers import inbound, make_config


class CodexOnlyTests(unittest.TestCase):
    def test_factory_and_command_reject_removed_agent(self):
        self.assertEqual([agent.value for agent in AgentKind], ['codex'])
        self.assertIsInstance(adapter_for(AgentKind.CODEX), CodexAdapter)
        for removed in ('qoder', 'qorder', 'unknown'):
            with self.subTest(agent=removed):
                with self.assertRaises(AdapterError):
                    adapter_for(removed)
                with self.assertRaises(CommandError):
                    parse_command(f'/new alpha --agent {removed} hello')
        self.assertEqual(parse_command('/new alpha --agent codex hello').agent, AgentKind.CODEX)

    def test_config_rejects_removed_default_and_allowed_agents(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            original = config.config_path.read_text()
            for changed in (
                original.replace('default_agent = "codex"', 'default_agent = "qoder"'),
                original.replace('allowed_agents = ["codex"]', 'allowed_agents = ["codex", "qoder"]'),
                original.replace('"codex"', '"qoder"'),
            ):
                config.config_path.write_text(changed)
                with self.assertRaisesRegex(ConfigError, 'must be one of: codex'):
                    load_config(config.config_path)

    def test_legacy_state_is_preserved_but_cannot_be_resumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            state = StateStore(config)
            state.authorize('ou-1')
            legacy = state.create_task(project_alias='alpha', agent=AgentKind.CODEX,
                                       chat_id='chat-1', owner_open_id='ou-1', prompt='legacy content',
                                       origin=TaskOrigin.CHAT)
            run = state.claimed_run()
            state.set_task_session(legacy.id, 'legacy-session')
            token = state.register_agent_sync(run.run_id)
            state.save_agent_sync_request(run.run_id, token, '{}')
            state.queue_message(legacy.id, 'ou-1', 'legacy pending content')
            db = state.path
            state.close()
            # Model an old installation; no old rows are rewritten as Codex.
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE tasks SET agent='qoder' WHERE id=?", (legacy.id,))
                connection.execute('DROP VIEW codex_tasks')
            for _ in range(2):
                reopened = StateStore(config)
                try:
                    self.assertIsNone(reopened.get_task(legacy.id))
                    self.assertEqual(reopened.list_tasks(), [])
                    self.assertEqual(reopened.list_tasks('chat-1'), [])
                    self.assertIsNone(reopened.selected_task('chat-1', 'ou-1'))
                    self.assertIsNone(reopened.select_default_chat('chat-1','ou-1','alpha',AgentKind.CODEX))
                    self.assertIsNone(reopened.select_task('chat-1',legacy.id,'ou-1'))
                    self.assertIsNone(reopened.queue_message(legacy.id,'ou-1','never execute'))
                    self.assertIsNone(reopened.stop_task(legacy.id,'ou-1'))
                    self.assertIsNone(reopened.claimed_run())
                    self.assertEqual(reopened.recover_interrupted(), [])
                    self.assertEqual(reopened.pending_agent_sync_requests(), [])
                    self.assertEqual(reopened.unfinished_agent_sync_runs(), [])
                    self.assertIsNone(reopened.agent_sync_context(run.run_id))
                    with sqlite3.connect(db) as connection:
                        self.assertEqual(connection.execute(
                            'SELECT agent,session_id,status FROM tasks WHERE id=?',(legacy.id,)
                        ).fetchone(), ('qoder','legacy-session','stopped'))
                        self.assertEqual(connection.execute(
                            'SELECT content,status FROM task_messages WHERE task_id=? ORDER BY id',(legacy.id,)
                        ).fetchall(), [('legacy content','cancelled'),('legacy pending content','cancelled')])
                        self.assertEqual(connection.execute('SELECT status FROM runs WHERE id=?',(run.run_id,)).fetchone()[0], 'interrupted')
                finally:
                    reopened.close()
            state = StateStore(config)
            try:
                router = MessageRouter(config,state)
                router.handle(inbound('new Codex conversation'))
                new_run = state.claimed_run()
                self.assertEqual(new_run.agent, AgentKind.CODEX)
                self.assertIsNone(new_run.session_id)
                self.assertNotEqual(new_run.task_id, legacy.id)
                with self.assertRaises(ValueError):
                    state.create_task(project_alias='alpha',agent='qoder',chat_id='chat-1',
                                      owner_open_id='ou-1',prompt='rejected')
            finally:
                state.close()

    def test_installer_uses_codex_only_when_other_cli_exists(self):
        # Exercise the real config generator with mocked CLI discovery.
        import subprocess
        root = Path(__file__).resolve().parents[1]
        source = (root / 'install.sh').read_text()
        start = source.index('toml_escape()')
        end = source.index('\n}', source.index('create_project_config()')) + 2
        functions = source[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            script = '''set -eu
INSTALL_DIR=$1
PROJECT_CONFIG=$1/config/projects.toml
mkdir -p "$1/config"
warn() { :; }
info() { :; }
command() { if [ "$1" = -v ]; then [ "$2" = qodercli ]; else builtin command "$@"; fi; }
''' + functions + '\ncreate_project_config\n'
            result = subprocess.run(['bash','-c',script,'test',temporary],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            config = load_config(Path(temporary)/'config/projects.toml')
            for project in config.projects.values():
                self.assertEqual(project.default_agent, AgentKind.CODEX)
                self.assertEqual(project.allowed_agents,frozenset({AgentKind.CODEX}))
