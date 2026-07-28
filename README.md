# AgentMessage

将飞书企业自建应用的单聊消息安全地转发给 WSL 中托管的 Codex CLI 或 Qoder CLI 任务，并把状态与最终反馈发回飞书。

## 特性

- 飞书长连接：不需要公网入站端口或内网穿透。
- 本地项目白名单：飞书只能引用 `projects.toml` 中已登记的项目别名。
- SQLite 持久化：任务、session ID、消息队列、入站去重、当前任务和发件箱均可恢复。
- 多项目并行、单项目串行；直接普通文本可进入长期默认 Codex 对话，选中任务后则继续该任务，同项目不会写入冲突。
- Codex 和 Qoder 的无交互会话适配；完整 JSONL 在本地 `var/logs/`，飞书只接收生命周期和最终摘要。
- 飞书进度更新：Codex 会话、分析、命令/文件操作和面向用户的中间说明会以限频消息发送到单聊。
- 默认不使用 YOLO / 绕过权限模式；Codex 工具网络由 `codex_tool_network` 显式控制，Qoder 的 Bash 工具使用无网络命名空间。

## 安装与配置

```bash
cd /home/moss_ubuntu/workspace/AgentMessage
uv sync
cp config/projects.example.toml config/projects.toml
vim config/projects.toml
chmod 600 config/projects.toml
```

在 Vim 中将每个 `path` 改为真实、绝对的项目目录，并将 `default_chat_project` 设为其中一个项目别名，然后保存退出。例如项目段落名是 `[projects.website]` 时，填写 `default_chat_project = "website"`。飞书可使用的项目只能在该文件登记，不能从消息中传路径。

默认 `codex_tool_network = false`：Codex 生成的工具命令不能访问网络。只对你信任的项目需要网络访问时，改为 `codex_tool_network = true` 并重启服务；这会允许 Codex 的工具访问外部网络，但不会解除项目白名单或 `workspace-write` 文件权限限制。

在仓库外创建并使用 `vim` 编辑凭证文件 `~/.config/agent-message/feishu.env`：

```bash
mkdir -p ~/.config/agent-message
chmod 700 ~/.config/agent-message
vim ~/.config/agent-message/feishu.env
```

在 Vim 中写入：

```text
AGENT_MESSAGE_FEISHU_APP_ID=cli_xxx
AGENT_MESSAGE_FEISHU_APP_SECRET=xxx
# 首次可留空。随后用本地 authorize 命令增加自己的 open_id。
AGENT_MESSAGE_ALLOWED_OPEN_IDS=
```

保存退出后执行：

```bash
chmod 600 ~/.config/agent-message/feishu.env
```

将环境变量加载到当前 shell 后，先检查本地条件：

```bash
set -a; source ~/.config/agent-message/feishu.env; set +a
uv run agent-message doctor
```

Qoder 是可选项：安装、登录 `qodercli` 后会自动在 `doctor` 中显示。没有 Qoder 时，Codex 仍可正常使用。

## 首次绑定：飞书应用 ↔ 此 WSL2

这套方案**没有**“填写 WSL IP、开放端口或配置回调 URL”的步骤。WSL 中的服务会带着
`App ID` 和 `App Secret` 主动连接飞书；只要该进程持续运行，这台 WSL2 就是该机器人的运行端。

按以下顺序完成首次绑定：

1. 在飞书开发者后台打开同一个企业自建应用，进入 **凭证与基础信息**，复制 `App ID` 和 `App Secret`。
2. 将这两个值填入 `~/.config/agent-message/feishu.env`，不要填入 `projects.toml`，也不要提交到 Git。
3. 在 **事件订阅** 中选择 **使用长连接接收事件**，不要填写 Request URL；添加 `im.message.receive_v1` 并发布应用版本。
4. 在 WSL 项目目录执行以下命令，让本机主动连上飞书：

   ```bash
   cd /home/moss_ubuntu/workspace/AgentMessage
   set -a; source ~/.config/agent-message/feishu.env; set +a
   uv run agent-message doctor
   uv run agent-message run
   ```

   这个终端必须保持运行。首次测试时不要用 `Ctrl-C` 停止它。
5. 在个人飞书客户端主动打开与机器人的单聊，并发送任意一条文本，例如 `/help`。外部/个人用户必须先发起会话，机器人不能先主动私聊。
6. 另开一个 WSL 终端，执行：

   ```bash
   cd /home/moss_ubuntu/workspace/AgentMessage
   uv run agent-message pending-senders
   uv run agent-message authorize ou_xxx
   ```

   将上一个命令显示的 `ou_xxx` 原样粘贴给 `authorize`。现在再在飞书发送 `/help`；收到帮助文本即表示绑定和权限均成功。

之后请将服务改为 systemd 或 tmux 常驻运行；WSL/电脑休眠、关机或服务退出时，机器人无法接收消息。

## 飞书后台配置清单

