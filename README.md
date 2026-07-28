<p align="center">
  <img src="assets/img/ChatGPT%20Image%202026%E5%B9%B48%E6%9C%882%E6%97%A5%2013_11_44.png" width="100%" alt="AgentMessage：飞书机器人连接 WSL 中的 Codex/Qoder，并可选使用 Docker 或 GPU 运行时">
</p>

<h1 align="center">AgentMessage</h1>

<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/README-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-2ea44f" alt="简体中文"></a>
  <a href="README_EN.md"><img src="https://img.shields.io/badge/README-English-555555" alt="English"></a>
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

AgentMessage 将飞书机器人单聊消息转发给 WSL 中的 Codex CLI 或 Qoder CLI，并把任务进度和结果发回飞书。项目命令也可以在已有 Docker 容器中执行。它使用飞书长连接，不需要公网 IP、端口或回调 URL。

主要能力：

- 飞书普通文本按项目的 `default_agent` 与 Codex 或 Qoder 长期对话。
- 项目路径使用本地白名单，飞书不能提交任意路径。
- 保存任务、Agent session ID、消息队列和日志；不同项目可并行，同一项目串行。
- 可在终端恢复飞书创建的同一个 Codex 或 Qoder session。
- 可选 Bubblewrap GPU runner，让 Codex/Qoder 在隔离环境中运行 CUDA/JAX 命令。
- 可选 Docker container runner，让 Codex/Qoder 在宿主读写 bind-mounted 源码、在项目容器中执行命令。

## 代码结构

- `core/`：配置、领域模型和 SQLite 持久化。
- `channels/`：飞书等消息通道。
- `orchestration/`：命令解析、消息路由、任务调度和服务编排。
- `agents/`：Codex/Qoder 适配器及其辅助入口。
- `runtimes/`：GPU 与 Docker 的隔离执行策略、runner 和 MCP 服务。
- `cli.py`：面向用户的统一命令行入口。

架构边界、扩展方法和开发检查清单请阅读 [`AGENTS.md`](AGENTS.md)。

## 安装与配置

要求 Python 3.10+、`uv`，以及至少一个已安装并登录的 Codex CLI 或 Qoder CLI。只需使用配置中 `allowed_agents` 列出的 CLI。

```bash
cd /path/to/AgentMessage
uv sync
codex login                         # 使用 Codex 时
qodercli login                      # 使用 Qoder 时
cp config/projects.example.toml config/projects.toml
vim config/projects.toml
chmod 600 config/projects.toml
```

最小配置示例：

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

- `website`、`your_proj_name` 是项目别名，不是目录路径。
- `path` 必须是本机存在的绝对路径。
- `default_chat_project` 是首次直接发送普通文本时使用的项目，实际 Agent 由该项目的 `default_agent` 决定。
- `default_agent` 必须出现在 `allowed_agents` 中；Codex-only、Qoder-only 和二者并存都受支持。
- `codex_tool_network = true` 允许 Codex 的普通工具联网，仅对可信项目启用。
- `gpu_network` 省略时默认为 `true`；设置为 `false` 才会隔离 GPU 命令网络。
- Qoder 的普通 Bash 默认允许联网，但仍禁止 `sudo`、危险 Git 操作和发布命令。
- 修改 `projects.toml` 后需要重启服务。
- `container_name` 只引用已有容器；AgentMessage 不创建、重建或删除容器。
- `container_path` 是项目在容器内的绝对路径；每次执行前都会用 bind mount 校验。

