# 命令参考

## 飞书命令

直接发送文字即可继续当前任务；没有当前任务时，会创建默认项目的对话。`<项目>` 是配置中的项目别名，`<ID>` 是任务 ID，可通过 `/status` 查看。

| 命令 | 功能 |
| --- | --- |
| `/new <项目> <描述>` | 创建并切换到新任务。 |
| `/chat` | 回到默认项目的长期对话。 |
| `/use <ID>` | 切换到指定任务，后续消息继续该会话。 |
| `/status [ID]` | 查看当前或指定任务的 ID 和状态。 |
| `/list` | 显示当前项目最新的 5 个未归档 Codex Chats 标题。 |
| `/history <ID>` | 查看该任务最近一次同步的最后 20 条对话。 |
| `/model` | 查看可用的 Codex 模型编号和思考强度。 |
| `/model 1` | 按编号切换模型，也可填写模型名称。 |
| `/model next` | 切换到下一个模型，`prev` 切换到上一个。 |
| `/model 1 high` | 同时设置模型和思考强度。 |
| `/model effort high` | 只设置思考强度，`default` 恢复默认。 |
| `/compact` | 压缩当前 Codex 上下文并保留会话，运行中则排队。 |
| `/logs <ID> 20` | 查看最近 20 行 Agent 日志。 |
| `/logs <ID> 20 sandbox` | 查看沙箱日志，改为 `container` 可查看容器日志。 |
| `/stop <ID>` | 停止指定任务。 |
| `/send <相对路径>` | 将当前项目内的图片或文件发送到飞书。 |
| `/help` | 查看命令帮助。 |

模型设置从下一轮生效，重启后保留。直接发送图片或文件即可交给 Agent，也可让 Agent 把项目文件发回来；图片限 10MB，其他文件限 30MB。

## Terminal 命令

在安装目录运行：`cd ~/workspace/AgentMessage`。

| 命令 | 用途 |
| --- | --- |
| `bash update.sh` | 更新代码、依赖和各机器人服务。 |
| `bash install.sh` | 重复安装或新增机器人。 |
| `bash install.sh --refresh-service` | 重新安装服务配置。 |
| `systemctl --user restart agent-message` | 重启默认机器人。 |
| `systemctl --user status agent-message --no-pager` | 查看服务状态。 |
| `journalctl --user -u agent-message -f` | 查看实时日志。 |
| `uv run agent-message doctor` | 检查配置与运行环境。 |
| `uv run agent-message tasks` | 列出任务。 |
| `uv run agent-message pending-senders` | 查看待授权用户。 |
| `uv run agent-message authorize ou_xxx` | 授权用户。 |
| `uv run agent-message resume <任务ID>` | 在终端接续会话。 |
| `uv run sync-feishu-to-codex <飞书任务ID>` | 将飞书会话同步到 Codex Chats。 |
| `uv run sync-codex-to-feishu "Chats 中的对话标题"` | 将 Codex Chats 会话同步到飞书，支持部分标题。 |
| `cd ~ && bash ~/workspace/AgentMessage/uninstall.sh` | 交互式选择要删除的新增机器人；`a`/`all` 或加 `--yes` 卸载整个共用安装，`--app-id` 直接删除指定新增机器人。 |

多机器人命令追加 `--app-id cli_example`，服务名见[安装说明](installation.md)。同步会话须空闲且未归档。

[返回 README](../../README_CN.md)
