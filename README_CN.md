<p align="center">
  <img src="assets/img/logo-codex.png" width="100%" alt="AgentMessage：飞书机器人连接 WSL 中的 Codex，并可选使用 Docker 或 Bubblewrap 沙箱运行时">
</p>

<p align="center">
  <a href="README.md">En</a> | <a href="README_CN.md">Cn</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/package%20manager-uv-DE5FE9" alt="uv">
  <img src="https://img.shields.io/badge/code%20style-PEP%208-306998" alt="Code style: PEP 8">
  <img src="https://img.shields.io/badge/tests-unittest-6C8549" alt="Tests: unittest">
  <img src="https://visitor-badge.laobi.icu/badge?page_id=wenqing-2021.AgentMessage&amp;left_color=gray&amp;right_color=blue" alt="访客访问次数">
  <img src="https://img.shields.io/badge/Docker-optional-2496ED?logo=docker&logoColor=white" alt="Docker optional">
  <img src="https://img.shields.io/badge/platform-WSL%20%7C%20Linux-FCC624?logo=linux&logoColor=black" alt="Platform: WSL and Linux">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2ea44f" alt="MIT License"></a>
</p>


AgentMessage 把飞书单聊接入 WSL/Linux 中的 Codex CLI，在指定项目中执行任务并回传进度、结果和文件。使用长连接，无需公网 IP 或开放端口。

## 1. 主要功能

- **任务管理**：持续对话、任务切换、排队、日志和停止；不同项目并行执行。
- **模型控制**：切换模型与思考强度，原生压缩上下文，保留会话。
- **文件收发**：发送图片或文件给 Agent，也可让 Agent 回传项目文件。
- **会话互通**：终端接续任务，飞书与 Codex Chats 双向同步。
- **隔离执行**：Bubblewrap 沙箱、可选 GPU 透传，或已有 Docker 容器。
- **多机器人**：共用代码，独立分配项目和授权用户。

## 2. 配置飞书

进入[飞书开放平台](https://open.feishu.cn/app)：

1. 创建企业自建应用，添加机器人能力。
2. 在权限管理中批量导入以下 JSON。
3. 事件订阅选择“使用长连接接收事件”，添加 `im.message.receive_v1`。
4. 发布应用版本，将可用范围限制为自己。
5. 保存 App ID 和 App Secret，安装时按提示输入。

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

## 3. 一键安装

需要 curl、已启用 systemd 的 WSL/Linux，以及已安装并登录的 Codex CLI。

```bash
curl -fsSLo /tmp/agent-message-install.sh \
  https://raw.githubusercontent.com/wenqing-2021/AgentMessage/main/install.sh &&
bash /tmp/agent-message-install.sh
```

按提示输入飞书凭证，脚本自动安装并启动服务。默认目录：`~/workspace/AgentMessage`。
重复安装、修改凭证或新增机器人见[安装说明](assets/docs/installation.md)。

## 4. 项目配置

编辑 `config/projects.toml`。下面按当前配置展示，身份、项目名和路径已替换为示例值：

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

替换 App ID、路径和 Git/SSH 信息；只保留需要的项目，并同步调整 `[bots]` 中的项目列表。路径必须存在，`sandbox_readonly_paths` 中不需要的条目应删除。
沙箱、GPU、Git/SSH 和 Docker 配置见[运行环境说明](assets/docs/runtimes.md)；完整字段见[配置示例](config/projects.example.toml)。保存后重启对应服务。

## 5. 首次授权

在飞书给机器人发送 `/help`，再在终端授权自己的账号：

```bash
cd ~/workspace/AgentMessage
uv run agent-message pending-senders
uv run agent-message authorize ou_xxx
```

再次发送 `/help`，收到回复即完成。

## 6. 飞书命令

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

## 7. Terminal 命令

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
| `cd ~ && bash ~/workspace/AgentMessage/uninstall.sh` | 卸载整个安装及所有机器人，可选备份；默认不备份。 |

多机器人命令追加 `--app-id cli_example`，服务名见[安装说明](assets/docs/installation.md)。同步会话须空闲且未归档。
