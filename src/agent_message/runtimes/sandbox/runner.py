"""Execute project commands inside the project-scoped bubblewrap sandbox."""

from __future__ import annotations

import os
import queue
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ...core.config import ProjectConfig


class SandboxRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxRunResult:
    exit_code: int
    duration_seconds: float
    tail: str
    error: str | None = None
    cancelled: bool = False
    timed_out: bool = False


PidCallback = Callable[[int], None]


def resolve_project_cwd(project_path: Path, relative_cwd: str) -> Path:
    if not isinstance(relative_cwd, str) or not relative_cwd.strip():
        raise SandboxRunnerError("cwd must be a non-empty project-relative path")
    requested = Path(relative_cwd)
    if requested.is_absolute():
        raise SandboxRunnerError("cwd must be relative to the configured project")
    project_root = project_path.resolve()
    candidate = (project_root / requested).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError as exc:
        raise SandboxRunnerError("cwd escapes the configured project") from exc
    if not candidate.is_dir():
        raise SandboxRunnerError(f"cwd is not a directory: {relative_cwd}")
    return candidate


def _directory_chain(path: Path) -> list[Path]:
    chain: list[Path] = []
    current = path
    while current != current.parent and current != Path("/"):
        chain.append(current)
        current = current.parent
    return list(reversed(chain))


def _append_dir(command: list[str], created: set[Path], path: Path) -> None:
    for candidate in _directory_chain(path):
        if candidate not in created:
            command.extend(["--dir", str(candidate)])
            created.add(candidate)


def _append_readonly_if_present(
    command: list[str], created: set[Path], source: Path, destination: Path | None = None
) -> None:
    if not source.exists():
        return
    target = destination or source
    _append_dir(command, created, target.parent)
    command.extend(["--ro-bind", str(source), str(target)])
    if source.is_dir():
        created.add(target)


