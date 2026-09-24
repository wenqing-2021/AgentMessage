from __future__ import annotations

import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_session_sync as fixtures
from agent_message.core.session_sync import install_sync_scripts
from agent_message.core.models import AgentKind
from agent_message.orchestration.sync_spool import SyncSpoolWatcher


class SyncSpoolTests(unittest.TestCase):
    setUp = fixtures.SessionSyncTests.setUp
    write_records = fixtures.SessionSyncTests.write_records

    def prepare(self):
        self.state.set_task_session(self.target.id, self.sid)
        self.state.queue_message(self.target.id, 'ou-1', 'sync')
        self.run = self.state.claimed_run()
        self.token = self.state.register_agent_sync(self.run.run_id)
        self.project = self.config.projects['alpha'].path
        install_sync_scripts(self.project, self.token)
        self.watcher = SyncSpoolWatcher(self.state, self.home)

    def execute(self, direction, *args):
        result = subprocess.run(['sh', '.agent-message/bin/sync-' + direction, *args],
                                cwd=self.project, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('已提交', result.stdout)

    def finish(self):
        self.watcher.capture_run(self.run.run_id)
        self.state.finish_run(run_id=self.run.run_id, task_id=self.target.id, exit_code=0,
                              session_id=self.sid, final_message='queued', error=None)

    def test_export_waits_for_turn_end_and_notifies_once(self):
        self.prepare()
        self.execute('feishu-to-codex')
        self.watcher.poll_once()
        self.assertEqual(self.codex.read(self.sid).source, 'exec')
        self.assertIsNone(self.state.next_outbox())
        self.finish()
        self.watcher.poll_once()
        self.assertEqual(self.codex.read(self.sid).source, 'cli')
        self.assertIn('已同步', self.state.next_outbox().content)
        self.watcher.poll_once()
        self.assertEqual(self.state._connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    def test_import_title_uses_originating_chat_without_terminal(self):
        self.prepare()
        self.execute('codex-to-feishu', 'title')
        self.finish()
        self.watcher.poll_once()
        self.assertEqual(len(self.state.session_history(self.target.id)), 2)
        self.assertEqual(self.state.next_outbox().chat_id, 'chat-1')

    def test_pending_request_survives_reopen(self):
        self.prepare()
        self.execute('codex-to-feishu', 'title')
        self.finish()
        from agent_message.core.state import StateStore
        other = StateStore(self.config)
        try:
            SyncSpoolWatcher(other, self.home).poll_once()
            self.assertIn('已同步', other.next_outbox().content)
        finally:
            other.close()

    def test_cross_chat_export_rejected(self):
        other = self.state.create_task(project_alias='alpha', agent=AgentKind.CODEX,
                                       chat_id='other-chat', owner_open_id='ou-1', prompt='hello')
        self.state.stop_task(other.id, 'ou-1')
        self.prepare()
        self.execute('feishu-to-codex', other.id)
        self.finish()
        self.watcher.poll_once()
        self.assertIn('只能同步', self.state.next_outbox().content)
        self.assertEqual(self.codex.read(self.sid).source, 'exec')

    def test_import_uuid_cannot_cross_project(self):
        self.prepare()
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE threads SET cwd=?', (str(self.root / 'other-project'),))
        self.execute('codex-to-feishu', self.sid)
        self.finish()
        self.watcher.poll_once()
        self.assertIn('当前项目', self.state.next_outbox().content)

    def test_archive_is_skipped(self):
        self.prepare()
        self.execute('feishu-to-codex')
        self.finish()
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE threads SET archived=1')
        self.watcher.poll_once()
        self.assertIn('跳过', self.state.next_outbox().content)

    def test_symlink_request_and_script_rejected(self):
        self.prepare()
        directory = self.project / '.agent-message/sync' / self.token
        outside = self.root / 'outside'
        outside.write_text('feishu-to-codex\ncurrent\n')
        (directory / 'sync-malicious.req').symlink_to(outside)
        self.finish()
        self.watcher.poll_once()
        self.assertIn('未完成', self.state.next_outbox().content)
        script = self.project / '.agent-message/bin/sync-feishu-to-codex'
        script.unlink()
        script.symlink_to(outside)
        with self.assertRaises(ValueError):
            install_sync_scripts(self.project, self.token)
        self.assertEqual(outside.read_text(), 'feishu-to-codex\ncurrent\n')

    def test_duplicate_title_returns_candidates_without_input(self):
        self.prepare()
        self.execute('codex-to-feishu', 'title')
        self.finish()
        session = self.codex.read(self.sid)
        with patch('agent_message.agents.codex_sessions.CodexSessionStore.find_by_title', return_value=[session, session]):
            self.watcher.poll_once()
        self.assertIn('匹配多个', self.state.next_outbox().content)
        self.assertIn(self.sid, self.state.next_outbox().content)
        self.assertEqual(self.state.session_history(self.target.id), [])

    def test_later_agent_cannot_modify_or_add_to_finished_run_requests(self):
        self.prepare()
        self.execute('feishu-to-codex')
        self.finish()
        directory = self.project / '.agent-message/sync' / self.token
        for request in directory.glob('*.req'):
            request.write_text('codex-to-feishu\nunrelated\n')
        (directory / 'sync-late.req').write_text('codex-to-feishu\nunrelated\n')
        self.watcher.poll_once()
        self.assertIn('已同步', self.state.next_outbox().content)
        self.assertEqual(self.codex.read(self.sid).source, 'cli')
        self.assertEqual(self.state._connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

    def test_sync_context_migration_and_recovery_are_repeatable(self):
        self.prepare()
        self.execute('feishu-to-codex')
        from agent_message.core.state import StateStore
        restored = StateStore(self.config)
        try:
            watcher = SyncSpoolWatcher(restored, self.home)
            for run_id in restored.unfinished_agent_sync_runs():
                watcher.capture_run(run_id)
                watcher.capture_run(run_id)
            restored.recover_interrupted()
            watcher.poll_once()
            self.assertIn('已同步', restored.next_outbox().content)
            self.assertEqual(restored._connection.execute('SELECT COUNT(*) FROM agent_sync_requests').fetchone()[0], 1)
        finally:
            restored.close()

    def test_chat_list_filters_scope_archive_and_background_sources(self):
        from uuid import uuid4
        project = self.config.projects['alpha'].path
        with sqlite3.connect(self.db) as c:
            c.execute('ALTER TABLE threads ADD COLUMN created_at INTEGER DEFAULT 0')
            c.execute('ALTER TABLE threads ADD COLUMN name TEXT')
            c.execute("UPDATE threads SET source='vscode', title='old title', name='Visible title', created_at=10")
            for title, source, archived, cwd, created in [
                ('newest', 'cli', 0, project, 20),
                ('archived', 'cli', 1, project, 30),
                ('background', 'exec', 0, project, 40),
                ('other project', 'vscode', 0, self.root / 'elsewhere', 50),
                ('subagent', 'subAgent', 0, project, 60),
            ]:
                c.execute('INSERT INTO threads(id,cwd,title,source,archived,rollout_path,created_at) VALUES(?,?,?,?,?,?,?)',
                          (str(uuid4()), str(cwd), title, source, archived, str(self.path), created))
        self.assertEqual(self.codex.list_chat_titles(project), ['newest', 'Visible title'])
        self.assertEqual(self.codex.list_chat_titles(project, 1), ['newest'])
        for limit in (0, 51):
            with self.assertRaises(ValueError):
                self.codex.list_chat_titles(project, limit)

    def test_chat_list_script_returns_immediately_and_validates_count(self):
        from agent_message.core.session_sync import install_chat_list
        self.prepare()
        install_chat_list(self.project, self.token, ['Title ' + str(i) for i in range(8)])
        command = ['sh', '.agent-message/bin/list-codex-chats']
        result = subprocess.run(command, cwd=self.project, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.splitlines()), 5)
        self.assertIn('Title 0', result.stdout)
        for limit in ('0', '51', 'bad', '1;whoami'):
            result = subprocess.run(command + [limit], cwd=self.project, capture_output=True, text=True, timeout=3)
            self.assertNotEqual(result.returncode, 0)
        install_chat_list(self.project, self.token, [], 'database unavailable')
        result = subprocess.run(command, cwd=self.project, capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('database unavailable', result.stdout)
        install_chat_list(self.project, self.token, [])
        result = subprocess.run(command, cwd=self.project, capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertIn('没有未归档', result.stdout)

    def test_scheduler_provides_real_titles_before_agent_starts(self):
        import asyncio
        from agent_message.orchestration.scheduler import Scheduler
        from agent_message.core.models import AgentResult
        self.prepare()
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE threads SET source='vscode'")
        outputs = []
        sid = self.sid

        class Adapter:
            async def execute(self, run, *callbacks):
                result = subprocess.run(['sh', '.agent-message/bin/list-codex-chats', '5'],
                                        cwd=run.project_path, capture_output=True, text=True, timeout=3)
                outputs.append(result)
                return AgentResult(0, sid, 'done')

        with patch.dict('os.environ', {'CODEX_HOME': str(self.home)}), patch(
            'agent_message.orchestration.scheduler.adapter_for', return_value=Adapter()
        ):
            asyncio.run(Scheduler(self.config, self.state, lambda *_: None)._run_claim(self.run))
        self.assertEqual(outputs[0].returncode, 0)
        self.assertEqual(outputs[0].stdout.strip(), '1. title')
