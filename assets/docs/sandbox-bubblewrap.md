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
sandbox_ssh_agent_socket = "/run/user/1000/agent-message-ssh.sock"
sandbox_ssh_known_hosts = "/home/alice/.ssh/known_hosts"
```

项目的 `sandbox_enabled`、`sandbox_gpu`、`sandbox_network` 和超时仍保留在 `[projects.<alias>]` 中；SSH 远程操作需项目开启网络。Git/SSH 四项只允许写在 `[service]`，放在项目段会提示迁移；旧的 `gpu_git_*`、`gpu_ssh_*` 键名仍可读取。

路径按本机服务用户调整。`sandbox_ssh_agent_socket` 必须指向该用户持有的 Unix socket；每次执行都会检查 socket 和 known_hosts 是否存在。没有配置时不自动继承 `SSH_AUTH_SOCK`。配置错误会终止该次运行并返回明确错误。

在宿主以运行 AgentMessage 的用户启动专用 agent，再添加项目需要的密钥：

```bash
ssh-agent -a "$XDG_RUNTIME_DIR/agent-message-ssh.sock"
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh.sock" ssh-add ~/.ssh/id_ed25519
```

私钥口令只在宿主终端输入。agent 应保持运行；重启机器后需要重新启动并加载密钥。固定 socket 路径使 systemd 服务无需继承终端的 `SSH_AUTH_SOCK`。known_hosts 应预先通过可信渠道核对主机指纹；未知或改变的主机公钥会被拒绝，不能靠关闭主机校验解决。

重启 AgentMessage 后，通过 `sandbox_run` 执行 `git add`、`git commit` 和授权的 `git push`/`git pull`。沙箱不加载宿主 `~/.ssh/config`，使用主机别名、自定义端口的 remote 需改用实际主机和端口的 SSH URL。沙箱也不加载宿主全局 Git 配置；仓库中已有的 commit 签名配置不会被关闭，签名所需公钥及配置须在项目内自行配置，私钥可由 agent 执行 SSH 签名。

转发 socket 允许沙箱使用该 agent 中所有已加载密钥的认证/签名能力，虽无法读取私钥，仍应使用仅加载所需密钥的专用 agent。这里的配置只适用于 Bubblewrap 沙箱项目；Docker 容器需要独立配置认证。

## 安全边界

- 沙箱只挂载当前项目和必要系统文件；启用 `sandbox_gpu` 时才额外挂载 `/dev/dxg`。
- 项目及其内部 `.git` 可写，Git 写操作通过 `sandbox_run` 执行。不会额外挂载项目外的 worktree Git 元数据或私钥；仅在显式配置时挂载 SSH agent socket 和 known_hosts，远程操作受网络配置限制。
- 宿主 HOME、Windows 目录、其他项目、飞书凭证和 AgentMessage 数据库不可见。
- Bubblewrap 使用 `--clearenv`，沙箱命令不会继承飞书服务环境变量。
- 单次命令受项目配置的超时、取消和日志策略约束。
