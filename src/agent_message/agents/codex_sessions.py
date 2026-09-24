"""Local Codex session interoperability (state_5 / JSONL, Codex 0.153).

Codex has no API for promoting an exec source to an interactive source. Keep
that compatibility operation here, fail closed on unknown stores, and preserve
all rollout records except the first record's source field.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


class SessionSyncError(ValueError):
    pass


@dataclass(frozen=True)
class CodexSession:
    id: str
    cwd: Path
    title: str
    archived: bool
    source: str
    path: Path


class CodexSessionStore:
    def __init__(self, home: Path) -> None:
        self.home = home.expanduser().resolve()
        self.database = self.home / 'state_5.sqlite'
        if not self.database.is_file():
            raise SessionSyncError('找不到受支持的 Codex state_5.sqlite；请指定 Chats 使用的 --codex-home。')

    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        mode = 'rw' if write else 'ro'
        connection = sqlite3.connect(self.database.as_uri() + '?mode=' + mode, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        columns = {r[1] for r in connection.execute('PRAGMA table_info(threads)')}
        if not {'id', 'cwd', 'title', 'source', 'archived', 'rollout_path'} <= columns:
            connection.close()
            raise SessionSyncError('不支持此 Codex threads schema；没有修改会话。')
        return connection

    def read(self, session_id: str) -> CodexSession:
        try:
            UUID(session_id)
        except ValueError as exc:
            raise SessionSyncError('需要完整的 Codex session UUID。') from exc
        connection = self._connect()
        try:
            row = connection.execute('SELECT * FROM threads WHERE id=?', (session_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise SessionSyncError('该 Codex home 中找不到 session。')
        path = Path(row['rollout_path']).resolve()
        archived = bool(row['archived']) or path.is_relative_to(self.home / 'archived_sessions')
        if not archived and not path.is_relative_to(self.home / 'sessions'):
            raise SessionSyncError('Codex rollout 路径不在 sessions 目录内。')
        title = (row['name'] if 'name' in row.keys() else None) or row['title']
        return CodexSession(session_id, Path(row['cwd']).resolve(), title, archived, row['source'], path)

    def find_by_title(self, title: str, projects: set[Path]) -> list[CodexSession]:
        """Prefer exact visible names; partial titles help with truncated UI labels."""
        title = title.strip()
        if not title:
            raise SessionSyncError('请输入 Chats 中的对话标题。')
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM threads WHERE archived=0 AND source IN ('cli','vscode','exec','appServer') ORDER BY id DESC"
            ).fetchall()
        finally:
            connection.close()
        exact, partial = [], []
        for row in rows:
            if Path(row['cwd']).resolve() not in projects:
                continue
            name = (row['name'] if 'name' in row.keys() else None) or row['title']
            if title.casefold() not in name.casefold():
                continue
            session = self.read(row['id'])
            if session.archived:
                continue
            (exact if title.casefold() == name.casefold() else partial).append(session)
        return exact or partial

    def list_chat_titles(self, project: Path, limit: int = 50) -> list[str]:
        """Match the interactive Chats sources; newest-created sessions first."""
        if not 1 <= limit <= 50:
            raise SessionSyncError('列表数量必须为 1 到 50。')
        connection = self._connect()
        try:
            columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
            order = 'created_at DESC, id DESC' if 'created_at' in columns else 'id DESC'
            rows = connection.execute(
                "SELECT * FROM threads WHERE archived=0 AND source IN ('cli','vscode') ORDER BY " + order
            ).fetchall()
        finally:
            connection.close()
        titles = []
        for row in rows:
            if Path(row['cwd']).resolve() != project.resolve():
                continue
            path = Path(row['rollout_path']).resolve()
            if not path.is_relative_to(self.home / 'sessions'):
                continue
            name = (row['name'] if 'name' in row.keys() else None) or row['title']
            titles.append(' '.join((name or '未命名对话').split()))
            if len(titles) == limit:
                break
        return titles

    def snapshot(self, session: CodexSession) -> tuple[bytes, list[dict], os.stat_result]:
        before = session.path.stat()
        data = session.path.read_bytes()
        after = session.path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise SessionSyncError('Codex 会话正在变化，请等待当前轮次结束。')
        try:
            records = [json.loads(line) for line in data.splitlines() if line.strip()]
            if any(not isinstance(record, dict) or not isinstance(record.get('payload'), dict)
                   for record in records):
                raise ValueError('invalid record')
            metadata = records[0]
            if metadata['type'] != 'session_meta' or metadata['payload']['id'] != session.id:
                raise ValueError('mismatched metadata')
            cwd = metadata['payload'].get('cwd')
            if cwd is not None and Path(cwd).resolve() != session.cwd:
                raise ValueError('mismatched cwd')
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise SessionSyncError('Codex rollout 格式不受支持或不完整。') from exc
        active = False
        for record in records:
            if record.get('type') == 'event_msg':
                kind = record.get('payload', {}).get('type')
                if kind == 'task_started':
                    active = True
                elif kind in {'task_complete', 'turn_aborted'}:
                    active = False
        if active:
            raise SessionSyncError('Codex 会话仍有未结束轮次；请先结束或停止该轮次。')
        return data, records, after

    def transcript(self, session: CodexSession) -> list[tuple[str, str]]:
        _, records, _ = self.snapshot(session)
        result: list[tuple[str, str]] = []
        for record in records:
            if record.get('type') != 'event_msg':
                continue
            event = record.get('payload', {})
            kind = event.get('type')
            if kind in {'user_message', 'agent_message'}:
                value = event.get('message')
                if isinstance(value, str) and value:
                    result.append(('user' if kind == 'user_message' else 'assistant', value))
            elif kind == 'item_completed':
                item = event.get('item', {})
                if not isinstance(item, dict):
                    raise SessionSyncError('Codex item 格式不受支持。')
                role = {'UserMessage': 'user', 'AgentMessage': 'assistant'}.get(item.get('type'))
                if role:
                    content = item.get('content', [])
                    if not isinstance(content, list):
                        raise SessionSyncError('Codex message content 格式不受支持。')
                    value = '\n'.join(part['text'] for part in content
                                      if isinstance(part, dict) and isinstance(part.get('text'), str))
                    if value:
                        result.append((role, value))
        if not result:
            raise SessionSyncError('此 session 没有可识别的对话消息；未导入工具或 reasoning 记录。')
        return result

    def make_visible(self, session: CodexSession) -> bool:
        """Promote exec metadata, retaining UUID, timestamps, and complete history.

        A durable backup precedes the atomic rollout replacement. Updating the
        index is idempotent, so rerunning repairs an interrupted index update.
        """
        if session.archived:
            return False
        if session.source not in {'exec', 'cli', 'vscode', 'appServer'}:
            raise SessionSyncError('不支持同步子 Agent 或未知来源的会话。')
        data, records, stat = self.snapshot(session)
        source = records[0]['payload'].get('source')
        if source not in {'exec', 'cli', 'vscode', 'appServer'}:
            raise SessionSyncError('rollout source 不受支持。')
        connection = self._connect(write=True)
        temporary: str | None = None
        try:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT archived, rollout_path FROM threads WHERE id=?', (session.id,)).fetchone()
            if current is None or current['archived']:
                return False
            if Path(current['rollout_path']).resolve() != session.path:
                raise SessionSyncError('会话路径已变化，请重试。')
            if source not in {'cli', 'vscode'}:
                backup_dir = self.home / 'agent-message-sync-backups'
                backup_dir.mkdir(mode=0o700, exist_ok=True)
                backup = backup_dir / (session.id + '.jsonl')
                fd, backup_temporary = tempfile.mkstemp(prefix='.backup-', dir=backup_dir)
                try:
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    try:
                        os.link(backup_temporary, backup)
                    except FileExistsError:
                        pass
                finally:
                    os.unlink(backup_temporary)
                first, separator, rest = data.partition(b'\n')
                metadata = json.loads(first)
                metadata['payload']['source'] = 'cli'
                replacement = json.dumps(metadata, ensure_ascii=False).encode() + separator + rest
                fd, temporary = tempfile.mkstemp(prefix='.agent-message-', dir=session.path.parent)
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(replacement)
                    handle.flush()
                    os.fsync(handle.fileno())
                current_stat = session.path.stat()
                if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != (current_stat.st_ino, current_stat.st_size, current_stat.st_mtime_ns):
                    raise SessionSyncError('会话正在变化，取消同步；请等待轮次结束。')
                os.replace(temporary, session.path)
                temporary = None
                source = 'cli'
            connection.execute('UPDATE threads SET source=? WHERE id=? AND archived=0', (source, session.id))
            connection.commit()
            return True
        finally:
            connection.close()
            if temporary is not None:
                os.unlink(temporary)
