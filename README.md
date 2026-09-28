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
sandbox_readonly_paths = ["/etc/fonts"]

[projects.agent_message]
path = "/home/alice/workspace/AgentMessage"
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
projects = ["agent_message", "container_project"]
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

## 6. Commands

[Feishu and Terminal commands](assets/docs/commands.en.md)

## 7. Roadmap

- [ ] Automatic uploads to Feishu cloud documents.
- [ ] Agent quota queries.
- [ ] Monitor and related monitoring features.
