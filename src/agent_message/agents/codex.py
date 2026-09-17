"""Headless Codex CLI adapter and Codex-specific MCP configuration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .base import (
    AdapterError,
    AgentAdapter,
    ParsedAgentEvent,
    PidCallback,
    ProgressCallback,
    SessionCallback,
)
from ..core.config import AppConfig
from ..core.models import AgentKind, AgentResult, ClaimedRun, Task
from ..runtimes.container.policy import CONTAINER_INSTRUCTIONS, CONTAINER_TURN_PREFIX
from ..runtimes.sandbox.policy import sandbox_instructions, sandbox_turn_prefix


_SANDBOX_MCP_SERVER_ID = "agent_message_sandbox"
_CONTAINER_MCP_SERVER_ID = "agent_message_container"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_string_array(values: list[str]) -> str:
    return "[" + ",".join(_toml_string(value) for value in values) + "]"


def sandbox_mcp_config_args(
    config: AppConfig,
    *,
    task_id: str,
    project_alias: str,
    run_id: int | None,
) -> list[str]:
    project = config.projects[project_alias]
    if project.sandbox is None or not project.sandbox.enabled:
        return []
    server_args = [
        "-m",
        "agent_message.runtimes.sandbox.mcp",
        "--config",
        str(config.config_path),
        "--task-id",
        task_id,
    ]
    if run_id is not None:
        server_args.extend(["--run-id", str(run_id)])
    root = config.config_path.parent.parent
    settings = {
        "developer_instructions": _toml_string(sandbox_instructions(project.sandbox.gpu)),
        f"mcp_servers.{_SANDBOX_MCP_SERVER_ID}.command": _toml_string(sys.executable),
        f"mcp_servers.{_SANDBOX_MCP_SERVER_ID}.args": _toml_string_array(server_args),
        f"mcp_servers.{_SANDBOX_MCP_SERVER_ID}.cwd": _toml_string(str(root)),
        f"mcp_servers.{_SANDBOX_MCP_SERVER_ID}.required": "true",
        f"mcp_servers.{_SANDBOX_MCP_SERVER_ID}.enabled_tools": _toml_string_array(["sandbox_run"]),
        f"mcp_servers.{_SANDBOX_MCP_SERVER_ID}.tool_timeout_sec": str(
            project.sandbox.timeout_seconds + 30
        ),
        f"mcp_servers.{_SANDBOX_MCP_SERVER_ID}.default_tools_approval_mode": _toml_string(
            "approve"
        ),
    }
    result: list[str] = []
    for key, value in settings.items():
        result.extend(["-c", f"{key}={value}"])
    return result


def container_mcp_config_args(
    config: AppConfig,
    *,
    task_id: str,
    project_alias: str,
    run_id: int | None,
) -> list[str]:
    project = config.projects[project_alias]
    if project.container is None:
        return []
    server_args = [
        "-m",
        "agent_message.runtimes.container.mcp",
        "--config",
        str(config.config_path),
        "--task-id",
        task_id,
    ]
    if run_id is not None:
        server_args.extend(["--run-id", str(run_id)])
    root = config.config_path.parent.parent
    settings = {
        "developer_instructions": _toml_string(CONTAINER_INSTRUCTIONS),
        f"mcp_servers.{_CONTAINER_MCP_SERVER_ID}.command": _toml_string(sys.executable),
        f"mcp_servers.{_CONTAINER_MCP_SERVER_ID}.args": _toml_string_array(server_args),
        f"mcp_servers.{_CONTAINER_MCP_SERVER_ID}.cwd": _toml_string(str(root)),
        f"mcp_servers.{_CONTAINER_MCP_SERVER_ID}.required": "true",
        f"mcp_servers.{_CONTAINER_MCP_SERVER_ID}.enabled_tools": _toml_string_array(
            ["container_run"]
        ),
        f"mcp_servers.{_CONTAINER_MCP_SERVER_ID}.tool_timeout_sec": str(
            project.container.timeout_seconds + 30
        ),
        f"mcp_servers.{_CONTAINER_MCP_SERVER_ID}.default_tools_approval_mode": _toml_string(
            "approve"
        ),
    }
    result: list[str] = []
    for key, value in settings.items():
        result.extend(["-c", f"{key}={value}"])
    return result


def _extract_session_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("thread_id", "session_id", "sessionId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    thread = value.get("thread")
    if isinstance(thread, dict):
        candidate = thread.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _extract_text(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("text", "content", "message", "summary", "result"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    item = value.get("item")
    if isinstance(item, dict):
        return _extract_text(item)
    return None


def _extract_progress(value: dict[str, object]) -> str | None:
    """Return safe lifecycle updates; never expose reasoning or tool output."""
    event_type = value.get("type")
    if event_type == "thread.started":
        return "Codex 会话已建立。"
    if event_type == "turn.started":
        return "Codex 正在分析任务。"
    item = value.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if event_type == "item.started":
        return {
            "command_execution": "Codex 正在执行本地命令。",
            "file_change": "Codex 正在修改项目文件。",
            "agent_message": "Codex 正在整理中间说明。",
        }.get(item_type)
    if event_type == "item.completed":
        if item_type == "agent_message":
            text = _extract_text(item)
            return f"Codex：{text[:700]}" if text else None
        return {
            "command_execution": "Codex 已完成一项本地命令。",
            "file_change": "Codex 已完成一项文件修改。",
        }.get(item_type)
    return None


class CodexAdapter(AgentAdapter):
    kind = AgentKind.CODEX
    executable = "codex"

    def __init__(
        self, tool_network_enabled: bool = False, app_config: AppConfig | None = None
    ) -> None:
        self.tool_network_enabled = tool_network_enabled
        self.app_config = app_config

    def _runtime_parts(
        self, task_id: str, project_alias: str, run_id: int | None
    ) -> tuple[list[str], str]:
        if self.app_config is None:
            return [], ""
        project = self.app_config.projects[project_alias]
        if project.container is not None:
            return (
                container_mcp_config_args(
                    self.app_config,
                    task_id=task_id,
                    project_alias=project_alias,
                    run_id=run_id,
                ),
                CONTAINER_TURN_PREFIX,
            )
        if project.sandbox is not None and project.sandbox.enabled:
            return (
                sandbox_mcp_config_args(
                    self.app_config,
                    task_id=task_id,
                    project_alias=project_alias,
                    run_id=run_id,
                ),
                sandbox_turn_prefix(project.sandbox.gpu),
            )
        return [], ""

    async def execute(
        self,
        run: ClaimedRun,
        on_pid: PidCallback,
        on_session: SessionCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AgentResult:
        if run.operation != "compact":
            return await super().execute(run, on_pid, on_session, on_progress)
        from .compact import execute_compaction

        runtime_config, _ = self._runtime_parts(run.task_id, run.project_alias, run.run_id)
        command = [
            self.executable,
            "-c", "sandbox_workspace_write.network_access="
            + ("true" if self.tool_network_enabled else "false"),
            *runtime_config,
            *(["-c", f"model_reasoning_effort={_toml_string(run.reasoning_effort)}"]
              if run.reasoning_effort else []),
            "app-server",
        ]
        return await execute_compaction(run, command, self.environment(), on_pid, on_progress)

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        runtime_config, prompt_prefix = self._runtime_parts(
            run.task_id, run.project_alias, run.run_id
        )
        prompt = prompt_prefix + run.prompt
        model_args = ["-m", run.model] if run.model else []
        # Codex replaces parent -c overrides when exec/resume has its own -c.
        # Keep every override together so effort cannot discard MCP or sandbox policy.
        reasoning_args = (
            ["-c", f"model_reasoning_effort={_toml_string(run.reasoning_effort)}"]
            if run.reasoning_effort else []
        )
        common = [
            self.executable,
            "-c",
            "sandbox_workspace_write.network_access="
            + ("true" if self.tool_network_enabled else "false"),
            *runtime_config,
            *reasoning_args,
            "exec",
        ]
        output = ["--json", "--output-last-message", str(last_message_path)]
        if run.session_id:
            return [
                *common,
                "resume",
                *model_args,
                "--skip-git-repo-check",
                *output,
                run.session_id,
                prompt,
            ]
        return [
            *common,
            *model_args,
            *output,
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-C",
            str(run.project_path),
            prompt,
        ]

    def build_terminal_resume_command(self, task: Task) -> list[str]:
        if self.app_config is None:
            raise AdapterError("Codex 终端恢复缺少应用配置。")
        if not task.session_id:
            raise AdapterError(f"任务 {task.id} 尚未创建 Codex 会话，无法恢复。")
        project = self.app_config.projects[task.project_alias]
        runtime_config, _ = self._runtime_parts(task.id, task.project_alias, None)
        return [
            self.executable,
            "-c",
            "sandbox_workspace_write.network_access="
            + ("true" if self.tool_network_enabled else "false"),
            *runtime_config,
            "resume",
            "--include-non-interactive",
            "--sandbox",
            "workspace-write",
            "-C",
            str(project.path),
            task.session_id,
        ]

    def parse_event(self, value: dict[str, object]) -> ParsedAgentEvent:
        return ParsedAgentEvent(
            session_id=_extract_session_id(value),
            progress=_extract_progress(value),
            final_message=_extract_text(value),
        )
