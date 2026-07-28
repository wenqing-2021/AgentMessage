from __future__ import annotations

from .commands import HELP_TEXT, CommandError, NewTaskCommand, SimpleCommand, parse_command
from .config import AppConfig
from .models import AgentKind, InboundMessage, Task, TaskOrigin, TaskStatus
from .state import StateStore


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
                agent=AgentKind.CODEX,
                chat_id=message.chat_id,
                owner_open_id=message.sender_open_id,
                prompt=message.text.strip(),
                origin=TaskOrigin.CHAT,
            )
            return [
                f"默认 Codex 对话 {task.id} 已排队：{project.alias}。"
                "后续可直接发送普通文本继续；发送 /chat 可随时回到此对话。"
            ]
        updated = self.state.queue_message(task.id, message.sender_open_id, message.text.strip())
        assert updated is not None
        prefix = "已排队" if task.status == TaskStatus.RUNNING else "已安排继续"
        return [f"{prefix}任务 {updated.id}。"]

    def _simple(self, message: InboundMessage, command: SimpleCommand) -> list[str]:
        if command.name == "help":
            return [HELP_TEXT]
        if command.name == "chat":
            project = self.config.projects[self.config.service.default_chat_project]
            task = self.state.select_default_chat(
                message.chat_id, message.sender_open_id, project.alias
            )
            if task is None:
                self.state.clear_selected_task(message.chat_id)
                return [
                    f"已切换到默认 Codex 对话（{project.alias}），但它尚未创建。"
                    "请直接发送第一条消息。"
                ]
            return [
                f"已切换到默认 Codex 对话 {task.id}（{project.alias}）。"
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
                return ["没有当前任务。直接发送普通文本可创建默认 Codex 对话，或先发送 /chat。"]
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
            lines = self.state.tail_log(task.id, command.log_lines or 20)
            return ["没有可用日志。"] if not lines else ["\n".join(lines)]
        raise AssertionError(f"unhandled command: {command.name}")

    @staticmethod
    def _format_task(task: Task) -> str:
        session = task.session_id or "尚未创建"
        summary = task.last_summary or "暂无"
        task_type = "默认 Codex 对话" if task.origin.value == "chat" else "独立任务"
        return (
            f"任务 {task.id}\n"
            f"类型：{task_type}\n"
            f"项目：{task.project_alias}\n"
            f"Agent：{task.agent.value}\n"
            f"状态：{task.status.value}\n"
            f"会话：{session}\n"
            f"最近反馈：{summary[:500]}"
        )
