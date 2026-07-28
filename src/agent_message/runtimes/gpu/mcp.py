"""MCP server exposing the project-scoped GPU execution tool."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any

from ...core.config import ConfigError, load_config
from ...core.state import StateStore
from .policy import GPU_INSTRUCTIONS
from .runner import GpuRunnerError, run_bubblewrap


SERVER_NAME = "agent-message-bwrap-gpu"
SERVER_VERSION = "0.1.0"
INSTRUCTIONS = GPU_INSTRUCTIONS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-message-gpu-mcp")
    parser.add_argument("--config", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-id", type=int)
    return parser


class GpuMcpServer:
    def __init__(self, config_path: str, task_id: str, run_id: int | None) -> None:
        self.config = load_config(config_path)
        self.state = StateStore(self.config)
        self.task = self.state.get_task(task_id)
        if self.task is None:
            raise ConfigError(f"unknown task ID: {task_id}")
        self.project = self.config.projects[self.task.project_alias]
        if self.project.gpu is None or not self.project.gpu.enabled:
            raise ConfigError(f"GPU runner is not enabled for project {self.project.alias}")
        self.task_id = task_id
        self.run_id = run_id
        self._write_lock = threading.Lock()
        self._tool_lock = threading.Lock()
        self._requests_lock = threading.Lock()
        self._cancellations: dict[object, threading.Event] = {}
        self._workers: list[threading.Thread] = []

    def _write(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()

    def _result(self, request_id: object, result: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: object, code: int, message: str) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message[:2000]},
            }
        )

    def _tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "gpu_run",
                "description": (
                    "Run an arbitrary command inside the current project's isolated WSL GPU "
                    "bubblewrap sandbox. Pass the executable and each argument separately."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                            "description": "Command argv without shell concatenation.",
                        },
                        "cwd": {
                            "type": "string",
                            "default": ".",
                            "description": "Working directory relative to the configured project.",
                        },
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": bool(self.project.gpu and self.project.gpu.network),
                },
            }
        ]

    def _tool_response(
        self, request_id: object, payload: dict[str, Any], *, is_error: bool
    ) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self._result(
            request_id,
            {
                "content": [{"type": "text", "text": text}],
                "structuredContent": payload,
                "isError": is_error,
            },
        )

    def _run_tool(self, request_id: object, arguments: dict[str, Any]) -> None:
        cancel = threading.Event()
        with self._requests_lock:
            self._cancellations[request_id] = cancel
        acquired = self._tool_lock.acquire(blocking=False)
        if not acquired:
            self._tool_response(
                request_id,
                {"error": "another GPU command is already running in this agent process"},
                is_error=True,
            )
            with self._requests_lock:
                self._cancellations.pop(request_id, None)
            return

        job = None
        try:
            argv = arguments.get("argv")
            cwd = arguments.get("cwd", ".")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(item, str) and item for item in argv)
            ):
                raise GpuRunnerError("argv must be a non-empty array of non-empty strings")
            if not isinstance(cwd, str):
                raise GpuRunnerError("cwd must be a string")
            job = self.state.create_gpu_job(
                task_id=self.task_id,
                run_id=self.run_id,
                argv=argv,
                cwd=cwd,
            )
            result = run_bubblewrap(
                self.project,
                argv,
                cwd,
                job.log_path,
                on_pid=lambda pid: self.state.set_gpu_job_pid(job.id, pid),
                cancel_event=cancel,
            )
            final = self.state.finish_gpu_job(
                job.id,
                exit_code=result.exit_code,
                error=result.error,
                cancelled=result.cancelled,
            )
            payload = {
                "job_id": final.id,
                "status": final.status,
                "exit_code": result.exit_code,
                "duration_seconds": round(result.duration_seconds, 3),
                "tail": result.tail,
            }
            if result.error:
                payload["error"] = result.error
            self._tool_response(request_id, payload, is_error=result.exit_code != 0)
        except (GpuRunnerError, ConfigError, ValueError) as exc:
            if job is not None:
                self.state.finish_gpu_job(job.id, exit_code=1, error=str(exc))
            self._tool_response(request_id, {"error": str(exc)}, is_error=True)
        except Exception as exc:
            if job is not None:
                self.state.finish_gpu_job(job.id, exit_code=1, error=f"unexpected error: {exc}")
            self._tool_response(
                request_id, {"error": f"unexpected GPU runner error: {exc}"}, is_error=True
            )
        finally:
            self._tool_lock.release()
            with self._requests_lock:
                self._cancellations.pop(request_id, None)

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params")
        if method == "initialize" and "id" in message:
            protocol = params.get("protocolVersion") if isinstance(params, dict) else None
            self._result(
                request_id,
                {
                    "protocolVersion": protocol or "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": INSTRUCTIONS,
                },
            )
            return
        if method == "ping" and "id" in message:
            self._result(request_id, {})
            return
        if method == "tools/list" and "id" in message:
            self._result(request_id, {"tools": self._tools()})
            return
        if method == "tools/call" and "id" in message:
            if not isinstance(params, dict) or params.get("name") != "gpu_run":
                self._error(request_id, -32602, "unknown tool")
                return
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                self._error(request_id, -32602, "tool arguments must be an object")
                return
            worker = threading.Thread(
                target=self._run_tool,
                args=(request_id, arguments),
                name=f"agent-message-gpu-{request_id}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()
            return
        if method == "notifications/cancelled":
            if isinstance(params, dict):
                cancelled_id = params.get("requestId")
                with self._requests_lock:
                    event = self._cancellations.get(cancelled_id)
                if event:
                    event.set()
            return
        if "id" in message:
            self._error(request_id, -32601, f"method not found: {method}")

    def run(self) -> None:
        try:
            for line in sys.stdin:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        finally:
            with self._requests_lock:
                for event in self._cancellations.values():
                    event.set()
            for worker in self._workers:
                worker.join(timeout=10)
            self.state.close()


def main() -> None:
    args = _parser().parse_args()
    try:
        GpuMcpServer(args.config, args.task_id, args.run_id).run()
    except (ConfigError, OSError, ValueError) as exc:
        print(f"agent-message GPU MCP failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
