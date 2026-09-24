"""Perform agent-requested sync on the host after its originating run ends."""
from __future__ import annotations

import asyncio
import logging
import json
import stat
import os
import re
from pathlib import Path

from ..agents.codex_sessions import CodexSessionStore
from ..core.models import Task
from ..core.session_sync import safe_path
from ..core.state import StateStore
from .session_sync import sync_codex_to_feishu, sync_feishu_to_codex

LOGGER = logging.getLogger(__name__)


class SyncSpoolWatcher:
    def __init__(self, state: StateStore, codex_home: Path | None = None) -> None:
        self.state = state
        self.home = codex_home or Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()

    async def run(self) -> None:
        while True:
            try:
                work = asyncio.create_task(asyncio.to_thread(self.poll_once))
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    await work
                    raise
            except Exception:
                LOGGER.exception('Session sync spool failed')
            await asyncio.sleep(1)

    def capture_run(self, run_id: int) -> None:
        """Freeze requests at the run boundary; never trust an old mutable spool.

        Only the scheduler (or interrupted-run recovery) invokes this, binding
        requests to the originating run instead of an agent-supplied identity.
        """
        context = self.state.agent_sync_context(run_id)
        if context is None:
            return
        token, task = context
        project = self.state.config.projects[task.project_alias]
        try:
            directory = safe_path(project.path, Path('.agent-message/sync') / token)
            if not directory.is_dir():
                return
            for request in sorted(directory.iterdir()):
                if not re.fullmatch(r'sync-[A-Za-z0-9]+\.req', request.name):
                    continue
                try:
                    fd = os.open(request, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'rb') as handle:
                        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                            raise ValueError('同步请求必须是普通文件。')
                        data = handle.read(8193)
                    if len(data) > 8192:
                        raise ValueError('同步请求过长。')
                    lines = data.decode('utf-8').splitlines()
                    if len(lines) != 2 or not lines[1].strip():
                        raise ValueError('无效的同步请求。')
                    payload = {'direction': lines[0], 'value': lines[1]}
                except (OSError, ValueError) as exc:
                    payload = {'error': str(exc)}
                self.state.save_agent_sync_request(run_id, f'{token}:{request.name}', json.dumps(payload))
        except (OSError, ValueError):
            LOGGER.exception('Cannot capture session sync spool for %s', task.project_alias)

    def poll_once(self) -> None:
        for key, raw, task in self.state.pending_agent_sync_requests():
            try:
                if not self.state.is_authorized(task.owner_open_id):
                    raise ValueError('同步用户已不在授权名单。')
                payload = json.loads(raw)
                if 'error' in payload:
                    raise ValueError(payload['error'])
                message = self._sync(task, payload['direction'], payload['value'])
            except Exception as exc:
                message = f'会话同步未完成：{exc}'
            self.state.finish_agent_sync(key, task.chat_id, message)

    def _sync(self, actor: Task, direction: str, value: str) -> str:
        codex = CodexSessionStore(self.home)
        if direction == 'feishu-to-codex':
            target = actor if value == 'current' else self.state.get_task(value)
            if target is None or (target.project_alias, target.chat_id, target.owner_open_id) != (
                actor.project_alias, actor.chat_id, actor.owner_open_id
            ):
                raise ValueError('只能同步当前项目、当前飞书用户及聊天内的任务。')
            return sync_feishu_to_codex(self.state, codex, target.id)
        if direction != 'codex-to-feishu':
            raise ValueError('不支持的同步方向。')

        def ambiguity(prompt: str, labels: list[str]) -> int:
            raise ValueError('匹配多个会话，请回复完整标题或候选 session ID 后重试：\n' + '\n'.join(labels))

        return sync_codex_to_feishu(
            self.state, codex, value, actor.id, choose=ambiguity, project_alias=actor.project_alias,
        )
