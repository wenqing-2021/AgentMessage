from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_message.agents.adapters import CodexAdapter
from agent_message.agents.compact import execute_compaction
from agent_message.core.models import AgentKind, AgentResult, ClaimedRun

from tests.helpers import make_config


FAKE_APP_SERVER = '''
import json
import sys

mode = sys.argv[1]


def send(value):
    sys.stdout.write(json.dumps(value) + "\\n")
    sys.stdout.flush()


thread_id = None
pending_request = None
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method is None:
        if request_id == 99:
            rejected = isinstance(message.get("error"), dict)
            print("REQUEST-REJECTED" if rejected else "REQUEST-ACCEPTED", file=sys.stderr)
            if rejected and pending_request is not None:
                send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": "turn-1", "completedAtMs": 1, "item": {"id": "item-1", "type": "contextCompaction"}}})
                send({"id": pending_request, "result": {}})
                send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": "turn-1", "status": "completed"}}})
                pending_request = None
        continue
    params = message.get("params") or {}
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fake-app-server/1.0"}})
    elif method == "initialized":
        continue
    elif method == "thread/resume":
        thread_id = params.get("threadId")
        print("RESUME %s model=%s cwd=%s sandbox=%s approval=%s" % (thread_id, params.get("model"), params.get("cwd"), params.get("sandbox"), params.get("approvalPolicy")), file=sys.stderr)
        if mode == "resume-mismatch":
            send({"id": request_id, "result": {"thread": {"id": "other-thread"}}})
        else:
            send({"id": request_id, "result": {"thread": {"id": thread_id, "sessionId": thread_id}}})
    elif method == "thread/compact/start":
        print("COMPACT-START %s" % params.get("threadId"), file=sys.stderr)
        if mode == "error":
            send({"id": request_id, "error": {"code": -32000, "message": "compaction unavailable"}})
            continue
        if mode == "server-request":
            send({"id": 99, "method": "item/commandExecution/requestApproval", "params": {}})
            pending_request = request_id
            continue
        if mode == "turn-failed":
            send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": "turn-1", "status": "failed", "error": {"message": "boom"}}}})
            send({"id": request_id, "result": {}})
            continue
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": "turn-1", "completedAtMs": 1, "item": {"id": "item-1", "type": "contextCompaction"}}})
        send({"id": request_id, "result": {}})
        if mode != "no-turn-completed":
            send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": "turn-1", "status": "completed"}}})
'''


def make_run(root: Path, **overrides: object) -> ClaimedRun:
    config = make_config(root)
    values: dict[str, object] = {
        "run_id": 7,
        "task_id": "task-compact",
        "message_id": 3,
        "project_alias": "alpha",
        "project_path": config.projects["alpha"].path,
        "agent": AgentKind.CODEX,
        "session_id": "thread-1",
        "prompt": "/compact",
        "chat_id": "chat-1",
        "log_path": root / "logs" / "task-compact" / "run-7.jsonl",
        "operation": "compact",
    }
    values.update(overrides)
    return ClaimedRun(**values)  # type: ignore[arg-type]


class CompactionProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.script = self.root / "fake_app_server.py"
        self.script.write_text(FAKE_APP_SERVER, encoding="utf-8")
        self.pids: list[int] = []
        self.progress: list[str] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_compaction(self, mode: str, **overrides: object) -> tuple[AgentResult, str]:
        run = make_run(self.root, **overrides)

        def on_pid(pid: int) -> None:
            self.pids.append(pid)

        def on_progress(message: str) -> None:
            self.progress.append(message)

        result = asyncio.run(
            execute_compaction(
                run,
                [sys.executable, str(self.script), mode],
                {},
                on_pid,
                on_progress,
            )
        )
        log = run.log_path.read_text(encoding="utf-8", errors="replace") if run.log_path.exists() else ""
        return result, log

    def test_compaction_succeeds_and_keeps_session(self) -> None:
        result, log = self.run_compaction("ok")

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.session_id, "thread-1")
        self.assertIn("上下文压缩完成", result.final_message)
        self.assertEqual(self.pids and len(self.pids), 1)
        self.assertEqual(self.progress, ["正在压缩当前 session 的上下文。"])
        self.assertIn("COMPACT-START thread-1", log)
        self.assertIn("RESUME thread-1 model=None", log)
        self.assertIn("sandbox=workspace-write approval=never", log)
        self.assertIn('"type": "contextCompaction"', log)

    def test_compaction_resumes_with_selected_model(self) -> None:
        result, log = self.run_compaction("ok", model="kimi-code/k3")

        self.assertEqual(result.exit_code, 0)
        self.assertIn("RESUME thread-1 model=kimi-code/k3", log)
        self.assertIn("cwd=", log)

    def test_compaction_reports_server_error(self) -> None:
        result, _ = self.run_compaction("error")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("compaction unavailable", result.error or "")

    def test_compaction_reports_failed_turn(self) -> None:
        result, _ = self.run_compaction("turn-failed")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("上下文压缩未完成", result.error or "")

    def test_compaction_rejects_unexpected_session(self) -> None:
        result, _ = self.run_compaction("resume-mismatch")

        self.assertEqual(result.exit_code, 1)
        self.assertIn("session 与压缩目标不一致", result.error or "")

    def test_compaction_requires_existing_session(self) -> None:
        result, _ = self.run_compaction("ok", session_id=None)

        self.assertEqual(result.exit_code, 1)
        self.assertIn("需要已有 Codex session", result.error or "")

    def test_compaction_denies_server_requests(self) -> None:
        result, log = self.run_compaction("server-request")

        self.assertEqual(result.exit_code, 0)
        self.assertIn("REQUEST-REJECTED", log)
        self.assertNotIn("REQUEST-ACCEPTED", log)

    def test_compaction_tolerates_missing_turn_completion(self) -> None:
        with patch("agent_message.agents.compact._TURN_GRACE_SECONDS", 0.5):
            result, _ = self.run_compaction("no-turn-completed")

        self.assertEqual(result.exit_code, 0)


class CompactionDispatchTests(unittest.TestCase):
    def test_codex_adapter_routes_compaction_to_app_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = make_run(root)
            captured: dict[str, object] = {}

            async def fake_compaction(
                run_arg: ClaimedRun,
                command: list[str],
                environment: dict[str, str],
                on_pid: object,
                on_progress: object = None,
            ) -> AgentResult:
                captured["command"] = command
                captured["environment"] = environment
                captured["run"] = run_arg
                return AgentResult(0, run_arg.session_id, "done")

            with patch(
                "agent_message.agents.compact.execute_compaction",
                new=AsyncMock(side_effect=fake_compaction),
            ):
                result = asyncio.run(CodexAdapter().execute(run, lambda pid: None))

            self.assertEqual(result.exit_code, 0)
            command = captured["command"]
            assert isinstance(command, list)
            self.assertIn("app-server", command)
            self.assertNotIn("exec", command)
            self.assertIn("sandbox_workspace_write.network_access=false", command)
            self.assertEqual(captured["run"], run)


if __name__ == "__main__":
    unittest.main()
