"""Agent-facing transfer script and its project spool.

Agents send project files by running the injected script
.agent-message/bin/send-to-feishu inside their project. The script never talks to
Feishu itself: it drops a request file into the project spool, and the bridge
service (which owns the credentials) performs the upload and writes the outcome
back. Keeping the exchange inside the project directory is what makes the same
script work on the host, inside the bubblewrap sandbox, and inside a
bind-mounted container.
"""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)

SPOOL_DIR = Path(".agent-message") / "outbox"
SCRIPT_PATH = Path(".agent-message") / "bin" / "send-to-feishu"
REQUEST_SUFFIX = ".req"
RESULT_SUFFIX = ".res"
ASSET_PATH = Path(__file__).resolve().parent.parent / "assets" / "send-to-feishu.sh"


def script_source() -> str:
    return ASSET_PATH.read_text(encoding="utf-8")


def ensure_send_script(project_root: Path) -> Path | None:
    """Install or refresh the transfer script inside the project.

    Returns the script path, or None when the project cannot be written (the run
    continues; the agent simply cannot offer file sending for that project).
    """
    destination = project_root / SCRIPT_PATH
    try:
        # The spool is shared with the container/sandbox runtime, which may run as
        # another UID, so both sides must be able to create and remove entries.
        spool = spool_dir(project_root)
        spool.mkdir(parents=True, exist_ok=True)
        spool.chmod(0o777)
        source = script_source()
        if destination.is_file() and destination.read_text(encoding="utf-8") == source:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
        destination.chmod(0o755)
    except OSError as exc:
        LOGGER.warning("Failed to install transfer script in %s: %s", project_root, exc)
        return None
    return destination


def spool_dir(project_root: Path) -> Path:
    return project_root / SPOOL_DIR


def pending_requests(project_root: Path) -> list[Path]:
    """List queued transfer requests, oldest first."""
    directory = spool_dir(project_root)
    if not directory.is_dir():
        return []
    try:
        return sorted(
            entry
            for entry in directory.iterdir()
            if entry.is_file() and entry.suffix == REQUEST_SUFFIX
        )
    except OSError as exc:
        LOGGER.warning("Failed to list spool %s: %s", directory, exc)
        return []


def read_request(request: Path) -> str | None:
    """Return the project-relative path stored in a request file."""
    try:
        content = request.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        LOGGER.warning("Failed to read spool request %s: %s", request, exc)
        return None
    lines = content.splitlines()
    relative = lines[0].strip() if lines else ""
    return relative or None


def result_path(request: Path) -> Path:
    return request.with_suffix(RESULT_SUFFIX)


def write_result(request: Path, *, ok: bool, message: str) -> None:
    """Report the transfer outcome to the waiting script. Never raises."""
    payload = ("ok" if ok else "error") + "\n" + message.strip() + "\n"
    try:
        result_path(request).write_text(payload, encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("Failed to write spool result for %s: %s", request, exc)


def discard_request(request: Path) -> None:
    """Consume a request before sending so a crash cannot resend it twice."""
    try:
        request.unlink(missing_ok=True)
    except OSError as exc:
        LOGGER.warning("Failed to remove spool request %s: %s", request, exc)


_PRUNABLE_SUFFIXES = {RESULT_SUFFIX, ".tmp"}


def prune_results(project_root: Path, *, older_than_seconds: float, now: float) -> None:
    """Drop leftovers whose waiting script is long gone."""
    directory = spool_dir(project_root)
    if not directory.is_dir():
        return
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_file() or entry.suffix not in _PRUNABLE_SUFFIXES:
            continue
        try:
            if now - entry.stat().st_mtime > older_than_seconds:
                entry.unlink(missing_ok=True)
        except OSError:
            continue
