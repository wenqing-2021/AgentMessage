"""Explicit local synchronization; never sends Feishu messages or starts turns."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from ..agents.codex_sessions import CodexSessionStore, SessionSyncError
from ..core.models import AgentKind
from ..core.state import StateStore


def sync_feishu_to_codex(state: StateStore, codex: CodexSessionStore, task_id: str) -> str:
    with state.idle_task_for_sync(task_id) as task:
        if task.agent != AgentKind.CODEX or not task.session_id:
            raise SessionSyncError('需要已有 Codex session 的飞书任务 ID。')
        project = state.config.projects.get(task.project_alias)
        session = codex.read(task.session_id)
        if session.archived:
            return f'跳过已归档 session：{session.id}'
        if project is None or session.cwd != project.path.resolve():
            raise SessionSyncError('session 工作目录与白名单项目不一致。')
        if not codex.make_visible(session):
            return f'跳过已归档 session：{session.id}'
        return f'已同步到 Codex Chats：{session.id}（保留原 session；请刷新 Chats 列表）'


def sync_codex_to_feishu(
    state: StateStore, codex: CodexSessionStore, session_id: str,
    target_task_id: str | None = None,
    *, choose: Callable[[str, list[str]], int] | None = None,
    project_alias: str | None = None,
) -> str:
    try:
        UUID(session_id)
    except ValueError:
        matches = codex.find_by_title(session_id, {
            p.path.resolve() for p in state.config.projects.values() if AgentKind.CODEX in p.allowed_agents
            and (project_alias is None or p.alias == project_alias)
        })
        if not matches:
            raise SessionSyncError('找不到匹配的未归档对话；请检查 Chats 标题和配置的项目。')
        index = _select('找到多个对话，请选择', [f'{s.title} | {s.cwd} | {datetime.fromtimestamp(s.path.stat().st_mtime).isoformat(sep=" ", timespec="seconds")}' + (f' | {s.id}' if project_alias else '') for s in matches], choose)
        session_id = matches[index].id
    session = codex.read(session_id)
    if project_alias is not None and session.cwd != state.config.projects[project_alias].path.resolve():
        raise SessionSyncError('只能导入当前项目的 Codex 会话。')
    if session.archived:
        return f'跳过已归档 session：{session.id}'
    if session.source not in {'cli', 'vscode', 'exec', 'appServer'}:
        raise SessionSyncError('不支持同步子 Agent 或未知来源的会话。')
    projects = [p for p in state.config.projects.values()
                if p.path.resolve() == session.cwd and AgentKind.CODEX in p.allowed_agents]
    if len(projects) != 1:
        raise SessionSyncError('session 工作目录必须唯一匹配允许 Codex 的白名单项目。')
    tasks = state.list_tasks()
    if target_task_id is None:
        linked = [t for t in tasks if t.session_id == session_id and t.agent == AgentKind.CODEX]
        candidates = linked or [t for t in tasks if t.project_alias == projects[0].alias
                                 and state.is_authorized(t.owner_open_id)]
        destinations = {}
        for task in candidates:
            destinations.setdefault((task.chat_id, task.owner_open_id), task)
        targets = list(destinations.values())
        if not targets:
            raise SessionSyncError('没有可用的飞书目标，请先在飞书为该项目创建一个对话。')
        index = _select('找到多个飞书目标，请选择', [
            f'{t.project_alias} | 任务 {t.id} | {t.last_summary or "无摘要"} | {t.chat_id}' for t in targets
        ], choose)
        target_task_id = targets[index].id
    transcript = codex.transcript(session)
    if codex.read(session_id).archived:
        return f'跳过已归档 session：{session.id}'
    task = state.import_codex_session(
        session_id=session_id, project_alias=projects[0].alias, target_task_id=target_task_id,
        title=session.title, transcript=transcript,
    )
    return f'已同步 {len(transcript)} 条对话到任务 {task.id}；飞书 /history {task.id} 查看记录，/use {task.id} 继续。'


def _select(prompt: str, labels: list[str], choose: Callable[[str, list[str]], int] | None) -> int:
    if len(labels) == 1:
        return 0
    if choose is None:
        raise SessionSyncError(f'{prompt}；请在终端运行同步命令以按编号选择。')
    index = choose(prompt, labels)
    if not isinstance(index, int) or not 0 <= index < len(labels):
        raise SessionSyncError('选择编号无效，未同步。')
    return index
