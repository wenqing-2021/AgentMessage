"""Execute project commands inside an approved Docker container."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from ...core.config import ProjectConfig


class ContainerRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContainerTarget:
    name: str
    status: str
    project_path: PurePosixPath


@dataclass(frozen=True)
class ContainerRunResult:
    exit_code: int
    duration_seconds: float
    tail: str
    error: str | None = None
    cancelled: bool = False
    timed_out: bool = False


PidCallback = Callable[[int], None]
_START_TIMEOUT_SECONDS = 15.0
_TERMINATE_GRACE_SECONDS = 5.0
_PID_MARKER = b"__AGENT_MESSAGE_CONTAINER_PID__="
_KILL_SESSION_SCRIPT = """
signal_name=$1
target_session=$2
targets=
for stat_path in /proc/[0-9]*/stat; do
    stat_line=
    IFS= read -r stat_line < "$stat_path" || continue
    stat_tail=${stat_line##*) }
    set -- $stat_tail
    if [ "${4:-}" = "$target_session" ]; then
        process_id=${stat_path#/proc/}
        process_id=${process_id%/stat}
        targets="$targets $process_id"
    fi
done
[ -n "$targets" ] || exit 1
kill "$signal_name" $targets
""".strip()


def _docker_path() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise ContainerRunnerError("docker CLI is not installed or not in PATH")
    return executable


def _inspect(container_name: str, *, timeout: float = 10.0) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [_docker_path(), "inspect", container_name],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerRunnerError(f"failed to inspect container {container_name}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ContainerRunnerError(
            f"cannot inspect configured container {container_name}: "
            f"{detail[:1000] or f'exit code {completed.returncode}'}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContainerRunnerError(f"docker inspect returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ContainerRunnerError("docker inspect returned an unexpected payload")
    return payload[0]


def _status(inspect: dict[str, object]) -> str:
    state = inspect.get("State")
    if not isinstance(state, dict) or not isinstance(state.get("Status"), str):
        raise ContainerRunnerError("docker inspect did not include State.Status")
    return str(state["Status"])


def _start(project: ProjectConfig) -> dict[str, object]:
    container = project.container
    if container is None:
        raise ContainerRunnerError(f"container execution is not enabled for {project.alias}")
    try:
        completed = subprocess.run(
            [_docker_path(), "start", container.name],
            capture_output=True,
            text=True,
            timeout=_START_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerRunnerError(f"failed to start container {container.name}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ContainerRunnerError(
            f"failed to start container {container.name}: "
            f"{detail[:1000] or f'exit code {completed.returncode}'}"
        )
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    latest = _inspect(container.name)
    while _status(latest) in {"created", "restarting"} and time.monotonic() < deadline:
        time.sleep(0.2)
        latest = _inspect(container.name)
    return latest


def _container_project_path(
    project_path: Path,
    inspect: dict[str, object],
    configured_path: PurePosixPath | None = None,
) -> PurePosixPath:
    project_root = project_path.resolve()
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list):
        raise ContainerRunnerError("docker inspect did not include Mounts")
    matches: list[tuple[int, bool, Path, PurePosixPath]] = []
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("Type") != "bind":
            continue
        source_raw = mount.get("Source")
        destination_raw = mount.get("Destination")
        if not isinstance(source_raw, str) or not isinstance(destination_raw, str):
            continue
        source = Path(source_raw).resolve()
        try:
            relative = project_root.relative_to(source)
        except ValueError:
            continue
        destination = PurePosixPath(destination_raw)
        target = destination.joinpath(*relative.parts)
        matches.append((len(source.parts), mount.get("RW") is True, source, target))
    if not matches:
        raise ContainerRunnerError(
            f"container has no bind mount containing host project path: {project_root}"
        )
    if configured_path is not None:
        configured_matches = [item for item in matches if item[3] == configured_path]
        if not configured_matches:
            inferred = ", ".join(
                str(item[3]) for item in sorted(matches, key=lambda item: item[0], reverse=True)
            )
            raise ContainerRunnerError(
                f"configured container_path {configured_path} does not match the host project "
                f"bind mount; docker inspect maps it to: {inferred}"
            )
        matches = configured_matches
    _, writable, source, target = max(matches, key=lambda item: item[0])
    if not writable:
        raise ContainerRunnerError(
            f"deepest matching bind mount is read-only: {source} -> {target}"
        )
    if not target.is_absolute():
        raise ContainerRunnerError(f"container bind destination is not absolute: {target}")
    return target


def resolve_container_target(
    project: ProjectConfig, *, allow_start: bool
) -> ContainerTarget:
    container = project.container
    if container is None:
        raise ContainerRunnerError(f"container execution is not enabled for {project.alias}")
    inspected = _inspect(container.name)
    status = _status(inspected)
    if status in {"created", "exited"}:
        if not allow_start or not container.auto_start:
            raise ContainerRunnerError(
                f"container {container.name} is {status}; start it first or enable "
                "container_auto_start"
            )
        inspected = _start(project)
        status = _status(inspected)
    if status != "running":
        raise ContainerRunnerError(
            f"container {container.name} is {status}; only a running container can accept commands"
        )
    return ContainerTarget(
        name=container.name,
        status=status,
        project_path=_container_project_path(
            project.path, inspected, container.project_path
        ),
    )


def inspect_container_target(project: ProjectConfig) -> ContainerTarget:
    """Inspect status and mount mapping without starting or otherwise changing the container."""
    container = project.container
    if container is None:
        raise ContainerRunnerError(f"container execution is not enabled for {project.alias}")
    inspected = _inspect(container.name)
    return ContainerTarget(
        name=container.name,
        status=_status(inspected),
        project_path=_container_project_path(
            project.path, inspected, container.project_path
        ),
    )


def resolve_project_cwd(project_path: Path, relative_cwd: str) -> tuple[Path, Path]:
    if not isinstance(relative_cwd, str) or not relative_cwd.strip():
        raise ContainerRunnerError("cwd must be a non-empty project-relative path")
    requested = Path(relative_cwd)
    if requested.is_absolute():
        raise ContainerRunnerError("cwd must be relative to the configured project")
    project_root = project_path.resolve()
    candidate = (project_root / requested).resolve()
    try:
        relative = candidate.relative_to(project_root)
    except ValueError as exc:
        raise ContainerRunnerError("cwd escapes the configured project") from exc
    if not candidate.is_dir():
        raise ContainerRunnerError(f"cwd is not a directory: {relative_cwd}")
    return candidate, relative


def build_container_command(
    project: ProjectConfig,
    argv: list[str],
    relative_cwd: str = ".",
    *,
    allow_start: bool = True,
) -> tuple[list[str], ContainerTarget]:
    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise ContainerRunnerError("argv must be a non-empty array of non-empty strings")
    if sum(len(item) for item in argv) > 64 * 1024:
        raise ContainerRunnerError("argv is too large")
    _, relative = resolve_project_cwd(project.path, relative_cwd)
    target = resolve_container_target(project, allow_start=allow_start)
    container_cwd = target.project_path.joinpath(*relative.parts)
    wrapper = 'printf "__AGENT_MESSAGE_CONTAINER_PID__=%s\\n" "$$"; exec "$@"'
    command = [
        _docker_path(),
        "exec",
        "--workdir",
        str(container_cwd),
        target.name,
        "setsid",
        "--wait",
        "sh",
        "-c",
        wrapper,
        "agent-message-container",
        *argv,
    ]
    return command, target


def terminate_container_process(
    container_name: str, container_pid: int, sig: signal.Signals
) -> bool:
    signal_name = "-TERM" if sig == signal.SIGTERM else "-KILL"
    try:
        completed = subprocess.run(
            [
                _docker_path(),
                "exec",
                container_name,
                "sh",
                "-c",
                _KILL_SESSION_SCRIPT,
                "agent-message-container-kill",
                signal_name,
                str(container_pid),
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _terminate_host_process(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def run_container(
    project: ProjectConfig,
    argv: list[str],
    relative_cwd: str,
    log_path: Path,
    *,
    on_pid: PidCallback | None = None,
    on_container_pid: PidCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> ContainerRunResult:
    container = project.container
    if container is None:
        raise ContainerRunnerError(f"container execution is not enabled for {project.alias}")
    command, target = build_container_command(project, argv, relative_cwd)
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.monotonic()
    cancel = cancel_event or threading.Event()
    chunks: queue.Queue[bytes | None] = queue.Queue()
    tail_lines: deque[str] = deque(maxlen=200)
    pending = bytearray()
    process: subprocess.Popen[bytes] | None = None
    container_pid: int | None = None
    stop_requested_at: float | None = None
    termination_started: float | None = None
    timed_out = False
    cancelled = False
    marker_seen = False

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
                    # BufferedReader.read(size) may wait for `size` bytes or EOF. The PID marker
                    # must be delivered immediately so cancellation can target the container
                    # session instead of only terminating the host docker client.
                    chunk = os.read(process.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        break
                    chunks.put(chunk)
            finally:
                chunks.put(None)

        reader = threading.Thread(
            target=read_output, name="agent-message-container-log", daemon=True
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
                    pending.extend(chunk)
                    while True:
                        newline = pending.find(b"\n")
                        if newline < 0:
                            break
                        raw = bytes(pending[:newline]).rstrip(b"\r")
                        del pending[: newline + 1]
                        if not marker_seen and raw.startswith(_PID_MARKER):
                            marker_seen = True
                            try:
                                container_pid = int(raw[len(_PID_MARKER) :])
                            except ValueError:
                                container_pid = None
                            if container_pid is not None and on_container_pid:
                                on_container_pid(container_pid)
                            continue
                        log.write(raw + b"\n")
                        log.flush()
                        tail_lines.append(raw.decode("utf-8", errors="replace"))
                    if len(pending) > 1024 * 1024:
                        raw = bytes(pending)
                        pending.clear()
                        log.write(raw)
                        log.flush()
                        tail_lines.append(raw[-64 * 1024 :].decode("utf-8", errors="replace"))

                now = time.monotonic()
                elapsed = now - started
                should_stop = cancel.is_set() or elapsed >= container.timeout_seconds
                if should_stop:
                    cancelled = cancel.is_set()
                    timed_out = not cancelled
                    if stop_requested_at is None:
                        stop_requested_at = now
                    if termination_started is None and container_pid is not None:
                        termination_started = now
                        terminate_container_process(target.name, container_pid, signal.SIGTERM)
                    elif (
                        termination_started is None
                        and process.poll() is None
                        and now - stop_requested_at >= 2.0
                    ):
                        # A valid wrapper prints its PID immediately. If startup is broken, do not
                        # leave the host docker client running forever while waiting for a marker.
                        termination_started = now
                        if container_pid is None:
                            _terminate_host_process(process, signal.SIGTERM)
                    elif (
                        termination_started is not None
                        and now - termination_started >= _TERMINATE_GRACE_SECONDS
                    ):
                        if container_pid is not None:
                            terminate_container_process(
                                target.name, container_pid, signal.SIGKILL
                            )
                        _terminate_host_process(process, signal.SIGKILL)
                if stream_done and process.poll() is not None:
                    break
            if pending:
                log.write(pending)
                tail_lines.append(pending.decode("utf-8", errors="replace"))
        reader.join(timeout=1)
        exit_code = process.wait()
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        return ContainerRunResult(
            127,
            time.monotonic() - started,
            "\n".join(tail_lines),
            f"failed to start docker exec: {exc}",
        )

    error: str | None = None
    if timed_out:
        error = f"container command timed out after {container.timeout_seconds} seconds"
    elif cancelled:
        error = "container command was cancelled"
    elif not marker_seen:
        error = "container command failed before the process group was established"
    elif exit_code != 0:
        error = f"container command exited with code {exit_code}"
    return ContainerRunResult(
        exit_code=exit_code,
        duration_seconds=time.monotonic() - started,
        tail="\n".join(tail_lines)[-20000:],
        error=error,
        cancelled=cancelled,
        timed_out=timed_out,
    )
