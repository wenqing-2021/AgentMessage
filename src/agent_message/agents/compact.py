"""Run native Codex context compaction over the app-server JSON-RPC protocol."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Callable
from typing import Any

from .base import PidCallback, ProgressCallback, _ensure_reliable_child_watcher
from ..core.models import AgentResult, ClaimedRun

_COMPACT_TIMEOUT_SECONDS = 300.0
# Manual compaction normally ends with a turn/completed notification. Treat the
# compaction item itself as success if that notification never arrives.
_TURN_GRACE_SECONDS = 10.0
_READ_LIMIT_BYTES = 16 * 1024 * 1024


async def _callback(callback: Callable[..., Any] | None, value: object) -> None:
    if callback is None:
        return
    result = callback(value)
    if asyncio.iscoroutine(result):
        await result


async def execute_compaction(
    run: ClaimedRun,
    command: list[str],
    environment: dict[str, str],
    on_pid: PidCallback,
    on_progress: ProgressCallback | None = None,
    *,
    timeout: float = _COMPACT_TIMEOUT_SECONDS,
) -> AgentResult:
    """Resume the task session and ask the Codex app-server to compact it."""
    if not run.session_id:
        return AgentResult(1, None, "", "上下文压缩需要已有 Codex session。")
    session_id = run.session_id
    _ensure_reliable_child_watcher()
    run.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(run.log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as log:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=run.project_path,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=_READ_LIMIT_BYTES,
        )

        async def drain_stderr() -> None:
            assert process.stderr is not None
            while True:
                chunk = await process.stderr.read(65536)
                if not chunk:
                    return
                log.write(chunk)
                log.flush()

        stderr_task = asyncio.create_task(drain_stderr())

        async def send(value: dict[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
            await process.stdin.drain()

        async def receive() -> dict[str, Any]:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    raise RuntimeError("Codex app-server 在压缩完成前退出。")
                log.write(line)
                log.flush()
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(value, dict):
                    continue
                # Compaction needs no tools or approvals. Answer every server request
                # with an error so the child process cannot escalate through a prompt.
                if "method" in value and "id" in value:
                    await send(
                        {
                            "id": value["id"],
                            "error": {
                                "code": -32601,
                                "message": "Unsupported during AgentMessage compaction",
                            },
                        }
                    )
                    continue
                return value

        async def request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            await send({"id": request_id, "method": method, "params": params})
            while True:
                value = await receive()
                if value.get("id") != request_id:
                    continue
                if "error" in value:
                    raise RuntimeError(f"{method} 调用失败：{value['error']}")
                result = value.get("result")
                return result if isinstance(result, dict) else {}

        async def compact() -> None:
            await request(
                1,
                "initialize",
                {"clientInfo": {"name": "agent_message", "version": "1.0"}},
            )
            await send({"method": "initialized", "params": {}})
            resume_params: dict[str, Any] = {
                "threadId": session_id,
                "cwd": str(run.project_path),
                "approvalPolicy": "never",
                # The app-server enum is kebab-case; "workspaceWrite" is rejected.
                "sandbox": "workspace-write",
            }
            if run.model:
                resume_params["model"] = run.model
            resumed = await request(2, "thread/resume", resume_params)
            thread = resumed.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != session_id:
                raise RuntimeError("Codex 恢复的 session 与压缩目标不一致。")
            await _callback(on_progress, "正在压缩当前 session 的上下文。")
            # The acknowledgement can arrive after the progress notifications, so the
            # request id is matched inside the loop instead of by request().
            await send(
                {
                    "id": 3,
                    "method": "thread/compact/start",
                    "params": {"threadId": session_id},
                }
            )
            acknowledged = False
            compacted_turns: set[str] = set()
            completed_turns: set[str] = set()
            while True:
                try:
                    value = await (
                        asyncio.wait_for(receive(), _TURN_GRACE_SECONDS)
                        if compacted_turns
                        else receive()
                    )
                except asyncio.TimeoutError:
                    if acknowledged and compacted_turns:
                        return
                    raise
                if value.get("id") == 3:
                    if "error" in value:
                        raise RuntimeError(f"上下文压缩失败：{value['error']}")
                    acknowledged = True
                method = value.get("method")
                params = value.get("params")
                if not isinstance(params, dict) or params.get("threadId") != session_id:
                    continue
                if method == "item/completed":
                    item = params.get("item")
                    if isinstance(item, dict) and item.get("type") == "contextCompaction":
                        compacted_turns.add(str(params.get("turnId")))
                elif method == "thread/compacted":
                    compacted_turns.add(str(params.get("turnId")))
                elif method == "turn/completed":
                    turn = params.get("turn")
                    if not isinstance(turn, dict):
                        continue
                    if turn.get("status") != "completed":
                        raise RuntimeError(
                            f"上下文压缩未完成：{turn.get('error') or turn.get('status')}"
                        )
                    completed_turns.add(str(turn.get("id")))
                elif method == "error" and not params.get("willRetry", False):
                    raise RuntimeError(f"上下文压缩失败：{params.get('error')}")
                if acknowledged and compacted_turns & completed_turns:
                    return

        try:
            await _callback(on_pid, process.pid)
            await asyncio.wait_for(compact(), timeout=timeout)
            return AgentResult(
                0,
                session_id,
                "上下文压缩完成，已保留当前 session，可直接继续发送消息。",
            )
        except asyncio.TimeoutError:
            return AgentResult(1, session_id, "", "上下文压缩超时，请用 /logs 查看输出。")
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
            return AgentResult(1, session_id, "", str(exc))
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            await stderr_task
