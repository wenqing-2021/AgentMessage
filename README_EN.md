<p align="center">
  <img src="assets/img/ChatGPT%20Image%202026%E5%B9%B48%E6%9C%882%E6%97%A5%2013_11_44.png" width="100%" alt="AgentMessage connects a Feishu bot to Codex or Qoder in WSL, with optional Docker and GPU runtimes">
</p>

<h1 align="center">AgentMessage</h1>

<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/README-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-555555" alt="简体中文"></a>
  <a href="README_EN.md"><img src="https://img.shields.io/badge/README-English-2ea44f" alt="English"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/package%20manager-uv-DE5FE9" alt="uv">
  <img src="https://img.shields.io/badge/code%20style-PEP%208-306998" alt="Code style: PEP 8">
  <img src="https://img.shields.io/badge/tests-unittest-6C8549" alt="Tests: unittest">
  <img src="https://img.shields.io/badge/Docker-optional-2496ED?logo=docker&logoColor=white" alt="Docker optional">
  <img src="https://img.shields.io/badge/platform-WSL%20%7C%20Linux-FCC624?logo=linux&logoColor=black" alt="Platform: WSL and Linux">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2ea44f" alt="MIT License"></a>
</p>

AgentMessage forwards direct messages from a Feishu bot to Codex CLI or Qoder CLI running in WSL, then sends task progress and results back to Feishu. Project commands can also run inside an existing Docker container. The bridge uses a Feishu long connection, so it requires no public IP address, open port, or callback URL.

Key capabilities:

- Chat with Codex or Qoder according to the default project's `default_agent`.
- Restrict project paths to a local allowlist; Feishu users cannot submit arbitrary paths.
- Persist tasks, agent session IDs, message queues, and logs; projects run concurrently while each project remains serialized.
- Resume the same Codex or Qoder session, originally created through Feishu, from a local terminal.
- Optionally run CUDA/JAX commands in an isolated Bubblewrap GPU runtime.
- Optionally let Codex or Qoder edit bind-mounted source on the host and execute commands in an existing Docker container.

## Code Layout

- `core/`: configuration, domain models, and SQLite persistence.
- `channels/`: Feishu and other messaging integrations.
- `orchestration/`: command parsing, message routing, task scheduling, and service composition.
- `agents/`: Codex/Qoder adapters and supporting entry points.
- `runtimes/`: isolation policies, runners, and MCP servers for GPU and Docker execution.
- `cli.py`: the unified user-facing command-line entry point.

See [`AGENTS.md`](AGENTS.md) for architecture boundaries, extension recipes, and the development checklist.

## Installation and Configuration

Requirements: Python 3.10+, `uv`, and at least one installed and authenticated Codex CLI or Qoder CLI. Only CLIs listed by a project's `allowed_agents` are required.

```bash
cd /path/to/AgentMessage
uv sync
codex login                         # when using Codex
qodercli login                      # when using Qoder
cp config/projects.example.toml config/projects.toml
vim config/projects.toml
chmod 600 config/projects.toml
```

Minimal configuration:

```toml
[service]
state_dir = "var"
log_dir = "var/logs"
default_chat_project = "website"
codex_tool_network = false
stop_grace_seconds = 10
final_message_limit = 3500

[projects.website]
path = "/home/alice/workspace/website"
default_agent = "qoder"
allowed_agents = ["codex", "qoder"]

[projects.your_proj_name]
path = "/home/alice/workspace/your_proj_name"
default_agent = "qoder"
allowed_agents = ["codex", "qoder"]
gpu_enabled = true
gpu_network = true
gpu_timeout_seconds = 86400

[projects.container_project]
path = "/home/alice/workspace/container_project"
default_agent = "qoder"
allowed_agents = ["codex", "qoder"]
container_name = "container_project_dev"
container_path = "/workspace/container_project"
container_auto_start = true
container_timeout_seconds = 86400
```

- `website` and `your_proj_name` are project aliases, not directory paths.
- `path` must be an existing absolute path on the host.
- `default_chat_project` selects the project used by the first ordinary text message; that project's `default_agent` selects the CLI.
- `default_agent` must be present in `allowed_agents`; Codex-only, Qoder-only, and mixed projects are supported.
- `codex_tool_network = true` enables network access for ordinary Codex tools; use it only for trusted projects.
- `gpu_network` defaults to `true` when omitted; set it to `false` to isolate GPU commands from the network.
- Ordinary Qoder Bash commands may use the network, while `sudo`, dangerous Git operations, and publishing remain blocked.
- Restart the service after modifying `projects.toml`.
- `container_name` references an existing container; AgentMessage never creates, rebuilds, or removes containers.
- `container_path` is the absolute project path inside the container and is verified against a bind mount before every execution.

