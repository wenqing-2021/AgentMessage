# 沙箱与 Docker 项目

在 `config/projects.toml` 为项目选择一种运行方式，保存后重启对应机器人服务。

## Bubblewrap 沙箱

```toml
[projects.example]
path = "/home/alice/workspace/example"
default_agent = "codex"
allowed_agents = ["codex"]
sandbox_enabled = true
sandbox_gpu = false
sandbox_network = true
sandbox_timeout_seconds = 86400
```

安装 `bubblewrap` 后，用以下命令配置共享 Git/SSH 并检查项目：

```bash
uv run python scripts/configure_sandbox_git.py
uv run agent-message doctor --sandbox example
```

需要 WSL GPU 时设置 `sandbox_gpu = true`。宿主工具只读挂载、SSH agent 自动启动与排错见 [Bubblewrap 详细指南](sandbox-bubblewrap.md)。

## Docker 项目

在已有 Docker 容器中执行项目命令。容器需把宿主项目目录以读写方式 bind mount 到 `container_path`：

```toml
[projects.container_project]
path = "/home/alice/workspace/container_project"
default_agent = "codex"
allowed_agents = ["codex"]
container_name = "container_project_dev"
container_path = "/workspace/container_project"
container_auto_start = true
container_timeout_seconds = 86400
```

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor --container container_project
```

容器项目不能同时启用 Bubblewrap 沙箱。项目命令通过受控的 `container_run` 执行；Docker socket 只应开放给可信的 AgentMessage 服务用户。

[返回 README](../../README_CN.md)
