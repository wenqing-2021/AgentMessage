"""Local installer support for adding a bot to the shared project registry."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from .config import ConfigError, load_config, tomllib


def register_bot(path: Path, primary_app_id: str, app_id: str, aliases: list[str]) -> None:
    """Validate a candidate registry before atomically replacing the original."""
    for value in (primary_app_id, app_id):
        if not re.fullmatch(r"cli_[A-Za-z0-9_-]{1,100}", value):
            raise ConfigError("invalid Feishu App ID")
    if app_id == primary_app_id:
        raise ConfigError("the new bot must have a different App ID")
    source = path.read_text(encoding="utf-8")
    raw = tomllib.loads(source)
    registry = raw.get("bots", {})
    if app_id in registry:
        raise ConfigError("this bot already exists; edit its credentials instead")
    service = raw.get("service", {})
    configured_primary = service.get("feishu_app_id", primary_app_id)
    if configured_primary != primary_app_id:
        raise ConfigError("primary credential App ID does not match the shared registry")
    if not aliases or len(set(aliases)) != len(aliases) or any(a not in raw["projects"] for a in aliases):
        raise ConfigError("choose unique existing project aliases separated by commas")
    if registry:
        primary = registry[primary_app_id]
        old_projects = primary["projects"]
        default = primary.get("default_chat_project", old_projects[0])
    else:
        old_projects = list(raw["projects"])
        default = service["default_chat_project"]
    remaining = [alias for alias in old_projects if alias not in aliases]
    if default not in remaining:
        raise ConfigError("keep the primary bot's default project; choose other projects for the new bot")
    if not registry:
        source, count = re.subn(r"(?m)^\[service\][ \t]*(?:#.*)?$",
                               f'[service]\nfeishu_app_id = {json.dumps(primary_app_id)}', source, count=1)
        if count != 1:
            raise ConfigError("installer requires a [service] table")
    else:
        # Only replace the primary bot's small table; retain other configuration/comments.
        pattern = rf"(?ms)^\[bots\.{re.escape(primary_app_id)}\][ \t]*(?:#[^\n]*)?\n.*?(?=^\[|\Z)"
        source, count = re.subn(pattern, "", source, count=1)
        if count != 1:
            raise ConfigError("use an unquoted [bots.<app_id>] table for installer-managed bots")
    for bot_id, projects, default_alias in ((primary_app_id, remaining, default), (app_id, aliases, aliases[0])):
        source += (f'\n[bots.{bot_id}]\nprojects = {json.dumps(projects)}\n'
                   f'default_chat_project = {json.dumps(default_alias)}\n')
    # Stage alongside the registry so relative project/state paths resolve identically.
    fd, temporary = tempfile.mkstemp(prefix=".projects-", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(source)
        load_config(temporary, app_id=app_id)
        load_config(temporary, app_id=primary_app_id)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--primary-app-id", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--projects", required=True)
    args = parser.parse_args()
    try:
        register_bot(args.config, args.primary_app_id, args.app_id,
                     [alias.strip() for alias in args.projects.split(",") if alias.strip()])
    except (ConfigError, OSError, ValueError) as exc:
        parser.exit(2, f"Cannot add bot: {exc}\n")


if __name__ == "__main__":
    main()