Qoder 的 `auto` 权限、工具白名单和严格 MCP 行为以官方的 [Permissions](https://docs.qoder.com/en/cli/permissions) 与 [MCP Servers](https://docs.qoder.com/en/cli/mcp-servers) 契约为准。

凭证必须放在仓库外：

```bash
mkdir -p ~/.config/agent-message
chmod 700 ~/.config/agent-message
vim ~/.config/agent-message/feishu.env
```

写入：

```text
AGENT_MESSAGE_FEISHU_APP_ID=cli_xxx
AGENT_MESSAGE_FEISHU_APP_SECRET=xxx
AGENT_MESSAGE_ALLOWED_OPEN_IDS=
```

`AGENT_MESSAGE_ALLOWED_OPEN_IDS` 首次可以留空，稍后用 `authorize` 添加自己的 `open_id`。
这些变量只供飞书桥接服务使用；AgentMessage 启动 Codex/Qoder 时会从子进程环境中移除它们。

```bash
chmod 600 ~/.config/agent-message/feishu.env
set -a; source ~/.config/agent-message/feishu.env; set +a
uv run agent-message doctor
```

## 连接飞书

在[飞书开放平台开发者后台](https://open.feishu.cn/app)完成：

1. 创建企业自建应用并添加机器人能力。
2. 开通 `im:message.p2p_msg:readonly` 和 `im:message:send_as_bot`。
3. 在事件订阅中选择“使用长连接接收事件”，添加 `im.message.receive_v1`。
4. 创建并发布应用版本，将机器人可用范围限制为自己。

首次在 WSL 前台启动：

```bash
cd /path/to/AgentMessage
set -a; source ~/.config/agent-message/feishu.env; set +a
uv run agent-message run
```

在飞书中给机器人发送 `/help`，然后另开终端授权发送者：

```bash
cd /path/to/AgentMessage
uv run agent-message pending-senders
uv run agent-message authorize ou_xxx
```

再次发送 `/help`，收到回复即表示绑定成功。机器人由 WSL 主动连接飞书，不需要填写 WSL IP。

## systemd 常驻运行

WSL 必须启用 systemd。若 `systemctl --user` 报 `Failed to connect to bus`，在 `/etc/wsl.conf` 中加入：

```ini
[boot]
systemd=true
```

然后在 Windows PowerShell 执行 `wsl --shutdown`，重新打开 WSL。

安装用户服务：

```bash
cd /path/to/AgentMessage
mkdir -p ~/.config/systemd/user
cp deploy/agent-message.service ~/.config/systemd/user/
vim ~/.config/systemd/user/agent-message.service
systemctl --user daemon-reload
systemctl --user enable --now agent-message
systemctl --user status agent-message --no-pager
```

仓库自带 unit 默认使用 `%h/workspace/AgentMessage`。如果仓库位于其他目录，必须在 unit 中同步修改 `WorkingDirectory` 和 `ExecStart`。

systemd 不读取交互式 shell 的 `PATH`。查看已启用 Agent 的路径：

```bash
command -v codex
command -v qodercli
dirname "$(command -v codex)"
dirname "$(command -v qodercli)"
```

确认 unit 的 `[Service]` 至少包含：

```ini
EnvironmentFile=%h/.config/agent-message/feishu.env
Environment="PATH=/home/alice/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
```

确保 `PATH` 包含 `codex`/`qodercli` 所在目录。`EnvironmentFile` 必须保留，不能替换成 CLI 路径。

查看实时日志：

```bash
journalctl --user -u agent-message -f
```

按 `Ctrl-C` 只退出日志查看，不会停止服务。

更新后通常直接重启，无需先停止：

```bash
# Python、配置或环境变量有变化
systemctl --user restart agent-message

# pyproject.toml 或 uv.lock 有变化
uv sync
systemctl --user restart agent-message

# unit 文件有变化
cp deploy/agent-message.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart agent-message
```

重启会中断正在运行的任务，但会保留 Codex/Qoder session，之后可以继续；任务不会自动重跑。

## 飞书使用示例

四个概念：

- **项目别名**：`projects.toml` 中的短名称，例如 `website`。
- **任务**：一次独立的 Codex/Qoder 工作会话。
- **任务 ID**：机器人返回的唯一编号，例如 `a1b2c3d4`。
- **当前任务**：直接发送普通文本时将继续的任务。

直接与默认项目的 `default_agent` 长期聊天（以下项目配置为 Qoder）：

```text
你：检查当前项目结构，不要修改文件。
机器人：默认 Qoder 对话 a1b2c3d4 已排队：website。

你：继续分析前端入口。
机器人：已安排继续任务 a1b2c3d4。
```

创建独立任务并继续对话：

```text
你：/new website 修复手机端登录按钮溢出
机器人：任务 a1b2c3d4 已排队：website / codex。

你：先检查 CSS，不要修改，告诉我原因。
机器人：已安排继续任务 a1b2c3d4。
```

常用命令：

```text
/new website <任务描述>
/new website --agent qoder <任务描述>
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

不确定当前任务时先发送 `/status`。运行中的新消息会排队，不会打断当前执行。

在终端恢复同一个 Codex 或 Qoder session：

```bash
uv run agent-message resume a1b2c3d4
```

任务正在由机器人运行时不能同时从终端恢复；退出终端 Agent 后再从飞书继续。

## Bubblewrap GPU

项目设置 `gpu_enabled = true` 后，Codex 或 Qoder 会通过唯一获准的 `gpu_run` MCP 工具运行 CUDA/JAX 命令。普通 Agent shell 按设计看不到 GPU，不能用其中的 CPU 结果判断宿主 GPU 不可用。

安装并验收：

```bash
sudo apt install bubblewrap
ls -l /dev/dxg
/usr/lib/wsl/lib/nvidia-smi
uv run agent-message doctor --gpu your_proj_name
```

成功运行时，飞书会收到“GPU 作业已开始”，并且以下命令能看到状态和输出：

```text
/status a1b2c3d4
/logs a1b2c3d4 50 gpu
```

若没有 GPU job，而 Agent 只报告 CPU 或缺少 `/dev/dxg`，发送：

```text
请通过 agent_message_bwrap_gpu 的 gpu_run 重新执行，不要使用普通 shell 检测 GPU。
```

GPU 沙箱只挂载当前项目和必要系统文件；项目可写、`.git` 只读。GPU 命令默认可以联网；设置 `gpu_network = false` 可关闭网络。沙箱不挂载宿主 HOME、Windows 目录、飞书凭证或 AgentMessage 数据库。

## Docker 项目容器

AgentMessage 和所选 Codex/Qoder CLI 仍运行在 WSL；目标容器只执行项目命令，因此不需要安装 AgentMessage、Agent CLI 或复制凭证。要求容器已经存在，并将配置中的宿主 `path` 以读写 bind mount 挂入容器。

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

`container_name` 与项目别名可以不同。`container_path` 显式指定项目在容器内的路径；若为兼容旧配置而省略，则从 `docker inspect` 自动推导。该路径必须对应宿主 `path` 的可写 bind mount。`container_auto_start = true` 只会启动 stopped/exited 容器，任务结束后不会停止它。先运行真实检查；参数可使用项目别名或容器名：

```bash
uv run agent-message doctor --container container_project
```

容器项目支持 Codex 和 Qoder，但不能同时设置 `gpu_enabled = true`。Qoder 在容器项目中不获得宿主 Bash，只能通过唯一获准的 `container_run` MCP 执行项目命令。GPU 和网络能力继承容器本身的 Docker 配置；`codex_tool_network` 不会关闭容器网络。容器必须提供 `sh`、支持 `--wait` 的 util-linux `setsid` 和 `kill`。Docker socket 等价于高权限本机控制接口，只应向受信任的 AgentMessage 服务用户开放。

## 安全与检查

- App Secret、token 和项目私钥不要写入仓库、日志或飞书消息。
- 飞书只能使用项目白名单；Codex 固定为 `workspace-write`，Qoder 使用 `auto` 权限、显式工具白名单和严格 MCP。Qoder 普通 Bash 允许联网。
- 只授权自己的 `open_id`，只登记允许 Agent 修改的项目。
- 完整 Agent JSONL、GPU 和容器命令日志保存在 `var/logs/`；Qoder thinking/hook/工具原始输出不会转发到飞书。

```bash
uv run agent-message doctor
uv run agent-message doctor --gpu your_proj_name
uv run agent-message doctor --container container_project
uv run agent-message tasks
uv run python -m unittest discover -s tests -t . -v
```
