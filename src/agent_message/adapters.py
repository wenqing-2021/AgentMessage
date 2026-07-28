from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Awaitable, Callable

from .models import AgentKind, AgentResult, ClaimedRun


PidCallback = Callable[[int], Awaitable[None] | None]
SessionCallback = Callable[[str], Awaitable[None] | None]
ProgressCallback = Callable[[str], Awaitable[None] | None]
_READ_CHUNK_BYTES = 64 * 1024
_MAX_PARSED_JSON_LINE_BYTES = 1024 * 1024


class AdapterError(RuntimeError):
    pass


class AgentAdapter(ABC):
    kind: AgentKind
    executable: str

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    @abstractmethod
    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        raise NotImplementedError

    def environment(self) -> dict[str, str]:
        return dict(os.environ)

    async def execute(
        self,
        run: ClaimedRun,
        on_pid: PidCallback,
        on_session: SessionCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> AgentResult:
        if not self.available():
            return AgentResult(127, run.session_id, "", f"找不到 {self.executable}，请安装并登录后重试。")
        run.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        last_message_path = run.log_path.with_suffix(".final.txt")
        command = self.build_command(run, last_message_path)
        environment = self.environment()
        # Agent commands are never executed through a shell. The prompt remains one argv element.
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=run.project_path,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        callback_result = on_pid(process.pid)
        if asyncio.iscoroutine(callback_result):
            await callback_result

        session_id = run.session_id
        last_text = ""
        fd = os.open(run.log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        parse_buffer = bytearray()
        discarding_oversized_line = False

        def process_json_line(raw_line: bytes) -> None:
            nonlocal last_text, session_id
            decoded = raw_line.decode("utf-8", errors="replace").rstrip("\r")
            parsed = _parse_json_line(decoded)
            if parsed is None:
                return
            discovered_session = _extract_session_id(parsed)
            if discovered_session and discovered_session != session_id:
                session_id = discovered_session
                if on_session:
                    session_callback = on_session(session_id)
                    if asyncio.iscoroutine(session_callback):
                        awaitable_callbacks.append(session_callback)
            progress = _extract_progress(parsed)
            if progress and on_progress:
                progress_callback = on_progress(progress)
                if asyncio.iscoroutine(progress_callback):
                    awaitable_callbacks.append(progress_callback)
            last_text = _extract_text(parsed) or last_text

        awaitable_callbacks: list[Awaitable[None]] = []

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
            final_message = last_message_path.read_text(encoding="utf-8", errors="replace").strip()
        else:
            final_message = last_text.strip()
        if not final_message:
            final_message = "Agent 未返回最终文本；请用 /logs 查看本地输出。"
        error = None if exit_code == 0 else f"{self.kind.value} 退出码：{exit_code}"
        return AgentResult(exit_code, session_id, final_message, error)


class CodexAdapter(AgentAdapter):
    kind = AgentKind.CODEX
    executable = "codex"

    def __init__(self, tool_network_enabled: bool = False) -> None:
        self.tool_network_enabled = tool_network_enabled

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        common = [
            self.executable,
            "-c",
            "sandbox_workspace_write.network_access="
            + ("true" if self.tool_network_enabled else "false"),
            "exec",
        ]
        output = ["--json", "--output-last-message", str(last_message_path)]
        if run.session_id:
            # Resume retains the original Codex thread and its workspace context.
            return [
                *common,
                "resume",
                "--skip-git-repo-check",
                *output,
                run.session_id,
                run.prompt,
            ]
        return [
            *common,
            *output,
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-C",
            str(run.project_path),
            run.prompt,
        ]


class QoderAdapter(AgentAdapter):
    kind = AgentKind.QODER
    executable = "qodercli"

    _blocked_rules = ",".join(
        [
            "Bash(sudo:*)",
            "Bash(rm -rf:*)",
            "Bash(git reset --hard:*)",
            "Bash(git clean:*)",
            "Bash(git push:*)",
            "Bash(npm publish:*)",
            "Bash(twine upload:*)",
            "Bash(curl:*)",
            "Bash(wget:*)",
            "Bash(ssh:*)",
            "WebFetch",
            "WebSearch",
            "Agent",
            "mcp__*",
        ]
    )

    def build_command(self, run: ClaimedRun, last_message_path: Path) -> list[str]:
        # Qoder's print/stream JSON mode is contract-checked by `agent-message doctor`.
        command = [
            self.executable,
            "-w",
            str(run.project_path),
            "-p",
            run.prompt,
            "--output-format",
            "stream-json",
            "--permission-mode",
            "accept_edits",
            "--tools",
            "Read,Grep,Glob,Edit,Write,Bash",
            "--disallowed-tools",
            self._blocked_rules,
        ]
        if run.session_id:
            command.extend(["-r", run.session_id])
        return command

    def environment(self) -> dict[str, str]:
        environment = super().environment()
        installed_prefix = Path(sys.executable).with_name("agent-message-qoder-shell")
        prefix = str(installed_prefix) if installed_prefix.is_file() else shutil.which("agent-message-qoder-shell")
        if prefix:
            # The wrapper puts each generated Bash command in a fresh network namespace.
            environment["QODER_SHELL_PREFIX"] = prefix
        return environment


def adapter_for(kind: AgentKind, *, codex_tool_network: bool = False) -> AgentAdapter:
    return CodexAdapter(codex_tool_network) if kind == AgentKind.CODEX else QoderAdapter()


def _parse_json_line(line: str) -> dict[str, object] | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


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


def _extract_progress(value: object) -> str | None:
    """Return safe, user-facing lifecycle updates; never expose reasoning or tool output."""
    if not isinstance(value, dict):
        return None
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
