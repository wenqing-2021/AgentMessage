from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .agents.adapters import (
    AdapterError,
    CodexAdapter,
    QoderAdapter,
    adapter_for,
)
from .core.config import AppConfig, ConfigError, ProjectConfig, load_config
from .core.models import TaskStatus
from .core.state import StateStore
from .runtimes.container.runner import (
    ContainerRunnerError,
    inspect_container_target,
    run_container,
)
from .runtimes.sandbox.runner import (
    SandboxRunnerError,
    build_bwrap_command,
    run_bubblewrap,
)


def _config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default="config/projects.toml",
        help="trusted project registry TOML (default: config/projects.toml)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-message", description="Feishu bridge for local coding agents")
    subcommands = parser.add_subparsers(dest="command", required=True)
    doctor = subcommands.add_parser("doctor", help="check configuration and available agent CLIs")
    _config_argument(doctor)
    doctor.add_argument(
        "--sandbox",
        "--gpu",
        dest="sandbox_project",
        metavar="PROJECT_ALIAS",
        help="run a real bubblewrap probe for one sandbox-enabled project",
    )
    doctor.add_argument(
        "--container",
        metavar="PROJECT_OR_CONTAINER",
        help="run a real Docker probe selected by project alias or container name",
    )
    for name, help_text in (
        ("run", "start the Feishu long-connection service"),
        ("tasks", "list locally persisted tasks"),
        ("pending-senders", "list recent Feishu sender open_ids for local bootstrap"),
    ):
        child = subcommands.add_parser(name, help=help_text)
        _config_argument(child)
    authorize = subcommands.add_parser("authorize", help="allow a Feishu open_id locally")
    _config_argument(authorize)
    authorize.add_argument("open_id")
    resume = subcommands.add_parser(
        "resume", help="open a persisted Codex or Qoder task in the terminal"
    )
    _config_argument(resume)
    resume.add_argument("task_id")
    return parser


def _version(command: str) -> str:
    path = shutil.which(command)
    if not path:
        return "未安装"
    completed = subprocess.run(
        [command, "--version"], capture_output=True, text=True, timeout=10, check=False
    )
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return output[0] if output else f"已找到：{path}"


def _command_output(command: list[str]) -> tuple[int, str]:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    return completed.returncode, (completed.stdout + completed.stderr)


