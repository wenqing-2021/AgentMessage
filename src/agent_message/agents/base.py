"""Shared subprocess lifecycle for headless coding-agent adapters."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ..core.models import AgentKind, AgentResult, ClaimedRun, Task
from ..core.session_sync import SYNC_HINT


PidCallback = Callable[[int], Awaitable[None] | None]
SessionCallback = Callable[[str], Awaitable[None] | None]
ProgressCallback = Callable[[str], Awaitable[None] | None]
_READ_CHUNK_BYTES = 64 * 1024
_MAX_PARSED_JSON_LINE_BYTES = 1024 * 1024
_FEISHU_ENV_PREFIX = "AGENT_MESSAGE_FEISHU_"
_SERVICE_ONLY_ENV_KEYS = frozenset({"AGENT_MESSAGE_ALLOWED_OPEN_IDS"})

# Appended to every agent prompt: sending a file is an explicit script call, so the
# agent gets a synchronous result instead of relying on output markers.
FILE_SEND_HINT = (
    "\n\n如需把项目内的图片或文档发送给飞书用户，请运行项目内的脚本："
    "sh .agent-message/bin/send-to-feishu <项目内相对路径>"
    "（例如 sh .agent-message/bin/send-to-feishu reports/result.png）。"
    "脚本会立即上传并返回结果：输出「已发送」表示成功，非零退出并输出原因表示失败，"
    "失败时请把原因告诉用户。只允许项目内文件，图片不超过 10MB，其他文件不超过 30MB；"
    "需要发送多个文件时逐个调用。若本项目要求通过 sandbox_run 或 container_run "
    "执行项目命令，请用同样的方式调用该脚本。"
) + SYNC_HINT


class AdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class ParsedAgentEvent:
    """Agent-neutral information extracted from one JSON stream event."""

    session_id: str | None = None
    progress: str | None = None
    final_message: str | None = None
    terminal: bool = False
    error: str | None = None


def _ensure_reliable_child_watcher() -> None:
    """Avoid Python 3.10 ThreadedChildWatcher stalls in nested WSL/PID namespaces."""
    if sys.platform == "win32" or threading.current_thread() is not threading.main_thread():
        return
    policy = asyncio.get_event_loop_policy()
    watcher = policy.get_child_watcher()
    if isinstance(watcher, asyncio.ThreadedChildWatcher):
        replacement = asyncio.SafeChildWatcher()
        replacement.attach_loop(asyncio.get_running_loop())
        policy.set_child_watcher(replacement)


def parse_json_line(line: str) -> dict[str, object] | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class AgentAdapter(ABC):
    kind: AgentKind
    executable: str
    requires_terminal_event = False

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    @abstractmethod
    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        raise NotImplementedError

    def build_terminal_resume_command(self, task: Task) -> list[str]:
        raise AdapterError(f"{self.kind.value} 不支持终端恢复。")

    def environment(self) -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(_FEISHU_ENV_PREFIX)
            and key not in _SERVICE_ONLY_ENV_KEYS
        }

    def parse_event(self, value: dict[str, object]) -> ParsedAgentEvent:
        """Parse one CLI event. Concrete adapters own their protocol semantics."""
        return ParsedAgentEvent()

    async def execute(
        self,
        run: ClaimedRun,
        on_pid: PidCallback,
        on_session: SessionCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AgentResult:
        if not self.available():
            return AgentResult(
                127,
                run.session_id,
                "",
                f"找不到 {self.executable}，请安装并登录后重试。",
            )
        _ensure_reliable_child_watcher()
        run.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        last_message_path = run.log_path.with_suffix(".final.txt")
        command = self.build_command(run, last_message_path)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=run.project_path,
            env=self.environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        callback_result = on_pid(process.pid)
        if asyncio.iscoroutine(callback_result):
            await callback_result

        session_id = run.session_id
        last_text = ""
        terminal_seen = False
        terminal_error: str | None = None
        protocol_error: str | None = None
        fd = os.open(run.log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        parse_buffer = bytearray()
        discarding_oversized_line = False
        awaitable_callbacks: list[Awaitable[None]] = []

        def process_json_line(raw_line: bytes) -> None:
            nonlocal last_text, protocol_error, session_id, terminal_error, terminal_seen
            decoded = raw_line.decode("utf-8", errors="replace").rstrip("\r")
            parsed = parse_json_line(decoded)
            if parsed is None:
                return
            try:
                event = self.parse_event(parsed)
            except Exception as exc:  # A malformed event must not crash the worker loop.
                protocol_error = protocol_error or f"{self.kind.value} 事件解析失败：{exc}"
                return
            if event.session_id:
                if session_id is not None and event.session_id != session_id:
                    protocol_error = (
                        protocol_error
                        or f"{self.kind.value} 协议错误：同一次执行返回了不同的会话 ID。"
                    )
                elif session_id is None:
                    session_id = event.session_id
                    if on_session:
                        session_callback = on_session(session_id)
                        if asyncio.iscoroutine(session_callback):
                            awaitable_callbacks.append(session_callback)
            if event.progress and on_progress:
                progress_callback = on_progress(event.progress)
                if asyncio.iscoroutine(progress_callback):
                    awaitable_callbacks.append(progress_callback)
            if event.final_message:
                last_text = event.final_message
            if event.terminal:
                if terminal_seen:
                    protocol_error = (
                        protocol_error
                        or f"{self.kind.value} 协议错误：收到多个终止事件。"
                    )
                terminal_seen = True
                terminal_error = event.error

        with os.fdopen(fd, "wb") as log:
            assert process.stdout is not None
            while True:
                chunk = await process.stdout.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                log.write(chunk)
                log.flush()
                remaining = chunk
                while remaining:
                    if discarding_oversized_line:
                        newline = remaining.find(b"\n")
                        if newline < 0:
                            break
                        discarding_oversized_line = False
                        remaining = remaining[newline + 1 :]
                        continue

                    newline = remaining.find(b"\n")
                    if newline < 0:
                        if len(parse_buffer) + len(remaining) > _MAX_PARSED_JSON_LINE_BYTES:
                            parse_buffer.clear()
                            discarding_oversized_line = True
                        else:
                            parse_buffer.extend(remaining)
                        break

                    if len(parse_buffer) + newline <= _MAX_PARSED_JSON_LINE_BYTES:
                        parse_buffer.extend(remaining[:newline])
                        process_json_line(bytes(parse_buffer))
                    parse_buffer.clear()
                    remaining = remaining[newline + 1 :]
                    while awaitable_callbacks:
                        await awaitable_callbacks.pop(0)

            if parse_buffer and not discarding_oversized_line:
                process_json_line(bytes(parse_buffer))
            while awaitable_callbacks:
                await awaitable_callbacks.pop(0)
        exit_code = await process.wait()

        if last_message_path.is_file():
            final_message = last_message_path.read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        else:
            final_message = last_text.strip()
        if not final_message:
            final_message = "Agent 未返回最终文本；请用 /logs 查看本地输出。"

        error = protocol_error
        if error is None and terminal_error:
            error = terminal_error
        if error is None and exit_code != 0:
            error = f"{self.kind.value} 退出码：{exit_code}"
        if error is None and self.requires_terminal_event and not terminal_seen:
            error = f"{self.kind.value} 未返回 result 终止事件；请用 /logs 查看本地输出。"
        return AgentResult(exit_code, session_id, final_message, error)
