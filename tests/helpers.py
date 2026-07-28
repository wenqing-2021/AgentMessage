from __future__ import annotations

from pathlib import Path

from agent_message.core.config import AppConfig, load_config
from agent_message.core.models import InboundMessage


def make_config(
    root: Path,
    projects: tuple[str, ...] = ("alpha",),
    gpu_projects: tuple[str, ...] = (),
    container_projects: tuple[str, ...] = (),
    qoder_only_projects: tuple[str, ...] = (),
) -> AppConfig:
    entries: list[str] = [
        "[service]",
        f'state_dir = "{root / "state"}"',
        f'log_dir = "{root / "logs"}"',
        f'default_chat_project = "{projects[0]}"',
        "codex_tool_network = false",
        "stop_grace_seconds = 1",
        "final_message_limit = 3500",
        "",
    ]
    for name in projects:
        project_path = root / "projects" / name
        project_path.mkdir(parents=True, exist_ok=True)
        entries.extend(
            [
                f"[projects.{name}]",
                f'path = "{project_path}"',
                (
                    'default_agent = "qoder"'
                    if name in qoder_only_projects
                    else 'default_agent = "codex"'
                ),
                (
                    'allowed_agents = ["qoder"]'
                    if name in qoder_only_projects
                    else 'allowed_agents = ["codex"]'
                    if name in container_projects
                    else 'allowed_agents = ["codex", "qoder"]'
                ),
                "",
            ]
        )
        if name in gpu_projects:
            entries.extend(
                [
                    "gpu_enabled = true",
                    "gpu_network = false",
                    "gpu_timeout_seconds = 3600",
                    "",
                ]
            )
        if name in container_projects:
            entries.extend(
                [
                    f'container_name = "{name}-container"',
                    "container_auto_start = true",
                    "container_timeout_seconds = 3600",
                    "",
                ]
            )
    config_path = root / "projects.toml"
    config_path.write_text("\n".join(entries), encoding="utf-8")
    return load_config(config_path)


def inbound(text: str, event_id: str = "event-1", message_id: str = "msg-1") -> InboundMessage:
    return InboundMessage(event_id, message_id, "chat-1", "p2p", "ou-1", text)
