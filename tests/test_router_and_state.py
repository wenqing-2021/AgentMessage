from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

from agent_message.agents.model_catalog import CodexModel
from agent_message.core.models import AgentKind, TaskOrigin, TaskStatus
from agent_message.core.state import StateStore
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
                gpu_table = state._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'gpu_jobs'"
                ).fetchone()
                self.assertIsNotNone(gpu_table)
                container_table = state._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'container_jobs'"
                ).fetchone()
                self.assertIsNotNone(container_table)
            finally:
                state.close()


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
