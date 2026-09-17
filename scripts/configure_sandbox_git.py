#!/usr/bin/env python3
"""Configure sandbox Git identity and existing SSH-agent forwarding without logging values."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


KEYS = ("sandbox_git_user_name", "sandbox_git_user_email",
        "sandbox_ssh_agent_socket", "sandbox_ssh_known_hosts")
LEGACY_KEYS = tuple(key.replace("sandbox_", "gpu_") for key in KEYS)


def git_default(key):
    result = subprocess.run(
        ["git", "config", "--global", "--get", key],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def ask(label, default=""):
    hint = " [Enter: reuse detected value]" if default else " [required]"
    value = (getpass.getpass(label + hint + ": ") or default).strip()
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("A required value is empty or contains control characters.")
    return value.strip()


def check_ssh(socket_value, hosts_value):
    socket_path = Path(socket_value).expanduser()
    hosts_path = Path(hosts_value).expanduser()
    for path in (socket_path, hosts_path):
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("SSH paths must be absolute and must not contain '..'.")
    socket_info = socket_path.stat()
    if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.getuid():
        raise ValueError("SSH agent must be a Unix socket owned by the current user.")
    if not hosts_path.is_file() or not os.access(hosts_path, os.R_OK):
        raise ValueError("known_hosts must be an existing readable file.")
    return str(socket_path), str(hosts_path)


def update_text(original, values):
    """Edit the standard service table; reject unusual syntax instead of losing data."""
    before = tomllib.loads(original)
    lines = original.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines)
              if re.fullmatch(r"\s*\[service\]\s*(?:#.*)?", line.strip())]
    if len(starts) != 1:
        raise ValueError("Expected one explicit [service] table.")
    start = starts[0] + 1
    end = next((i for i in range(start, len(lines))
                if lines[i].lstrip().startswith("[")), len(lines))
    pattern = '\\s*(?:' + "|".join(KEYS + LEGACY_KEYS) + ')\\s*='
    body = [line for line in lines[start:end] if not re.match(pattern, line)]
    inserted = [f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
                for key, value in values.items()]
    prefix = "".join(lines[:start] + body)
    candidate = prefix.rstrip("\n") + "\n" + "".join(inserted) + "\n" + "".join(lines[end:])
    expected = before.copy()
    expected["service"] = dict(before.get("service", {}))
    for legacy in LEGACY_KEYS:
        expected["service"].pop(legacy, None)
    for key in KEYS:
        expected["service"].pop(key, None)
    expected["service"].update(values)
    if tomllib.loads(candidate) != expected:
        raise ValueError("Unsupported TOML layout; configuration was not changed.")
    return candidate


def save(path, original, candidate):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Configuration must be a regular file, not a symlink.")
    if path.read_text() != original:
        raise ValueError("Configuration changed during setup; please rerun.")
    fd, name = tempfile.mkstemp(prefix=".sandbox-git-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parents[1] / "config/projects.toml")
    args = parser.parse_args()
    try:
        # Require a local terminal; never fall back to echoing private input.
        with open("/dev/tty", "r+") as tty:
            if not tty.isatty():
                raise ValueError("Run this script in a local interactive terminal.")
        path = args.config.absolute()
        if path.is_symlink():
            raise ValueError("Configuration symlinks are not supported.")
        original = path.read_text()
        service = tomllib.loads(original).get("service", {})
        print("Values are hidden. Enter reuses detected values; Ctrl-C cancels before saving.")
        values = {}
        for key, git_key, label in (
            (KEYS[0], "user.name", "Git author name (not a login username)"),
            (KEYS[1], "user.email", "Git author email"),
        ):
            values[key] = ask(label, service.get(key) or git_default(git_key))
        ssh = ask("Enable SSH forwarding? y/n", "y").lower()
        if ssh not in ("y", "n"):
            raise ValueError("Choose y or n.")
        if ssh == "y":
            print("Use a dedicated agent: sandbox projects can authenticate with all its loaded keys.")
            socket_default = service.get(KEYS[2]) or os.environ.get("SSH_AUTH_SOCK", "")
            hosts_default = service.get(KEYS[3]) or str(Path.home() / ".ssh/known_hosts")
            socket_value = ask("SSH agent socket path", socket_default)
            hosts_value = ask("Previously verified known_hosts path", hosts_default)
            values[KEYS[2]], values[KEYS[3]] = check_ssh(socket_value, hosts_value)
        candidate = update_text(original, values)
        if ask("Save global sandbox Git settings? y/n", "y").lower() != "y":
            print("Cancelled; configuration unchanged.")
            return 0
        save(path, original, candidate)
        print("Saved with mode 0600. Restart AgentMessage when current tasks are finished.")
        if ssh == "y":
            print("Keep the agent running with the intended key loaded; the project needs sandbox networking.")
        return 0
    except (OSError, ValueError, TypeError, EOFError):
        # Exceptions can contain private paths, usernames or TOML source lines.
        print("Setup failed. Check config syntax, required values, and SSH paths locally; no values logged.")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