1. 创建**企业自建应用**，添加“机器人”能力，并把机器人可用范围限制为自己。
2. 开通应用权限 `im:message.p2p_msg:readonly` 和 `im:message:send_as_bot`。
3. 在“事件订阅”选择**使用长连接接收事件**，添加 `im.message.receive_v1`，创建应用版本并发布。
4. 按上一节“首次绑定”启动 WSL 服务并完成 `open_id` 的本地授权。

未被授权的消息会被去重但不会执行，也不会返回内容。

## 运行

前台运行（首次接入时推荐）：

```bash
set -a; source ~/.config/agent-message/feishu.env; set +a
uv run agent-message run
```

WSL 启用 user systemd 后：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/agent-message.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-message
journalctl --user -u agent-message -f
```

### systemd 找不到已安装的 Codex：配置 PATH

systemd 不会读取你的 `~/.bashrc` 或交互式终端的 `PATH`。因此，即使终端中可以运行 `codex`，机器人服务也可能报“找不到 codex”。`EnvironmentFile=%h/.config/agent-message/feishu.env` **必须保留**，它用于加载飞书 App ID 和 App Secret；`PATH` 应作为紧接着的新一行添加，而不是替换它。

先在与 Codex 相同的 WSL 用户终端中查看 Codex 的实际安装路径：

```bash
command -v codex
dirname "$(command -v codex)"
```

例如第一条输出 `/home/alice/.local/bin/codex`，第二条输出 `/home/alice/.local/bin`。编辑已安装的 unit：

```bash
vim ~/.config/systemd/user/agent-message.service
```

确认 `[Service]` 中包含下面两行。将 `PATH=` 最前面的目录替换成上一步 `dirname` 的输出；其他目录保留。**不要**把 `feishu.env` 的路径替换成 Codex 路径。

```ini
EnvironmentFile=%h/.config/agent-message/feishu.env
Environment="PATH=/home/alice/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
```

对于常见的 `~/.local/bin/codex` 安装，仓库自带的 unit 已使用可移植的 `%h/.local/bin`。若实际安装在 `~/.npm-global/bin`、`/opt/.../bin` 等其他目录，则仍以 `dirname "$(command -v codex)"` 的输出为准。

保存后使修改生效：

```bash
systemctl --user daemon-reload
systemctl --user restart agent-message
systemctl --user status agent-message --no-pager
```

若 WSL 没有 user systemd，可在持久 tmux 会话中使用同一条 `uv run agent-message run` 命令。

如果执行 `systemctl --user daemon-reload` 时显示 `Failed to connect to bus: No such file or directory`，说明
当前 WSL 没有启动 systemd；不是 `agent-message.service` 的错误。任选以下一种方式。

#### 方式 A：立即使用 tmux

```bash
tmux new-session -d -s agent-message 'cd /home/moss_ubuntu/workspace/AgentMessage && set -a && . ~/.config/agent-message/feishu.env && set +a && exec uv run agent-message run'
tmux capture-pane -pt agent-message
```

常用管理命令：

```bash
tmux attach -t agent-message   # 查看实时输出；按 Ctrl-b 后按 d 可离开但不停止服务
tmux ls                        # 查看会话是否仍在运行
tmux kill-session -t agent-message  # 停止服务
```

#### 方式 B：在 WSL2 启用 systemd

先在 WSL 内编辑现有配置（保留其他节，例如 `[network]`）：

```bash
sudo vim /etc/wsl.conf
```

添加：

```ini
[boot]
systemd=true
```

保存退出 Vim 后，在 **Windows PowerShell** 中执行：

```powershell
wsl --version
wsl --shutdown
```

若 WSL 版本低于 `0.67.6`，先在 Windows PowerShell 中执行 `wsl --update`。重新打开 WSL 后，确认：

```bash
ps -p 1 -o comm=
```

输出应为 `systemd`。此时再运行本节前面的 `systemctl --user daemon-reload`、`enable --now` 命令。若
希望用户服务在没有登录终端时仍保留其 user manager，可额外执行一次：

```bash
sudo loginctl enable-linger "$USER"
```

### 更新服务

代码或配置更新后，服务必须重启才能加载新内容；通常直接使用 `restart`，不需要先单独执行
`stop`：

```bash
cd /home/moss_ubuntu/workspace/AgentMessage
systemctl --user restart agent-message
systemctl --user status agent-message --no-pager
```

重启会中断当前正在运行的 agent 任务。若任务不应中断，请先等待其完成；中断任务的 session
上下文会保留，可稍后从飞书继续，但不会自动重跑。

不同更新类型对应的命令：

```bash
# 仅修改 Python 源码
systemctl --user restart agent-message

# pyproject.toml 或 uv.lock 有变
cd /home/moss_ubuntu/workspace/AgentMessage
uv sync
systemctl --user restart agent-message

# 修改 feishu.env 或 config/projects.toml
systemctl --user restart agent-message

