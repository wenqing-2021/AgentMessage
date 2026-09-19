from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from agent_message.agents.model_catalog import CodexModel
from agent_message.core.models import AgentKind, InboundMessage, TaskOrigin, TaskStatus
from agent_message.core.state import StateStore
from agent_message.orchestration.commands import CommandError, parse_command
from agent_message.orchestration.router import MessageRouter

from tests.helpers import inbound, make_config


class RouterAndStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(Path(self.temp.name), ("alpha", "beta"))
        self.state = StateStore(self.config)
        self.state.authorize("ou-1")
        self.router = MessageRouter(self.config, self.state)

    def tearDown(self) -> None:
        self.state.close()
        self.temp.cleanup()

    def test_new_task_deduplicates_and_selects_context(self) -> None:
        reply = self.router.handle(inbound("/new alpha inspect the project"))
        self.assertIn("已排队", reply[0])
        self.assertEqual(self.router.handle(inbound("/new alpha inspect the project")), [])
        task = self.state.selected_task("chat-1", "ou-1")
        assert task is not None
        self.assertEqual(task.project_alias, "alpha")
        self.assertEqual(task.agent, AgentKind.CODEX)

    def test_plain_text_queues_after_running_message_and_reuses_session(self) -> None:
        self.router.handle(inbound("/new alpha first", "e1", "m1"))
        task = self.state.selected_task("chat-1", "ou-1")
        assert task is not None
        first = self.state.claimed_run()
        assert first is not None
        self.assertEqual(first.task_id, task.id)
        self.router.handle(inbound("then add tests", "e2", "m2"))
        updated = self.state.get_task(task.id)
        assert updated is not None
        self.assertEqual(updated.status, TaskStatus.RUNNING)
        self.state.finish_run(
            run_id=first.run_id,
            task_id=task.id,
            exit_code=0,
            session_id="thread-1",
            final_message="first done",
            error=None,
        )
        second = self.state.claimed_run()
        assert second is not None
        self.assertEqual(second.session_id, "thread-1")
        self.assertEqual(second.prompt, "then add tests")

    def test_plain_text_creates_persistent_default_codex_chat(self) -> None:
        reply = self.router.handle(inbound("inspect the current project", "e1", "m1"))
        self.assertIn("默认 Codex 对话", reply[0])
        task = self.state.selected_task("chat-1", "ou-1")
        assert task is not None
        self.assertEqual(task.project_alias, "alpha")
        self.assertEqual(task.agent, AgentKind.CODEX)
        self.assertEqual(task.origin, TaskOrigin.CHAT)

        first = self.state.claimed_run()
        assert first is not None
        self.state.finish_run(
            run_id=first.run_id,
            task_id=task.id,
            exit_code=0,
            session_id="default-thread",
            final_message="done",
            error=None,
        )
        self.router.handle(inbound("now explain the result", "e2", "m2"))
        second = self.state.claimed_run()
        assert second is not None
        self.assertEqual(second.task_id, task.id)
        self.assertEqual(second.session_id, "default-thread")

    def test_qoder_only_project_creates_and_selects_default_qoder_chat(self) -> None:
        self.state.close()
        self.config = make_config(
            Path(self.temp.name),
            qoder_only_projects=("alpha",),
        )
        self.state = StateStore(self.config)
        self.state.authorize("ou-1")
        self.router = MessageRouter(self.config, self.state)

        reply = self.router.handle(inbound("hello qoder", "q1", "qm1"))
        self.assertIn("默认 Qoder 对话", reply[0])
        task = self.state.selected_task("chat-1", "ou-1")
        assert task is not None
        self.assertEqual(task.agent, AgentKind.QODER)
        self.assertEqual(task.origin, TaskOrigin.CHAT)

        self.router.handle(inbound("/new alpha separate", "q2", "qm2"))
        reply = self.router.handle(inbound("/chat", "q3", "qm3"))
        self.assertIn(task.id, reply[0])
        self.assertIn("Qoder", reply[0])

    def test_chat_returns_to_default_chat_after_new_task(self) -> None:
        self.router.handle(inbound("start the long-lived chat", "e1", "m1"))
        default_chat = self.state.selected_task("chat-1", "ou-1")
        assert default_chat is not None
        self.router.handle(inbound("/new beta independent work", "e2", "m2"))
        selected = self.state.selected_task("chat-1", "ou-1")
        assert selected is not None
        self.assertNotEqual(selected.id, default_chat.id)

        reply = self.router.handle(inbound("/chat", "e3", "m3"))
        self.assertIn(default_chat.id, reply[0])
        selected = self.state.selected_task("chat-1", "ou-1")
        assert selected is not None
        self.assertEqual(selected.id, default_chat.id)

    def test_chat_before_first_message_prepares_default_chat(self) -> None:
        reply = self.router.handle(inbound("/chat", "e1", "m1"))
        self.assertIn("尚未创建", reply[0])
        self.assertIsNone(self.state.selected_task("chat-1", "ou-1"))
        self.router.handle(inbound("hello codex", "e2", "m2"))
        task = self.state.selected_task("chat-1", "ou-1")
        assert task is not None
        self.assertEqual(task.origin, TaskOrigin.CHAT)

    def test_help_and_missing_task_errors_explain_next_action(self) -> None:
        help_reply = self.router.handle(inbound("/help", "e1", "m1"))
        self.assertIn("项目别名", help_reply[0])
        self.assertIn("任务 ID", help_reply[0])
        self.assertIn("/chat", help_reply[0])
        use_reply = self.router.handle(inbound("/use does-not-exist", "e2", "m2"))
        self.assertIn("/status", use_reply[0])

    def test_same_project_waits_but_different_project_can_run(self) -> None:
        self.router.handle(inbound("/new alpha first", "e1", "m1"))
        first = self.state.claimed_run()
        assert first is not None
        self.router.handle(inbound("/new alpha second", "e2", "m2"))
        self.router.handle(inbound("/new beta parallel", "e3", "m3"))
        parallel = self.state.claimed_run()
        assert parallel is not None
        self.assertEqual(parallel.project_alias, "beta")
        self.assertIsNone(self.state.claimed_run())
        self.state.finish_run(
            run_id=first.run_id,
            task_id=first.task_id,
            exit_code=0,
            session_id="thread-1",
            final_message="done",
            error=None,
        )
        waiting = self.state.claimed_run()
        assert waiting is not None
        self.assertEqual(waiting.project_alias, "alpha")
        self.assertEqual(waiting.prompt, "second")

    def test_untrusted_sender_is_silent_and_event_is_deduplicated(self) -> None:
        message = inbound("/new alpha nope", "e1", "m1")
        untrusted = message.__class__(
            message.event_id,
            message.message_id,
            message.chat_id,
            message.chat_type,
            "ou-untrusted",
            message.text,
        )
        self.assertEqual(self.router.handle(untrusted), [])
        self.state.authorize("ou-untrusted")
        self.assertEqual(self.router.handle(untrusted), [])

    def test_recovery_marks_running_task_interrupted(self) -> None:
        self.router.handle(inbound("/new alpha first"))
        claimed = self.state.claimed_run()
        assert claimed is not None
        self.state.set_task_session(claimed.task_id, "thread-before-restart")
        self.router.handle(inbound("continue after this", "e2", "m2"))
        recovered = self.state.recover_interrupted()
        self.assertEqual([task.id for task in recovered], [claimed.task_id])
        task = self.state.get_task(claimed.task_id)
        assert task is not None
        self.assertEqual(task.status, TaskStatus.INTERRUPTED)
        self.assertEqual(task.session_id, "thread-before-restart")
        self.assertIsNone(self.state.claimed_run())

    def test_default_chat_mapping_survives_state_reopen(self) -> None:
        self.router.handle(inbound("remember this chat", "e1", "m1"))
        task = self.state.selected_task("chat-1", "ou-1")
        assert task is not None
        self.state.close()
        self.state = StateStore(self.config)
        self.router = MessageRouter(self.config, self.state)
        restored = self.state.select_default_chat(
            "chat-1", "ou-1", "alpha", AgentKind.CODEX
        )
        assert restored is not None
        self.assertEqual(restored.id, task.id)

    def test_migrates_existing_database_with_task_origin_and_chat_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp))
            config.service.state_dir.mkdir(parents=True)
            database = config.service.state_dir / "agent-message.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    project_alias TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    owner_open_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_summary TEXT
                );
                CREATE TABLE chat_context (
                    chat_id TEXT PRIMARY KEY,
                    selected_task_id TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO tasks(
                    id, project_alias, agent, chat_id, owner_open_id, status, created_at, updated_at
                ) VALUES ('legacy', 'alpha', 'codex', 'chat-1', 'ou-1', 'succeeded', 'now', 'now')
                """
            )
            connection.commit()
            connection.close()

            state = StateStore(config)
            try:
                task = state.get_task("legacy")
                assert task is not None
                self.assertEqual(task.origin, TaskOrigin.TASK)
                columns = {
                    row["name"]
                    for row in state._connection.execute("PRAGMA table_info(chat_context)").fetchall()
                }
                self.assertIn("default_chat_task_id", columns)
                message_columns = {
                    row["name"]
                    for row in state._connection.execute(
                        "PRAGMA table_info(task_messages)"
                    ).fetchall()
                }
                self.assertIn("operation", message_columns)
                gpu_table = state._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sandbox_jobs'"
                ).fetchone()
                self.assertIsNotNone(gpu_table)
                container_table = state._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'container_jobs'"
                ).fetchone()
                self.assertIsNotNone(container_table)
            finally:
                state.close()


class CompactCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(
            Path(self.temp.name), ("alpha", "beta"), qoder_only_projects=("beta",)
        )
        self.state = StateStore(self.config)
        self.state.authorize("ou-1")
        self.router = MessageRouter(self.config, self.state)

    def tearDown(self) -> None:
        self.state.close()
        self.temp.cleanup()

    def start_session(self, project: str = "alpha") -> str:
        self.router.handle(inbound(f"/new {project} start work", "e-new", "m-new"))
        task = self.state.selected_task("chat-1", "ou-1")
        assert task is not None
        run = self.state.claimed_run()
        assert run is not None
        self.state.finish_run(
            run_id=run.run_id,
            task_id=run.task_id,
            exit_code=0,
            session_id="thread-compact",
            final_message="done",
            error=None,
        )
        return task.id

    def test_compact_rejects_arguments(self) -> None:
        with self.assertRaises(CommandError):
            parse_command("/compact now")

    def test_compact_requires_current_task(self) -> None:
        reply = self.router.handle(inbound("/compact", "e1", "m1"))

        self.assertIn("没有当前任务", reply[0])

    def test_compact_requires_existing_session(self) -> None:
        self.router.handle(inbound("/new alpha start work", "e1", "m1"))
        reply = self.router.handle(inbound("/compact", "e2", "m2"))

        self.assertIn("尚未创建 session", reply[0])

    def test_compact_rejects_qoder_task(self) -> None:
        task_id = self.start_session("beta")
        task = self.state.get_task(task_id)
        assert task is not None
        self.assertEqual(task.agent, AgentKind.QODER)

        reply = self.router.handle(inbound("/compact", "e2", "m2"))

        self.assertIn("仅支持 Codex", reply[0])
        self.assertIsNone(
            self.state.queue_message(task_id, "ou-1", "/compact", operation="compact")
        )

    def test_compact_queues_operation_for_selected_session(self) -> None:
        task_id = self.start_session()
        reply = self.router.handle(inbound("/compact", "e2", "m2"))

        self.assertIn("上下文压缩已排队", reply[0])
        run = self.state.claimed_run()
        assert run is not None
        self.assertEqual(run.task_id, task_id)
        self.assertEqual(run.operation, "compact")
        self.assertEqual(run.session_id, "thread-compact")

    def test_compact_queues_behind_running_turn(self) -> None:
        task_id = self.start_session()
        self.router.handle(inbound("keep going", "e2", "m2"))
        running = self.state.claimed_run()
        assert running is not None
        reply = self.router.handle(inbound("/compact", "e3", "m3"))

        self.assertIn("已排队", reply[0])
        self.assertIsNone(self.state.claimed_run())
        self.state.finish_run(
            run_id=running.run_id,
            task_id=running.task_id,
            exit_code=0,
            session_id="thread-compact",
            final_message="done",
            error=None,
        )
        compaction = self.state.claimed_run()
        assert compaction is not None
        self.assertEqual(compaction.task_id, task_id)
        self.assertEqual(compaction.operation, "compact")


class ModelCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(Path(self.temp.name), ("alpha",))
        self.state = StateStore(self.config)
        self.state.authorize("ou-1")
        self.router = MessageRouter(self.config, self.state)

    def tearDown(self) -> None:
        self.state.close()
        self.temp.cleanup()

    def test_numbered_models_effort_and_wraparound(self) -> None:
        models = [CodexModel("first", "First", ("low", "high"), "low"),
                  CodexModel("second", "Second", ("medium",), "medium")]
        with patch("agent_message.orchestration.router.list_codex_models", return_value=models):
            listed = self.router.handle(inbound("/model", "list", "list"))[0]
            self.assertIn("1. first", listed)
            self.assertIn("2. second", listed)
            for index, (command, model, effort) in enumerate([
                ("/model 1 high", "first", "high"),
                ("/model next", "second", "medium"),
                ("/model next", "first", "low"),
                ("/model prev", "second", "medium"),
                ("/model 1 high", "first", "high"),
                ("/model effort default", "first", "low"),
                ("/model effort high", "first", "high"),
            ]):
                self.router.handle(inbound(command, str(index), str(index)))
                self.assertEqual(self.state.get_setting("codex_model"), model)
                self.assertEqual(self.state.get_setting("codex_reasoning_effort"), effort)
            self.state.close()
            self.state = StateStore(self.config)
            self.assertEqual(self.state.get_setting("codex_reasoning_effort"), "high")

    def test_invalid_model_selection_does_not_change_settings(self) -> None:
        self.state.set_settings({"codex_model": "first", "codex_reasoning_effort": "low"})
        with patch("agent_message.orchestration.router.list_codex_models", return_value=[
            CodexModel("first", "First", ("low",), "low")
        ]):
            for index, command in enumerate([
                "/model 0", "/model 2", "/model " + "9" * 5000,
                "/model 1 high", "/model effort wrong", "/model effort",
                '/model "bad\x01name"', "/model 1 low extra",
            ]):
                self.router.handle(inbound(command, str(index), str(index)))
                self.assertEqual(self.state.get_setting("codex_model"), "first")
                self.assertEqual(self.state.get_setting("codex_reasoning_effort"), "low")

    def test_default_effort_without_metadata_does_not_silently_reuse_session_effort(self) -> None:
        self.state.set_settings({"codex_model": "first", "codex_reasoning_effort": "high"})
        with patch("agent_message.orchestration.router.list_codex_models", return_value=[
            CodexModel("first", "First")
        ]), patch("agent_message.orchestration.router.configured_reasoning_effort", return_value=None):
            reply = self.router.handle(inbound("/model effort default"))
        self.assertIn("未提供可用默认强度", reply[0])
        self.assertEqual(self.state.get_setting("codex_reasoning_effort"), "high")

    def test_model_switch_authorization_and_duplicate_event(self) -> None:
        self.state.set_setting("codex_model", "first")
        with patch("agent_message.orchestration.router.list_codex_models", return_value=[
            CodexModel("first", "First"), CodexModel("second", "Second")
        ]):
            self.router.handle(inbound("/model next"))
            self.assertEqual(self.state.get_setting("codex_model"), "second")
            self.assertEqual(self.router.handle(inbound("/model next")), [])
            self.assertEqual(self.state.get_setting("codex_model"), "second")
            with patch.object(self.state, "is_authorized", return_value=False):
                self.assertEqual(self.router.handle(inbound("/model prev", "e2", "m2")), [])
            self.assertEqual(self.state.get_setting("codex_model"), "second")

    def test_model_lists_available_models(self) -> None:
        models = [
            CodexModel("gpt-5.6-sol", "GPT-5.6-Sol"),
            CodexModel("deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro"),
        ]
        with patch(
            "agent_message.orchestration.router.list_codex_models", return_value=models
        ), patch(
            "agent_message.orchestration.router.configured_codex_model",
            return_value="deepseek/deepseek-v4-pro",
        ):
            reply = self.router.handle(inbound("/model"))
        self.assertIn("deepseek/deepseek-v4-pro", reply[0])
        self.assertIn("（当前）", reply[0])

    def test_model_switches_and_persists(self) -> None:
        with patch(
            "agent_message.orchestration.router.list_codex_models",
            return_value=[CodexModel("gpt-5.6-sol", "GPT-5.6-Sol")],
        ):
            reply = self.router.handle(inbound("/model gpt-5.6-sol"))
        self.assertIn("已切换 Codex 模型", reply[0])
        self.assertEqual(self.state.get_setting("codex_model"), "gpt-5.6-sol")

    def test_model_rejects_unknown_name(self) -> None:
        with patch(
            "agent_message.orchestration.router.list_codex_models",
            return_value=[CodexModel("gpt-5.6-sol", "GPT-5.6-Sol")],
        ):
            reply = self.router.handle(inbound("/model not-a-real-model"))
        self.assertIn("未知模型", reply[0])
        self.assertIsNone(self.state.get_setting("codex_model"))

    def test_model_unavailable_catalog(self) -> None:
        with patch("agent_message.orchestration.router.list_codex_models", return_value=[]):
            reply = self.router.handle(inbound("/model"))
        self.assertIn("无法读取 Codex 模型目录", reply[0])

    def test_claimed_run_includes_selected_model(self) -> None:
        self.state.set_setting("codex_model", "deepseek/deepseek-v4-pro")
        self.router.handle(inbound("hello", "e1", "m1"))
        run = self.state.claimed_run()
        assert run is not None
        self.assertEqual(run.model, "deepseek/deepseek-v4-pro")

    def test_model_switch_applies_to_queued_turn_in_same_session(self) -> None:
        self.state.set_settings({"codex_model": "old-model", "codex_reasoning_effort": "low"})
        self.router.handle(inbound("hello", "e1", "m1"))
        first = self.state.claimed_run()
        assert first is not None
        self.router.handle(inbound("continue", "e2", "m2"))
        with patch(
            "agent_message.orchestration.router.list_codex_models",
            return_value=[CodexModel("new-model", "New Model")],
        ):
            reply = self.router.handle(inbound("/model new-model high", "e3", "m3"))
        self.assertIn("当前 session", reply[0])
        self.assertEqual(first.model, "old-model")
        self.assertEqual(first.reasoning_effort, "low")
        self.assertIsNone(self.state.claimed_run())
        self.state.finish_run(
            run_id=first.run_id,
            task_id=first.task_id,
            exit_code=0,
            session_id="existing-thread",
            final_message="done",
            error=None,
        )
        second = self.state.claimed_run()
        assert second is not None
        self.assertEqual(second.task_id, first.task_id)
        self.assertEqual(second.session_id, "existing-thread")
        self.assertEqual(second.model, "new-model")
        self.assertEqual(second.reasoning_effort, "high")
        self.assertEqual(second.prompt, "continue")


class AttachmentRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(Path(self.temp.name), ("alpha",))
        self.state = StateStore(self.config)
        self.state.authorize("ou-1")
        self.downloads: list[tuple[str, str, str, Path, int]] = []

        def fake_downloader(
            message_id: str, file_key: str, kind: str, dest: Path, max_bytes: int
        ) -> None:
            self.downloads.append((message_id, file_key, kind, dest, max_bytes))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"payload")

        self.router = MessageRouter(self.config, self.state, downloader=fake_downloader)

    def tearDown(self) -> None:
        self.state.close()
        self.temp.cleanup()

    @staticmethod
    def attachment(
        message_type: str = "image",
        event_id: str = "e1",
        message_id: str = "m1",
        open_id: str = "ou-1",
        file_name: str | None = None,
    ) -> InboundMessage:
        return InboundMessage(
            event_id,
            message_id,
            "chat-1",
            "p2p",
            open_id,
            "",
            message_type=message_type,
            file_key="key-1",
            file_name=file_name,
        )

    def test_image_downloads_into_project_and_queues_note(self) -> None:
        self.router.handle(inbound("/new alpha work", "e0", "m0"))
        first = self.state.claimed_run()
        assert first is not None
        self.state.finish_run(
            run_id=first.run_id,
            task_id=first.task_id,
            exit_code=0,
            session_id="thread-1",
            final_message="done",
            error=None,
        )

        replies = self.router.handle(self.attachment())

        self.assertIn("已接收图片", replies[0])
        project_path = self.config.projects["alpha"].path
        expected = project_path / ".agent-message" / "inbox" / "m1-image-m1.jpg"
        self.assertEqual(expected.read_bytes(), b"payload")
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.downloads[0][2], "image")
        second = self.state.claimed_run()
        assert second is not None
        self.assertEqual(second.session_id, "thread-1")
        self.assertIn(".agent-message/inbox/m1-image-m1.jpg", second.prompt)

    def test_file_attachment_creates_default_chat_when_no_task(self) -> None:
        replies = self.router.handle(self.attachment("file", file_name="报告.pdf"))

        self.assertIn("已接收文件", replies[0])
        self.assertIn("默认 Codex 对话", replies[0])
        project_path = self.config.projects["alpha"].path
        expected = project_path / ".agent-message" / "inbox" / "m1-报告.pdf"
        self.assertTrue(expected.is_file())
        claimed = self.state.claimed_run()
        assert claimed is not None
        self.assertIn(".agent-message/inbox/m1-报告.pdf", claimed.prompt)

    def test_attachment_is_deduplicated_and_requires_authorization(self) -> None:
        self.router.handle(self.attachment())
        again = self.router.handle(self.attachment())
        self.assertEqual(again, [])
        self.assertEqual(len(self.downloads), 1)

        outsider = self.router.handle(
            self.attachment(event_id="e2", message_id="m2", open_id="ou-2")
        )
        self.assertEqual(outsider, [])
        self.assertEqual(len(self.downloads), 1)

    def test_attachment_download_failure_replies_error_without_queueing(self) -> None:
        def failing(_mid: str, _key: str, _kind: str, _dest: Path, _limit: int) -> None:
            raise RuntimeError("boom")

        router = MessageRouter(self.config, self.state, downloader=failing)
        replies = router.handle(self.attachment())
        self.assertIn("下载失败", replies[0])
        self.assertIsNone(self.state.claimed_run())

    def test_attachment_without_downloader_is_explained(self) -> None:
        router = MessageRouter(self.config, self.state)
        replies = router.handle(self.attachment())
        self.assertIn("未启用附件下载", replies[0])


class SendCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = make_config(Path(self.temp.name), ("alpha",))
        self.state = StateStore(self.config)
        self.state.authorize("ou-1")
        self.router = MessageRouter(self.config, self.state)
        self.router.handle(inbound("/new alpha work", "e0", "m0"))

    def tearDown(self) -> None:
        self.state.close()
        self.temp.cleanup()

    def test_send_enqueues_image_and_file_outbox_entries(self) -> None:
        project_path = self.config.projects["alpha"].path
        (project_path / "plot.png").write_bytes(b"png")
        (project_path / "report.pdf").write_bytes(b"pdf")

        reply = self.router.handle(inbound("/send plot.png", "e1", "m1"))
        self.assertIn("已排队发送图片", reply[0])
        entry = self.state.next_outbox()
        assert entry is not None
        self.assertEqual(entry.kind, "image")
        self.assertEqual(entry.file_path, str((project_path / "plot.png").resolve()))
        self.state.mark_outbox_sent(entry.id)

        reply = self.router.handle(inbound("/send report.pdf", "e2", "m2"))
        self.assertIn("已排队发送文件", reply[0])
        entry = self.state.next_outbox()
        assert entry is not None
        self.assertEqual(entry.kind, "file")
        self.assertTrue(entry.file_path.endswith("report.pdf"))

    def test_send_rejects_escape_missing_and_missing_task(self) -> None:
        reply = self.router.handle(inbound("/send ../outside.txt", "e1", "m1"))
        self.assertIn("无法发送该文件", reply[0])
        reply = self.router.handle(inbound("/send missing.txt", "e2", "m2"))
        self.assertIn("无法发送该文件", reply[0])
        self.assertIsNone(self.state.next_outbox())

        other = MessageRouter(self.config, self.state)
        reply = other.handle(
            InboundMessage("e3", "m3", "chat-2", "p2p", "ou-1", "/send plot.png")
        )
        self.assertIn("没有当前任务", reply[0])

    def test_send_rejects_arguments_errors(self) -> None:
        reply = self.router.handle(inbound("/send", "e1", "m1"))
        self.assertIn("用法：/send", reply[0])


class OutboxMediaMigrationTests(unittest.TestCase):
    def test_outbox_gains_media_columns_and_text_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = make_config(Path(temp))
            config.service.state_dir.mkdir(parents=True)
            database = config.service.state_dir / "agent-message.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO outbox(chat_id, content, status, created_at) "
                "VALUES ('chat-1', 'old text', 'pending', 'now')"
            )
            connection.commit()
            connection.close()

            state = StateStore(config)
            try:
                columns = {
                    row["name"]
                    for row in state._connection.execute("PRAGMA table_info(outbox)").fetchall()
                }
                self.assertIn("kind", columns)
                self.assertIn("file_path", columns)
                entry = state.next_outbox()
                assert entry is not None
                self.assertEqual(entry.kind, "text")
                self.assertEqual(entry.content, "old text")
            finally:
                state.close()

            # Reopening runs the migration again without errors or data loss.
            reopened = StateStore(config)
            try:
                entry = reopened.next_outbox()
                assert entry is not None
                self.assertEqual(entry.content, "old text")
            finally:
                reopened.close()
