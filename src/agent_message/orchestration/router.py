"""Route inbound messages to durable task state transitions."""

from __future__ import annotations

from ..agents.model_catalog import (
    REASONING_EFFORTS, configured_codex_model, configured_reasoning_effort, list_codex_models,
)
from .commands import HELP_TEXT, CommandError, NewTaskCommand, SimpleCommand, parse_command
from ..core.config import AppConfig
from ..core.models import AgentKind, InboundMessage, Task, TaskOrigin, TaskStatus
from ..core.state import StateStore


def _agent_label(agent: AgentKind) -> str:
    value = getattr(agent, "value", str(agent))
    return "Codex" if value == "codex" else "Qoder" if value == "qoder" else str(value)


def _valid_model_name(name: str) -> bool:
    return (
        bool(name)
        and len(name) <= 200
        and all(ord(ch) >= 32 and ord(ch) != 127 for ch in name)
        and not any(ch.isspace() for ch in name)
    )


class MessageRouter:
    """Converts trusted Feishu messages to durable state changes and short replies."""

    def __init__(self, config: AppConfig, state: StateStore) -> None:
        self.config = config
        self.state = state

    def handle(self, message: InboundMessage) -> list[str]:
        if message.chat_type != "p2p" or not message.text.strip():
            return []
        # Save even unauthorised message IDs: Feishu retries must not produce unbounded work.
        if not self.state.register_inbound(
            message.event_id, message.message_id, message.chat_id, message.sender_open_id, message.text
        ):
            return []
        if not self.state.is_authorized(message.sender_open_id):
            return []
        try:
            parsed = parse_command(message.text)
        except CommandError as exc:
            return [str(exc)]
        if parsed is None:
            return self._continue(message)
        if isinstance(parsed, NewTaskCommand):
            return self._new(message, parsed)
        return self._simple(message, parsed)

    def _new(self, message: InboundMessage, command: NewTaskCommand) -> list[str]:
        project = self.config.projects.get(command.project_alias)
        if project is None:
            aliases = ", ".join(sorted(self.config.projects))
            return [f"未知项目别名：{command.project_alias}。可用项目：{aliases}"]
        agent = command.agent or project.default_agent
        if agent not in project.allowed_agents:
            permitted = ", ".join(item.value for item in sorted(project.allowed_agents, key=str))
            return [f"项目 {project.alias} 不允许使用 {agent.value}；可用：{permitted}"]
        task = self.state.create_task(
            project_alias=project.alias,
            agent=agent,
            chat_id=message.chat_id,
            owner_open_id=message.sender_open_id,
            prompt=command.prompt,
        )
        return [f"任务 {task.id} 已排队：{project.alias} / {agent.value}。"]

    def _continue(self, message: InboundMessage) -> list[str]:
        task = self.state.selected_task(message.chat_id, message.sender_open_id)
        if task is None:
            project = self.config.projects[self.config.service.default_chat_project]
            task = self.state.create_task(
                project_alias=project.alias,
                agent=project.default_agent,
                chat_id=message.chat_id,
                owner_open_id=message.sender_open_id,
                prompt=message.text.strip(),
                origin=TaskOrigin.CHAT,
            )
            return [
                f"默认 {_agent_label(project.default_agent)} 对话 {task.id} 已排队："
                f"{project.alias}。"
                "后续可直接发送普通文本继续；发送 /chat 可随时回到此对话。"
            ]
        updated = self.state.queue_message(task.id, message.sender_open_id, message.text.strip())
        assert updated is not None
        prefix = "已排队" if task.status == TaskStatus.RUNNING else "已安排继续"
        return [f"{prefix}任务 {updated.id}。"]

    def _simple(self, message: InboundMessage, command: SimpleCommand) -> list[str]:
        if command.name == "help":
            return [HELP_TEXT]
        if command.name == "model":
            return self._model(command)
        if command.name == "compact":
            task = self.state.selected_task(message.chat_id, message.sender_open_id)
            if task is None:
                return ["没有当前任务，请先发送消息创建会话，或用 /use 选择任务。"]
            if task.agent != AgentKind.CODEX:
                return ["/compact 目前仅支持 Codex，暂不支持 Qoder。"]
            if not task.session_id:
                return ["当前任务尚未创建 session，请等待首轮执行后再发送 /compact。"]
            updated = self.state.queue_message(
                task.id, message.sender_open_id, "/compact", operation="compact"
            )
            if updated is None:
                return ["当前任务无法压缩，请用 /status 检查会话状态。"]
            return [f"任务 {task.id} 的上下文压缩已排队，完成后会通知你；保留当前 session。"]
        if command.name == "chat":
            project = self.config.projects[self.config.service.default_chat_project]
            task = self.state.select_default_chat(
                message.chat_id,
                message.sender_open_id,
                project.alias,
                project.default_agent,
            )
            if task is None:
                self.state.clear_selected_task(message.chat_id)
                return [
                    f"已切换到默认 {_agent_label(project.default_agent)} 对话"
                    f"（{project.alias}），"
                    "但它尚未创建。"
                    "请直接发送第一条消息。"
                ]
            return [
                f"已切换到默认 {_agent_label(task.agent)} 对话 {task.id}"
                f"（{project.alias}）。"
                "接下来直接发送普通文本会继续它。"
            ]
        if command.name == "use":
            task = self.state.select_task(message.chat_id, command.task_id or "", message.sender_open_id)
            return [f"当前任务已切换为 {task.id}。"] if task else [
                "找不到该任务 ID，或它不属于当前会话。发送 /status 查看可用任务。"
            ]
        if command.name == "status":
            task = (
                self.state.get_task(command.task_id or "", message.chat_id)
                if command.task_id
                else self.state.selected_task(message.chat_id, message.sender_open_id)
            )
            if task is None:
                project = self.config.projects[self.config.service.default_chat_project]
                return [
                    "没有当前任务。直接发送普通文本可创建默认 "
                    f"{_agent_label(project.default_agent)} 对话，或先发送 /chat。"
                ]
            return [self._format_task(task)]
        if command.name == "stop":
            current = self.state.get_task(command.task_id or "", message.chat_id)
            if current is None or current.owner_open_id != message.sender_open_id:
                return ["找不到该任务 ID。发送 /status 查看可用任务。"]
            if current.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                return [f"任务 {current.id} 当前状态为 {current.status.value}，无需停止。"]
            task = self.state.stop_task(command.task_id or "", message.sender_open_id)
            assert task is not None
            return [f"已请求停止任务 {task.id}。"]
        if command.name == "logs":
            task = self.state.get_task(command.task_id or "", message.chat_id)
            if task is None:
                return ["找不到该任务 ID。发送 /status 查看可用任务。"]
            lines = (
                self.state.tail_sandbox_log(task.id, command.log_lines or 20)
                if command.log_source == "sandbox"
                else (
                    self.state.tail_container_log(task.id, command.log_lines or 20)
                    if command.log_source == "container"
                    else self.state.tail_log(task.id, command.log_lines or 20)
                )
            )
            return ["没有可用日志。"] if not lines else ["\n".join(lines)]
        raise AssertionError(f"unhandled command: {command.name}")

    def _model(self, command: SimpleCommand) -> list[str]:
        models = list_codex_models()
        current = self.state.get_setting("codex_model") or configured_codex_model()
        effort = self.state.get_setting("codex_reasoning_effort") or configured_reasoning_effort()
        if command.model_name is None:
            if not models:
                return ["无法读取 Codex 模型目录：请确认 model_catalog_json 指向有效目录。"]
            lines = ["可用 Codex 模型（按目录顺序编号）："]
            for index, model in enumerate(models, 1):
                label = model.slug
                if model.display_name != model.slug:
                    label += f"（{model.display_name}）"
                if model.slug == current:
                    label += "（当前）"
                levels = (
                    "、".join(model.reasoning_levels) or "不支持"
                    if model.reasoning_levels is not None else "目录未声明"
                )
                lines.append(f"{index}. {label}；思考强度：{levels}")
            lines.append(f"当前模型：{current or 'Codex 默认'}；思考强度：{effort or '模型默认'}")
            lines.append("发送 /model 1 或 /model <模型名称> 切换；/model next、/model prev 按顺序循环切换。")
            lines.append("/model 1 high 同时设置强度；/model effort high 仅设置强度；强度 default 恢复默认。")
            return ["\n".join(lines)]

        name = command.model_name
        if name == "effort":
            if not current:
                return ["请先通过 /model <编号或名称> 选择模型。"]
            name = current
        elif name in {"next", "prev"}:
            if not models:
                return ["无法读取模型目录，请先发送 /model 检查配置。"]
            index = next((i for i, model in enumerate(models) if model.slug == current), None)
            if index is None:
                index = 0 if name == "next" else len(models) - 1
            else:
                index = (index + (1 if name == "next" else -1)) % len(models)
            name = models[index].slug
        elif name.isascii() and name.isdecimal():
            if len(name) > 6 or not 1 <= int(name) <= len(models):
                return ["模型编号超出范围，请发送 /model 查看当前编号。"]
            name = models[int(name) - 1].slug
        if not _valid_model_name(name):
            return ["模型名称不能为空，且不能包含空白或控制字符。发送 /model 查看可用模型。"]
        model = next((model for model in models if model.slug == name), None)
        if models and model is None:
            return [f"未知模型：{name}。发送 /model 查看完整列表。"]
        requested = command.reasoning_effort
        levels = model.reasoning_levels if model else None
        if requested is not None and requested != "default":
            if requested not in REASONING_EFFORTS or (levels is not None and requested not in levels):
                return [f"模型 {name} 不支持思考强度 {requested}；可用：{'、'.join(levels if levels is not None else REASONING_EFFORTS) or '无'}。"]
            effort = requested
        elif requested == "default" or (levels is not None and effort not in levels):
            effort = (model.default_reasoning_level if model else None) or configured_reasoning_effort()
            if levels is not None and effort not in levels:
                effort = levels[0] if levels else None
            if requested == "default" and effort is None:
                return ["模型目录和 Codex 配置未提供可用默认强度，请通过 /model 查看并指定强度。"]
        self.state.set_settings({"codex_model": name, "codex_reasoning_effort": effort})
        return [f"已切换 Codex 模型为 {name}；思考强度：{effort or '模型默认'}；从下一轮 Codex 执行起生效，包括当前 session；正在运行的轮次不变。"]

    def _format_task(self, task: Task) -> str:
        session = task.session_id or "尚未创建"
        summary = task.last_summary or "暂无"
        task_type = (
            f"默认 {_agent_label(task.agent)} 对话"
            if task.origin.value == "chat"
            else "独立任务"
        )
        sandbox_job = self.state.latest_sandbox_job(task.id)
        sandbox_status = (
            f"{sandbox_job.status}（作业 {sandbox_job.id}）" if sandbox_job is not None else "暂无"
        )
        container_job = self.state.latest_container_job(task.id)
        container_status = (
            f"{container_job.status}（作业 {container_job.id}，"
            f"{container_job.container_name}）"
            if container_job is not None
            else "暂无"
        )
        return (
            f"任务 {task.id}\n"
            f"类型：{task_type}\n"
            f"项目：{task.project_alias}\n"
            f"Agent：{task.agent.value}\n"
            f"状态：{task.status.value}\n"
            f"沙箱：{sandbox_status}\n"
            f"容器：{container_status}\n"
            f"会话：{session}\n"
            f"最近反馈：{summary[:500]}"
        )
