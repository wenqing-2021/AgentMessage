from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from .adapters import CodexAdapter, QoderAdapter
from .config import ConfigError, load_config
from .models import AgentKind, TaskStatus
from .service import run_feishu_bridge
from .state import StateStore


def _config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default="config/projects.toml",
        help="trusted project registry TOML (default: config/projects.toml)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-message", description="Feishu bridge for local coding agents")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("run", "start the Feishu long-connection service"),
        ("doctor", "check configuration and available agent CLIs"),
        ("tasks", "list locally persisted tasks"),
        ("pending-senders", "list recent Feishu sender open_ids for local bootstrap"),
    ):
        child = subcommands.add_parser(name, help=help_text)
        _config_argument(child)
    authorize = subcommands.add_parser("authorize", help="allow a Feishu open_id locally")
    _config_argument(authorize)
    authorize.add_argument("open_id")
    resume = subcommands.add_parser("resume", help="open a persisted Codex task in the terminal")
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


def command_doctor(config_path: str) -> int:
    config = load_config(config_path)
    print(f"配置：{config.config_path}")
    print(f"状态目录：{config.service.state_dir}")
    print(f"Codex 工具网络：{'开启' if config.service.codex_tool_network else '关闭'}")
    print("项目：")
    for project in config.projects.values():
        agents = ", ".join(agent.value for agent in sorted(project.allowed_agents, key=str))
        print(f"  {project.alias}: {project.path} ({agents})")
    print(f"Codex：{_version('codex')}")
    if CodexAdapter().available():
        _, exec_help = _command_output(["codex", "exec", "--help"])
        _, resume_help = _command_output(["codex", "exec", "resume", "--help"])
        contract_ok = "--json" in exec_help and "--output-last-message" in exec_help and "--json" in resume_help
        login_status, _ = _command_output(["codex", "login", "status"])
        print(f"  Codex JSON/恢复契约：{'通过' if contract_ok else '不兼容'}")
        print(f"  Codex 登录：{'已登录' if login_status == 0 else '未登录或无法确认'}")
    print(f"Qoder：{_version('qodercli')}")
    if QoderAdapter().available():
        _, qoder_help = _command_output(["qodercli", "--help"])
        contract_ok = "--output-format" in qoder_help and ("-r" in qoder_help or "--resume" in qoder_help)
        print(f"  Qoder JSON/恢复契约：{'通过' if contract_ok else '不兼容'}")
    print(
        "飞书凭证："
        + ("已设置" if config.app_id and config.app_secret else "缺失（仅 run 命令需要）")
    )
    local_prefix = Path(sys.executable).with_name("agent-message-qoder-shell")
    if QoderAdapter().available() and not (local_prefix.is_file() or shutil.which("agent-message-qoder-shell")):
        print("警告：找不到 agent-message-qoder-shell；Qoder Bash 将不具备额外的无网络封装。")
    if not CodexAdapter().available():
        print("警告：Codex 未启用。")
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
    if task.agent != AgentKind.CODEX:
        print(f"任务 {task_id} 使用的是 {task.agent.value}，只能在终端恢复 Codex 任务。", file=sys.stderr)
        return 2
    if not task.session_id:
        print(f"任务 {task_id} 尚未创建 Codex 会话，无法恢复。", file=sys.stderr)
        return 2
    if task.status == TaskStatus.RUNNING:
        print(f"任务 {task_id} 正在由桥接服务运行；请等待完成或先 /stop，避免并发恢复同一会话。", file=sys.stderr)
        return 2
    project = config.projects[task.project_alias]
    command = [
        "codex",
        "-c",
        "sandbox_workspace_write.network_access="
        + ("true" if config.service.codex_tool_network else "false"),
        "resume",
        "--include-non-interactive",
        "--sandbox",
        "workspace-write",
        "-C",
        str(project.path),
        task.session_id,
    ]
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        print("找不到 codex；请确认 system PATH 和 Codex CLI 安装。", file=sys.stderr)
        return 127


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            raise SystemExit(command_doctor(args.config))
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
            asyncio.run(run_feishu_bridge(config))
            return
        raise AssertionError(f"unknown command: {args.command}")
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        print("已停止。", file=sys.stderr)


if __name__ == "__main__":
    main()
