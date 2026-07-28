"""Load and validate the trusted AgentMessage project registry."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10, which remains supported by this project.
    import tomli as tomllib  # type: ignore[no-redef]

from .models import AgentKind


class ConfigError(ValueError):
    """Raised when the local, trusted project registry is invalid."""


@dataclass(frozen=True)
class GpuConfig:
    enabled: bool = False
    network: bool = True
    timeout_seconds: int = 86400


@dataclass(frozen=True)
class ContainerConfig:
    name: str
    auto_start: bool = True
    timeout_seconds: int = 86400
    project_path: PurePosixPath | None = None


@dataclass(frozen=True)
class ProjectConfig:
    alias: str
    path: Path
    default_agent: AgentKind
    allowed_agents: frozenset[AgentKind]
    gpu: GpuConfig | None = None
    container: ContainerConfig | None = None


@dataclass(frozen=True)
class ServiceConfig:
    state_dir: Path
    log_dir: Path
    default_chat_project: str
    codex_tool_network: bool = False
    stop_grace_seconds: int = 10
    final_message_limit: int = 3500


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    projects: dict[str, ProjectConfig]
    service: ServiceConfig

    @property
    def app_id(self) -> str | None:
        return os.environ.get("AGENT_MESSAGE_FEISHU_APP_ID")

    @property
    def app_secret(self) -> str | None:
        return os.environ.get("AGENT_MESSAGE_FEISHU_APP_SECRET")

    @property
    def configured_open_ids(self) -> frozenset[str]:
        raw = os.environ.get("AGENT_MESSAGE_ALLOWED_OPEN_IDS", "")
        return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _relative_to_config(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (config_path.parent.parent / path)


def _parse_agent(value: object, label: str) -> AgentKind:
    try:
        return AgentKind(str(value))
    except ValueError as exc:
        allowed = ", ".join(member.value for member in AgentKind)
        raise ConfigError(f"{label} must be one of: {allowed}") from exc


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(
            f"project registry not found: {config_path}. "
            "Copy config/projects.example.toml to config/projects.toml first."
        )
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    service_raw = raw.get("service", {})
    if not isinstance(service_raw, dict):
        raise ConfigError("[service] must be a TOML table")
    state_dir = _relative_to_config(str(service_raw.get("state_dir", "var")), config_path).resolve()
    log_dir = _relative_to_config(str(service_raw.get("log_dir", "var/logs")), config_path).resolve()
    default_chat_project = service_raw.get("default_chat_project")
    codex_tool_network = service_raw.get("codex_tool_network", False)
    stop_grace = int(service_raw.get("stop_grace_seconds", 10))
    limit = int(service_raw.get("final_message_limit", 3500))
    if stop_grace < 1 or limit < 256:
        raise ConfigError("stop_grace_seconds must be positive and final_message_limit >= 256")
    if not isinstance(codex_tool_network, bool):
        raise ConfigError("[service].codex_tool_network must be true or false")

    projects_raw = raw.get("projects")
    if not isinstance(projects_raw, dict) or not projects_raw:
        raise ConfigError("define at least one [projects.<alias>] entry")

    projects: dict[str, ProjectConfig] = {}
    for alias, project_raw in projects_raw.items():
        if not isinstance(alias, str) or not alias.replace("_", "").replace("-", "").isalnum():
            raise ConfigError(f"invalid project alias: {alias!r}")
        if not isinstance(project_raw, dict):
            raise ConfigError(f"[projects.{alias}] must be a TOML table")
        raw_path = project_raw.get("path")
        if not isinstance(raw_path, str) or not Path(raw_path).expanduser().is_absolute():
            raise ConfigError(f"projects.{alias}.path must be an absolute path")
        project_path = Path(raw_path).expanduser().resolve()
        if not project_path.is_dir():
            raise ConfigError(f"projects.{alias}.path is not a directory: {project_path}")

        allowed_raw = project_raw.get("allowed_agents", ["codex"])
        if not isinstance(allowed_raw, list) or not allowed_raw:
            raise ConfigError(f"projects.{alias}.allowed_agents must be a non-empty array")
        allowed = frozenset(_parse_agent(item, f"projects.{alias}.allowed_agents") for item in allowed_raw)
        default = _parse_agent(project_raw.get("default_agent", "codex"), f"projects.{alias}.default_agent")
        if default not in allowed:
            raise ConfigError(f"projects.{alias}.default_agent must be listed in allowed_agents")
        flat_gpu_keys = {"gpu_enabled", "gpu_network", "gpu_timeout_seconds"}
        has_flat_gpu = any(key in project_raw for key in flat_gpu_keys)
        legacy_gpu_raw = project_raw.get("gpu")
        if has_flat_gpu and legacy_gpu_raw is not None:
            raise ConfigError(
                f"projects.{alias} cannot mix flat GPU settings with the legacy gpu table"
            )
        if legacy_gpu_raw is not None and not isinstance(legacy_gpu_raw, dict):
            raise ConfigError(f"projects.{alias}.gpu must be a TOML table")
        gpu_raw = (
            {
                "enabled": project_raw.get("gpu_enabled", False),
                "network": project_raw.get("gpu_network", True),
                "timeout_seconds": project_raw.get("gpu_timeout_seconds", 86400),
            }
            if has_flat_gpu
            else legacy_gpu_raw
        )
        gpu: GpuConfig | None = None
        if gpu_raw is not None:
            assert isinstance(gpu_raw, dict)
            enabled = gpu_raw.get("enabled", False)
            network = gpu_raw.get("network", True)
            timeout_seconds = gpu_raw.get("timeout_seconds", 86400)
            if not isinstance(enabled, bool):
                raise ConfigError(f"projects.{alias}.gpu_enabled must be true or false")
            if not isinstance(network, bool):
                raise ConfigError(f"projects.{alias}.gpu_network must be true or false")
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
                raise ConfigError(f"projects.{alias}.gpu_timeout_seconds must be an integer")
            if not 60 <= timeout_seconds <= 604800:
                raise ConfigError(
                    f"projects.{alias}.gpu_timeout_seconds must be between 60 and 604800"
                )
            gpu = GpuConfig(enabled=enabled, network=network, timeout_seconds=timeout_seconds)
        container_name = project_raw.get("container_name")
        container: ContainerConfig | None = None
        has_container_settings = any(
            key in project_raw
            for key in (
                "container_name",
                "container_path",
                "container_auto_start",
                "container_timeout_seconds",
            )
        )
        if has_container_settings:
            if not isinstance(container_name, str) or not container_name.strip():
                raise ConfigError(
                    f"projects.{alias}.container_name must be a non-empty container name"
                )
            container_name = container_name.strip()
            if not all(character.isalnum() or character in "_.-" for character in container_name):
                raise ConfigError(
                    f"projects.{alias}.container_name may only contain letters, digits, _, ., and -"
                )
            auto_start = project_raw.get("container_auto_start", True)
            timeout_seconds = project_raw.get("container_timeout_seconds", 86400)
            container_path_raw = project_raw.get("container_path")
            if not isinstance(auto_start, bool):
                raise ConfigError(
                    f"projects.{alias}.container_auto_start must be true or false"
                )
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
                raise ConfigError(
                    f"projects.{alias}.container_timeout_seconds must be an integer"
                )
            if not 60 <= timeout_seconds <= 604800:
                raise ConfigError(
                    f"projects.{alias}.container_timeout_seconds must be between 60 and 604800"
                )
            container_path: PurePosixPath | None = None
            if container_path_raw is not None:
                if not isinstance(container_path_raw, str) or not container_path_raw.strip():
                    raise ConfigError(
                        f"projects.{alias}.container_path must be a non-empty absolute "
                        "container path"
                    )
                if "\x00" in container_path_raw:
                    raise ConfigError(f"projects.{alias}.container_path cannot contain NUL")
                container_path = PurePosixPath(container_path_raw.strip())
                if not container_path.is_absolute() or ".." in container_path.parts:
                    raise ConfigError(
                        f"projects.{alias}.container_path must be an absolute path without .."
                    )
            if gpu is not None and gpu.enabled:
                raise ConfigError(
                    f"projects.{alias} cannot enable both container execution and GPU bubblewrap"
                )
            container = ContainerConfig(
                name=container_name,
                auto_start=auto_start,
                timeout_seconds=timeout_seconds,
                project_path=container_path,
            )
        projects[alias] = ProjectConfig(
            alias, project_path, default, allowed, gpu, container
        )

    if not isinstance(default_chat_project, str) or not default_chat_project.strip():
        raise ConfigError("[service].default_chat_project must name a configured project alias")
    default_chat_project = default_chat_project.strip()
    default_project = projects.get(default_chat_project)
    if default_project is None:
        aliases = ", ".join(sorted(projects))
        raise ConfigError(
            f"[service].default_chat_project is unknown: {default_chat_project}. "
            f"Available projects: {aliases}"
        )
    return AppConfig(
        config_path=config_path,
        projects=projects,
        service=ServiceConfig(
            state_dir, log_dir, default_chat_project, codex_tool_network, stop_grace, limit
        ),
    )
