from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class AgentKind(str, Enum):
    CODEX = "codex"
    QODER = "qoder"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class TaskOrigin(str, Enum):
    TASK = "task"
    CHAT = "chat"


class MessageStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class InboundMessage:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    text: str


@dataclass(frozen=True)
class Task:
    id: str
    project_alias: str
    agent: AgentKind
    chat_id: str
    owner_open_id: str
    origin: TaskOrigin
    status: TaskStatus
    session_id: str | None
    created_at: str
    updated_at: str
    last_summary: str | None


@dataclass(frozen=True)
class ClaimedRun:
    run_id: int
    task_id: str
    message_id: int
    project_alias: str
    project_path: Path
    agent: AgentKind
    session_id: str | None
    prompt: str
    chat_id: str
    log_path: Path


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    session_id: str | None
    final_message: str
    error: str | None = None


@dataclass(frozen=True)
class OutboxMessage:
    id: int
    chat_id: str
    content: str