def _qoder_login_status() -> bool | None:
    try:
        status, output = _command_output(
            ["qodercli", "--setting-sources", "", "status", "-o", "json"]
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if status != 0:
        return False
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    logged_in = payload.get("logged_in") if isinstance(payload, dict) else None
    return logged_in if isinstance(logged_in, bool) else None


def _sandbox_doctor(config: AppConfig, project_alias: str) -> int:
    project = config.projects.get(project_alias)
    if project is None:
        aliases = ", ".join(sorted(config.projects))
        print(f"沙箱检查失败：未知项目 {project_alias}；可用项目：{aliases}", file=sys.stderr)
        return 2
    sandbox = project.sandbox
    if sandbox is None or not sandbox.enabled:
        print(
            f"沙箱检查失败：项目 {project_alias} 未设置 sandbox_enabled = true。",
            file=sys.stderr,
        )
        return 2
    python_path = project.path / ".venv" / "bin" / "python"
    if not python_path.is_file():
        print(f"沙箱检查失败：找不到项目 Python：{python_path}", file=sys.stderr)
        return 2
    sensitive_paths = [
        str(config.config_path),
        str(config.service.state_dir / "agent-message.sqlite3"),
        str(Path.home() / ".ssh"),
        str(Path.home() / ".codex"),
        str(Path.home() / ".config" / "agent-message"),
        "/mnt/c",
        *[
            str(candidate.path)
            for alias, candidate in config.projects.items()
            if alias != project_alias
        ],
    ]
    sensitive_literal = json.dumps(sensitive_paths)
    isolation_probe = (
        f"sensitive={sensitive_literal}; "
        "print(json.dumps({'project_visible': os.path.isdir(os.getcwd()), "
        "'sandbox_home': os.environ.get('HOME'), "
        "'sensitive_visible': [item for item in sensitive if os.path.exists(item)], "
        "'feishu_env_visible': any(key.startswith('AGENT_MESSAGE_FEISHU_') "
        "for key in os.environ)}))"
    )
    if sandbox.gpu:
        probe = (
            "import json, os, jax; " + isolation_probe + "; "
            "print(json.dumps({'backend': jax.default_backend(), "
            "'devices': [str(item) for item in jax.devices()]}))"
        )
    else:
        probe = "import json, os; " + isolation_probe
    with tempfile.TemporaryDirectory(prefix="agent-message-sandbox-doctor-") as temp:
        log_path = Path(temp) / "sandbox-doctor.log"
        try:
            result = run_bubblewrap(
                project,
                [str(python_path), "-c", probe],
                ".",
                log_path,
            )
        except SandboxRunnerError as exc:
            print(f"沙箱检查失败：{exc}", file=sys.stderr)
            return 2
    payload: dict[str, object] | None = None
    for line in reversed(result.tail.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "sandbox_home" in candidate:
            payload = candidate
            break
    if result.exit_code != 0 or payload is None:
        print(f"沙箱检查失败：{result.error or '没有收到探针结果'}", file=sys.stderr)
        if result.tail:
            print(result.tail, file=sys.stderr)
        return 2
    isolation_ok = (
        payload.get("project_visible") is True
        and payload.get("sandbox_home") == "/tmp/home"
        and payload.get("sensitive_visible") == []
        and payload.get("feishu_env_visible") is False
    )
    if sandbox.gpu:
        devices = payload.get("devices")
        backend = payload.get("backend")
        gpu_ok = backend == "gpu" and isinstance(devices, list) and any(
            "cuda" in str(item).lower() for item in devices
        )
        print(f"GPU JAX backend：{backend}")
        print(f"GPU JAX devices：{devices}")
        if not gpu_ok:
            print("沙箱检查失败：JAX 未识别 cuda 设备。", file=sys.stderr)
            return 2
    print(
        "沙箱敏感路径与环境隔离："
        + ("通过" if isolation_ok else f"失败（{payload}）")
    )
    if not isolation_ok:
        return 2
    print("bubblewrap 沙箱契约：通过")
    return 0


def _container_project_for_selector(
    config: AppConfig, selector: str
) -> ProjectConfig:
    project = config.projects.get(selector)
    if project is not None:
        return project
    matches = [
        candidate
        for candidate in config.projects.values()
        if candidate.container is not None and candidate.container.name == selector
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        aliases = ", ".join(sorted(candidate.alias for candidate in matches))
        raise ValueError(
            f"容器 {selector} 同时用于多个项目：{aliases}；请改用项目别名"
        )
    aliases = ", ".join(sorted(config.projects))
    containers = ", ".join(
        sorted(
            {
                candidate.container.name
                for candidate in config.projects.values()
                if candidate.container is not None
            }
        )
    )
    detail = f"；已配置容器：{containers}" if containers else ""
    raise ValueError(
        f"未知项目或容器 {selector}；可用项目别名：{aliases}{detail}"
    )


def _container_doctor(config: AppConfig, selector: str) -> int:
    try:
        project = _container_project_for_selector(config, selector)
    except ValueError as exc:
        print(f"容器检查失败：{exc}", file=sys.stderr)
        return 2
    if project.container is None:
        print(
            f"容器检查失败：项目 {project.alias} 未设置 container_name。",
            file=sys.stderr,
        )
        return 2
    probe = (
        "command -v sh >/dev/null && command -v setsid >/dev/null && "
        "command -v kill >/dev/null && test -d . && test -w . && pwd"
    )
    with tempfile.TemporaryDirectory(prefix="agent-message-container-doctor-") as temp:
        log_path = Path(temp) / "container-doctor.log"
        try:
            result = run_container(project, ["sh", "-c", probe], ".", log_path)
        except ContainerRunnerError as exc:
            print(f"容器检查失败：{exc}", file=sys.stderr)
            return 2
    if result.exit_code != 0:
        print(f"容器检查失败：{result.error or '探测命令失败'}", file=sys.stderr)
        if result.tail:
            print(result.tail, file=sys.stderr)
        return 2
    target = inspect_container_target(project)
    print(f"容器：{target.name}（{target.status}）")
    print(f"容器项目路径：{target.project_path}")
    print("容器 sh/setsid --wait/kill 与读写挂载：通过")
    return 0


def _bwrap_capability(project: ProjectConfig) -> tuple[bool, str]:
    try:
        command = build_bwrap_command(
            project,
            ["/usr/bin/true"],
        )
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (SandboxRunnerError, OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if completed.returncode == 0:
        return True, "通过"
    detail = (completed.stderr or completed.stdout).strip().splitlines()
    return False, detail[-1][:300] if detail else f"退出码 {completed.returncode}"


def command_doctor(
    config_path: str,
    sandbox_project: str | None = None,
    container_project: str | None = None,
) -> int:
    config = load_config(config_path)
    if sandbox_project and container_project:
        print("沙箱检查与容器检查一次只能选择一个。", file=sys.stderr)
        return 2
    print(f"配置：{config.config_path}")
    print(f"状态目录：{config.service.state_dir}")
    print(f"Codex 工具网络：{'开启' if config.service.codex_tool_network else '关闭'}")
    print("项目：")
    for project in config.projects.values():
        agents = ", ".join(agent.value for agent in sorted(project.allowed_agents, key=str))
        sandbox = project.sandbox
        sandbox_text = (
            f"，bubblewrap 沙箱已启用（"
            f"{'GPU 透传' if sandbox.gpu else '无 GPU'}，"
            f"{'联网' if sandbox.network else '无网络'}，"
            f"超时 {sandbox.timeout_seconds}s）"
            if sandbox and sandbox.enabled
            else ""
        )
        container = project.container
        container_text = (
            f"，Docker 容器 {container.name}（"
            f"容器路径 {container.project_path or '自动推导'}，"
            f"{'自动启动' if container.auto_start else '需预先运行'}，"
            f"超时 {container.timeout_seconds}s）"
            if container
            else ""
        )
        print(f"  {project.alias}: {project.path} ({agents}){sandbox_text}{container_text}")
        if sandbox and sandbox.enabled:
            isolation_ok, isolation_detail = _bwrap_capability(project)
            print(
                "    Bubblewrap 隔离能力："
                + ("通过" if isolation_ok else f"失败（{isolation_detail}）")
            )
        if container:
            try:
                target = inspect_container_target(project)
            except ContainerRunnerError as exc:
                print(f"    Docker 映射：失败（{exc}）")
            else:
                print(
                    f"    Docker 映射：{target.status}，{project.path} -> "
                    f"{target.project_path}"
                )
    print(f"Codex：{_version('codex')}")
    if CodexAdapter().available():
        _, exec_help = _command_output(["codex", "exec", "--help"])
        _, resume_help = _command_output(["codex", "exec", "resume", "--help"])
        contract_ok = (
            "--json" in exec_help
            and "--output-last-message" in exec_help
            and "--json" in resume_help
        )
        login_status, _ = _command_output(["codex", "login", "status"])
        print(f"  Codex JSON/恢复契约：{'通过' if contract_ok else '不兼容'}")
        print(f"  Codex 登录：{'已登录' if login_status == 0 else '未登录或无法确认'}")
    print(f"Qoder：{_version('qodercli')}")
    if QoderAdapter().available():
        _, qoder_help = _command_output(["qodercli", "--help"])
        _, qoder_mcp_help = _command_output(["qodercli", "mcp", "--help"])
        required_flags = (
            "--output-format",
            "--resume",
            "--permission-mode",
            "--tools",
            "--allowed-tools",
            "--disallowed-tools",
            "--mcp-config",
            "--strict-mcp-config",
            "--setting-sources",
        )
        _, format_probe = _command_output(
            [
                "qodercli",
                "--tools",
                "",
                "--no-session-persistence",
                "-p",
                "probe",
                "--output-format",
                "__agent_message_probe__",
            ]
        )
        contract_ok = (
            all(flag in qoder_help for flag in required_flags)
            and "stream-json" in format_probe
            and "list" in qoder_mcp_help
        )
        login = _qoder_login_status()
        print(f"  Qoder JSON/恢复契约：{'通过' if contract_ok else '不兼容'}")
        print(
            "  Qoder 登录："
            + (
                "已登录"
                if login is True
                else "未登录或当前环境无法连接"
                if login is False
                else "无法确认"
            )
        )
        print("  Qoder Bash 网络：允许")
    print(
        "飞书凭证："
        + ("已设置" if config.app_id and config.app_secret else "缺失（仅 run 命令需要）")
    )
    if not CodexAdapter().available():
        print("警告：Codex 未启用。")
    print(f"Bubblewrap：{_version('bwrap')}")
    print(f"Docker：{_version('docker')}")
    print(f"WSL GPU 设备：{'可用' if Path('/dev/dxg').exists() else '不可用（缺少 /dev/dxg）'}")
    print(
        "WSL CUDA 库："
        + ("可用" if Path("/usr/lib/wsl/lib").is_dir() else "不可用（缺少 /usr/lib/wsl/lib）")
    )
    if sandbox_project:
        return _sandbox_doctor(config, sandbox_project)
    if container_project:
        return _container_doctor(config, container_project)
    return 0


def command_tasks(config_path: str) -> int:
    config = load_config(config_path)
    state = StateStore(config)
    try:
        tasks = state.list_tasks()
        if not tasks:
            print("没有任务。")
            return 0
        for task in tasks:
            print(
                f"{task.id}\t{task.status.value}\t{task.agent.value}\t"
                f"{task.project_alias}\t{task.created_at}\t{task.last_summary or ''}"
            )
    finally:
        state.close()
    return 0


def command_authorize(config_path: str, open_id: str) -> int:
    config = load_config(config_path)
    state = StateStore(config)
    try:
        state.authorize(open_id)
    finally:
        state.close()
    print(f"已授权飞书 open_id：{open_id}")
    return 0


def command_pending_senders(config_path: str) -> int:
    config = load_config(config_path)
    state = StateStore(config)
    try:
        senders = state.recent_senders()
    finally:
        state.close()
    if not senders:
        print("尚未收到飞书消息。")
        return 0
    for open_id, last_seen in senders:
        print(f"{open_id}\t{last_seen}")
    return 0


def command_resume(config_path: str, task_id: str) -> int:
    config = load_config(config_path)
    state = StateStore(config)
    try:
        task = state.get_task(task_id)
    finally:
        state.close()
    if task is None:
        print(f"找不到任务 ID：{task_id}", file=sys.stderr)
        return 2
    if not task.session_id:
        print(f"任务 {task_id} 尚未创建 {task.agent.value} 会话，无法恢复。", file=sys.stderr)
        return 2
    if task.status == TaskStatus.RUNNING:
        print(f"任务 {task_id} 正在由桥接服务运行；请等待完成或先 /stop，避免并发恢复同一会话。", file=sys.stderr)
        return 2
    project = config.projects[task.project_alias]
    adapter = adapter_for(
        task.agent,
        codex_tool_network=config.service.codex_tool_network,
        app_config=config,
    )
    try:
        command = adapter.build_terminal_resume_command(task)
    except AdapterError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        return subprocess.run(
            command,
            cwd=project.path,
            env=adapter.environment(),
            check=False,
        ).returncode
    except FileNotFoundError:
        print(
            f"找不到 {adapter.executable}；请确认 system PATH 和 CLI 安装。",
            file=sys.stderr,
        )
        return 127


async def _run_service(config: AppConfig) -> None:
    # lark_oapi captures asyncio.get_event_loop() at import time. Import it only after
    # asyncio.run() has installed the service loop so subprocess handling stays attached
    # to the active loop on Python 3.10.
    from .orchestration.service import run_feishu_bridge

    await run_feishu_bridge(config)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            raise SystemExit(command_doctor(args.config, args.sandbox_project, args.container))
        if args.command == "tasks":
            raise SystemExit(command_tasks(args.config))
        if args.command == "authorize":
            raise SystemExit(command_authorize(args.config, args.open_id))
        if args.command == "pending-senders":
            raise SystemExit(command_pending_senders(args.config))
        if args.command == "resume":
            raise SystemExit(command_resume(args.config, args.task_id))
        if args.command == "run":
            config = load_config(args.config)
            asyncio.run(_run_service(config))
            return
        raise AssertionError(f"unknown command: {args.command}")
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        print("已停止。", file=sys.stderr)


if __name__ == "__main__":
    main()
