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

AgentMessage uses a Feishu long connection to forward direct bot messages to Codex CLI or Qoder CLI on WSL/Linux, then sends task progress and results back to Feishu. It requires no public IP address, open port, or callback URL.

## Configure Feishu First

This step is independent of the repository and can be completed before installing AgentMessage. Open the [Feishu Developer Console](https://open.feishu.cn/app):

1. Create an enterprise self-built app and add the bot capability.
2. Grant `im:message.p2p_msg:readonly` and `im:message:send_as_bot`.
3. Select long-connection event delivery and add `im.message.receive_v1`.
4. Create and publish an app version, restricting the bot's availability to yourself.
5. Keep the App ID and App Secret ready. The installer asks for them in the terminal and does not echo the Secret.

## One-command Installation

Use curl on Ubuntu/WSL or another common systemd Linux distribution, with at least one Codex CLI or Qoder CLI installed. Agent CLI authentication still uses its official command:

```bash
codex login       # when using Codex
qodercli login    # when using Qoder
```

Download the installer over HTTPS and run it:

```bash
curl -fsSLo /tmp/agent-message-install.sh \
  https://raw.githubusercontent.com/wenqing-2021/AgentMessage/main/install.sh &&
bash /tmp/agent-message-install.sh
```

The script installs to `~/workspace/AgentMessage` by default and automatically:

- Clones over HTTPS or fast-forwards an existing checkout.
- Installs Git, systemd, and uv when missing, then runs `uv sync --frozen`.
- Generates a minimal `config/projects.toml` for the detected Codex/Qoder CLI.
- Reads the App ID and hidden App Secret from the terminal and stores them outside the repository in `~/.config/agent-message/feishu.env`.
- Installs and starts the `agent-message.service` systemd user service.

Progress is checkpointed in local Git metadata. If credential input or systemd setup is interrupted, run the same command again to continue from that stage; an App ID already entered is not requested again.

To use another install directory:

```bash
bash /tmp/agent-message-install.sh --install-dir /absolute/path/AgentMessage
```

If systemd is installed but inactive in WSL, the installer pauses with the required `/etc/wsl.conf` and `wsl --shutdown` instructions. Reopen WSL and run it again to resume.

## Project Configuration

The installer initially registers the AgentMessage repository itself as the default project. Add your own projects by editing:

```bash
vim ~/workspace/AgentMessage/config/projects.toml
```

Minimal project configuration:

```toml
[service]
state_dir = "var"
log_dir = "var/logs"
default_chat_project = "website"
codex_tool_network = false

[projects.website]
path = "/home/alice/workspace/website"
default_agent = "qoder"
allowed_agents = ["codex", "qoder"]
```

- `path` must be an existing absolute local path; Feishu messages cannot submit arbitrary paths.
- `default_agent` must be present in `allowed_agents`; Codex-only, Qoder-only, and mixed projects are supported.
- Ordinary Codex tools can use the network only when `codex_tool_network = true`; ordinary Qoder Bash commands can use the network by default.
- See [`config/projects.example.toml`](config/projects.example.toml) for all common fields.
- See [GPU and Bubblewrap](assets/docs/gpu-bubblewrap.en.md) for isolated GPU/CUDA/JAX execution.

Use the restart commands at the end of this README after changing the configuration.

## First Authorization

After installation, send `/help` to the bot in Feishu, then inspect and authorize your own `open_id`:

```bash
cd ~/workspace/AgentMessage
uv run agent-message pending-senders
uv run agent-message authorize ou_xxx
```

Send `/help` again. A bot response confirms the connection. Authorize only your own account.

Inspect the service and logs with:

```bash
systemctl --user status agent-message --no-pager
journalctl --user -u agent-message -f
```

## Feishu Commands

Ordinary text continues the current task. When there is no current task, the default project's `default_agent` starts a long-lived conversation.

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

Resume the same Codex/Qoder session from a terminal:

```bash
cd ~/workspace/AgentMessage
uv run agent-message resume a1b2c3d4
```

## Docker Projects

AgentMessage and the Agent CLI run on the host, while project commands execute through `container_run` inside an existing container. The container must bind-mount the host project directory read-write at `container_path`.

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

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor --container container_project
```

A container project cannot also enable the Bubblewrap GPU runtime. Qoder receives no host Bash tool and can use only the controlled `container_run`. Expose the Docker socket only to the trusted AgentMessage service user.

## Uninstall

Run the uninstaller from outside the repository:

```bash
cd ~
bash ~/workspace/AgentMessage/uninstall.sh
```

The script asks whether to retain the project registry, task database, logs, and Feishu credentials. Pressing Enter selects the default of deleting them; a second confirmation is required before removal. Retained data is copied to `~/.local/share/agent-message/backups/`.

## Security and Development

- Feishu credentials remain outside the repository and are removed from Codex/Qoder child-process environments.
- Register only projects the Agent may modify. Full JSONL and command logs remain local under `var/logs/`.
- Qoder uses `auto` permissions, an explicit tool allowlist, and strict MCP; Codex uses `workspace-write`.
- See [`AGENTS.md`](AGENTS.md) for architecture boundaries, extension recipes, and test requirements.

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor
uv run agent-message tasks
uv run python -m unittest discover -s tests -t . -v
```

## Update and Restart

```bash
cd ~/workspace/AgentMessage

# Python, projects.toml, or feishu.env changed
systemctl --user restart agent-message

# pyproject.toml or uv.lock changed
uv sync --frozen
systemctl --user restart agent-message

# The systemd unit changed
bash install.sh --refresh-service
```
