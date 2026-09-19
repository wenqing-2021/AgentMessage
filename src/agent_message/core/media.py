"""Shared helpers for exchanging project files with Feishu."""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Feishu upload/download API limits; keep as constants instead of new config keys.
IMAGE_MAX_BYTES = 10 * 1024 * 1024
FILE_MAX_BYTES = 30 * 1024 * 1024

IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff"}
)

INBOX_DIR = Path(".agent-message") / "inbox"


def kind_for_path(path: Path) -> str:
    return "image" if path.suffix.lower() in IMAGE_EXTENSIONS else "file"


def size_limit_for_kind(kind: str) -> int:
    return IMAGE_MAX_BYTES if kind == "image" else FILE_MAX_BYTES


def safe_filename(name: str | None) -> str | None:
    """Reduce a Feishu-provided file name to a plain basename, or None if unusable."""
    if not name:
        return None
    cleaned = Path(name.strip()).name.strip()
    if cleaned in {"", ".", ".."}:
        return None
    return cleaned


def resolve_project_file(project_root: Path, relative: str) -> Path | None:
    """Resolve a user/agent-supplied path to an existing file inside the project.

    Returns None (and logs the reason) for absolute paths, escapes or symlinks
    leaving the project, missing files, and files above their size limit.
    """
    candidate = Path(relative.strip())
    if not str(candidate) or candidate.is_absolute():
        LOGGER.warning("Rejected non-relative file path: %r", relative)
        return None
    root = project_root.resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        LOGGER.warning("Rejected file path escaping project root: %r", relative)
        return None
    if not resolved.is_file():
        LOGGER.warning("Rejected missing project file: %r", relative)
        return None
    kind = kind_for_path(resolved)
    limit = size_limit_for_kind(kind)
    try:
        size = resolved.stat().st_size
    except OSError:
        LOGGER.warning("Rejected unreadable project file: %r", relative)
        return None
    if size > limit:
        LOGGER.warning(
            "Rejected oversized %s %s: %d bytes > %d limit", kind, resolved, size, limit
        )
        return None
    return resolved
