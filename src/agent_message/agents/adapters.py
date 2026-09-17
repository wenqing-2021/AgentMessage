"""Compatibility facade for coding-agent adapters.

Concrete implementations live in dedicated modules so Codex and Qoder can
evolve independently without changing established imports.
"""

from __future__ import annotations

from .base import AdapterError, AgentAdapter, ParsedAgentEvent
from .codex import CodexAdapter, container_mcp_config_args, sandbox_mcp_config_args
from .qoder import QoderAdapter
from ..core.config import AppConfig
from ..core.models import AgentKind


def adapter_for(
    kind: AgentKind,
    *,
    codex_tool_network: bool = False,
    app_config: AppConfig | None = None,
) -> AgentAdapter:
    if kind == AgentKind.CODEX:
        return CodexAdapter(codex_tool_network, app_config)
    return QoderAdapter(app_config)


__all__ = [
    "AdapterError",
    "AgentAdapter",
    "CodexAdapter",
    "ParsedAgentEvent",
    "QoderAdapter",
    "adapter_for",
    "container_mcp_config_args",
    "sandbox_mcp_config_args",
]
