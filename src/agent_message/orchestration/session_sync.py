"""Explicit local synchronization; never sends Feishu messages or starts turns."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID

from ..agents.codex_sessions import CodexSession, CodexSessionStore, SessionSyncError
from ..core.config import AppConfig, load_config
from ..core.models import AgentKind
from ..core.state import StateStore


def _bot_configs(base: AppConfig, config_path: str | Path) -> list[AppConfig]:
    """Every bot view of a shared installation; the primary view alone otherwise."""
    if not base.bots:
        return [base]
    return [load_config(config_path, app_id=bot_id) for bot_id in base.bots]


def _state_contains_task(config: AppConfig, task_id: str) -> bool:
    database = config.service.state_dir / 'agent-message.sqlite3'
    if not database.is_file():
        return False
    connection = sqlite3.connect(f'{database.resolve().as_uri()}?mode=ro', uri=True)
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone() is None:
            return False
        row = connection.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def select_config_for_task(
    config_path: str | Path, task_id: str, app_id: str | None = None
) -> AppConfig:
    """Pick the bot that stores the Feishu task; an explicit app_id wins."""
    if app_id is not None:
        return load_config(config_path, app_id=app_id)
    configs = _bot_configs(load_config(config_path), config_path)
    matches = [config for config in configs if _state_contains_task(config, task_id)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        if len(configs) == 1:
            raise SessionSyncError('任务不存在、正在运行或排队，请等待完成后同步。')
        names = '、'.join(config.selected_app_id or '默认机器人' for config in configs)
        raise SessionSyncError(
            f'找不到任务 {task_id}；已检查机器人：{names}。请确认任务 ID，或用 --app-id 指定机器人。'
        )
    raise SessionSyncError(f'任务 {task_id} 在多个机器人中重复出现；请用 --app-id 指定机器人。')


def resolve_codex_session(
    codex: CodexSessionStore,
    reference: str,
    project_paths: set[Path],
    choose: Callable[[str, list[str]], int] | None = None,
    *,
    show_ids: bool = False,
) -> CodexSession:
    """Resolve a session ID or a full/partial Chats title to one Codex session."""
    try:
        UUID(reference)
    except ValueError:
        matches = codex.find_by_title(reference, project_paths)
        if not matches:
            raise SessionSyncError('找不到匹配的未归档对话；请检查 Chats 标题和配置的项目。')
        labels = [
            f'{session.title} | {session.cwd} | '
            f'{datetime.fromtimestamp(session.path.stat().st_mtime).isoformat(sep=" ", timespec="seconds")}'
            + (f' | {session.id}' if show_ids else '')
            for session in matches
        ]
        return matches[_select('找到多个对话，请选择', labels, choose)]
    return codex.read(reference)


def _codex_project_paths(configs: list[AppConfig]) -> set[Path]:
    return {
        project.path.resolve()
        for config in configs
        for project in config.projects.values()
        if AgentKind.CODEX in project.allowed_agents
    }


def select_config_for_codex_reference(
    config_path: str | Path,
    codex: CodexSessionStore,
    reference: str,
    app_id: str | None = None,
    choose: Callable[[str, list[str]], int] | None = None,
) -> tuple[AppConfig, str]:
    """Resolve a Chats title/ID and return the bot that owns the session directory."""
    if app_id is not None:
        return load_config(config_path, app_id=app_id), reference
    configs = _bot_configs(load_config(config_path), config_path)
    session = resolve_codex_session(codex, reference, _codex_project_paths(configs), choose)
    matches = [
        config
        for config in configs
        if any(
            project.path.resolve() == session.cwd and AgentKind.CODEX in project.allowed_agents
            for project in config.projects.values()
        )
    ]
    if len(matches) == 1:
        return matches[0], session.id
    if not matches:
        raise SessionSyncError(f'session 工作目录不在已配置的项目白名单中：{session.cwd}')
    raise SessionSyncError('session 的工作目录同时属于多个机器人；请用 --app-id 指定机器人。')


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
    allowed_paths = {
        project.path.resolve()
        for project in state.config.projects.values()
        if AgentKind.CODEX in project.allowed_agents
        and (project_alias is None or project.alias == project_alias)
    }
    session = resolve_codex_session(codex, session_id, allowed_paths, choose,
                                    show_ids=project_alias is not None)
    session_id = session.id
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
