"""Compatibility facade for coding-agent adapters.

The Codex implementation lives in its own module behind stable imports.
"""

from __future__ import annotations

from .base import AdapterError, AgentAdapter, ParsedAgentEvent
from .codex import CodexAdapter, container_mcp_config_args, sandbox_mcp_config_args
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
    raise AdapterError(f"Unsupported agent: {kind}; only codex is supported.")


__all__ = [
    "AdapterError",
    "AgentAdapter",
    "CodexAdapter",
    "ParsedAgentEvent",
    "adapter_for",
    "container_mcp_config_args",
    "sandbox_mcp_config_args",
]
