<p align="center">
  <img src="assets/img/logo.png" width="100%" alt="AgentMessage：飞书机器人连接 WSL 中的 Codex/Qoder，并可选使用 Docker 或 GPU 运行时">
</p>

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

AgentMessage 通过飞书长连接，把机器人单聊消息交给 WSL/Linux 中的 Codex CLI 或 Qoder CLI，再将任务进度和结果发回飞书；不需要公网 IP、端口或回调 URL。

## 先配置飞书

这一步独立于仓库，可以在安装 AgentMessage 前完成。进入[飞书开放平台开发者后台](https://open.feishu.cn/app)：

1. 创建企业自建应用并添加机器人能力。
2. 开通 `im:message.p2p_msg:readonly` 和 `im:message:send_as_bot` 权限。
3. 在事件订阅中选择“使用长连接接收事件”，添加 `im.message.receive_v1`。
4. 创建并发布应用版本，将机器人可用范围限制为自己。
5. 保存 App ID 和 App Secret；安装脚本稍后会在终端中询问，Secret 输入不会回显。

## 一键安装

需要 curl、Ubuntu/WSL 或常见 systemd Linux，以及至少一个已安装的 Codex CLI 或 Qoder CLI。Agent CLI 登录仍使用各自的官方命令：

```bash
codex login       # 使用 Codex 时
qodercli login    # 使用 Qoder 时
```

从 HTTPS 下载安装脚本并运行：

```bash
curl -fsSLo /tmp/agent-message-install.sh \
  https://raw.githubusercontent.com/wenqing-2021/AgentMessage/main/install.sh &&
bash /tmp/agent-message-install.sh
```

脚本默认安装到 `~/workspace/AgentMessage`，并自动完成：

- 通过 HTTPS clone 或 fast-forward 更新项目。
- 缺少时安装 Git、systemd 和 uv，并执行 `uv sync --frozen`。
- 根据已安装的 Codex/Qoder 生成最小 `config/projects.toml`。
- 在终端读取 App ID 和隐藏输入的 App Secret，写入仓库外的 `~/.config/agent-message/feishu.env`。
- 安装并启动 `agent-message.service` systemd 用户服务。

安装进度保存在本地 Git 元数据中。若在凭证输入或 systemd 配置阶段退出，重新运行同一命令会直接从断点继续；已经输入的 App ID 不需要重复输入。

自定义安装目录时使用：

```bash
bash /tmp/agent-message-install.sh --install-dir /absolute/path/AgentMessage
```

如果 systemd 已安装但尚未在 WSL 中启用，脚本会暂停并给出 `/etc/wsl.conf` 和 `wsl --shutdown` 提示；重新打开 WSL 后再次运行即可继续。

## 项目配置

安装脚本会先把 AgentMessage 仓库本身注册为默认项目。添加自己的项目时编辑：

```bash
vim ~/workspace/AgentMessage/config/projects.toml
```

最小项目配置：

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

- `path` 必须是本机存在的绝对路径，飞书消息不能提交任意路径。
- `default_agent` 必须包含在 `allowed_agents` 中，支持 Codex-only、Qoder-only 或二者并存。
- `codex_tool_network = true` 才会允许普通 Codex 工具联网；Qoder 普通 Bash 默认可以联网。
- 完整字段见 [`config/projects.example.toml`](config/projects.example.toml)。
- GPU/CUDA/JAX 隔离运行见 [GPU 与 Bubblewrap 指南](assets/docs/gpu-bubblewrap.md)。

修改配置后使用 README 最后的重启命令。

## 首次授权

安装完成后，在飞书中给机器人发送 `/help`，再在终端查看并授权自己的 `open_id`：

```bash
cd ~/workspace/AgentMessage
uv run agent-message pending-senders
uv run agent-message authorize ou_xxx
```

再次发送 `/help`，收到回复即表示连接完成。只授权自己的账号。

查看服务与日志：

```bash
systemctl --user status agent-message --no-pager
journalctl --user -u agent-message -f
```

## 飞书命令

普通文本会继续当前任务；没有当前任务时，会使用默认项目的 `default_agent` 创建长期对话。

```text
/new website <任务描述>
/new website --agent qoder <任务描述>
/chat
/model
/model <模型名称>
/use a1b2c3d4
/status
/status a1b2c3d4
/logs a1b2c3d4 50
/logs a1b2c3d4 50 gpu
/logs a1b2c3d4 50 container
/stop a1b2c3d4
/help
```

发送 /model 查看 Codex 可用模型，发送 /model <模型名称> 切换后续 Codex 任务使用的模型。

在终端恢复飞书创建的同一个 Codex/Qoder session：

```bash
cd ~/workspace/AgentMessage
uv run agent-message resume a1b2c3d4
```

## Docker 项目

AgentMessage 与 Agent CLI 运行在宿主，项目命令通过 `container_run` 在已有容器中执行。容器必须把宿主项目目录以读写方式 bind mount 到 `container_path`。

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

容器项目不能同时启用 Bubblewrap GPU。Qoder 不会获得宿主 Bash，只能使用受控的 `container_run`；Docker socket 只应开放给可信的 AgentMessage 服务用户。

## 卸载

从仓库外运行卸载脚本：

```bash
cd ~
bash ~/workspace/AgentMessage/uninstall.sh
```

脚本会询问是否保留项目配置、任务数据库、日志和飞书凭证，直接回车默认不保留；真正删除前还会再次确认。选择保留时，数据会备份到 `~/.local/share/agent-message/backups/`。

## 安全与二次开发

- 飞书凭证只保存在仓库外，且不会传给 Codex/Qoder 子进程。
- 只注册允许 Agent 修改的项目；完整 JSONL 和命令日志保存在本地 `var/logs/`。
- Qoder 使用 `auto` 权限、显式工具白名单和严格 MCP；Codex 使用 `workspace-write`。
- 架构边界、模块扩展和测试要求见 [`AGENTS.md`](AGENTS.md)。

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor
uv run agent-message tasks
uv run python -m unittest discover -s tests -t . -v
```

## 更新与重启

```bash
cd ~/workspace/AgentMessage

# Python、projects.toml 或 feishu.env 有变化
systemctl --user restart agent-message

# pyproject.toml 或 uv.lock 有变化
uv sync --frozen
systemctl --user restart agent-message

# systemd unit 有变化
bash install.sh --refresh-service
```
