# Sandbox and Docker Projects

Choose one runtime per project in `config/projects.toml`, then restart the corresponding bot service.

## Bubblewrap Sandbox

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

Install `bubblewrap`, configure shared Git/SSH, and check the project:

```bash
uv run python scripts/configure_sandbox_git.py
uv run agent-message doctor --sandbox example
```

Set `sandbox_gpu = true` for WSL GPU access. See the [Bubblewrap guide](sandbox-bubblewrap.en.md) for read-only host tools, SSH-agent startup, and troubleshooting.

## Docker Projects

Run project commands in an existing Docker container. Bind-mount the host project directory read-write at `container_path`:

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

A container project cannot also enable the Bubblewrap sandbox. Project commands run through the controlled `container_run` tool. Expose the Docker socket only to the trusted AgentMessage service user.

[Back to README](../../README.md)