def build_bwrap_command(
    project: ProjectConfig,
    argv: list[str],
    relative_cwd: str = ".",
    *,
    bwrap_path: str | None = None,
    uv_path: str | None = None,
    require_gpu_device: bool | None = None,
) -> list[str]:
    sandbox = project.sandbox
    if sandbox is None or not sandbox.enabled:
        raise SandboxRunnerError(f"sandbox is not enabled for project {project.alias}")
    gpu_enabled = sandbox.gpu if require_gpu_device is None else require_gpu_device
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise SandboxRunnerError("argv must be a non-empty array of non-empty strings")
    if sum(len(item) for item in argv) > 64 * 1024:
        raise SandboxRunnerError("argv is too large")

    working_directory = resolve_project_cwd(project.path, relative_cwd)
    executable = bwrap_path or shutil.which("bwrap")
    if not executable:
        raise SandboxRunnerError("bubblewrap is not installed")
    dxg = Path("/dev/dxg")
    if gpu_enabled and not dxg.exists():
        raise SandboxRunnerError("/dev/dxg is not available; WSL GPU access is not enabled")
    wsl_lib = Path("/usr/lib/wsl/lib")
    if gpu_enabled and not wsl_lib.is_dir():
        raise SandboxRunnerError("/usr/lib/wsl/lib is not available")

    command = [
        executable,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
    ]
    if not sandbox.network:
        command.append("--unshare-net")
    command.extend(
        [
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/sbin",
            "/sbin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
        ]
    )
    created = {Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64")}

    for source in (
        Path("/etc/ld.so.cache"),
        Path("/etc/passwd"),
        Path("/etc/group"),
        Path("/etc/nsswitch.conf"),
        Path("/etc/localtime"),
        Path("/etc/ssl/certs"),
        # Debian-style alternatives back commands such as awk, cc and vi, so the
        # symlink farm must be visible for the toolchain to resolve.
        Path("/etc/alternatives"),
    ):
        _append_readonly_if_present(command, created, source)
    if sandbox.network:
        for source in (Path("/etc/hosts"), Path("/etc/resolv.conf")):
            _append_readonly_if_present(command, created, source)

    # Host applications installed outside /usr stay invisible unless the operator
    # lists them in [service].sandbox_readonly_paths. A configured path that
    # disappeared on the host fails the build, so the problem stays visible
    # instead of degrading into a bare "command not found".
    for source in sandbox.readonly_paths:
        if not source.exists():
            raise SandboxRunnerError(
                f"sandbox_readonly_paths entry is not available on the host: {source}"
            )
        _append_readonly_if_present(command, created, source)

    command.extend(["--proc", "/proc", "--dev", "/dev"])
    created.update({Path("/proc"), Path("/dev")})
    if gpu_enabled:
        command.extend(["--dev-bind", str(dxg), str(dxg)])
    if Path("/sys").is_dir():
        command.extend(["--ro-bind", "/sys", "/sys"])
        created.add(Path("/sys"))

    command.extend(["--tmpfs", "/tmp", "--dir", "/tmp/home", "--dir", "/tmp/cache"])
    created.update({Path("/tmp"), Path("/tmp/home"), Path("/tmp/cache")})

    project_root = project.path.resolve()
    _append_dir(command, created, project_root.parent)
    command.extend(["--bind", str(project_root), str(project_root)])
    created.add(project_root)

    resolved_uv = Path(uv_path).resolve() if uv_path else None
    if resolved_uv is None:
        found_uv = shutil.which("uv")
        resolved_uv = Path(found_uv).resolve() if found_uv else None
    path_entries = ["/usr/local/bin", "/usr/bin", "/bin"]
    if gpu_enabled:
        path_entries.insert(0, "/usr/lib/wsl/lib")
    if resolved_uv and resolved_uv.is_file() and not resolved_uv.is_relative_to(project_root):
        _append_dir(command, created, resolved_uv.parent)
        command.extend(["--ro-bind", str(resolved_uv), str(resolved_uv)])
        path_entries.insert(0, str(resolved_uv.parent))

    # Only explicit identity values and the agent socket cross this boundary.
    # Never mount ~/.ssh, private keys, or the host Git configuration.
    for suffix, value in (("NAME", sandbox.git_user_name), ("EMAIL", sandbox.git_user_email)):
        if value is not None:
            for role in ("AUTHOR", "COMMITTER"):
                command.extend(["--setenv", f"GIT_{role}_{suffix}", value])
    if sandbox.ssh_agent_socket is not None:
        try:
            socket_path = sandbox.ssh_agent_socket.resolve(strict=True)
            socket_stat = socket_path.stat()
            if not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid != os.getuid():
                raise SandboxRunnerError(
                    "sandbox_ssh_agent_socket must be a Unix socket owned by the service user"
                )
            if sandbox.ssh_known_hosts is None:
                raise SandboxRunnerError(
                    "sandbox_ssh_known_hosts is required for SSH agent forwarding"
                )
            hosts_path = sandbox.ssh_known_hosts.resolve(strict=True)
            if not hosts_path.is_file():
                raise SandboxRunnerError("sandbox_ssh_known_hosts must be a regular file")
        except OSError as exc:
            raise SandboxRunnerError("configured SSH socket or known_hosts is unavailable; check the service user's SSH agent") from exc
        command.extend([
            "--ro-bind", str(socket_path), "/tmp/agent-message-ssh-agent",
            "--ro-bind", str(hosts_path), "/tmp/agent-message-known-hosts",
            "--setenv", "SSH_AUTH_SOCK", "/tmp/agent-message-ssh-agent",
            "--setenv", "GIT_SSH_COMMAND",
            "ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes "
            "-o UserKnownHostsFile=/tmp/agent-message-known-hosts "
            "-o GlobalKnownHostsFile=/dev/null -o IdentityAgent=/tmp/agent-message-ssh-agent",
        ])

    command.extend(
        [
            "--setenv",
            "HOME",
            "/tmp/home",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "XDG_CACHE_HOME",
            "/tmp/cache",
            "--setenv",
            "UV_CACHE_DIR",
            "/tmp/cache/uv",
            "--setenv",
            "PYTHONNOUSERSITE",
            "1",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--setenv",
            "PATH",
            ":".join(path_entries),
            *(["--setenv", "LD_LIBRARY_PATH", "/usr/lib/wsl/lib"] if gpu_enabled else []),
            "--chdir",
            str(working_directory),
            "--",
            *argv,
        ]
    )
    return command


def _terminate_process_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def run_bubblewrap(
    project: ProjectConfig,
    argv: list[str],
    relative_cwd: str,
    log_path: Path,
    *,
    on_pid: PidCallback | None = None,
    cancel_event: threading.Event | None = None,
    bwrap_path: str | None = None,
    uv_path: str | None = None,
    require_gpu_device: bool | None = None,
) -> SandboxRunResult:
    sandbox = project.sandbox
    if sandbox is None or not sandbox.enabled:
        raise SandboxRunnerError(f"sandbox is not enabled for project {project.alias}")
    command = build_bwrap_command(
        project,
        argv,
        relative_cwd,
        bwrap_path=bwrap_path,
        uv_path=uv_path,
        require_gpu_device=require_gpu_device,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.monotonic()
    cancel = cancel_event or threading.Event()
    process: subprocess.Popen[bytes] | None = None
    chunks: queue.Queue[bytes | None] = queue.Queue()
    tail_lines: deque[str] = deque(maxlen=200)
    pending = bytearray()
    termination_started: float | None = None
    timed_out = False
    cancelled = False

    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(log_path, 0o600)
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if on_pid:
            on_pid(process.pid)
        assert process.stdout is not None

        def read_output() -> None:
            try:
                while True:
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.put(chunk)
            finally:
                chunks.put(None)

        reader = threading.Thread(
            target=read_output, name="agent-message-sandbox-log", daemon=True
        )
        reader.start()
        stream_done = False
        with os.fdopen(fd, "wb") as log:
            fd = -1
            while not stream_done or process.poll() is None:
                try:
                    chunk = chunks.get(timeout=0.2)
                except queue.Empty:
                    chunk = b""
                if chunk is None:
                    stream_done = True
                elif chunk:
                    log.write(chunk)
                    log.flush()
                    pending.extend(chunk)
                    if len(pending) > 1024 * 1024:
                        tail_lines.append(
                            pending[-64 * 1024 :].decode("utf-8", errors="replace")
                        )
                        pending.clear()
                    while True:
                        newline = pending.find(b"\n")
                        if newline < 0:
                            break
                        raw = bytes(pending[:newline])
                        del pending[: newline + 1]
                        tail_lines.append(raw.decode("utf-8", errors="replace").rstrip("\r"))

                elapsed = time.monotonic() - started
                if process.poll() is None and (
                    cancel.is_set() or elapsed >= sandbox.timeout_seconds
                ):
                    cancelled = cancel.is_set()
                    timed_out = not cancelled
                    if termination_started is None:
                        termination_started = time.monotonic()
                        _terminate_process_group(process, signal.SIGTERM)
                    elif time.monotonic() - termination_started >= 5:
                        _terminate_process_group(process, signal.SIGKILL)
                if stream_done and process.poll() is not None:
                    break
            if pending:
                tail_lines.append(pending.decode("utf-8", errors="replace").rstrip("\r"))
        reader.join(timeout=1)
        exit_code = process.wait()
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        return SandboxRunResult(
            127,
            time.monotonic() - started,
            "\n".join(tail_lines),
            f"failed to start bubblewrap: {exc}",
        )

    error: str | None = None
    if timed_out:
        error = f"sandbox command timed out after {sandbox.timeout_seconds} seconds"
    elif cancelled:
        error = "sandbox command was cancelled"
    elif exit_code != 0:
        error = f"sandbox command exited with code {exit_code}"
    return SandboxRunResult(
        exit_code=exit_code,
        duration_seconds=time.monotonic() - started,
        tail="\n".join(tail_lines)[-20000:],
        error=error,
        cancelled=cancelled,
        timed_out=timed_out,
    )
