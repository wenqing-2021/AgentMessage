"""Secure headless Qoder CLI adapter and stream-json protocol parser."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .base import AdapterError, AgentAdapter, ParsedAgentEvent
from ..core.config import AppConfig
from ..core.models import AgentKind, ClaimedRun, Task
from ..runtimes.container.policy import CONTAINER_INSTRUCTIONS, CONTAINER_TURN_PREFIX
from ..runtimes.gpu.policy import GPU_INSTRUCTIONS, GPU_TURN_PREFIX


_GPU_MCP_SERVER_ID = "agent_message_bwrap_gpu"
_GPU_MCP_TOOL = f"mcp__{_GPU_MCP_SERVER_ID}__gpu_run"
_CONTAINER_MCP_SERVER_ID = "agent_message_container"
_CONTAINER_MCP_TOOL = f"mcp__{_CONTAINER_MCP_SERVER_ID}__container_run"
_BASE_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write")
_BLOCKED_RULES = (
    "Bash(sudo:*)",
    "Bash(rm -rf:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(npm publish:*)",
    "Bash(twine upload:*)",
    "WebFetch",
    "WebSearch",
    "Agent",
)


@dataclass(frozen=True)
class _Runtime:
    server_id: str
    tool_name: str
    module: str
    instructions: str
    turn_prefix: str


def _safe_error_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:500] if cleaned else None


class QoderAdapter(AgentAdapter):
    kind = AgentKind.QODER
    executable = "qodercli"
    requires_terminal_event = True

    def __init__(self, app_config: AppConfig | None = None) -> None:
        self.app_config = app_config

    def _runtime(self, project_alias: str) -> _Runtime | None:
        if self.app_config is None:
            return None
        project = self.app_config.projects[project_alias]
        if project.container is not None:
            return _Runtime(
                _CONTAINER_MCP_SERVER_ID,
                _CONTAINER_MCP_TOOL,
                "agent_message.runtimes.container.mcp",
                CONTAINER_INSTRUCTIONS,
                CONTAINER_TURN_PREFIX,
            )
        if project.gpu is not None and project.gpu.enabled:
            return _Runtime(
                _GPU_MCP_SERVER_ID,
                _GPU_MCP_TOOL,
                "agent_message.runtimes.gpu.mcp",
                GPU_INSTRUCTIONS,
                GPU_TURN_PREFIX,
            )
        return None

    def _mcp_config(
        self,
        runtime: _Runtime | None,
        *,
        task_id: str,
        run_id: int | None,
    ) -> str:
        if runtime is None:
            return json.dumps({"mcpServers": {}}, separators=(",", ":"))
        if self.app_config is None:
            raise AdapterError("Qoder MCP 配置缺少应用配置。")
        server_args = [
            "-m",
            runtime.module,
            "--config",
            str(self.app_config.config_path),
            "--task-id",
            task_id,
        ]
        if run_id is not None:
            server_args.extend(["--run-id", str(run_id)])
        payload = {
            "mcpServers": {
                runtime.server_id: {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": server_args,
                    "cwd": str(self.app_config.config_path.parent.parent),
                }
            }
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _common_command(
        self,
        *,
        task_id: str,
        project_alias: str,
        project_path: Path,
        run_id: int | None,
    ) -> tuple[list[str], _Runtime | None]:
        runtime = self._runtime(project_alias)
        tools = list(_BASE_TOOLS)
        if runtime is None or runtime.server_id == _GPU_MCP_SERVER_ID:
            tools.append("Bash")
        if runtime is not None:
            tools.append(runtime.tool_name)
        blocked = list(_BLOCKED_RULES)
        if runtime is None:
            blocked.append("mcp__*")
        command = [
            self.executable,
            "-w",
            str(project_path),
            "--setting-sources",
            "",
            "--permission-mode",
            "auto",
            "--strict-mcp-config",
            "--mcp-config",
            self._mcp_config(runtime, task_id=task_id, run_id=run_id),
            "--tools",
            ",".join(tools),
            "--disallowed-tools",
            ",".join(blocked),
        ]
        if runtime is not None:
            command.extend(
                [
                    "--allowed-mcp-server-names",
                    runtime.server_id,
                    "--allowed-tools",
                    runtime.tool_name,
                    "--append-system-prompt",
                    runtime.instructions,
                ]
            )
        return command, runtime

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        command, runtime = self._common_command(
            task_id=run.task_id,
            project_alias=run.project_alias,
            project_path=run.project_path,
            run_id=run.run_id,
        )
        if run.session_id:
            command.extend(["-r", run.session_id])
        prompt = (runtime.turn_prefix if runtime else "") + run.prompt
        command.extend(["--output-format", "stream-json", "-p", prompt])
        return command

    def build_terminal_resume_command(self, task: Task) -> list[str]:
        if self.app_config is None:
            raise AdapterError("Qoder 终端恢复缺少应用配置。")
        if not task.session_id:
            raise AdapterError(f"任务 {task.id} 尚未创建 Qoder 会话，无法恢复。")
        project = self.app_config.projects[task.project_alias]
        command, _ = self._common_command(
            task_id=task.id,
            project_alias=task.project_alias,
            project_path=project.path,
            run_id=None,
        )
        command.extend(["-r", task.session_id])
        return command

    def environment(self) -> dict[str, str]:
        environment = super().environment()
        for key in (
            "QODER_SHELL_PREFIX",
            "QODERCN_SHELL_PREFIX",
            "CLAUDE_CODE_SHELL_PREFIX",
        ):
            environment.pop(key, None)
        return environment

    def parse_event(self, value: dict[str, object]) -> ParsedAgentEvent:
        event_type = value.get("type")
        session = value.get("session_id")
        session_id = session if isinstance(session, str) and session else None
        if event_type == "system" and value.get("subtype") == "init":
            return ParsedAgentEvent(
                session_id=session_id,
                progress="Qoder 会话已建立。",
            )
        if event_type == "assistant":
            message = value.get("message")
            if not isinstance(message, dict):
                return ParsedAgentEvent(session_id=session_id)
            content = message.get("content")
            if not isinstance(content, list):
                return ParsedAgentEvent(session_id=session_id)
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                progress = {
                    "Read": "Qoder 正在读取项目文件。",
                    "Grep": "Qoder 正在搜索项目内容。",
                    "Glob": "Qoder 正在查找项目文件。",
                    "Edit": "Qoder 正在修改项目文件。",
                    "Write": "Qoder 正在写入项目文件。",
                    "Bash": "Qoder 正在执行受限本地命令。",
                    _GPU_MCP_TOOL: "Qoder 正在执行受控 GPU 命令。",
                    _CONTAINER_MCP_TOOL: "Qoder 正在执行受控容器命令。",
                }.get(name)
                if progress:
                    return ParsedAgentEvent(session_id=session_id, progress=progress)
            return ParsedAgentEvent(session_id=session_id)
        if event_type != "result":
            return ParsedAgentEvent()

        subtype = value.get("subtype")
        is_error = value.get("is_error")
        result = value.get("result")
        final_message = result.strip() if isinstance(result, str) and result.strip() else None
        if subtype == "success" and is_error is False:
            return ParsedAgentEvent(
                session_id=session_id,
                final_message=final_message,
                terminal=True,
            )
        errors = value.get("errors")
        details: list[str] = []
        if isinstance(errors, list):
            for candidate in errors[:2]:
                cleaned = _safe_error_text(candidate)
                if cleaned:
                    details.append(cleaned)
        denials = value.get("permission_denials")
        if isinstance(denials, list) and denials:
            details.append(f"{len(denials)} 项工具权限被拒绝")
        label = subtype if isinstance(subtype, str) and subtype else "unknown_error"
        suffix = f"：{'；'.join(details)}" if details else ""
        return ParsedAgentEvent(
            session_id=session_id,
            final_message=final_message,
            terminal=True,
            error=f"Qoder 执行失败（{label}）{suffix}",
        )
