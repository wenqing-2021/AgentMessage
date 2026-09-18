# AgentMessage 二次开发指南

本文件适用于整个仓库，供自动化编码 Agent 和维护者在修改代码前阅读。用户安装、配置和飞书命令请看英文 `README.md` 或中文 `README_CN.md`；这里记录架构边界、开发流程、扩展方法和验证要求。

## 项目目标

AgentMessage 通过飞书长连接接收单聊文本，将任务持久化后交给本机的 Codex CLI 或 Qoder CLI，并把进度与最终结果可靠地发回飞书。可选运行时允许二者在受控的 Bubblewrap 沙箱（GPU 透传按项目配置）或已有 Docker 容器内执行项目命令。

设计目标按优先级排列：

1. 不把凭证、高权限接口或任意宿主路径暴露给远程消息。
2. 消息、任务、会话和发送结果可恢复、可去重、可审计。
3. 同一项目串行执行，不同项目可以并行。
4. Codex/Qoder、消息通道和执行运行时可以独立扩展。

## 技术栈与开发命令

- Python 3.10+，依赖和虚拟环境统一由 `uv` 管理。
- 构建后端为 Hatchling，命令行入口定义在 `pyproject.toml`。
- 测试使用标准库 `unittest`，不要假定仓库安装了 pytest。
- 若默认 uv 缓存不可写，使用仓库外临时缓存：

```bash
UV_CACHE_DIR=/tmp/agent-message-uv-cache uv sync
UV_CACHE_DIR=/tmp/agent-message-uv-cache uv run python -m unittest discover -s tests
```

常规代码修改完成后至少运行：

```bash
UV_CACHE_DIR=/tmp/agent-message-uv-cache uv run python -m compileall -q src tests
UV_CACHE_DIR=/tmp/agent-message-uv-cache uv run python -m unittest discover -s tests
git diff --check
```

修改包结构或命令行入口时再运行：

```bash
UV_CACHE_DIR=/tmp/agent-message-uv-cache uv run agent-message --help
UV_CACHE_DIR=/tmp/agent-message-uv-cache uv run agent-message-sandbox-mcp --help
UV_CACHE_DIR=/tmp/agent-message-uv-cache uv run agent-message-container-mcp --help
UV_CACHE_DIR=/tmp/agent-message-uv-cache uv build --out-dir /tmp/agent-message-dist
```

## 模块边界

```text
src/agent_message/
├── core/                    # 配置、领域模型、SQLite 持久化
├── channels/                # 飞书等外部消息通道
├── agents/                  # Codex/Qoder CLI 适配器
├── orchestration/           # 命令、路由、调度和服务生命周期
├── runtimes/
│   ├── sandbox/             # Bubblewrap policy / runner / MCP（GPU 透传可选）
│   └── container/           # Docker policy / runner / MCP
└── cli.py                   # 本地统一命令行入口
```

仓库根目录的 `install.sh` 负责 HTTPS 安装、凭证断点录入和 systemd 用户服务生成：`deploy/` 存放 systemd 模板，本体 unit 模板含占位符并由 `install.sh` 渲染，SSH agent unit、drop-in 与 `scripts/load_ssh_keys.py` 也由它在 service 阶段默认安装并启用；`uninstall.sh` 在二次确认后卸载，并可先备份本地数据；Bubblewrap 沙箱等较长的用户文档放在 `assets/docs/`。

保持以下依赖方向：

```text
channels      ──> core
runtimes      ──> core
agents        ──> core + runtimes policies
orchestration ──> core + channels + agents + runtimes
cli           ──> core + agents + orchestration + runtimes
```

- `core/` 不应反向导入其他业务子包。
- `channels/` 只负责外部协议与统一消息模型之间的转换，不负责业务状态机。
- `agents/` 负责构造和管理 Agent CLI 进程，不直接处理飞书事件。
- `orchestration/` 组合各模块，不应复制 runner 或持久化逻辑。
- `runtimes/` 是安全边界。路径校验、进程终止、超时和日志逻辑应保留在对应 runner/MCP 内。
- `agent_message.__init__` 只暴露稳定的少量公共接口；不要在其中导入有运行时副作用的模块。

