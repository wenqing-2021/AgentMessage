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


AgentMessage connects Feishu direct messages to Codex CLI on WSL/Linux. Run tasks in registered projects and receive progress, results, and files through a long connection—no public IP or open port required.

## 1. Features

- **Task management:** ongoing conversations, task switching, queues, logs, and cancellation; concurrent work across projects.
- **Model controls:** switch models and reasoning effort, and compact context while keeping the session.
- **File exchange:** send images or files to the Agent and receive project files back.
- **Session continuity:** resume tasks in a terminal and sync sessions between Feishu and Codex Chats.
- **Isolated execution:** Bubblewrap with optional GPU passthrough, or an existing Docker container.
- **Multiple bots:** shared code with separate project assignments and user authorization.

## 2. Configure Feishu

Open the [Feishu Developer Console](https://open.feishu.cn/app):

1. Create an enterprise self-built app and add the bot capability.
2. Batch-import the following JSON in permission management.
3. Select long-connection event delivery and add `im.message.receive_v1`.
4. Publish the app version, restricting availability to yourself.
5. Save the App ID and App Secret for the installer.

```json
{
  "scopes": {
    "tenant": [
      "docs:document.comment:create",
      "docs:document.comment:read",
      "docx:document",
      "im:message.p2p_msg:readonly",
      "im:message:send_as_bot",
      "im:resource"
    ],
    "user": []
  }
}
```

## 3. One-command Installation

Requires curl, WSL/Linux with systemd enabled, and an installed, authenticated Codex CLI.

```bash
curl -fsSLo /tmp/agent-message-install.sh \
  https://raw.githubusercontent.com/wenqing-2021/AgentMessage/main/install.sh &&
bash /tmp/agent-message-install.sh
```

Enter the Feishu credentials when prompted. The installer sets up and starts the service in `~/workspace/AgentMessage` by default.
See [installation details](assets/docs/installation.en.md) to rerun the installer, change credentials, or add bots.

## 4. Project Configuration

Edit `config/projects.toml`. This mirrors the current configuration with example identities, project names, and paths:

```toml
[service]
feishu_app_id = "cli_example"
state_dir = "var"
log_dir = "var/logs"
default_chat_project = "agent_message"
codex_tool_network = true
stop_grace_seconds = 10
final_message_limit = 3500
sandbox_git_user_name = "Alice"
sandbox_git_user_email = "alice@example.com"
sandbox_ssh_agent_socket = "/run/user/1000/agent-message-ssh/agent.sock"
sandbox_ssh_known_hosts = "/home/alice/.ssh/known_hosts"
sandbox_readonly_paths = ["/opt/quarto", "/etc/fonts"]

[projects.agent_message]
sandbox_enabled = true
sandbox_gpu = false
path = "/home/alice/workspace/AgentMessage"
default_agent = "codex"
allowed_agents = ["codex"]

[projects.planning]
path = "/home/alice/workspace/Planning"
default_agent = "codex"
allowed_agents = ["codex"]
sandbox_enabled = true
sandbox_network = true
sandbox_timeout_seconds = 86400
sandbox_gpu = false

[projects.training]
path = "/home/alice/workspace/Training"
default_agent = "codex"
allowed_agents = ["codex"]
sandbox_enabled = true
sandbox_network = true
sandbox_timeout_seconds = 86400
sandbox_gpu = true

[projects.experiments]
path = "/home/alice/workspace/Experiments"
default_agent = "codex"
allowed_agents = ["codex"]
sandbox_enabled = true
sandbox_network = true
sandbox_timeout_seconds = 86400
sandbox_gpu = true

[projects.container_project]
path = "/home/alice/workspace/ContainerProject"
default_agent = "codex"
allowed_agents = ["codex"]
container_name = "project_dev"
container_path = "/root/workspace/ContainerProject"
container_auto_start = true
container_timeout_seconds = 86400

[bots.cli_example]
projects = ["agent_message", "planning", "training", "experiments", "container_project"]
default_chat_project = "agent_message"
```

Replace the App ID, paths, and Git/SSH details. Keep only the projects you need and update the `[bots]` project list. Paths must exist; remove unused `sandbox_readonly_paths` entries.
See [runtime setup](assets/docs/runtimes.en.md) for sandbox, GPU, Git/SSH, and Docker configuration, or the [configuration example](config/projects.example.toml) for available fields. Restart the corresponding service after editing.

## 5. First Authorization

Send `/help` to the bot, then authorize your account locally:

```bash
cd ~/workspace/AgentMessage
uv run agent-message pending-senders
uv run agent-message authorize ou_xxx
```

Send `/help` again. A reply confirms the connection.

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

## 7. Terminal Commands

Run from the installation directory: `cd ~/workspace/AgentMessage`.

| Command | Purpose |
| --- | --- |
| `bash update.sh` | Update code, dependencies, and all bot services. |
| `bash install.sh` | Rerun installation or add a bot. |
| `bash install.sh --refresh-service` | Reinstall service configuration. |
| `systemctl --user restart agent-message` | Restart the default bot. |
| `systemctl --user status agent-message --no-pager` | Check service status. |
| `journalctl --user -u agent-message -f` | Follow service logs. |
| `uv run agent-message doctor` | Check configuration and runtimes. |
| `uv run agent-message tasks` | List tasks. |
| `uv run agent-message pending-senders` | List users awaiting authorization. |
| `uv run agent-message authorize ou_xxx` | Authorize a user. |
| `uv run agent-message resume <task-id>` | Resume a session in the terminal. |
| `uv run sync-feishu-to-codex <feishu-task-id>` | Sync a Feishu session to Codex Chats. |
| `uv run sync-codex-to-feishu "Title shown in Chats"` | Sync a Codex Chats session to Feishu; partial titles work. |
| `cd ~ && bash ~/workspace/AgentMessage/uninstall.sh` | Remove the shared installation and all bots; optional backup defaults to no. |

Append `--app-id cli_example` to CLI commands to select a bot; see [installation details](assets/docs/installation.en.md) for service names. Session sync requires an idle, unarchived session.
