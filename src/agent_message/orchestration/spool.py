"""Deliver project files that agents request through the injected transfer script.

The script only writes a request into the project spool; this watcher owns the
actual Feishu upload, so credentials never reach the agent. The chat is resolved
from the project's most recent run instead of from the request file, which keeps
an agent from addressing arbitrary conversations.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..core.config import AppConfig
from ..core.media import kind_for_path, resolve_project_file
from ..core.state import StateStore
from ..core.transfer import (
    discard_request,
    pending_requests,
    prune_results,
    read_request,
    write_result,
)

LOGGER = logging.getLogger(__name__)

SendMedia = Callable[[str, str, Path], Awaitable[None]]
_POLL_SECONDS = 0.5
_RESULT_TTL_SECONDS = 3600.0
_SIZE_HINT = "图片 10MB，其他文件 30MB"


class SpoolWatcher:
    """Polls every project's .agent-message/outbox for transfer requests."""

    def __init__(
        self,
        config: AppConfig,
        state: StateStore,
        send_media: SendMedia,
        *,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        self.config = config
        self.state = state
        self.send_media = send_media
        self.poll_seconds = poll_seconds
        self._stopping = False
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stopping = True
        self._stop.set()

    async def run(self) -> None:
        while not self._stopping:
            try:
                await self.poll_once()
            except Exception:  # One bad project must not stop the watcher.
                LOGGER.exception("transfer spool iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> None:
        now = time.time()
        for project in self.config.projects.values():
            for request in pending_requests(project.path):
                await self._handle(project.alias, project.path, request)
            prune_results(project.path, older_than_seconds=_RESULT_TTL_SECONDS, now=now)

    async def _handle(self, project_alias: str, project_path: Path, request: Path) -> None:
        relative = read_request(request)
        # Consume first: a bridge crash must not turn one request into two sends.
        discard_request(request)
        if relative is None:
            write_result(request, ok=False, message="发送请求内容为空。")
            return
        resolved = resolve_project_file(project_path, relative)
        if resolved is None:
            write_result(
                request,
                ok=False,
                message=f"无法发送 {relative}：路径必须位于项目内、文件必须存在且不超过 {_SIZE_HINT}。",
            )
            return
        context = self.state.last_chat_for_project(project_alias)
        if context is None:
            write_result(
                request,
                ok=False,
                message=f"项目 {project_alias} 还没有飞书会话，无法确定发送目标。",
            )
            return
        chat_id, _task_id = context
        kind = kind_for_path(resolved)
        try:
            await self.send_media(chat_id, kind, resolved)
        except Exception as exc:
            LOGGER.warning("Transfer of %s failed: %s", resolved, exc)
            write_result(request, ok=False, message=str(exc)[:500])
            return
        LOGGER.info("Transferred %s (%s) to chat %s", resolved, kind, chat_id)
        write_result(request, ok=True, message=resolved.name)