## 运行流程

```text
Feishu event
    │
    ▼
channels.feishu.parse_receive_event
    │  仅接受 p2p text
    ▼
BridgeService.receive
    │
    ├── MessageRouter ──> StateStore ──> SQLite task/message queue
    │
    └── durable outbox
             ▲
             │
Scheduler ──> AgentAdapter ──> Codex/Qoder process
    │                              │
    ├── Sandbox/Container monitor  └── JSONL progress/session/final text
    └── finish_run ──> outbox ──> FeishuGateway.send_text
```

需要维持的行为：

- 在鉴权判断前登记入站事件 ID，使飞书重试不会重复创建任务。
- 用户必须来自环境变量白名单或 SQLite `authorized_users`。
- 任务和消息先持久化，再由 scheduler 原子 claim。
- `StateStore.claimed_run()` 保证同一项目只有一个 running task；不要把该限制改成全局串行。
- 普通文本继续当前任务；没有当前任务时，按默认项目的 `default_agent` 创建长期对话。
- 服务重启时将未完成状态恢复为 interrupted，并通过 outbox 通知用户。
- 回复先写入持久化 outbox，再异步发送；发送失败必须保留重试状态。

## 安全边界

这些约束不是普通实现细节，修改时必须补充针对性测试。

### 配置与凭证

- 项目只能来自 `config/projects.toml` 的绝对路径白名单；飞书消息不能提交路径。
- App ID、App Secret 和初始 open_id 白名单只从环境变量读取。
- Agent 子进程环境必须移除 `AGENT_MESSAGE_FEISHU_*` 和 `AGENT_MESSAGE_ALLOWED_OPEN_IDS`，不得让 Codex/Qoder 或其普通 Bash 继承飞书凭证。
- 专用 SSH agent 由 `deploy/agent-message-ssh-agent.service` 与 `deploy/agent-message-ssh-agent.conf` 定义，`install.sh` 默认安装启用，`uninstall.sh` 仅按 `# Managed by AgentMessage:` 标记清理；`scripts/load_ssh_keys.py` 只对当前用户所有、无 group/other 权限且无口令的私钥执行 `ssh-add`，不得提示口令、复制私钥进仓库或关闭主机指纹校验。
- `install.sh` 的凭证输入必须通过 `/dev/tty`，App Secret 不回显；断点文件、环境文件、项目配置和生成的 unit 保持 `0600`。
- `uninstall.sh` 必须先校验精确安装路径、停止服务并完成可选备份，再删除仓库和凭证目录；不得删除 systemd、uv、Agent CLI、Docker 或 Bubblewrap。
- 不要把真实凭证、open_id、用户目录、私有项目名或真实容器名写入 tracked 文件。
- `config/projects.toml`、`var/` 和 SQLite/WAL/SHM 文件是本地状态，不得提交。
- 新增配置项时同时修改 dataclass、`load_config()` 校验、`config/projects.example.toml`、中英文 README 和配置测试。

### Agent 进程

- Agent 命令必须通过 argv 调用；不要使用 `shell=True` 或拼接 shell 字符串。
- Codex 默认使用 `workspace-write`，且 `sandbox_workspace_write.network_access=false`。
- Codex resume 必须保留 session ID、项目上下文和当前轮次的 runtime policy 前缀。
- `/compact` 通过 Codex app-server 的 `thread/compact/start` 执行原生压缩，不经过 `exec` 提示词；压缩请求作为 `task_messages.operation=compact` 持久化排队，并保留原 session ID。
- Qoder 使用 `auto` 权限、空 `setting-sources`、显式工具列表和严格 MCP 配置；普通 Bash 允许联网，但继续限制 `sudo`、破坏性 Git 操作和发布命令。
- Qoder 的 `result` 事件是唯一成败依据；不得把 assistant text、thinking、hook 输出或工具参数作为最终结果转发。
- 不要以普通 Codex shell 的 CPU-only、NVML 或 `/dev/dxg` 结果判断宿主 GPU 是否可用。
- 宿主 `workspace-write` 会把项目内 `.git`、`.agents`、`.codex` 设为只读；项目需要 Git 写入时应启用 Bubblewrap 沙箱，而不是放宽 Codex 默认沙箱。

