"""Local installer support for adding and removing shared-registry bots."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from .config import SHARED_PROJECT_ALIAS, ConfigError, load_config, tomllib


def _set_service_default(source: str, value: str) -> str:
    """Replace default_chat_project inside the [service] table only."""
    lines = source.splitlines()
    in_service = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        table_line = stripped.split("#", 1)[0].strip()
        if table_line.startswith("[") and table_line.endswith("]"):
            in_service = table_line[1:-1].strip() == "service"
            continue
        if in_service and re.match(r"^default_chat_project\s*=", stripped):
            comment = "  " + line[line.index("#"):] if "#" in line else ""
            lines[index] = f"default_chat_project = {json.dumps(value)}{comment}"
            break
    else:
        raise ConfigError("[service].default_chat_project is missing")
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def register_bot(path: Path, primary_app_id: str, app_id: str, aliases: list[str]) -> str:
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
    if aliases != [SHARED_PROJECT_ALIAS]:
        raise ConfigError(f"only {SHARED_PROJECT_ALIAS} may be shared with another bot")
    if registry:
        primary = registry.get(primary_app_id)
        if not isinstance(primary, dict) or not isinstance(primary.get("projects"), list):
            raise ConfigError(f"bots.{primary_app_id} must list its projects")
        old_projects = primary["projects"]
        fallback_default = old_projects[0] if old_projects else ""
        default = primary.get("default_chat_project", fallback_default)
    else:
        old_projects = list(raw["projects"])
        default = service.get("default_chat_project")
        if not isinstance(default, str):
            raise ConfigError("[service].default_chat_project is missing")
    if SHARED_PROJECT_ALIAS not in old_projects:
        raise ConfigError(
            f"the primary bot does not manage {SHARED_PROJECT_ALIAS}; it may already be shared"
        )
    remaining = [alias for alias in old_projects if alias not in aliases]
    if not remaining:
        raise ConfigError("the primary bot must keep at least one project")
    # The primary bot cannot keep a default project that moved to the new bot.
    new_default = default if default in remaining else remaining[0]
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
    if service.get("default_chat_project") != new_default:
        source = _set_service_default(source, new_default)
    for bot_id, projects, default_alias in ((primary_app_id, remaining, new_default), (app_id, aliases, aliases[0])):
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
    return new_default


def unregister_bot(path: Path, primary_app_id: str, app_id: str) -> list[str]:
    """Remove one registered bot and return its projects to the primary bot."""
    for value in (primary_app_id, app_id):
        if not re.fullmatch(r"cli_[A-Za-z0-9_-]{1,100}", value):
            raise ConfigError("invalid Feishu App ID")
    if app_id == primary_app_id:
        raise ConfigError("the primary bot cannot be removed; run the full uninstall instead")
    source = path.read_text(encoding="utf-8")
    raw = tomllib.loads(source)
    registry = raw.get("bots", {})
    primary = registry.get(primary_app_id)
    bot = registry.get(app_id)
    if not isinstance(primary, dict) or not isinstance(primary.get("projects"), list):
        raise ConfigError(f"bots.{primary_app_id} must list its projects")
    if not isinstance(bot, dict) or not isinstance(bot.get("projects"), list):
        raise ConfigError(f"bots.{app_id} is not registered")
    old_projects = list(primary["projects"])
    returned = [alias for alias in bot["projects"] if alias not in old_projects]
    fallback_default = old_projects[0] if old_projects else ""
    default = primary.get("default_chat_project", fallback_default)
    for bot_id in (primary_app_id, app_id):
        pattern = rf"(?ms)^\[bots\.{re.escape(bot_id)}\][ \t]*(?:#[^\n]*)?\n.*?(?=^\[|\Z)"
        source, count = re.subn(pattern, "", source, count=1)
        if count != 1:
            raise ConfigError(f"use an unquoted [bots.{bot_id}] table for installer-managed bots")
    source += (f'\n[bots.{primary_app_id}]\nprojects = {json.dumps(old_projects + returned)}\n'
               f'default_chat_project = {json.dumps(default)}\n')
    fd, temporary = tempfile.mkstemp(prefix=".projects-", suffix=".toml", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(source)
        load_config(temporary, app_id=primary_app_id)
        try:
            load_config(temporary, app_id=app_id)
        except ConfigError:
            pass
        else:
            raise ConfigError(f"{app_id} is still configured after removal")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return returned


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--primary-app-id", required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--projects", help="project aliases to share when adding a bot")
    parser.add_argument("--remove", action="store_true",
                        help="remove a registered bot instead of adding one")
    args = parser.parse_args()
    try:
        if args.remove:
            returned = unregister_bot(args.config, args.primary_app_id, args.app_id)
        else:
            if not args.projects:
                raise ConfigError("--projects is required when adding a bot")
            default = register_bot(args.config, args.primary_app_id, args.app_id,
                                   [alias.strip() for alias in args.projects.split(",") if alias.strip()])
    except (ConfigError, OSError, ValueError) as exc:
        action = "remove" if args.remove else "add"
        parser.exit(2, f"Cannot {action} bot: {exc}\n")
    if args.remove:
        shared = ", ".join(returned) if returned else "none"
        print(f"Removed {args.app_id}; returned to the primary bot: {shared}")
    else:
        print(f"Registered {args.app_id}; project {SHARED_PROJECT_ALIAS} is now shared with it.")
        print(f"Primary bot default project: {default}")


if __name__ == "__main__":
    main()
