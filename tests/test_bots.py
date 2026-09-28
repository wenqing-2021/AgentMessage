from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_message.core.bot_registry import register_bot
from agent_message.core.config import ConfigError, load_config
from agent_message.core.models import AgentKind
from agent_message.core.state import StateStore
from agent_message.orchestration.router import MessageRouter
from tests.helpers import inbound


class BotConfigTests(unittest.TestCase):
    def fixture(self, root: Path) -> Path:
        (root / 'config').mkdir()
        for alias in ('alpha', 'beta', 'gamma'):
            (root / alias).mkdir()
        path = root / 'config/projects.toml'
        path.write_text('[service]\ndefault_chat_project = "alpha"\n' + ''.join(
            f'\n[projects.{alias}]\npath = "{root / alias}"\n' for alias in ('alpha', 'beta', 'gamma')
        ))
        return path

    def register(self, path: Path) -> None:
        register_bot(path, 'cli_primary', 'cli_second', ['beta'])

    def test_shared_registry_filters_projects_and_preserves_primary_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            legacy = load_config(path)
            self.register(path)
            primary = load_config(path)
            secondary = load_config(path, app_id='cli_second')
            self.assertEqual(set(primary.projects), {'alpha', 'gamma'})
            self.assertEqual(set(secondary.projects), {'beta'})
            self.assertEqual(primary.service.state_dir, legacy.service.state_dir)
            self.assertEqual(secondary.service.state_dir, legacy.service.state_dir / 'bots/cli_second')
            self.assertEqual(secondary.config_path, primary.config_path)
            self.assertEqual(secondary.service.default_chat_project, 'beta')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_wrong_credentials_cannot_authorize_or_connect_another_bot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            self.register(path)
            with patch.dict(os.environ, {'AGENT_MESSAGE_FEISHU_APP_ID': 'cli_primary',
                    'AGENT_MESSAGE_FEISHU_APP_SECRET': 'secret_test', 'AGENT_MESSAGE_ALLOWED_OPEN_IDS': 'ou_test'}):
                primary = load_config(path)
                second = load_config(path, app_id='cli_second')
                self.assertEqual(primary.app_secret, 'secret_test')
                self.assertIsNone(second.app_secret)
                self.assertEqual(second.configured_open_ids, frozenset())

    def test_new_bot_has_separate_authorizations_tasks_and_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            self.register(path)
            primary = StateStore(load_config(path))
            second = StateStore(load_config(path, app_id='cli_second'))
            try:
                primary.authorize('ou_test')
                self.assertFalse(second.is_authorized('ou_test'))
                task = primary.create_task(project_alias='alpha', agent=AgentKind.CODEX,
                    chat_id='chat_test', owner_open_id='ou_test', prompt='hello')
                self.assertIsNone(second.get_task(task.id))
                primary.enqueue_outbox('chat_test', 'private reply')
                self.assertIsNone(second.next_outbox())
                second.authorize('ou_test')
                router = MessageRouter(second.config, second)
                message = replace(inbound('/new alpha forbidden'), sender_open_id='ou_test')
                replies = router.handle(message)
                self.assertIn('未知项目', '\n'.join(replies))
                self.assertEqual(second.list_tasks(), [])
            finally:
                primary.close()
                second.close()

    def test_removed_project_old_tasks_are_not_visible_or_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            state = StateStore(load_config(path))
            task = state.create_task(project_alias='beta', agent=AgentKind.CODEX,
                chat_id='chat_test', owner_open_id='ou_test', prompt='old queued work')
            state.close()
            self.register(path)
            state = StateStore(load_config(path))
            try:
                self.assertIsNone(state.get_task(task.id))
                self.assertEqual(state.list_tasks(), [])
                self.assertIsNone(state.selected_task('chat_test', 'ou_test'))
                self.assertIsNone(state.select_task('chat_test', task.id, 'ou_test'))
                self.assertIsNone(state.queue_message(task.id, 'ou_test', 'resume'))
                self.assertIsNone(state.claimed_run())
                self.assertEqual(state._connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            finally:
                state.close()

    def test_registration_is_atomic_and_rejects_invalid_or_duplicate_bots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            for app_id, aliases in (('../bad', ['beta']), ('cli_new', ['missing']), ('cli_new', ['alpha'])):
                before = path.read_bytes()
                with self.assertRaises(ConfigError):
                    register_bot(path, 'cli_primary', app_id, aliases)
                self.assertEqual(path.read_bytes(), before)
            self.register(path)
            before = path.read_bytes()
            with self.assertRaises(ConfigError):
                self.register(path)
            self.assertEqual(path.read_bytes(), before)
            register_bot(path, 'cli_primary', 'cli_third', ['gamma'])
            self.assertEqual(set(load_config(path).projects), {'alpha'})

    def test_runtime_mcp_uses_selected_bot_state_without_credentials(self) -> None:
        from agent_message.agents.codex import sandbox_mcp_config_args
        from agent_message.runtimes.sandbox.mcp import SandboxMcpServer
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            path.write_text(path.read_text().replace('[projects.beta]\n', '[projects.beta]\nsandbox_enabled = true\n'))
            self.register(path)
            config = load_config(path, app_id='cli_second')
            state = StateStore(config)
            try:
                task = state.create_task(project_alias='beta', agent=AgentKind.CODEX,
                    chat_id='chat_test', owner_open_id='ou_test', prompt='work')
                arguments = sandbox_mcp_config_args(config, task_id=task.id, project_alias='beta', run_id=None)
                self.assertIn('"--app-id","cli_second"', ' '.join(arguments))
                with patch.dict(os.environ, {}, clear=True):
                    server = SandboxMcpServer(str(path), task.id, None, 'cli_second')
                    try:
                        self.assertEqual(server.project.alias, 'beta')
                        self.assertEqual(server.state.path, state.path)
                    finally:
                        server.state.close()
            finally:
                state.close()

    def test_outbox_rechecks_project_access_before_upload(self) -> None:
        import asyncio
        from unittest.mock import Mock
        from agent_message.orchestration.service import BridgeService
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.fixture(root)
            self.register(path)
            private = root / 'alpha/private.txt'
            private.write_text('private')
            sender = Mock()
            service = BridgeService(load_config(path, app_id='cli_second'), Mock(), send_media=sender)
            try:
                service.state.enqueue_outbox('chat_test', 'file', kind='file', file_path=str(private))
                self.assertFalse(asyncio.run(service._deliver(service.state.next_outbox())))
                sender.assert_not_called()
                self.assertIsNone(service.state.next_outbox())
            finally:
                service.state.close()

    def test_unknown_bot_and_cross_bot_overlapping_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            self.register(path)
            with self.assertRaises(ConfigError):
                load_config(path, app_id='cli_missing')
            source = path.read_text()
            for value in (str(path.parent.parent / 'alpha'), str(path.parent.parent)):
                path.write_text(source.replace(str(path.parent.parent / 'beta'), value))
                with self.assertRaisesRegex(ConfigError, 'overlap'):
                    load_config(path)