### 沙箱与容器运行时

- 启用沙箱的项目只能通过 `agent_message_sandbox` 的 `sandbox_run` 执行项目命令；`sandbox_gpu = true` 时才 dev-bind `/dev/dxg` 并加入 WSL CUDA 库路径。
- 沙箱命令默认允许联网；只有显式设置 `sandbox_network = false` 时才添加 network namespace 隔离。
- Bubblewrap 沙箱只挂载当前项目和必要系统路径；项目及内部 `.git` 可写，Git 写操作通过 `sandbox_run` 执行，敏感 HOME/Windows/其他项目路径不可见。
- Container 项目只能通过 `agent_message_container` 的 `container_run` 执行项目命令。
- Container runner 必须用 `docker inspect` 验证容器状态和宿主项目目录的可写 bind mount。
- MCP 参数使用 `argv: list[str]` 和项目相对 `cwd`；必须拒绝逃逸项目根目录的路径。
- 单个 MCP 进程同一时刻只运行一个工具请求，并支持取消、超时、SIGTERM/SIGKILL 和持久化日志。
- 当前配置不允许同一项目同时启用 Bubblewrap 沙箱和 Container。容器项目使用 Qoder 时不得提供宿主 Bash，只能暴露精确的 `container_run` MCP 工具。
- Docker socket 等价于高权限宿主控制接口；不要弱化相关文档或校验。

## 已知兼容性约束

- Python 3.10 没有 `tomllib`；保留条件依赖 `tomli` 和 fallback import。
- WSL/Python 3.10 的 `ThreadedChildWatcher` 可能卡住子进程；不要在没有对应回归测试的情况下移除 `SafeChildWatcher` 处理。
- `lark-oapi` 的 WebSocket 客户端会捕获事件循环。保持 Feishu SDK 在线程内部延迟导入，并保持 `cli._run_service()` 的延迟 service import。
- Codex 与 Qoder 的流式 JSON 结构不完全相同；解析器应容忍未知事件、超长行和缺失最终消息。
- 移动模块时要同时更新：相对导入、`pyproject.toml` scripts、适配器中的 `python -m ...` 路径和测试 patch 路径。
- 项目级配置以 `sandbox_enabled`、`sandbox_gpu`、`sandbox_network`、`sandbox_timeout_seconds` 为准；旧 `gpu_enabled`/`gpu_network`/`gpu_timeout_seconds`、`[projects.<alias>.gpu]` 表和 `[service].gpu_git_*`/`gpu_ssh_*` 仍可读取，等价于开启 GPU 透传，但不能与新键混用。
- `gpu_jobs` 表已重命名为 `sandbox_jobs`；迁移必须保留历史作业记录且可重复执行。

## 常见扩展方式

### 新增飞书命令

1. 在 `orchestration/commands.py` 添加类型、语法和用户可读错误，并更新 `HELP_TEXT`。
2. 在 `orchestration/router.py` 实现授权后的状态转换。
3. 状态变化放入 `core/state.py`，保持事务和锁边界。
4. 在 `tests/test_router_and_state.py` 覆盖成功、非法参数、越权、重复事件和状态边界。
5. 同步更新中英文 README 的飞书使用示例。

不要在 command parser 中访问数据库，也不要在 router 中直接启动进程。

### 新增 Agent 类型

