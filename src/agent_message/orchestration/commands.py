"""Parse user-facing Feishu commands into typed command objects."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal, cast

from ..core.models import AgentKind


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class NewTaskCommand:
    project_alias: str
    agent: AgentKind | None
    prompt: str


@dataclass(frozen=True)
class SimpleCommand:
    name: Literal["use", "status", "stop", "logs", "chat", "model", "help"]
    task_id: str | None = None
    log_lines: int | None = None
    log_source: Literal["agent", "gpu", "container"] = "agent"
    model_name: str | None = None


ParsedCommand = NewTaskCommand | SimpleCommand | None


HELP_TEXT = """先认识 4 个概念：
项目别名：配置中项目路径的短名称，例如 website；不能填写任意目录路径。
任务：一次独立的 Codex/Qoder 工作会话，例如“修复首页按钮样式”。
任务 ID：创建任务后返回的编号，例如 a1b2c3d4；用于切换、查看、停止任务。
当前任务：你接下来直接发送的普通文本会继续发送到的任务。

直接与默认 Agent 聊天：首次直接发送普通文本，会按默认项目的 default_agent 创建长期对话；以后普通文本会续写它。/chat 可回到该对话。
GPU：项目启用 bubblewrap GPU 后，可直接要求 Codex 或 Qoder 运行 CUDA/JAX/训练命令；无需新增飞书命令。
容器：项目设置 container_name 后，Codex 或 Qoder 会在宿主编辑挂载源码，并在已有容器中执行命令。

例子：
/new website 修复首页登录按钮在手机端溢出的问题
→ 创建并选中一个任务；之后直接发送“先检查 CSS，不要修改文件”会继续该任务。
/new website --agent qoder 检查测试失败原因
→ 创建并选中 Qoder 任务。
/use a1b2c3d4
→ 切换当前任务；之后普通文本会继续 a1b2c3d4。
/chat
→ 回到默认长期 Agent 对话；之后普通文本会继续它。
/model
→ 列出 Codex 可用模型；发送 /model <模型名称> 切换模型。
/status 或 /status a1b2c3d4
/logs a1b2c3d4 50
/logs a1b2c3d4 50 gpu
/logs a1b2c3d4 50 container
/stop a1b2c3d4

不确定当前任务时先发送 /status。任务描述不是任务 ID。"""


def parse_command(text: str) -> ParsedCommand:
    text = text.strip()
    if not text.startswith("/"):
        return None
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        raise CommandError("命令引号不完整。") from exc
    if not parts:
        raise CommandError("空命令。")
    command = parts[0].lower()
    if command == "/help":
        if len(parts) != 1:
            raise CommandError("/help 不接收参数。")
        return SimpleCommand("help")
    if command == "/chat":
        if len(parts) != 1:
            raise CommandError("/chat 不接收参数。")
        return SimpleCommand("chat")
    if command == "/model":
        if len(parts) > 2:
            raise CommandError("用法：/model [模型名称]")
        return SimpleCommand("model", model_name=parts[1] if len(parts) == 2 else None)
    if command == "/new":
        if len(parts) < 3:
            raise CommandError("用法：/new <项目别名> [--agent codex|qoder] <任务>")
        alias = parts[1]
        agent: AgentKind | None = None
        prompt_start = 2
        if len(parts) >= 4 and parts[2] == "--agent":
            try:
                agent = AgentKind(parts[3])
            except ValueError as exc:
                raise CommandError("--agent 只能是 codex 或 qoder。") from exc
            prompt_start = 4
        if len(parts) <= prompt_start:
            raise CommandError("/new 需要任务描述。")
        return NewTaskCommand(alias, agent, " ".join(parts[prompt_start:]))
    if command in {"/use", "/status", "/stop", "/logs"}:
        name = command[1:]
        if name == "status":
            if len(parts) > 2:
                raise CommandError("用法：/status [任务 ID]")
            return SimpleCommand("status", parts[1] if len(parts) == 2 else None)
        if name in {"use", "stop"}:
            if len(parts) != 2:
                raise CommandError(f"用法：/{name} <任务 ID>")
            return SimpleCommand(name, parts[1])
        if len(parts) not in {2, 3, 4}:
            raise CommandError("用法：/logs <任务 ID> [行数] [agent|gpu|container]")
        line_count = 20
        if len(parts) >= 3:
            try:
                line_count = int(parts[2])
            except ValueError as exc:
                raise CommandError("日志行数必须是整数。") from exc
        if not 1 <= line_count <= 100:
            raise CommandError("日志行数必须介于 1 和 100。")
        source = parts[3].lower() if len(parts) == 4 else "agent"
        if source not in {"agent", "gpu", "container"}:
            raise CommandError("日志来源只能是 agent、gpu 或 container。")
        return SimpleCommand(
            "logs",
            parts[1],
            line_count,
            cast(Literal["agent", "gpu", "container"], source),
        )
    raise CommandError(f"未知命令：{parts[0]}。发送 /help 查看帮助。")
