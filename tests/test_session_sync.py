from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from agent_message.agents.codex_sessions import CodexSessionStore, SessionSyncError
from agent_message.core.models import AgentKind
from agent_message.core.state import StateStore
from agent_message.orchestration.session_sync import sync_codex_to_feishu, sync_feishu_to_codex
from agent_message.orchestration.router import MessageRouter
from agent_message.orchestration.commands import parse_command, CommandError
from helpers import make_config, inbound


class SessionSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = make_config(self.root)
        self.state = StateStore(self.config)
        self.addCleanup(self.state.close)
        self.state.authorize('ou-1')
        self.target = self.state.create_task(project_alias='alpha', agent=AgentKind.CODEX,
                                            chat_id='chat-1', owner_open_id='ou-1', prompt='hello')
        self.state.stop_task(self.target.id, 'ou-1')
        self.home = self.root / 'codex'
        (self.home / 'sessions').mkdir(parents=True)
        self.db = self.home / 'state_5.sqlite'
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, cwd TEXT, title TEXT, source TEXT, archived INTEGER, rollout_path TEXT)')
        self.sid = str(uuid4())
        self.path = self.home / 'sessions' / 'rollout.jsonl'
        self.records = [
            {'type': 'session_meta', 'payload': {'id': self.sid, 'source': 'exec'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started'}},
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'hello'}},
            {'type': 'response_item', 'payload': {'type': 'reasoning', 'text': 'private'}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'world'}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete'}},
        ]
        self.write_records()
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',
                      (self.sid, str(self.config.projects['alpha'].path), 'title', 'exec', 0, str(self.path)))
        self.codex = CodexSessionStore(self.home)

    def write_records(self):
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in self.records))

    def import_session(self):
        return sync_codex_to_feishu(self.state, self.codex, self.sid)

    def test_import_is_idempotent_and_never_queues_or_sends_history(self):
        self.import_session()
        self.import_session()
        imported = [t for t in self.state.list_tasks() if t.session_id == self.sid]
        self.assertEqual(len(imported), 1)
        task = imported[0]
        self.assertEqual(self.state.session_history(task.id), ['user: hello', 'assistant: world'])
        self.assertIsNone(self.state.claimed_run())
        self.assertEqual(self.state._connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
        self.state.select_task('chat-1', task.id, 'ou-1')
        self.state.queue_message(task.id, 'ou-1', 'continue')
        run = self.state.claimed_run()
        self.assertEqual(run.session_id, self.sid)
        self.assertEqual(run.prompt, 'continue')

    def test_export_preserves_uuid_history_and_backup(self):
        self.state.set_task_session(self.target.id, self.sid)
        original = self.path.read_bytes()
        sync_feishu_to_codex(self.state, self.codex, self.target.id)
        sync_feishu_to_codex(self.state, self.codex, self.target.id)
        self.assertEqual(self.codex.read(self.sid).source, 'cli')
        self.assertEqual(json.loads(self.path.read_text().splitlines()[0])['payload']['id'], self.sid)
        self.assertEqual(self.path.read_bytes().partition(b'\n')[2], original.partition(b'\n')[2])
        backup = self.home / 'agent-message-sync-backups' / (self.sid + '.jsonl')
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_archive_skipped_both_directions_without_reading_rollout(self):
        self.state.set_task_session(self.target.id, self.sid)
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE threads SET archived=1')
        self.path.unlink()
        self.assertIn('跳过', self.import_session())
        self.assertIn('跳过', sync_feishu_to_codex(self.state, self.codex, self.target.id))
        self.assertEqual(len(self.state.list_tasks()), 1)

    def test_archive_path_overrides_stale_index(self):
        archived = self.home / 'archived_sessions' / 'x.jsonl'
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE threads SET rollout_path=?', (str(archived),))
        self.assertIn('跳过', self.import_session())

    def test_active_session_and_invalid_path_rejected(self):
        self.records.pop()
        self.write_records()
        with self.assertRaisesRegex(SessionSyncError, '未结束'):
            self.import_session()
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE threads SET rollout_path=?', (str(self.root / 'outside'),))
        with self.assertRaisesRegex(SessionSyncError, '路径'):
            self.import_session()

    def test_destination_ambiguity_and_unauthorized_target(self):
        other = self.state.create_task(project_alias='alpha', agent=AgentKind.CODEX,
                                       chat_id='chat-2', owner_open_id='ou-1', prompt='hello')
        with self.assertRaisesRegex(SessionSyncError, '编号'):
            self.import_session()
        sync_codex_to_feishu(self.state, self.codex, self.sid, self.target.id)
        with self.assertRaisesRegex(ValueError, '其他飞书'):
            sync_codex_to_feishu(self.state, self.codex, self.sid, other.id)
        self.state._connection.execute('DELETE FROM authorized_users')
        self.state._connection.commit()
        with self.assertRaisesRegex(ValueError, '授权'):
            sync_codex_to_feishu(self.state, self.codex, self.sid, self.target.id)

    def test_unregistered_project_rejected(self):
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE threads SET cwd=?', (str(self.root / 'other'),))
        with self.assertRaisesRegex(SessionSyncError, '白名单'):
            self.import_session()

    def test_modern_events_exclude_reasoning_tools_and_duplicate_response_items(self):
        self.records = [self.records[0],
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item':
                {'type': 'UserMessage', 'content': [{'type': 'text', 'text': 'question'}]}}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item':
                {'type': 'AgentMessage', 'content': [{'type': 'Text', 'text': 'answer'}]}}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'text': 'answer'}]}},
        ]
        self.write_records()
        self.assertEqual(self.codex.transcript(self.codex.read(self.sid)), [('user', 'question'), ('assistant', 'answer')])

    def test_commands_and_ownership(self):
        self.import_session()
        task = next(t for t in self.state.list_tasks() if t.session_id == self.sid)
        router = MessageRouter(self.config, self.state)
        with patch('agent_message.orchestration.router.CodexSessionStore') as store:
            store.return_value.list_chat_titles.return_value = ['title']
            self.assertEqual(router.handle(inbound('/list')), ['1. title'])
        self.assertIn('world', '\n'.join(router.handle(inbound('/history ' + task.id, 'e2', 'm2'))))
        self.state._connection.execute('UPDATE tasks SET owner_open_id=? WHERE id=?', ('another', task.id))
        self.state._connection.commit()
        self.assertNotIn('world', '\n'.join(router.handle(inbound('/history ' + task.id, 'e3', 'm3'))))
        with self.assertRaises(CommandError):
            parse_command('/list extra')
        with self.assertRaises(CommandError):
            parse_command('/history')

    def test_export_rejects_queued_and_running_bridge_tasks(self):
        self.state.set_task_session(self.target.id, self.sid)
        original = self.path.read_bytes()
        self.state.queue_message(self.target.id, 'ou-1', 'next')
        with self.assertRaisesRegex(ValueError, '排队'):
            sync_feishu_to_codex(self.state, self.codex, self.target.id)
        self.state.claimed_run()
        with self.assertRaisesRegex(ValueError, '运行'):
            sync_feishu_to_codex(self.state, self.codex, self.target.id)
        self.assertEqual(original, self.path.read_bytes())

    def test_reimport_rejects_queued_task_and_preserves_snapshot(self):
        self.import_session()
        task = next(t for t in self.state.list_tasks() if t.session_id == self.sid)
        self.state.queue_message(task.id, 'ou-1', 'next')
        with self.assertRaisesRegex(ValueError, '排队'):
            self.import_session()
        self.assertEqual(len(self.state.session_history(task.id)), 2)

    def test_malformed_history_does_not_create_a_task(self):
        for invalid in (b'{broken', b'[]\n', b'{"type":"session_meta","payload":null}\n'):
            self.path.write_bytes(invalid)
            with self.assertRaises(SessionSyncError):
                self.import_session()
            self.assertEqual(len(self.state.list_tasks()), 1)

    def test_export_retries_index_update_after_interruption(self):
        self.state.set_task_session(self.target.id, self.sid)
        original = self.path.read_bytes()
        # Model a crash between the rollout replacement and index commit.
        self.records[0]['payload']['source'] = 'cli'
        self.write_records()
        sync_feishu_to_codex(self.state, self.codex, self.target.id)
        self.assertEqual(self.codex.read(self.sid).source, 'cli')
        self.assertEqual(original.partition(b'\n')[2], self.path.read_bytes().partition(b'\n')[2])

    def test_atomic_replacement_failure_preserves_original_and_index(self):
        self.state.set_task_session(self.target.id, self.sid)
        original = self.path.read_bytes()
        with patch('agent_message.agents.codex_sessions.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                sync_feishu_to_codex(self.state, self.codex, self.target.id)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.codex.read(self.sid).source, 'exec')
        self.assertEqual(list(self.path.parent.glob('.agent-message-*')), [])

    def test_unknown_schema_fails_closed(self):
        with sqlite3.connect(self.db) as c:
            c.execute('ALTER TABLE threads RENAME COLUMN archived TO incompatible')
        with self.assertRaisesRegex(SessionSyncError, 'schema'):
            self.import_session()
        self.assertEqual(len(self.state.list_tasks()), 1)

    def test_import_by_title_and_partial_title_is_idempotent(self):
        sync_codex_to_feishu(self.state, self.codex, 'title')
        sync_codex_to_feishu(self.state, self.codex, 'itl')
        self.assertEqual(len(self.state.list_tasks()), 2)

    def test_visible_name_overrides_title_and_archive_is_excluded(self):
        with sqlite3.connect(self.db) as c:
            c.execute('ALTER TABLE threads ADD COLUMN name TEXT')
            c.execute("UPDATE threads SET name='Renamed chat'")
        sync_codex_to_feishu(self.state, self.codex, 'Renamed chat')
        with self.assertRaisesRegex(SessionSyncError, '找不到'):
            sync_codex_to_feishu(self.state, self.codex, 'title')
        with sqlite3.connect(self.db) as c:
            c.execute('UPDATE threads SET archived=1')
        with self.assertRaisesRegex(SessionSyncError, '未归档'):
            sync_codex_to_feishu(self.state, self.codex, 'Renamed chat')

    def test_duplicate_titles_prompt_and_exact_title_takes_priority(self):
        other_id = str(uuid4())
        other_path = self.home / 'sessions' / 'other.jsonl'
        records = [dict(r) for r in self.records]
        records[0] = {'type': 'session_meta', 'payload': {'id': other_id, 'source': 'cli'}}
        other_path.write_text(''.join(json.dumps(r) + '\n' for r in records))
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',
                      (other_id, str(self.config.projects['alpha'].path), 'title extended', 'cli', 0, str(other_path)))
        choose = unittest.mock.Mock(return_value=0)
        sync_codex_to_feishu(self.state, self.codex, 'title', choose=choose)
        choose.assert_not_called()
        sync_codex_to_feishu(self.state, self.codex, 'tit', choose=choose)
        self.assertEqual(len(choose.call_args.args[1]), 2)
        with self.assertRaisesRegex(SessionSyncError, '编号无效'):
            sync_codex_to_feishu(self.state, self.codex, 'tit', choose=lambda *_: -1)

    def test_multiple_destinations_can_be_selected_without_extra_arguments(self):
        self.state.create_task(project_alias='alpha', agent=AgentKind.CODEX,
                               chat_id='chat-2', owner_open_id='ou-1', prompt='hello')
        choose = unittest.mock.Mock(return_value=1)
        sync_codex_to_feishu(self.state, self.codex, 'title', choose=choose)
        self.assertEqual(len(choose.call_args.args[1]), 2)
        self.assertEqual(len([t for t in self.state.list_tasks() if t.session_id == self.sid]), 1)

    def test_list_returns_only_five_newest_unarchived_chat_titles(self):
        with sqlite3.connect(self.db) as c:
            c.execute('ALTER TABLE threads ADD COLUMN created_at INTEGER DEFAULT 0')
            for index in range(7):
                c.execute('INSERT INTO threads(id,cwd,title,source,archived,rollout_path,created_at) VALUES(?,?,?,?,?,?,?)',
                          (str(uuid4()), str(self.config.projects['alpha'].path), f'Chat {index}',
                           'vscode', int(index == 6), str(self.path), index))
        router = MessageRouter(self.config, self.state)
        with patch.dict('os.environ', {'CODEX_HOME': str(self.home)}):
            reply = router.handle(inbound('/list'))
            self.assertEqual(reply, ['1. Chat 5\n2. Chat 4\n3. Chat 3\n4. Chat 2\n5. Chat 1'])
            self.assertEqual(router.handle(inbound('/list')), [])
        self.assertEqual(len(self.state.list_tasks()), 1)

    def test_list_uses_selected_project_or_default_and_handles_empty(self):
        router = MessageRouter(self.config, self.state)
        with patch('agent_message.orchestration.router.CodexSessionStore') as store:
            store.return_value.list_chat_titles.return_value = []
            self.assertIn('没有未归档', router.handle(inbound('/list'))[0])
            store.return_value.list_chat_titles.assert_called_once_with(self.config.projects['alpha'].path, limit=5)
            self.state.clear_selected_task('chat-1')
            router.handle(inbound('/list', 'second', 'second'))
            self.assertEqual(store.return_value.list_chat_titles.call_count, 2)

    def test_list_requires_authorization_and_reports_read_failure(self):
        router = MessageRouter(self.config, self.state)
        with patch('agent_message.orchestration.router.CodexSessionStore', side_effect=OSError('unavailable')):
            with self.assertLogs('agent_message.orchestration.router', level='ERROR'):
                self.assertEqual(router.handle(inbound('/list')), ['暂时无法读取 Codex Chats，请稍后重试。'])
        self.state._connection.execute('DELETE FROM authorized_users')
        self.state._connection.commit()
        with patch('agent_message.orchestration.router.CodexSessionStore') as store, patch.dict('os.environ', {'AGENT_MESSAGE_ALLOWED_OPEN_IDS': ''}):
            self.assertEqual(router.handle(inbound('/list', 'unauthorized', 'unauthorized')), [])
            store.assert_not_called()

    def test_schema_migration_preserves_existing_tasks_and_history(self):
        # Simulate a pre-sync installation, then exercise the migration twice.
        self.state._connection.execute('DROP TABLE session_transcripts')
        self.state._connection.commit()
        migrated = StateStore(self.config)
        self.assertEqual(migrated.get_task(self.target.id).id, self.target.id)
        migrated.close()
        self.import_session()
        other = StateStore(self.config)
        try:
            self.assertEqual(len(other.list_tasks()), 2)
            task = next(t for t in other.list_tasks() if t.session_id == self.sid)
            self.assertEqual(len(other.session_history(task.id)), 2)
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