1. 扩展 `core/models.py::AgentKind`。
2. 在 `agents/base.py` 复用进程生命周期，在独立模块实现协议解析和命令构造，并通过 `agents/adapters.py` 兼容导出、更新 `adapter_for()`。
3. 在 `core/config.py` 和 `cli.py::command_doctor()` 增加校验与能力检查。
4. 明确新 Agent 的 sandbox、网络、恢复 session、进度和停止语义。
5. 更新配置示例、中英文 README、`tests/test_adapters.py`、`tests/test_config.py` 和 scheduler 测试。

### 新增消息通道

1. 在 `channels/<channel>.py` 把外部事件规范化为 `InboundMessage`。
2. 通道实现只暴露 ingress callback 和 `send_text` 等发送接口。
3. 复用 `MessageRouter`、`Scheduler`、`StateStore` 和 durable outbox。
4. 为事件过滤、字段缺失、重复消息和发送失败建立无网络单元测试。

### 新增受控执行运行时

参照 `runtimes/sandbox/` 或 `runtimes/container/`，保持三层结构：

- `policy.py`：注入 Agent 的强制执行说明。
- `runner.py`：路径、环境、进程、日志、超时和终止机制。
- `mcp.py`：JSON-RPC/MCP 协议、工具 schema、并发控制和状态记录。

同时需要扩展配置模型、job 领域模型、SQLite 表与迁移、adapter MCP 参数、scheduler 监控/停止、doctor 检查、命令行入口和 runner/MCP 测试。

### 修改 SQLite schema

- 所有 schema 变更集中在 `StateStore._migrate()`。
- 新安装使用 `CREATE TABLE IF NOT EXISTS`；旧安装使用可重复执行的增量迁移。
- 迁移必须位于现有锁和事务内，不能丢弃或重建用户数据库。
- 同步更新 row-to-model 转换、恢复逻辑和状态转换测试。
- 测试应构造旧 schema 后重新打开数据库，验证数据仍在且迁移可重复执行。

## 测试文件导航

- `tests/test_config.py`：TOML、白名单、sandbox_/gpu_ 兼容键、沙箱与 Container 互斥和边界值。
- `tests/test_router_and_state.py`：命令路由、鉴权、去重、任务选择和 SQLite 状态。
- `tests/test_scheduler.py`：claim、并发、进度、恢复和停止信号。
- `tests/test_adapters.py`：Codex/Qoder argv、session、MCP 注入和 JSON 流解析。
- `tests/test_feishu.py`：飞书事件规范化；不连接真实飞书。
- `tests/test_sandbox_runner.py`：Bubblewrap 命令、GPU 透传开关、隔离路径、作业迁移和 sandbox MCP。
- `tests/test_container_runner.py`：Docker inspect、bind mount、命令、停止和 Container MCP。
- `tests/test_cli.py`：doctor、selector、沙箱探针和本地 resume。
- `tests/helpers.py`：临时项目、配置和入站消息 fixtures。

优先使用临时目录、fake adapter、mock subprocess 和 plain-dict 飞书事件。单元测试不能依赖真实凭证、网络、GPU、Docker daemon 或已登录的 Agent CLI。

## 完成标准

提交修改前逐项确认：

- 改动位于正确模块，没有引入反向依赖或重复实现。
- 新输入有类型、范围、路径和权限校验。
- 持久状态变更具备事务、恢复和兼容迁移路径。
- 进程仍可取消，日志仍写入 task/job 对应目录。
- 配置示例和中英文 README 没有真实身份、路径、项目或容器信息。
- 相关单元测试和全量测试通过，`git diff --check` 无错误。
- 修改入口或包结构时，wheel 构建和全部 console scripts 已验证。
- 只有在真实授权环境执行后，才宣称飞书、Bubblewrap 沙箱、GPU、Docker 或 systemd 集成验证成功；否则明确说明只完成了静态或单元测试验证。