Qoder's `auto` permission mode, tool allowlist, and strict MCP behavior follow the official [Permissions](https://docs.qoder.com/en/cli/permissions) and [MCP Servers](https://docs.qoder.com/en/cli/mcp-servers) contracts.

Keep credentials outside the repository:

```bash
mkdir -p ~/.config/agent-message
chmod 700 ~/.config/agent-message
vim ~/.config/agent-message/feishu.env
```

Add:

```text
AGENT_MESSAGE_FEISHU_APP_ID=cli_xxx
AGENT_MESSAGE_FEISHU_APP_SECRET=xxx
AGENT_MESSAGE_ALLOWED_OPEN_IDS=
```

`AGENT_MESSAGE_ALLOWED_OPEN_IDS` can remain empty during initial setup. Add your own `open_id` later with `authorize`.
These variables are bridge-only; AgentMessage removes them from the Codex/Qoder child-process environment.

```bash
chmod 600 ~/.config/agent-message/feishu.env
set -a; source ~/.config/agent-message/feishu.env; set +a
uv run agent-message doctor
```

## Connect Feishu

Complete the following steps in the [Feishu Developer Console](https://open.feishu.cn/app):

1. Create a custom enterprise app and enable its bot capability.
2. Grant `im:message.p2p_msg:readonly` and `im:message:send_as_bot`.
3. Select long-connection event delivery and subscribe to `im.message.receive_v1`.
4. Create and publish an app version, restricting the bot's availability to yourself.

Start the service in the WSL foreground for the first run:

```bash
cd /path/to/AgentMessage
set -a; source ~/.config/agent-message/feishu.env; set +a
uv run agent-message run
```

Send `/help` to the bot in Feishu, then authorize the sender from another terminal:

```bash
cd /path/to/AgentMessage
uv run agent-message pending-senders
uv run agent-message authorize ou_xxx
```

Send `/help` again. Receiving a reply confirms the binding. The bot connects outbound from WSL, so you do not need to configure a WSL IP address.

## Run as a systemd User Service

WSL must have systemd enabled. If `systemctl --user` reports `Failed to connect to bus`, add the following to `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Run `wsl --shutdown` from Windows PowerShell, then reopen WSL.

Install the user service:

```bash
cd /path/to/AgentMessage
mkdir -p ~/.config/systemd/user
cp deploy/agent-message.service ~/.config/systemd/user/
vim ~/.config/systemd/user/agent-message.service
systemctl --user daemon-reload
systemctl --user enable --now agent-message
systemctl --user status agent-message --no-pager
```

The bundled unit assumes `%h/workspace/AgentMessage`. If the repository is elsewhere, update both `WorkingDirectory` and `ExecStart` in the unit.

systemd does not read the interactive shell's `PATH`. Locate each enabled agent first:

```bash
command -v codex
command -v qodercli
dirname "$(command -v codex)"
dirname "$(command -v qodercli)"
```

Make sure the unit's `[Service]` section contains at least:

```ini
EnvironmentFile=%h/.config/agent-message/feishu.env
Environment="PATH=/home/alice/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
```

Make sure `PATH` includes the directories containing `codex` and `qodercli`. Keep `EnvironmentFile`; it must not be replaced with a CLI path.

Follow live logs:

```bash
journalctl --user -u agent-message -f
```

`Ctrl-C` exits the log viewer without stopping the service.

After an update, restart directly; stopping first is unnecessary:

```bash
# Python, configuration, or environment variables changed
systemctl --user restart agent-message

# pyproject.toml or uv.lock changed
uv sync
systemctl --user restart agent-message

# The unit file changed
cp deploy/agent-message.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart agent-message
```

A restart interrupts running tasks but preserves their Codex/Qoder sessions for later continuation. Tasks are not rerun automatically.

## Feishu Usage Examples

Four concepts:

- **Project alias**: a short name from `projects.toml`, such as `website`.
- **Task**: one independent Codex/Qoder work session.
- **Task ID**: the unique identifier returned by the bot, such as `a1b2c3d4`.
- **Current task**: the task that receives your next ordinary text message.

Start a long-running conversation with the default project's `default_agent` (Qoder in this example):

```text
You: Inspect the current project structure without changing files.
Bot: Default Qoder conversation a1b2c3d4 queued for website.

You: Continue by analyzing the frontend entry point.
Bot: Continuation queued for task a1b2c3d4.
```

Create and continue an independent task:

```text
You: /new website Fix the overflowing mobile login button
Bot: Task a1b2c3d4 queued: website / codex.

You: Inspect the CSS first. Do not modify files; tell me the cause.
Bot: Continuation queued for task a1b2c3d4.
```

Common commands:

```text
/new website <task description>
/new website --agent qoder <task description>
/chat
/use a1b2c3d4
/status
/status a1b2c3d4
/logs a1b2c3d4 50
/logs a1b2c3d4 50 gpu
/logs a1b2c3d4 50 container
/stop a1b2c3d4
/help
```

Send `/status` whenever you are unsure which task is current. New messages sent while a task is running are queued rather than interrupting the active run.

Resume the same Codex or Qoder session in a terminal:

```bash
uv run agent-message resume a1b2c3d4
```

A session cannot be resumed in a terminal while the bot is running that task. Exit the terminal agent before continuing from Feishu.

## Bubblewrap GPU

When a project sets `gpu_enabled = true`, Codex or Qoder runs CUDA/JAX commands through the only approved `gpu_run` MCP tool. The ordinary agent shell intentionally cannot see the GPU, so a CPU-only result there is not evidence that the host GPU is unavailable.

Install and verify:

```bash
sudo apt install bubblewrap
ls -l /dev/dxg
/usr/lib/wsl/lib/nvidia-smi
uv run agent-message doctor --gpu your_proj_name
```

When a job starts successfully, Feishu receives a GPU-job notification. Use these commands for status and output:

```text
/status a1b2c3d4
/logs a1b2c3d4 50 gpu
```

If no GPU job exists and the agent only reports a CPU backend or missing `/dev/dxg`, send:

```text
Use gpu_run from agent_message_bwrap_gpu to run this command again. Do not detect the GPU through the ordinary shell.
```

The GPU sandbox mounts only the current project and required system files. The project is writable and `.git` is read-only. GPU commands can access the network by default; set `gpu_network = false` to disable it. The sandbox does not mount the host HOME, Windows drives, Feishu credentials, other projects, or the AgentMessage database.

## Docker Project Containers

AgentMessage and the selected Codex/Qoder CLI continue to run in WSL; only project commands execute in the target container. The container therefore does not need AgentMessage, an agent CLI, or copied credentials. It must already exist and bind-mount the configured host `path` read-write.

```toml
[projects.container_project]
path = "/home/alice/workspace/container_project"
default_agent = "qoder"
allowed_agents = ["codex", "qoder"]
container_name = "container_project_dev"
container_path = "/workspace/container_project"
container_auto_start = true
container_timeout_seconds = 86400
```

`container_name` may differ from the project alias. `container_path` explicitly names the path inside the container; when omitted for compatibility with an older configuration, AgentMessage infers it from `docker inspect`. The path must map to the host project's writable bind mount. `container_auto_start = true` starts only a stopped or exited container and never stops it after the task completes.

Run the real capability check first. The selector may be either the project alias or the container name:

```bash
uv run agent-message doctor --container container_project
```

Container projects support both Codex and Qoder and cannot be combined with `gpu_enabled = true`. Qoder receives no host Bash tool for a container project and can execute project commands only through the approved `container_run` MCP tool. GPU and network capabilities come from the Docker container configuration; `codex_tool_network` does not disable container networking. The container must provide `sh`, util-linux `setsid` with `--wait` support, and `kill`. The Docker socket is equivalent to a privileged local control interface and should be available only to the trusted AgentMessage service user.

## Security and Checks

- Never put the App Secret, tokens, or project private keys in the repository, logs, or Feishu messages.
- Feishu can use only allowlisted projects; Codex is fixed to `workspace-write`, while Qoder uses `auto` permissions, an explicit tool allowlist, and strict MCP configuration. Ordinary Qoder Bash commands may use the network.
- Authorize only your own `open_id` and register only projects that the Agent may modify.
- Full agent JSONL, GPU command, and container command logs are stored under `var/logs/`; Qoder thinking, hook output, and raw tool details are never forwarded to Feishu.

```bash
uv run agent-message doctor
uv run agent-message doctor --gpu your_proj_name
uv run agent-message doctor --container container_project
uv run agent-message tasks
uv run python -m unittest discover -s tests -t . -v
```
