from __future__ import annotations

import os
import sys


def main() -> None:
    """Run one Qoder-generated Bash command without network access.

    QODER_SHELL_PREFIX passes the complete, quoted command as argv[1].  `exec`
    keeps stdout, stderr, exit code and signals visible to the Qoder process.
    """

    if len(sys.argv) != 2:
        raise SystemExit("agent-message-qoder-shell expects exactly one command argument")
    os.execvp("unshare", ["unshare", "-Urn", "--", "bash", "-c", sys.argv[1]])


if __name__ == "__main__":
    main()

