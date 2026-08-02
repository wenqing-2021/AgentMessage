# GPU 与 Bubblewrap

AgentMessage 可让 Codex 或 Qoder 通过受控的 `gpu_run` MCP 工具执行 CUDA、JAX 和训练命令。普通 Agent shell 按设计看不到 GPU；不能用普通 shell 的 CPU-only 结果判断宿主 GPU 不可用。

## 前置条件

适用于带 WSL GPU 支持的环境。先确认设备和驱动可见：

```bash
sudo apt install bubblewrap
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
gpu_enabled = true
gpu_network = true
gpu_timeout_seconds = 86400
```

- `gpu_network` 省略时默认为 `true`；设为 `false` 会隔离 GPU 命令的网络。
- 同一个项目不能同时启用 Bubblewrap GPU 与 Docker container runner。
- 修改配置后需要重启 AgentMessage。

## 验收与使用

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor --gpu your_proj_name
```

验收必须显示真实 GPU backend 和设备。任务启动后，可在飞书查看状态和日志：

```text
/status a1b2c3d4
/logs a1b2c3d4 50 gpu
```

若 Agent 只使用普通 shell 并报告 CPU 或缺少 `/dev/dxg`，发送：

```text
请通过 agent_message_bwrap_gpu 的 gpu_run 重新执行，不要使用普通 shell 检测 GPU。
```

## 安全边界

- GPU sandbox 只挂载当前项目和必要系统文件。
- 项目可写，`.git` 只读。
- 宿主 HOME、Windows 目录、其他项目、飞书凭证和 AgentMessage 数据库不可见。
- Bubblewrap 使用 `--clearenv`，GPU 命令不会继承飞书服务环境变量。
- 单次命令受项目配置的超时、取消和日志策略约束。