# 修改 systemd unit 文件本身
cp deploy/agent-message.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart agent-message
```

该 unit 没有定义 `reload` 行为，请使用 `restart`。`journalctl --user -u agent-message -f` 中按
`Ctrl-C` 只会退出日志查看，不会停止机器人服务。

## 飞书命令：先理解这 4 个概念

- **项目别名**：`projects.toml` 中项目路径的短名称。例如 `[projects.website]` 的项目别名是 `website`。它不是目录路径，飞书不能发送任意路径。
- **任务**：一次独立的 Codex 或 Qoder 工作会话，例如“修复首页登录按钮在手机端溢出”。
- **任务 ID**：创建任务后机器人返回的唯一编号，例如 `a1b2c3d4`。用它切换、查看日志或停止这个任务。
- **当前任务**：你此刻直接发送普通文本时，机器人会继续发送到的任务。

### 直接和 Codex 长期聊天

首次直接发送一条普通文本，不必先写命令：

```text
你：检查当前项目结构，告诉我主要入口文件，不要修改文件。
机器人：默认 Codex 对话 a1b2c3d4 已排队：website。后续可直接发送普通文本继续……

你：现在只分析前端部分，并提出三个修改建议。
机器人：已安排继续任务 a1b2c3d4。
```

这两条普通文本使用同一个 Codex session。默认对话由 `[service]` 的 `default_chat_project` 决定。若你后来切换到其他任务，发送 `/chat` 即可回到这个长期 Codex 对话。

### 任务进行时的飞书进度

任务启动后，机器人会在单聊中发送简短进度，例如“Codex 正在分析任务”“正在执行本地命令”“已完成一项文件修改”，以及 Codex 已面向用户输出的中间说明。更新最多约每 2 秒一条、每条最多 800 个字符，避免刷屏和触发飞书频率限制。

不会转发 Codex 的原始内部推理、命令原始输出或工具返回内容；完整 JSONL 仍只保存在本机。需要查看原始执行记录时使用 `/logs <任务 ID> [行数]`。

### 在终端打开飞书创建的同一 Codex 会话

桥接器保存每个任务的 Codex thread ID。任务完成、失败或停止后，可以在 WSL 终端运行：

```bash
cd /home/moss_ubuntu/workspace/AgentMessage
uv run agent-message resume <任务-ID>
```

例如：

```bash
uv run agent-message resume 881e17193a
```

该命令会执行 `codex resume --include-non-interactive`，以该任务的项目目录、`workspace-write` 沙箱和当前 `codex_tool_network` 设置打开 Codex TUI。因此终端和飞书会继续同一个 Codex thread；在终端退出后，再在飞书发送普通文本也会继续同一个 thread。

同一时间只能从一个入口继续会话：任务状态为 `running` 时，本地 `resume` 命令会拒绝启动。终端 TUI 打开期间不要再从飞书发送普通文本；先退出 TUI，再继续飞书对话，避免两个 Codex 进程同时写入同一会话。

### 创建和管理独立任务

```text
你：/new website 修复首页登录按钮在手机端溢出的问题
机器人：任务 a1b2c3d4 已排队：website / codex。

你：先检查现有 CSS，不要修改文件，告诉我原因。
机器人：已安排继续任务 a1b2c3d4。
```

`/new` 会创建并自动选中独立任务；所以上例最后的普通文本会继续 `a1b2c3d4`，而不是默认聊天。可显式选择 Qoder（仅在该项目已允许并安装登录后可用）：

```text
/new website --agent qoder 检查测试失败原因，不要修改文件
```

其余命令和含义：

```text
/chat                     # 回到默认长期 Codex 对话
/use a1b2c3d4             # 把当前任务切换到指定任务；之后普通文本继续它
/status                   # 查看当前任务
/status a1b2c3d4          # 查看指定任务
/logs a1b2c3d4 50         # 查看该任务最近 50 行本地 JSONL 日志
/stop a1b2c3d4            # 停止该任务；之后仍可选中它并发送普通文本恢复
/help                     # 在飞书中显示相同的简明说明
```

不确定任务 ID 或当前任务时，先发送 `/status`。**任务描述不是任务 ID**；例如“修复首页按钮”只能作为 `/new` 的描述，不能用于 `/use`、`/stop` 或 `/logs`。未知项目别名会返回允许的别名；无效任务 ID 会提示先用 `/status`。

如果任务仍在运行，新消息会按顺序排队，不会中断当前执行。不同项目可以并行，同一个项目严格串行。服务意外重启时，运行中的任务标记为 `interrupted`，不会自动重跑；其 session ID 和默认聊天映射仍会保留。

## 安全边界

- 不要把 App Secret、Codex/Qoder token 或项目私钥写进仓库、`projects.toml`、任务 prompt 或飞书消息。
- 该工具禁止危险 CLI 启动参数；Codex 固定以 `workspace-write` 运行，工具网络是否开启由 `codex_tool_network` 决定。
- Qoder 禁用 Web/MCP/子代理与常见危险命令；其 Bash 命令进入独立无网络 user/network namespace。仍应只登记你信任、允许 agent 修改的工作目录。
- 桥接服务不接管已有交互式 TUI/tmux 会话；它只管理自己创建的 headless CLI 子进程。

## 本地检查

```bash
uv run python -m unittest discover -s tests -t . -v
uv run agent-message doctor
uv run agent-message tasks
uv run agent-message pending-senders
```
