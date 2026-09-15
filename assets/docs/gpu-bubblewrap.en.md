# GPU and Bubblewrap

AgentMessage lets Codex or Qoder execute CUDA, JAX, and training commands through the controlled `gpu_run` MCP tool. The ordinary agent shell intentionally cannot see the GPU, so a CPU-only result there does not prove that the host GPU is unavailable.

## Prerequisites

This runtime targets environments with WSL GPU support. Confirm that the device and driver are visible first:

```bash
sudo apt install bubblewrap
ls -l /dev/dxg
/usr/lib/wsl/lib/nvidia-smi
```

## Project configuration

Add these fields to the target project in `config/projects.toml`:

```toml
[projects.your_proj_name]
path = "/home/alice/workspace/your_proj_name"
default_agent = "qoder"
allowed_agents = ["codex", "qoder"]
gpu_enabled = true
gpu_network = true
gpu_timeout_seconds = 86400
```

- `gpu_network` defaults to `true`; set it to `false` to isolate GPU-command networking.
- A project cannot enable both the Bubblewrap GPU runtime and the Docker container runner.
- Restart AgentMessage after changing the configuration.

## Validation and usage

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor --gpu your_proj_name
```

The validation must report a real GPU backend and device. After a task starts, inspect its status and logs from Feishu:

```text
/status a1b2c3d4
/logs a1b2c3d4 50 gpu
```

If the agent uses only its ordinary shell and reports a CPU backend or missing `/dev/dxg`, send:

```text
Use gpu_run from agent_message_bwrap_gpu to run this command again. Do not detect the GPU through the ordinary shell.
```

## Security boundary

- The GPU sandbox mounts only the current project and required system files.
- The project and its internal `.git` are writable; use `gpu_run` for Git writes. External worktree metadata and host credentials are not mounted. Remote operations still require enabled networking and credentials available inside the sandbox.
- The host HOME, Windows drives, other projects, Feishu credentials, and the AgentMessage database are not visible.
- Bubblewrap uses `--clearenv`, so GPU commands do not inherit Feishu service variables.
- Every command remains subject to the configured timeout, cancellation, and logging policy.

