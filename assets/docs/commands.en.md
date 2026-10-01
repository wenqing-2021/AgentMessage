# Command Reference

## Feishu Commands

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

## Terminal Commands

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
| `uv run sync-feishu-to-codex <feishu-task-id>` | Sync a Feishu session to Codex Chats; the owning bot is detected automatically. |
| `uv run sync-codex-to-feishu "Title shown in Chats"` | Sync a Codex Chats session to Feishu; partial titles work and the owning bot is detected automatically. |
| `cd ~ && bash ~/workspace/AgentMessage/uninstall.sh` | Interactively select additional bots to remove; `a`/`all` or `--yes` removes the whole shared installation, and `--app-id` targets one additional bot directly. |

Append `--app-id cli_example` to the other CLI commands to select a bot; both sync commands detect the owning bot automatically (an explicit `--app-id` still wins). See [installation details](installation.en.md) for service names. Session sync requires an idle, unarchived session.

[Back to README](../../README.md)
