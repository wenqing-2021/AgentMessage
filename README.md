<p align="center">
  <img src="assets/img/logo-codex.png" width="100%" alt="AgentMessage connects a Feishu bot to Codex in WSL, with optional Docker and Bubblewrap sandbox runtimes">
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

AgentMessage uses a Feishu long connection to forward direct bot messages to Codex CLI on WSL/Linux, then sends task progress and results back to Feishu. It requires no public IP address, open port, or callback URL.

## 1. Features

- **Feishu remote coding:** send tasks and receive progress and results without a public IP or open port.
- **Codex:** run tasks in configured projects and resume the same session from your terminal.
- **Persistent conversations:** queue tasks, track status and logs, and stop work from Feishu; different projects can run concurrently.
- **Model controls:** switch Codex models and reasoning effort, and compact context without starting a new session.
- **Bubblewrap sandbox and Docker:** give any project a controlled sandbox with writable Git metadata, and enable GPU passthrough per project.
- **Access control:** authorize users and restrict work to registered projects.

## 2. Configure Feishu

Open the [Feishu Developer Console](https://open.feishu.cn/app):

1. Create an enterprise self-built app and add the bot capability.
2. Grant `im:message.p2p_msg:readonly` and `im:message:send_as_bot`.
3. Select long-connection event delivery and add `im.message.receive_v1`.
4. Create and publish an app version, restricting the bot's availability to yourself.
5. Keep the App ID and App Secret ready. The installer asks for them in the terminal and does not echo the Secret.

## 3. One-command Installation

Use curl on Ubuntu/WSL or another common systemd Linux distribution, with Codex CLI installed. Agent CLI authentication still uses its official command:

```bash
codex login
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

## 4. Project Configuration

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
default_agent = "codex"
allowed_agents = ["codex"]
```

- `path` must be an existing absolute local path; Feishu messages cannot submit arbitrary paths.
- `default_agent` must be present in `allowed_agents`; only `codex` is supported.
- Ordinary Codex tools can use the network only when `codex_tool_network = true`.
- See [`config/projects.example.toml`](config/projects.example.toml) for all common fields.
- See [Bubblewrap sandbox](assets/docs/sandbox-bubblewrap.en.md) for isolated execution, Git writes, optional GPU/CUDA/JAX access, and exposing host tools through `[service].sandbox_readonly_paths`.

Use the restart commands at the end of this README after changing the configuration.

## 5. First Authorization

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

## 6. Feishu Commands

Send text to continue the current task; if none is selected, a conversation starts in the default project. `<project>` is a configured alias, and `<id>` is the task ID shown by `/status`.

| Command | Function |
| --- | --- |
| `/new <project> <description>` | Create and select a new task. |
| `/chat` | Return to the default project's ongoing conversation. |
| `/use <id>` | Select a task for subsequent messages. |
| `/status [id]` | Show the current or specified task's ID and status. |
| `/list` | Show the current project's 5 newest unarchived Codex Chats titles. |
| `/history <id>` | Show the last 20 messages from the task's latest sync. |
| `/model` | List available Codex model numbers and reasoning levels. |
| `/model 1` | Select a model by number or name. |
| `/model next` | Select the next model; use `prev` for the previous one. |
| `/model 1 high` | Set both the model and reasoning effort. |
| `/model effort high` | Set reasoning effort only; use `default` to reset it. |
| `/compact` | Compact the current Codex context, retaining the session; queue if busy. |
| `/logs <id> 20` | Show the last 20 lines of Agent logs. |
| `/logs <id> 20 sandbox` | Show sandbox logs; use `container` for container logs. |
| `/stop <id>` | Stop the specified task. |
| `/send <relative-path>` | Send an image or file from the current project to Feishu. |
| `/help` | Show command help. |

Model settings apply from the next turn and persist across restarts. Send images or files directly to the Agent, or ask it to send project files back; limits are 10MB for images and 30MB for other files.

Continue the same session in a terminal: `uv run agent-message resume <id>`.

## 7. Sandbox Git Setup

Configure the Git author identity and SSH authentication shared by all Bubblewrap sandbox projects:

```bash
cd ~/workspace/AgentMessage
uv run python scripts/configure_sandbox_git.py
```

For automatic SSH-agent startup on WSL boot, follow the [systemd setup](assets/docs/sandbox-bubblewrap.en.md#start-the-ssh-agent-automatically-with-wsl). `install.sh` installs and enables the dedicated user service by default, so no files need copying by hand; run `bash install.sh --refresh-service` to reinstall its units from the latest templates. The service scans `~/.ssh` and loads all unencrypted private keys before AgentMessage starts; linger enables startup without a terminal login and still has to be enabled by the user.

Prepare a dedicated SSH agent and verified `known_hosts` file for SSH push/pull, then restart AgentMessage after saving. See the [sandbox guide](assets/docs/sandbox-bubblewrap.en.md) for setup and troubleshooting. Container Git authentication is configured separately inside the container.

## 8. Docker Projects

Run project commands in an existing Docker container. Bind-mount the host project directory read-write at `container_path`:

```toml
[projects.container_project]
path = "/home/alice/workspace/container_project"
default_agent = "codex"
allowed_agents = ["codex"]
container_name = "container_project_dev"
container_path = "/workspace/container_project"
container_auto_start = true
container_timeout_seconds = 86400
```

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor --container container_project
```

A container project cannot also enable the Bubblewrap sandbox. Project commands run through the controlled `container_run` tool. Expose the Docker socket only to the trusted AgentMessage service user.

## 9. Uninstall

Run the uninstaller from outside the repository:

```bash
cd ~
bash ~/workspace/AgentMessage/uninstall.sh
```

Removes AgentMessage and its service, with an optional backup of configuration, history, logs, and credentials. At the backup prompt, Enter defaults to no backup.

## 10. Security and Development

- Feishu credentials remain outside the repository and are removed from Codex child-process environments.
- Register only projects the Agent may modify. Full JSONL and command logs remain local under `var/logs/`.
- See [`AGENTS.md`](AGENTS.md) for architecture boundaries, extension recipes, and test requirements.

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor
uv run agent-message tasks
```

## 11. Update and Restart

```bash
cd ~/workspace/AgentMessage

# Code update: pull, sync dependencies, restart both services, print service status and loaded keys
bash update.sh

# After a service in deploy/ changed (update.sh runs this itself when needed)
bash install.sh --refresh-service

# Only config/projects.toml or feishu.env changed
systemctl --user restart agent-message

# For a fuller check (configuration, agent CLIs, sandbox, Docker)
uv run agent-message doctor
```

## 12. Synchronize Feishu and Codex Chats sessions

Run these two commands from the repository directory. No Codex UUID lookup is needed:

```bash
uv run sync-feishu-to-codex <feishu-task-id>
uv run sync-codex-to-feishu "Title shown in Chats"
```

Feishu `/list` shows the current project’s 5 newest unarchived Codex Chats titles.

## 13. Planned Features

- [ ] Automatically upload files to Feishu.
- [ ] Monitor: track task progress.
- [ ] Usage: query usage quotas.
