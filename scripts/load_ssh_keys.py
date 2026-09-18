#!/usr/bin/env python3
# Managed by AgentMessage: SSH key loader
"""Load unencrypted private keys from ~/.ssh into the configured dedicated agent."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import time


PRIVATE_HEADERS = {
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
}


def private_key_candidate(path: Path) -> bool:
    """Ignore links, non-key files and private keys with unsafe ownership/modes."""
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            return False
        with path.open('rb') as stream:
            return stream.readline(128).strip() in PRIVATE_HEADERS
    except OSError:
        return False


def load_keys(directory: Path, *, budget_seconds: float = 45) -> tuple[int, int]:
    """Return loaded/skipped counts without printing paths, keys, or subprocess output."""
    environment = dict(os.environ, SSH_ASKPASS_REQUIRE='never')
    environment.pop('DISPLAY', None)
    environment.pop('SSH_ASKPASS', None)
    if not environment.get('SSH_AUTH_SOCK'):
        raise RuntimeError('SSH_AUTH_SOCK is not configured')
    status = subprocess.run(['ssh-add', '-l'], env=environment, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    if status.returncode not in (0, 1):
        raise RuntimeError('SSH agent is unavailable')
    if not directory.exists():
        return 0, 0
    deadline = time.monotonic() + budget_seconds
    loaded = skipped = 0
    for path in sorted(directory.iterdir()):
        if not private_key_candidate(path):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            skipped += 1
            continue
        try:
            # An empty passphrase never prompts; encrypted/malformed keys are skipped.
            check = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', str(path)],
                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=min(5, remaining))
            if check.returncode:
                skipped += 1
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                skipped += 1
                continue
            result = subprocess.run(['ssh-add', '-q', str(path)], env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=min(5, remaining))
            if result.returncode == 0:
                loaded += 1
            else:
                skipped += 1
        except subprocess.TimeoutExpired:
            skipped += 1
    return loaded, skipped


def main() -> int:
    try:
        loaded, skipped = load_keys(Path.home() / '.ssh')
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        print('SSH key loading failed: check the dedicated agent and ~/.ssh access.', file=sys.stderr)
        return 1
    print(f'SSH keys: loaded={loaded}, skipped={skipped}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
