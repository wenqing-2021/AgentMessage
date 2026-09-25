# Bubblewrap 沙箱

AgentMessage 可让 Codex 或 Qoder 通过受控的 `sandbox_run` MCP 工具执行项目命令：依赖安装、构建、测试、训练和 Git 写操作都在同一个项目级 Bubblewrap 沙箱内完成。Codex 自己的 `workspace-write` 沙箱会把项目内 `.git` 设为只读，所以需要提交或推送的项目应启用这个沙箱。

## 前置条件

```bash
sudo apt install bubblewrap
```

启用 `sandbox_gpu = true` 的项目还需确认 WSL GPU 支持可见：

```bash
ls -l /dev/dxg
/usr/lib/wsl/lib/nvidia-smi
```

## 项目配置

在 `config/projects.toml` 的目标项目中增加：

```toml
[projects.your_proj_name]
path = "/home/alice/workspace/your_proj_name"
default_agent = "qoder"
allowed_agents = ["codex", "qoder"]
sandbox_enabled = true
sandbox_gpu = false
sandbox_network = true
sandbox_timeout_seconds = 86400
```

- `sandbox_enabled` 打开沙箱；`sandbox_gpu` 只在需要 CUDA/JAX 时设为 `true`，此时才挂载 `/dev/dxg` 并加入 WSL CUDA 库路径。
- `sandbox_network` 省略时默认为 `true`；设为 `false` 会隔离沙箱命令的网络。
- 同一个项目不能同时启用 Bubblewrap 沙箱与 Docker container runner。
- `[service].sandbox_readonly_paths` 是全局白名单，把 `/usr` 之外的宿主工具只读暴露给所有启用沙箱的项目，例如 `sandbox_readonly_paths = ["/opt/quarto", "/etc/fonts"]`；条目必须是宿主上真实存在的绝对路径，缺失时该项目所有沙箱命令都会报错，且应填安装根目录而不是 `/usr/local/bin` 中的符号链接。
- 旧键 `gpu_enabled`、`gpu_network`、`gpu_timeout_seconds` 和 `[projects.<alias>.gpu]` 表仍可读取，等价于 `sandbox_enabled = true` 加 `sandbox_gpu = true`；不要与新键混用。
- 修改配置后需要重启 AgentMessage。

