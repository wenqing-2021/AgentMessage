from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_message.core.bot_registry import register_bot, unregister_bot
from agent_message.core.config import SHARED_PROJECT_ALIAS, ConfigError, load_config, tomllib
from agent_message.core.models import AgentKind
from agent_message.core.state import StateStore
from agent_message.orchestration.router import MessageRouter
from tests.helpers import inbound


class BotConfigTests(unittest.TestCase):
    def fixture(self, root: Path) -> Path:
        (root / 'config').mkdir()
        for alias in (SHARED_PROJECT_ALIAS, 'beta', 'gamma'):
            (root / alias).mkdir()
        path = root / 'config/projects.toml'
        path.write_text('[service]\ndefault_chat_project = "agent_message"\n' + ''.join(
            f'\n[projects.{alias}]\npath = "{root / alias}"\n'
            for alias in (SHARED_PROJECT_ALIAS, 'beta', 'gamma')
        ))
        return path

    def register(self, path: Path) -> str:
        return register_bot(path, 'cli_primary', 'cli_second', [SHARED_PROJECT_ALIAS])

    def test_shared_registry_preserves_primary_projects_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            legacy = load_config(path)
            default = self.register(path)
            primary = load_config(path)
            secondary = load_config(path, app_id='cli_second')
            self.assertEqual(default, SHARED_PROJECT_ALIAS)
            self.assertEqual(set(primary.projects), {SHARED_PROJECT_ALIAS, 'beta', 'gamma'})
            self.assertEqual(set(secondary.projects), {SHARED_PROJECT_ALIAS})
            self.assertEqual(primary.service.state_dir, legacy.service.state_dir)
            self.assertEqual(secondary.service.state_dir, legacy.service.state_dir / 'bots/cli_second')
            self.assertEqual(secondary.config_path, primary.config_path)
            self.assertEqual(secondary.service.default_chat_project, SHARED_PROJECT_ALIAS)
            self.assertEqual(primary.service.default_chat_project, SHARED_PROJECT_ALIAS)
            raw = tomllib.loads(path.read_text())
            self.assertEqual(raw['service']['default_chat_project'], SHARED_PROJECT_ALIAS)
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
                task = primary.create_task(project_alias='beta', agent=AgentKind.CODEX,
                    chat_id='chat_test', owner_open_id='ou_test', prompt='hello')
                self.assertIsNone(second.get_task(task.id))
                primary.enqueue_outbox('chat_test', 'private reply')
                self.assertIsNone(second.next_outbox())
                second.authorize('ou_test')
                router = MessageRouter(second.config, second)
                message = replace(inbound('/new beta forbidden'), sender_open_id='ou_test')
                replies = router.handle(message)
                self.assertIn('未知项目', '\n'.join(replies))
                self.assertEqual(second.list_tasks(), [])
            finally:
                primary.close()
                second.close()

    def test_shared_project_tasks_disappear_from_the_primary_bot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            state = StateStore(load_config(path))
            task = state.create_task(project_alias=SHARED_PROJECT_ALIAS, agent=AgentKind.CODEX,
                chat_id='chat_test', owner_open_id='ou_test', prompt='old queued work')
            state.close()
            self.register(path)
            path.write_text(path.read_text().replace(
                'projects = ["agent_message", "beta", "gamma"]', 'projects = ["beta", "gamma"]'
            ).replace('default_chat_project = "agent_message"', 'default_chat_project = "beta"', 2))
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

    def test_registration_is_atomic_and_rejects_invalid_or_forbidden_bots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            for app_id, aliases in (
                ('../bad', [SHARED_PROJECT_ALIAS]),
                ('cli_new', ['missing']),
                ('cli_new', ['beta']),
                ('cli_new', [SHARED_PROJECT_ALIAS, 'beta']),
            ):
                before = path.read_bytes()
                with self.assertRaises(ConfigError):
                    register_bot(path, 'cli_primary', app_id, aliases)
                self.assertEqual(path.read_bytes(), before)
            self.register(path)
            before = path.read_bytes()
            with self.assertRaises(ConfigError):
                self.register(path)
            self.assertEqual(path.read_bytes(), before)
            register_bot(path, 'cli_primary', 'cli_third', [SHARED_PROJECT_ALIAS])
            self.assertEqual(set(load_config(path, app_id='cli_third').projects), {SHARED_PROJECT_ALIAS})

    def test_config_rejects_duplicate_private_assignments_but_allows_shared_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            base = path.read_text().replace('[service]\n', '[service]\nfeishu_app_id = "cli_primary"\n')
            path.write_text(base + '\n[bots.cli_primary]\nprojects = ["beta", "gamma"]\n'
                                 'default_chat_project = "beta"\n'
                                 '\n[bots.cli_second]\nprojects = ["beta"]\ndefault_chat_project = "beta"\n')
            with self.assertRaisesRegex(ConfigError, 'more than one bot'):
                load_config(path)
            path.write_text(base + '\n[bots.cli_primary]\nprojects = ["agent_message", "beta"]\n'
                                 'default_chat_project = "agent_message"\n'
                                 '\n[bots.cli_second]\nprojects = ["agent_message"]\ndefault_chat_project = "agent_message"\n')
            self.assertEqual(set(load_config(path).projects), {SHARED_PROJECT_ALIAS, 'beta'})
            path.write_text(base + '\n[bots.cli_primary]\nprojects = ["beta", "gamma"]\n'
                                 'default_chat_project = "beta"\n'
                                 '\n[bots.cli_second]\nprojects = ["agent_message"]\ndefault_chat_project = "agent_message"\n')
            self.assertEqual(set(load_config(path).projects), {'beta', 'gamma'})
            self.assertEqual(set(load_config(path, app_id='cli_second').projects), {SHARED_PROJECT_ALIAS})
            path.write_text(base + '\n[bots.cli_primary]\nprojects = ["agent_message", "gamma"]\n'
                                 'default_chat_project = "agent_message"\n'
                                 '\n[bots.cli_second]\nprojects = ["agent_message", "beta"]\n'
                                 'default_chat_project = "agent_message"\n')
            self.assertEqual(set(load_config(path, app_id='cli_second').projects), {SHARED_PROJECT_ALIAS, 'beta'})

    def test_third_bot_migrates_legacy_transfer_without_changing_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            self.register(path)
            path.write_text(path.read_text().replace(
                'projects = ["agent_message", "beta", "gamma"]', 'projects = ["beta", "gamma"]'
            ).replace('default_chat_project = "agent_message"', 'default_chat_project = "beta"', 2))
            register_bot(path, 'cli_primary', 'cli_third', [SHARED_PROJECT_ALIAS])
            primary = load_config(path)
            self.assertEqual(primary.service.default_chat_project, 'beta')
            self.assertEqual(set(primary.projects), {SHARED_PROJECT_ALIAS, 'beta', 'gamma'})
            unregister_bot(path, 'cli_primary', 'cli_second')
            self.assertEqual(set(load_config(path, app_id='cli_third').projects), {SHARED_PROJECT_ALIAS})
            self.assertIn(SHARED_PROJECT_ALIAS, load_config(path).projects)

    def test_install_with_only_shared_project_supports_multiple_bots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.fixture(root)
            path.write_text(path.read_text().split('[projects.beta]')[0])
            self.register(path)
            register_bot(path, 'cli_primary', 'cli_third', [SHARED_PROJECT_ALIAS])
            for bot in ('cli_primary', 'cli_second', 'cli_third'):
                config = load_config(path, app_id=bot)
                self.assertEqual(set(config.projects), {SHARED_PROJECT_ALIAS})
                self.assertEqual(config.service.default_chat_project, SHARED_PROJECT_ALIAS)

    def test_shared_runs_are_serial_across_processes_but_private_projects_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            self.register(path)
            primary = StateStore(load_config(path))
            second = StateStore(load_config(path, app_id='cli_second'))
            try:
                first_task = second.create_task(project_alias=SHARED_PROJECT_ALIAS, agent=AgentKind.CODEX,
                    chat_id='second_chat', owner_open_id='ou_test', prompt='shared')
                first = second.claimed_run()
                self.assertIsNotNone(first)
                primary.create_task(project_alias=SHARED_PROJECT_ALIAS, agent=AgentKind.CODEX,
                    chat_id='primary_chat', owner_open_id='ou_test', prompt='wait')
                probe = subprocess.run([sys.executable, '-c',
                    'import sys; from agent_message.core.config import load_config; '
                    'from agent_message.core.state import StateStore; '
                    's=StateStore(load_config(sys.argv[1])); '
                    'assert s.claimed_run() is None; s.close()', str(path)],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(probe.returncode, 0, probe.stderr)
                private_task = primary.create_task(project_alias='beta', agent=AgentKind.CODEX,
                    chat_id='primary_chat', owner_open_id='ou_test', prompt='private')
                private = primary.claimed_run()
                self.assertEqual(private.task_id, private_task.id)
                self.assertIsNone(primary.claimed_run())
                second.finish_run(run_id=first.run_id, task_id=first_task.id,
                    exit_code=0, session_id=None, final_message='done', error=None)
                self.assertEqual(primary.claimed_run().project_alias, SHARED_PROJECT_ALIAS)
            finally:
                primary.close()
                second.close()

    def test_close_releases_shared_project_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            self.register(path)
            primary = StateStore(load_config(path))
            second = StateStore(load_config(path, app_id='cli_second'))
            try:
                for state in (primary, second):
                    state.create_task(project_alias=SHARED_PROJECT_ALIAS, agent=AgentKind.CODEX,
                        chat_id='chat_test', owner_open_id='ou_test', prompt='work')
                self.assertIsNotNone(second.claimed_run())
                self.assertIsNone(primary.claimed_run())
            finally:
                second.close()
            try:
                self.assertIsNotNone(primary.claimed_run())
            finally:
                primary.close()

    def test_file_requests_are_only_consumed_by_the_originating_bot(self) -> None:
        import asyncio
        from agent_message.core.transfer import ensure_send_script, pending_requests, spool_dir, result_path
        from agent_message.orchestration.spool import SpoolWatcher
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            self.register(path)
            register_bot(path, 'cli_primary', 'cli_third', [SHARED_PROJECT_ALIAS])
            second = StateStore(load_config(path, app_id='cli_second'))
            third = StateStore(load_config(path, app_id='cli_third'))
            project = second.config.projects[SHARED_PROJECT_ALIAS].path
            (project / 'result.txt').write_text('result')
            delivered = []
            async def send_media(chat, kind, file):
                delivered.append(chat)
            try:
                for state, chat in ((second, 'second_chat'), (third, 'third_chat')):
                    task = state.create_task(project_alias=SHARED_PROJECT_ALIAS, agent=AgentKind.CODEX,
                        chat_id=chat, owner_open_id='ou_test', prompt='work')
                    run = state.claimed_run()
                    state.finish_run(run_id=run.run_id, task_id=task.id, exit_code=0,
                        session_id=None, final_message='done', error=None)
                script = ensure_send_script(project, 'cli_second')
                completed = subprocess.run(['sh', str(script), 'result.txt', '0'], cwd=project,
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                requests = pending_requests(project, 'cli_second')
                self.assertEqual(len(requests), 1)
                # Refreshing the shared script cannot redirect an already queued request.
                ensure_send_script(project, 'cli_third')
                legacy = spool_dir(project) / 'legacy.req'
                legacy.write_text('result.txt')
                asyncio.run(SpoolWatcher(third.config, third, send_media).poll_once())
                self.assertEqual(delivered, [])
                self.assertTrue(requests[0].exists())
                asyncio.run(SpoolWatcher(second.config, second, send_media).poll_once())
                self.assertEqual(delivered, ['second_chat'])
                self.assertTrue(result_path(requests[0]).read_text().startswith('ok'))
                self.assertTrue(legacy.exists())
                with self.assertRaises(ValueError):
                    spool_dir(project, '../bad')
            finally:
                second.close()
                third.close()

    def test_runtime_mcp_uses_selected_bot_state_without_credentials(self) -> None:
        from agent_message.agents.codex import sandbox_mcp_config_args
        from agent_message.runtimes.sandbox.mcp import SandboxMcpServer
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            path.write_text(path.read_text().replace(
                f'[projects.{SHARED_PROJECT_ALIAS}]\n',
                f'[projects.{SHARED_PROJECT_ALIAS}]\nsandbox_enabled = true\n',
            ))
            self.register(path)
            config = load_config(path, app_id='cli_second')
            state = StateStore(config)
            try:
                task = state.create_task(project_alias=SHARED_PROJECT_ALIAS, agent=AgentKind.CODEX,
                    chat_id='chat_test', owner_open_id='ou_test', prompt='work')
                arguments = sandbox_mcp_config_args(
                    config, task_id=task.id, project_alias=SHARED_PROJECT_ALIAS, run_id=None)
                self.assertIn('"--app-id","cli_second"', ' '.join(arguments))
                with patch.dict(os.environ, {}, clear=True):
                    server = SandboxMcpServer(str(path), task.id, None, 'cli_second')
                    try:
                        self.assertEqual(server.project.alias, SHARED_PROJECT_ALIAS)
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
            private = root / 'beta/private.txt'
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

    def test_unregister_bot_preserves_primary_shared_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = self.fixture(Path(temp))
            self.register(path)
            returned = unregister_bot(path, 'cli_primary', 'cli_second')
            self.assertEqual(returned, [])
            primary = load_config(path)
            self.assertEqual(set(primary.projects), {SHARED_PROJECT_ALIAS, 'beta', 'gamma'})
            self.assertEqual(primary.service.default_chat_project, SHARED_PROJECT_ALIAS)
            with self.assertRaises(ConfigError):
                load_config(path, app_id='cli_second')
            before = path.read_bytes()
            with self.assertRaises(ConfigError):
                unregister_bot(path, 'cli_primary', 'cli_second')
            with self.assertRaises(ConfigError):
                unregister_bot(path, 'cli_primary', 'cli_primary')
            self.assertEqual(path.read_bytes(), before)

    def test_unknown_bot_and_cross_bot_overlapping_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.fixture(root)
            self.register(path)
            with self.assertRaises(ConfigError):
                load_config(path, app_id='cli_missing')
            source = path.read_text()
            for value in (str(root / SHARED_PROJECT_ALIAS), str(root)):
                path.write_text(source.replace(str(root / 'gamma'), value))
                with self.assertRaisesRegex(ConfigError, 'overlap'):
                    load_config(path)
