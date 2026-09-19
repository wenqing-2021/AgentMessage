<p align="center">
  <img src="assets/img/logo.png" width="100%" alt="AgentMessage connects a Feishu bot to Codex or Qoder in WSL, with optional Docker and Bubblewrap sandbox runtimes">
</p>

<p align="center">
  <a href="README.md">En</a> | <a href="README_CN.md">Cn</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/package%20manager-uv-DE5FE9" alt="uv">
  <img src="https://img.shields.io/badge/code%20style-PEP%208-306998" alt="Code style: PEP 8">
  <img src="https://img.shields.io/badge/tests-unittest-6C8549" alt="Tests: unittest">
  <img src="https://visitor-badge.laobi.icu/badge?page_id=wenqing-2021.AgentMessage&amp;left_color=gray&amp;right_color=blue" alt="Visitors">
  <img src="https://img.shields.io/badge/Docker-optional-2496ED?logo=docker&logoColor=white" alt="Docker optional">
  <img src="https://img.shields.io/badge/platform-WSL%20%7C%20Linux-FCC624?logo=linux&logoColor=black" alt="Platform: WSL and Linux">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2ea44f" alt="MIT License"></a>
</p>

AgentMessage uses a Feishu long connection to forward direct bot messages to Codex CLI or Qoder CLI on WSL/Linux, then sends task progress and results back to Feishu. It requires no public IP address, open port, or callback URL.

## Features

- **Feishu remote coding:** send tasks and receive progress and results without a public IP or open port.
- **Codex and Qoder:** choose an agent per project and resume the same session from your terminal.
- **Persistent conversations:** queue tasks, track status and logs, and stop work from Feishu; different projects can run concurrently.
- **Model controls:** switch Codex models and reasoning effort, and compact context without starting a new session.
- **Bubblewrap sandbox and Docker:** give any project a controlled sandbox with writable Git metadata, and enable GPU passthrough per project.
- **Access control:** authorize users and restrict work to registered projects.

## Configure Feishu

Open the [Feishu Developer Console](https://open.feishu.cn/app):

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

The installer sets up AgentMessage, collects Feishu credentials, and starts the background service. The default directory is `~/workspace/AgentMessage`. Rerun it to resume an interrupted installation.

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
- See [Bubblewrap sandbox](assets/docs/sandbox-bubblewrap.en.md) for isolated execution, Git writes, and optional GPU/CUDA/JAX access.

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
/model
/model <model name>
/model 1
/model next
/model prev
/model 1 high
/model effort high
/model effort default
/compact
/use a1b2c3d4
/status
/status a1b2c3d4
/logs a1b2c3d4 50
/logs a1b2c3d4 50 sandbox
/logs a1b2c3d4 50 container
/stop a1b2c3d4
/send reports/result.png
/help
```

`/send` uploads a file from the current task's project to the chat: images (png/jpg/jpeg/gif/webp/bmp) arrive as Feishu image messages, everything else as file messages. Paths must stay inside the project; images are limited to 10MB and other files to 30MB.

You can also send image or file messages directly to the bot. They are downloaded into the project at `.agent-message/inbox/` and handed to the agent with their path, so the host, Bubblewrap sandboxes, and bind-mounted containers can all read them. The same size limits apply, and the directory is not cleaned automatically; consider adding `.agent-message/` to the project's `.gitignore`.

Agents send project files with the script the bridge keeps installed in the project:

```bash
sh .agent-message/bin/send-to-feishu reports/result.png
```

The script queues the file in `.agent-message/outbox/` and waits for the bridge, which does the upload with its own credentials, so nothing is exposed to the agent or its shell. It prints `已发送` and exits 0 when the file reached Feishu, and exits non-zero with the reason otherwise. The same limits apply: project files only, 10MB for images, 30MB for everything else. Add the call to the project's `AGENTS.md` if you want agents to reach for it without being asked. Unlike `/send`, this path is not queued in the durable outbox: it is a direct transfer whose result the agent reports in its own reply.

`/model` lists available models and reasoning levels. Select by name or number, cycle with `next` / `prev`, or set effort with `/model 1 high` and `/model effort high`. Use `/model effort default` to reset effort. These global Codex settings persist across restarts and take effect on the next turn.

`/compact` compresses the current Codex conversation while preserving its session. If a turn is running, compaction waits until it finishes.

Resume the same Codex/Qoder session from a terminal:

```bash
cd ~/workspace/AgentMessage
uv run agent-message resume a1b2c3d4
```

## Sandbox Git Setup

Configure the Git author identity and SSH authentication shared by all Bubblewrap sandbox projects:

```bash
cd ~/workspace/AgentMessage
uv run python scripts/configure_sandbox_git.py
```

For automatic SSH-agent startup on WSL boot, follow the [systemd setup](assets/docs/sandbox-bubblewrap.en.md#start-the-ssh-agent-automatically-with-wsl). `install.sh` installs and enables the dedicated user service by default, so no files need copying by hand; run `bash install.sh --refresh-service` to reinstall its units from the latest templates. The service scans `~/.ssh` and loads all unencrypted private keys before AgentMessage starts; linger enables startup without a terminal login and still has to be enabled by the user.

Prepare a dedicated SSH agent and verified `known_hosts` file for SSH push/pull, then restart AgentMessage after saving. See the [sandbox guide](assets/docs/sandbox-bubblewrap.en.md) for setup and troubleshooting. Container Git authentication is configured separately inside the container.

## Docker Projects

Run project commands in an existing Docker container. Bind-mount the host project directory read-write at `container_path`:

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

A container project cannot also enable the Bubblewrap sandbox. Qoder receives no host Bash tool and can use only the controlled `container_run`. Expose the Docker socket only to the trusted AgentMessage service user.

## Uninstall

Run the uninstaller from outside the repository:

```bash
cd ~
bash ~/workspace/AgentMessage/uninstall.sh
```

Removes AgentMessage and its service, with an optional backup of configuration, history, logs, and credentials. At the backup prompt, Enter defaults to no backup.

## Security and Development

- Feishu credentials remain outside the repository and are removed from Codex/Qoder child-process environments.
- Register only projects the Agent may modify. Full JSONL and command logs remain local under `var/logs/`.
- See [`AGENTS.md`](AGENTS.md) for architecture boundaries, extension recipes, and test requirements.

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor
uv run agent-message tasks
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