## 验收与使用

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor --sandbox your_proj_name
```

`--gpu` 是同一检查的旧别名。启用 `sandbox_gpu` 时，验收必须显示真实 GPU backend 和设备。

任务启动后，可在飞书查看状态和日志：

```text
/status a1b2c3d4
/logs a1b2c3d4 50 sandbox
```

`/logs a1b2c3d4 50 gpu` 仍然可用，等同于 `sandbox`。若 Agent 只使用普通 shell，发送：

```text
请通过 agent_message_sandbox 的 sandbox_run 重新执行，不要使用普通 shell。
```

## Git commit 与 SSH push/pull

在仓库根目录的本机终端运行 `uv run python scripts/configure_sandbox_git.py`，可自动检测 Git 全局身份、当前 SSH agent 和 `~/.ssh/known_hosts`。已有配置优先，回车复用；也可用 `--config PATH` 指定本地配置。输入不回显，保存时保留其他 TOML 设置并设为 `0600`。选择 SSH `n` 会移除已有的两项转发配置。脚本不会创建或加载密钥、启动 agent、下载主机公钥或重启服务；首次使用请先按下文准备专用 agent 和已验证的 known_hosts。保存后在任务结束时重启服务。

普通本地 commit 不需要 SSH key，但需要提交者身份。在 `config/projects.toml` 已有的全局 `[service]` 段中增加以下设置即可，所有沙箱项目自动共用，无需在每个仓库重复配置：

```toml
[service]
sandbox_git_user_name = "Alice"
sandbox_git_user_email = "alice@example.com"
# SSH 认证使用下面两个全局绝对路径，须同时配置：
sandbox_ssh_agent_socket = "/run/user/1000/agent-message-ssh/agent.sock"
sandbox_ssh_known_hosts = "/home/alice/.ssh/known_hosts"
```

项目的 `sandbox_enabled`、`sandbox_gpu`、`sandbox_network` 和超时仍保留在 `[projects.<alias>]` 中；SSH 远程操作需项目开启网络。Git/SSH 四项只允许写在 `[service]`，放在项目段会提示迁移；旧的 `gpu_git_*`、`gpu_ssh_*` 键名仍可读取。

路径按本机服务用户调整。`sandbox_ssh_agent_socket` 必须指向该用户持有的 Unix socket；每次执行都会检查 socket 和 known_hosts 是否存在。没有配置时不自动继承 `SSH_AUTH_SOCK`。配置错误会终止该次运行并返回明确错误。

### WSL 启动时自动启动 SSH agent

WSL 的 `/etc/wsl.conf` 需要已有 `[boot]` / `systemd=true`。首次开启需从 Windows 执行 `wsl --shutdown` 再打开发行版；正常配置服务不需要关闭 WSL。

`install.sh` 现在会默认安装并启用这套专用 systemd 用户服务，固定 socket 位于 `/run/user/<uid>/agent-message-ssh/agent.sock`，正常安装无需任何手工步骤。`service` 阶段会把 `deploy/agent-message-ssh-agent.service` 复制到 `~/.config/systemd/user/agent-message-ssh-agent.service`、`deploy/agent-message-ssh-agent.conf` 复制到 `~/.config/systemd/user/agent-message.service.d/ssh-agent.conf`、`scripts/load_ssh_keys.py` 复制到 `~/.local/libexec/agent-message/load_ssh_keys.py`，三者均为 `600`；随后执行 `systemctl --user daemon-reload`，启用并启动 agent，最后启用并重启 `agent-message.service`。仓库内的 systemd 模板统一放在 `deploy/`；本体服务 unit 由 `install.sh` 基于 `deploy/agent-message.service` 渲染，把占位符替换为真实安装路径、凭证文件、可执行文件和 PATH。只有手工部署或更新 unit 时，才需要按最新模板重新安装这三个受管文件：

```bash
cd ~/workspace/AgentMessage && bash install.sh --refresh-service
```

`linger` 使用户服务管理器无需终端登录即可启动。`install.sh` 不会自动开启，需要时请自行执行一次：

```bash
sudo loginctl enable-linger "$(id -un)"
```

验证安装结果：

```bash
systemctl --user status agent-message-ssh-agent.service
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh/agent.sock" ssh-add -l
```

不使用 `install.sh` 的手工部署，也可自行从仓库复制同一批文件：

```bash
cd ~/workspace/AgentMessage
mkdir -p ~/.config/systemd/user/agent-message.service.d ~/.local/libexec/agent-message
install -m 600 scripts/load_ssh_keys.py ~/.local/libexec/agent-message/
install -m 600 deploy/agent-message-ssh-agent.service ~/.config/systemd/user/
install -m 600 deploy/agent-message-ssh-agent.conf ~/.config/systemd/user/agent-message.service.d/ssh-agent.conf
systemctl --user daemon-reload
systemctl --user enable --now agent-message-ssh-agent.service
```

服务每次启动时自动扫描 `~/.ssh` 顶层文件，加载所有当前用户拥有、组和其他用户无访问权限、且无需口令解锁的私钥。支持 OpenSSH、RSA、DSA、EC 和 PKCS#8 私钥格式；不依赖文件名，也不要求有对应 `.pub`。符号链接、目录、公钥、`config`、`known_hosts` 等文件均跳过。加密或损坏的私钥也跳过，不弹出口令提示、不修改密钥；单把密钥加载失败不会阻止其他密钥加载。

所有自动加载的密钥都可被沙箱用于认证或签名。日志只记录加载/跳过数量，不输出密钥、文件名或公钥指纹。有口令的密钥仍可在宿主终端手动解锁：

```bash
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh/agent.sock" ssh-add ~/.ssh/your_encrypted_key
```

新增密钥后，在没有沙箱任务运行时重启专用 agent 即可重新扫描；重启会丢失之前手动解锁的密钥，需要再次解锁：

```bash
systemctl --user restart agent-message-ssh-agent.service
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh/agent.sock" ssh-add -l
```

将全局 `sandbox_ssh_agent_socket` 更新为 `/run/user/<id -u 的输出>/agent-message-ssh/agent.sock`，保持 `sandbox_ssh_known_hosts` 为已验证的文件。也可在 socket 启动后运行配置向导：

```bash
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh/agent.sock" uv run python scripts/configure_sandbox_git.py
# 已有旧路径时，在向导中输入新路径，不要直接保留旧值。
# 等待当前任务完成，再加载新配置：
systemctl --user restart agent-message.service
```

`linger` 使用户服务管理器随发行版启动；依赖配置让 AgentMessage 等到 SSH agent 的启动及密钥加载步骤结束。agent 异常退出时会自动重启并重新加载密钥。依赖使用 `Wants`，agent 重启不会连带终止正在执行的 AgentMessage 任务；agent 暂时不可用时新沙箱命令会明确失败。

验证：

```bash
loginctl show-user "$(id -un)" -p Linger
systemctl --user is-enabled agent-message.service agent-message-ssh-agent.service
systemctl --user is-active agent-message.service agent-message-ssh-agent.service
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh/agent.sock" ssh-add -l
```

这保证 WSL **发行版启动时**拉起服务，不会替你在 Windows 登录时启动一个尚未运行的发行版。`known_hosts` 中的主机指纹仍须通过可信渠道核对。三处受管文件的首行都带有 `# Managed by AgentMessage:` 托管标记，卸载 AgentMessage 时 `uninstall.sh` 会停用并删除它们，保留 SSH 密钥、其他 agent 和用户共享的 linger 设置。

重启 AgentMessage 后，通过 `sandbox_run` 执行 `git add`、`git commit` 和授权的 `git push`/`git pull`。沙箱不加载宿主 `~/.ssh/config`，使用主机别名、自定义端口的 remote 需改用实际主机和端口的 SSH URL。沙箱也不加载宿主全局 Git 配置；仓库中已有的 commit 签名配置不会被关闭，签名所需公钥及配置须在项目内自行配置，私钥可由 agent 执行 SSH 签名。

转发 socket 允许沙箱使用该 agent 中所有已加载密钥的认证/签名能力，虽无法读取私钥，仍应使用仅加载所需密钥的专用 agent。这里的配置只适用于 Bubblewrap 沙箱项目；Docker 容器需要独立配置认证。

## 安全边界

- 沙箱只挂载当前项目和必要系统文件：整个 `/usr` 只读可见，`[service].sandbox_readonly_paths` 列出的宿主路径按只读挂载（例如 `/opt/quarto`、`/etc/fonts`）；启用 `sandbox_gpu` 时才额外挂载 `/dev/dxg`。
- 项目及其内部 `.git` 可写，Git 写操作通过 `sandbox_run` 执行。不会额外挂载项目外的 worktree Git 元数据或私钥；仅在显式配置时挂载 SSH agent socket 和 known_hosts，远程操作受网络配置限制。
- 宿主 HOME、Windows 目录、其他项目、飞书凭证和 AgentMessage 数据库不可见。
- Bubblewrap 使用 `--clearenv`，沙箱命令不会继承飞书服务环境变量。
- 单次命令受项目配置的超时、取消和日志策略约束。
