"""SQLite-backed persistence for tasks, runs, messages, and jobs."""

from __future__ import annotations

import hashlib
import shlex
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import AppConfig
from .models import (
    AgentKind,
    ClaimedRun,
    ContainerJob,
    SandboxJob,
    MessageStatus,
    OutboxMessage,
    Task,
    TaskOrigin,
    TaskStatus,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _task_id() -> str:
    return uuid.uuid4().hex[:10]


class StateStore:
    """Durable task state. Every public operation is serialized by one lock."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        config.service.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config.service.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = config.service.state_dir / "agent-message.sqlite3"
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    project_alias TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    owner_open_id TEXT NOT NULL,
                    origin TEXT NOT NULL DEFAULT 'task',
                    status TEXT NOT NULL,
                    session_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_summary TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_project_status
                    ON tasks(project_alias, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_chat
                    ON tasks(chat_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS task_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_messages_runnable
                    ON task_messages(status, created_at, task_id);

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    message_id INTEGER NOT NULL REFERENCES task_messages(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    pid INTEGER,
                    log_path TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    exit_code INTEGER,
                    final_message TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_task_status ON runs(task_id, status);

                CREATE TABLE IF NOT EXISTS sandbox_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
                    status TEXT NOT NULL,
                    pid INTEGER,
                    command_summary TEXT NOT NULL,
                    argv_json TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    exit_code INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sandbox_jobs_task_status
                    ON sandbox_jobs(task_id, status, id);

                CREATE TABLE IF NOT EXISTS container_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
                    status TEXT NOT NULL,
                    pid INTEGER,
                    container_pid INTEGER,
                    container_name TEXT NOT NULL,
                    command_summary TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    exit_code INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_container_jobs_task_status
                    ON container_jobs(task_id, status, id);

                CREATE TABLE IF NOT EXISTS inbound_events (
                    event_id TEXT PRIMARY KEY,
                    message_id TEXT UNIQUE NOT NULL,
                    chat_id TEXT NOT NULL,
                    sender_open_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_context (
                    chat_id TEXT PRIMARY KEY,
                    selected_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    default_chat_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS authorized_users (
                    open_id TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, id);
                """
            )
            self._add_column_if_missing("task_messages", "operation", "TEXT NOT NULL DEFAULT 'message'")
            self._add_column_if_missing("tasks", "origin", "TEXT NOT NULL DEFAULT 'task'")
            self._add_column_if_missing("chat_context", "default_chat_task_id", "TEXT")
            self._migrate_legacy_sandbox_jobs()

    def _migrate_legacy_sandbox_jobs(self) -> None:
        """Move jobs recorded before the GPU runtime became the sandbox runtime.

        The table was renamed from `gpu_jobs` to `sandbox_jobs`; copy any existing rows so an
        upgraded install keeps its audit trail. The guard makes this repeatable.
        """
        legacy = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'gpu_jobs'"
        ).fetchone()
        if legacy is None:
            return
        columns = (
            "id, task_id, run_id, status, pid, command_summary, argv_json, cwd, "
            "log_path, started_at, finished_at, exit_code, error"
        )
        self._connection.execute(
            f"INSERT OR IGNORE INTO sandbox_jobs({columns}) SELECT {columns} FROM gpu_jobs"
        )
        self._connection.execute("DROP TABLE gpu_jobs")

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            project_alias=row["project_alias"],
            agent=AgentKind(row["agent"]),
            chat_id=row["chat_id"],
            owner_open_id=row["owner_open_id"],
            origin=TaskOrigin(row["origin"]),
            status=TaskStatus(row["status"]),
            session_id=row["session_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_summary=row["last_summary"],
        )

    def authorize(self, open_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO authorized_users(open_id, added_at) VALUES (?, ?)",
                (open_id, _now()),
            )

    def is_authorized(self, open_id: str) -> bool:
        if open_id in self.config.configured_open_ids:
            return True
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM authorized_users WHERE open_id = ?", (open_id,)
            ).fetchone() is not None

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row is not None else None

    def set_setting(self, key: str, value: str | None) -> None:
        self.set_settings({key: value})

    def set_settings(self, values: dict[str, str | None]) -> None:
        """Apply related settings atomically with respect to run claiming."""
        timestamp = _now()
        with self._lock, self._connection:
            for key, value in values.items():
                if value is None:
                    self._connection.execute("DELETE FROM settings WHERE key = ?", (key,))
                else:
                    self._connection.execute(
                        """
                        INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                            updated_at=excluded.updated_at
                        """,
                        (key, value, timestamp),
                    )

    def register_inbound(
        self, event_id: str, message_id: str, chat_id: str, sender_open_id: str, text: str
    ) -> bool:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    """
                    INSERT INTO inbound_events(
                        event_id, message_id, chat_id, sender_open_id, content_sha256, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, message_id, chat_id, sender_open_id, digest, _now()),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def create_task(
        self,
        *,
        project_alias: str,
        agent: AgentKind,
        chat_id: str,
        owner_open_id: str,
        prompt: str,
        origin: TaskOrigin = TaskOrigin.TASK,
    ) -> Task:
        task_id = _task_id()
        timestamp = _now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO tasks(
                    id, project_alias, agent, chat_id, owner_open_id, origin, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    project_alias,
                    agent.value,
                    chat_id,
                    owner_open_id,
                    origin.value,
                    TaskStatus.QUEUED.value,
                    timestamp,
                    timestamp,
                ),
            )
            self._connection.execute(
                "INSERT INTO task_messages(task_id, content, status, created_at) VALUES (?, ?, ?, ?)",
                (task_id, prompt, MessageStatus.QUEUED.value, timestamp),
            )
            self._set_selected_locked(chat_id, task_id)
            if origin == TaskOrigin.CHAT:
                self._set_default_chat_locked(chat_id, task_id)
            row = self._connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row)

    def _set_selected_locked(self, chat_id: str, task_id: str | None) -> None:
        self._connection.execute(
            """
            INSERT INTO chat_context(chat_id, selected_task_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET selected_task_id=excluded.selected_task_id,
                updated_at=excluded.updated_at
            """,
            (chat_id, task_id, _now()),
        )

    def _set_default_chat_locked(self, chat_id: str, task_id: str) -> None:
        self._connection.execute(
            """
            INSERT INTO chat_context(chat_id, default_chat_task_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET default_chat_task_id=excluded.default_chat_task_id,
                updated_at=excluded.updated_at
            """,
            (chat_id, task_id, _now()),
        )

    def clear_selected_task(self, chat_id: str) -> None:
        with self._lock, self._connection:
            self._set_selected_locked(chat_id, None)

    def select_default_chat(
        self,
        chat_id: str,
        owner_open_id: str,
        project_alias: str,
        agent: AgentKind,
    ) -> Task | None:
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT t.* FROM chat_context c
                JOIN tasks t ON t.id = c.default_chat_task_id
                WHERE c.chat_id = ? AND t.owner_open_id = ? AND t.project_alias = ?
                  AND t.agent = ? AND t.origin = ?
                """,
                (
                    chat_id,
                    owner_open_id,
                    project_alias,
                    agent.value,
                    TaskOrigin.CHAT.value,
                ),
            ).fetchone()
            if row is None:
                return None
            self._set_selected_locked(chat_id, str(row["id"]))
            return self._row_to_task(row)

    def select_task(self, chat_id: str, task_id: str, owner_open_id: str) -> Task | None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE id = ? AND chat_id = ? AND owner_open_id = ?",
                (task_id, chat_id, owner_open_id),
            ).fetchone()
            if row is None:
                return None
            self._set_selected_locked(chat_id, task_id)
            return self._row_to_task(row)

    def selected_task(self, chat_id: str, owner_open_id: str) -> Task | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT t.* FROM chat_context c
                JOIN tasks t ON t.id = c.selected_task_id
                WHERE c.chat_id = ? AND t.owner_open_id = ?
                """,
                (chat_id, owner_open_id),
            ).fetchone()
            return self._row_to_task(row) if row else None

    def queue_message(
        self, task_id: str, owner_open_id: str, content: str, *, operation: str = "message"
    ) -> Task | None:
        if operation not in {"message", "compact"}:
            raise ValueError("Unknown message operation")
        timestamp = _now()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE id = ? AND owner_open_id = ?", (task_id, owner_open_id)
            ).fetchone()
            if row is None:
                return None
            task = self._row_to_task(row)
            if operation == "compact" and (task.agent != AgentKind.CODEX or not task.session_id):
                return None
            if task.status == TaskStatus.STOPPED:
                # A user continuation is explicitly allowed to revive a stopped session.
                next_status = TaskStatus.QUEUED.value
            elif task.status == TaskStatus.RUNNING:
                next_status = TaskStatus.RUNNING.value
            else:
                next_status = TaskStatus.QUEUED.value
            self._connection.execute(
                "INSERT INTO task_messages(task_id, content, status, created_at, operation) VALUES (?, ?, ?, ?, ?)",
                (task_id, content, MessageStatus.QUEUED.value, timestamp, operation),
            )
            self._connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (next_status, timestamp, task_id),
            )
            return self._row_to_task(
                self._connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            )

    def get_task(self, task_id: str, chat_id: str | None = None) -> Task | None:
        with self._lock:
            sql = "SELECT * FROM tasks WHERE id = ?"
            params: tuple[str, ...] = (task_id,)
            if chat_id is not None:
                sql += " AND chat_id = ?"
                params = (task_id, chat_id)
            row = self._connection.execute(sql, params).fetchone()
            return self._row_to_task(row) if row else None

    def list_tasks(self, chat_id: str | None = None) -> list[Task]:
        with self._lock:
            if chat_id:
                rows = self._connection.execute(
                    "SELECT * FROM tasks WHERE chat_id = ? ORDER BY created_at DESC", (chat_id,)
                ).fetchall()
            else:
                rows = self._connection.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
            return [self._row_to_task(row) for row in rows]

    def stop_task(self, task_id: str, owner_open_id: str) -> Task | None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM tasks WHERE id = ? AND owner_open_id = ?", (task_id, owner_open_id)
            ).fetchone()
            if row is None:
                return None
            timestamp = _now()
            self._connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.STOPPED.value, timestamp, task_id),
            )
            self._connection.execute(
                "UPDATE task_messages SET status = ?, finished_at = ? WHERE task_id = ? AND status = ?",
                (MessageStatus.CANCELLED.value, timestamp, task_id, MessageStatus.QUEUED.value),
            )
            return self._row_to_task(
                self._connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            )

    def claimed_run(self) -> ClaimedRun | None:
        """Atomically claim the oldest queued message whose project is not running."""
        with self._lock, self._connection:
            row = self._connection.execute(
                """
                SELECT m.id AS message_id, m.task_id, m.content, m.operation, t.project_alias, t.agent,
                       t.session_id, t.chat_id
                FROM task_messages m
                JOIN tasks t ON t.id = m.task_id
                WHERE m.status = ?
                  AND t.status != ?
                  AND NOT EXISTS (
                    SELECT 1 FROM tasks active
                    WHERE active.project_alias = t.project_alias AND active.status = ?
                  )
                ORDER BY m.created_at, m.id
                LIMIT 1
                """,
                (MessageStatus.QUEUED.value, TaskStatus.STOPPED.value, TaskStatus.RUNNING.value),
            ).fetchone()
            if row is None:
                return None
            project = self.config.projects[row["project_alias"]]
            timestamp = _now()
            self._connection.execute(
                "UPDATE task_messages SET status = ?, started_at = ? WHERE id = ? AND status = ?",
                (MessageStatus.RUNNING.value, timestamp, row["message_id"], MessageStatus.QUEUED.value),
            )
            self._connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.RUNNING.value, timestamp, row["task_id"]),
            )
            run_cursor = self._connection.execute(
                """
                INSERT INTO runs(task_id, message_id, status, log_path, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["task_id"],
                    row["message_id"],
                    "running",
                    str(self.config.service.log_dir / row["task_id"] / f"pending-{row['message_id']}.jsonl"),
                    timestamp,
                ),
            )
            run_id = int(run_cursor.lastrowid)
            log_path = self.config.service.log_dir / row["task_id"] / f"run-{run_id}.jsonl"
            self._connection.execute("UPDATE runs SET log_path = ? WHERE id = ?", (str(log_path), run_id))
            model_row = self._connection.execute(
                "SELECT value FROM settings WHERE key = 'codex_model'"
            ).fetchone()
            model = str(model_row["value"]) if model_row is not None else None
            return ClaimedRun(
                run_id=run_id,
                task_id=row["task_id"],
                message_id=int(row["message_id"]),
                project_alias=row["project_alias"],
                project_path=project.path,
                agent=AgentKind(row["agent"]),
                session_id=row["session_id"],
                prompt=row["content"],
                chat_id=row["chat_id"],
                log_path=log_path,
                model=model,
                reasoning_effort=self.get_setting("codex_reasoning_effort"),
                operation=row["operation"],
            )

    def set_run_pid(self, run_id: int, pid: int) -> None:
        with self._lock, self._connection:
            self._connection.execute("UPDATE runs SET pid = ? WHERE id = ?", (pid, run_id))

    def set_task_session(self, task_id: str, session_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE tasks SET session_id = ?, updated_at = ? WHERE id = ?",
                (session_id, _now(), task_id),
            )

    @staticmethod
    def _row_to_container_job(row: sqlite3.Row) -> ContainerJob:
        return ContainerJob(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            run_id=int(row["run_id"]) if row["run_id"] is not None else None,
            status=str(row["status"]),
            pid=int(row["pid"]) if row["pid"] is not None else None,
            container_pid=(
                int(row["container_pid"]) if row["container_pid"] is not None else None
            ),
            container_name=str(row["container_name"]),
            command_summary=str(row["command_summary"]),
            cwd=str(row["cwd"]),
            log_path=Path(str(row["log_path"])),
            started_at=str(row["started_at"]),
            finished_at=(
                str(row["finished_at"]) if row["finished_at"] is not None else None
            ),
            exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
            error=str(row["error"]) if row["error"] is not None else None,
        )

    def create_container_job(
        self,
        *,
        task_id: str,
        run_id: int | None,
        container_name: str,
        argv: list[str],
        cwd: str,
    ) -> ContainerJob:
        timestamp = _now()
        summary = shlex.join(argv)[:1000]
        with self._lock, self._connection:
            task = self._connection.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise ValueError(f"unknown task: {task_id}")
            if run_id is not None:
                linked = self._connection.execute(
                    "SELECT 1 FROM runs WHERE id = ? AND task_id = ?", (run_id, task_id)
                ).fetchone()
                if linked is None:
                    raise ValueError(f"run {run_id} does not belong to task {task_id}")
            cursor = self._connection.execute(
                """
                INSERT INTO container_jobs(
                    task_id, run_id, status, container_name, command_summary,
                    cwd, log_path, started_at
                ) VALUES (?, ?, 'running', ?, ?, ?, '', ?)
                """,
                (task_id, run_id, container_name, summary, cwd, timestamp),
            )
            job_id = int(cursor.lastrowid)
            log_path = self.config.service.log_dir / task_id / f"container-job-{job_id}.log"
            self._connection.execute(
                "UPDATE container_jobs SET log_path = ? WHERE id = ?",
                (str(log_path), job_id),
            )
            row = self._connection.execute(
                "SELECT * FROM container_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_container_job(row)

    def set_container_job_pid(self, job_id: int, pid: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE container_jobs SET pid = ? WHERE id = ? AND status = 'running'",
                (pid, job_id),
            )

    def set_container_job_inner_pid(self, job_id: int, container_pid: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE container_jobs SET container_pid = ?
                WHERE id = ? AND status IN ('running', 'stopping')
                """,
                (container_pid, job_id),
            )

    def finish_container_job(
        self, job_id: int, *, exit_code: int, error: str | None, cancelled: bool = False
    ) -> ContainerJob:
        timestamp = _now()
        with self._lock, self._connection:
            previous = self._connection.execute(
                "SELECT status FROM container_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if previous is None:
                raise ValueError(f"unknown container job: {job_id}")
            was_stopping = str(previous["status"]) == "stopping"
            if cancelled or was_stopping:
                status = "stopped"
            elif exit_code == 0 and error is None:
                status = "succeeded"
            else:
                status = "failed"
            self._connection.execute(
                """
                UPDATE container_jobs SET status = ?, finished_at = ?, exit_code = ?, error = ?
                WHERE id = ?
                """,
                (status, timestamp, exit_code, error[:1000] if error else None, job_id),
            )
            row = self._connection.execute(
                "SELECT * FROM container_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_container_job(row)

    def latest_container_job(self, task_id: str) -> ContainerJob | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM container_jobs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return self._row_to_container_job(row) if row else None

    def mark_container_jobs_stopping(self, task_id: str) -> list[ContainerJob]:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE container_jobs SET status = 'stopping'
                WHERE task_id = ? AND status = 'running'
                """,
                (task_id,),
            )
            rows = self._connection.execute(
                """
                SELECT * FROM container_jobs
                WHERE task_id = ? AND status = 'stopping'
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_container_job(row) for row in rows]

    @staticmethod
    def _row_to_sandbox_job(row: sqlite3.Row) -> SandboxJob:
        return SandboxJob(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            run_id=int(row["run_id"]) if row["run_id"] is not None else None,
            status=str(row["status"]),
            pid=int(row["pid"]) if row["pid"] is not None else None,
            command_summary=str(row["command_summary"]),
            cwd=str(row["cwd"]),
            log_path=Path(str(row["log_path"])),
            started_at=str(row["started_at"]),
            finished_at=str(row["finished_at"]) if row["finished_at"] is not None else None,
            exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
            error=str(row["error"]) if row["error"] is not None else None,
        )

    def create_sandbox_job(
        self,
        *,
        task_id: str,
        run_id: int | None,
        argv: list[str],
        cwd: str,
    ) -> SandboxJob:
        timestamp = _now()
        summary = shlex.join(argv)[:1000]
        # Keep the legacy column non-sensitive: the requested audit record is the
        # bounded command summary, not an unlimited copy of every command argument.
        argv_json = "[]"
        with self._lock, self._connection:
            task = self._connection.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise ValueError(f"unknown task: {task_id}")
            if run_id is not None:
                linked = self._connection.execute(
                    "SELECT 1 FROM runs WHERE id = ? AND task_id = ?", (run_id, task_id)
                ).fetchone()
                if linked is None:
                    raise ValueError(f"run {run_id} does not belong to task {task_id}")
            cursor = self._connection.execute(
                """
                INSERT INTO sandbox_jobs(
                    task_id, run_id, status, command_summary, argv_json, cwd, log_path, started_at
                ) VALUES (?, ?, 'running', ?, ?, ?, '', ?)
                """,
                (task_id, run_id, summary, argv_json, cwd, timestamp),
            )
            job_id = int(cursor.lastrowid)
            log_path = self.config.service.log_dir / task_id / f"sandbox-job-{job_id}.log"
            self._connection.execute(
                "UPDATE sandbox_jobs SET log_path = ? WHERE id = ?", (str(log_path), job_id)
            )
            row = self._connection.execute(
                "SELECT * FROM sandbox_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_sandbox_job(row)

    def set_sandbox_job_pid(self, job_id: int, pid: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE sandbox_jobs SET pid = ? WHERE id = ? AND status = 'running'",
                (pid, job_id),
            )

    def finish_sandbox_job(
        self, job_id: int, *, exit_code: int, error: str | None, cancelled: bool = False
    ) -> SandboxJob:
        timestamp = _now()
        with self._lock, self._connection:
            previous = self._connection.execute(
                "SELECT status FROM sandbox_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if previous is None:
                raise ValueError(f"unknown sandbox job: {job_id}")
            was_stopping = str(previous["status"]) == "stopping"
            if cancelled or was_stopping:
                status = "stopped"
            elif exit_code == 0 and error is None:
                status = "succeeded"
            else:
                status = "failed"
            self._connection.execute(
                """
                UPDATE sandbox_jobs SET status = ?, finished_at = ?, exit_code = ?, error = ?
                WHERE id = ?
                """,
                (status, timestamp, exit_code, error[:1000] if error else None, job_id),
            )
            row = self._connection.execute(
                "SELECT * FROM sandbox_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        assert row is not None
        return self._row_to_sandbox_job(row)

    def latest_sandbox_job(self, task_id: str) -> SandboxJob | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sandbox_jobs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return self._row_to_sandbox_job(row) if row else None

    def mark_sandbox_jobs_stopping(self, task_id: str) -> list[int]:
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT pid FROM sandbox_jobs WHERE task_id = ? AND status = 'running' AND pid IS NOT NULL",
                (task_id,),
            ).fetchall()
            self._connection.execute(
                "UPDATE sandbox_jobs SET status = 'stopping' WHERE task_id = ? AND status = 'running'",
                (task_id,),
            )
        return [int(row["pid"]) for row in rows]

    def running_pid(self, task_id: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT pid FROM runs WHERE task_id = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return int(row["pid"]) if row and row["pid"] else None

    def finish_run(
        self,
        *,
        run_id: int,
        task_id: str,
        exit_code: int,
        session_id: str | None,
        final_message: str,
        error: str | None,
    ) -> Task:
        timestamp = _now()
        task_status = TaskStatus.SUCCEEDED if exit_code == 0 and not error else TaskStatus.FAILED
        with self._lock, self._connection:
            previous = self._connection.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            self._connection.execute(
                """
                UPDATE runs SET status = ?, finished_at = ?, exit_code = ?, final_message = ?, error = ?
                WHERE id = ?
                """,
                (task_status.value, timestamp, exit_code, final_message, error, run_id),
            )
            self._connection.execute(
                "UPDATE task_messages SET status = ?, finished_at = ? WHERE id = (SELECT message_id FROM runs WHERE id = ?)",
                (MessageStatus.DONE.value, timestamp, run_id),
            )
            pending = self._connection.execute(
                "SELECT 1 FROM task_messages WHERE task_id = ? AND status = ? LIMIT 1",
                (task_id, MessageStatus.QUEUED.value),
            ).fetchone()
            if previous and previous["status"] == TaskStatus.STOPPED.value:
                next_status = TaskStatus.STOPPED
            else:
                next_status = TaskStatus.QUEUED if pending else task_status
            self._connection.execute(
                """
                UPDATE tasks SET status = ?, session_id = COALESCE(?, session_id),
                    last_summary = ?, updated_at = ? WHERE id = ?
                """,
                (next_status.value, session_id, final_message, timestamp, task_id),
            )
            row = self._connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return self._row_to_task(row)

    def recover_interrupted(self) -> list[Task]:
        timestamp = _now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE sandbox_jobs SET status = 'interrupted', finished_at = ?,
                    error = COALESCE(error, 'bridge restarted')
                WHERE status IN ('running', 'stopping')
                """,
                (timestamp,),
            )
            self._connection.execute(
                """
                UPDATE container_jobs SET status = 'interrupted', finished_at = ?,
                    error = COALESCE(error, 'bridge restarted')
                WHERE status IN ('running', 'stopping')
                """,
                (timestamp,),
            )
            rows = self._connection.execute(
                "SELECT * FROM tasks WHERE status = ?", (TaskStatus.RUNNING.value,)
            ).fetchall()
            task_ids = [row["id"] for row in rows]
            if not task_ids:
                return []
            placeholders = ",".join("?" for _ in task_ids)
            self._connection.execute(
                f"UPDATE tasks SET status = ?, updated_at = ? WHERE id IN ({placeholders})",
                (TaskStatus.INTERRUPTED.value, timestamp, *task_ids),
            )
            self._connection.execute(
                f"UPDATE runs SET status = ?, finished_at = ?, error = COALESCE(error, ?) "
                f"WHERE task_id IN ({placeholders}) AND status = 'running'",
                (TaskStatus.INTERRUPTED.value, timestamp, "bridge restarted", *task_ids),
            )
            self._connection.execute(
                f"UPDATE task_messages SET status = ?, finished_at = ? "
                f"WHERE task_id IN ({placeholders}) AND status = ?",
                (MessageStatus.INTERRUPTED.value, timestamp, *task_ids, MessageStatus.RUNNING.value),
            )
            # Do not silently run continuations that were waiting behind an interrupted process.
            self._connection.execute(
                f"UPDATE task_messages SET status = ?, finished_at = ? "
                f"WHERE task_id IN ({placeholders}) AND status = ?",
                (MessageStatus.CANCELLED.value, timestamp, *task_ids, MessageStatus.QUEUED.value),
            )
            return [self._row_to_task(row) for row in rows]

    def enqueue_outbox(self, chat_id: str, content: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO outbox(chat_id, content, status, created_at) VALUES (?, ?, 'pending', ?)",
                (chat_id, content, _now()),
            )

    def next_outbox(self) -> OutboxMessage | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, chat_id, content FROM outbox WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            return OutboxMessage(int(row["id"]), row["chat_id"], row["content"]) if row else None

    def mark_outbox_sent(self, outbox_id: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE outbox SET status = 'sent', sent_at = ?, attempts = attempts + 1 WHERE id = ?",
                (_now(), outbox_id),
            )

    def mark_outbox_failed(self, outbox_id: int, error: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (error[:500], outbox_id),
            )

    def tail_log(self, task_id: str, limit: int) -> list[str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT log_path FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
            ).fetchone()
        if row is None:
            return []
        log_path = Path(row["log_path"])
        if not log_path.is_file():
            return []
        return self._tail_file(log_path, limit)

    def tail_sandbox_log(self, task_id: str, limit: int) -> list[str]:
        job = self.latest_sandbox_job(task_id)
        if job is None or not job.log_path.is_file():
            return []
        return self._tail_file(job.log_path, limit)

    def tail_container_log(self, task_id: str, limit: int) -> list[str]:
        job = self.latest_container_job(task_id)
        if job is None or not job.log_path.is_file():
            return []
        return self._tail_file(job.log_path, limit)

    @staticmethod
    def _tail_file(path: Path, limit: int, max_bytes: int = 256 * 1024) -> list[str]:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]
        return lines[-limit:]

    def task_count_by_status(self) -> Iterable[tuple[str, int]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status ORDER BY status"
            ).fetchall()
            return [(str(row["status"]), int(row["count"])) for row in rows]

    def recent_senders(self, limit: int = 20) -> list[tuple[str, str]]:
        """Local-only bootstrap aid; message content is never persisted in this table."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT sender_open_id, MAX(received_at) AS last_seen
                FROM inbound_events GROUP BY sender_open_id ORDER BY last_seen DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [(str(row["sender_open_id"]), str(row["last_seen"])) for row in rows]
