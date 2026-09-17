# Bubblewrap Sandbox

AgentMessage can run project commands for Codex or Qoder through the controlled `sandbox_run` MCP tool: dependency installs, builds, tests, training, and Git writes all happen inside one project-scoped Bubblewrap sandbox. Codex's own `workspace-write` sandbox keeps the project `.git` directory read-only, so projects that need commits or pushes should enable this sandbox.

## Prerequisites

```bash
sudo apt install bubblewrap
```

Projects with `sandbox_gpu = true` also need visible WSL GPU support:

```bash
ls -l /dev/dxg
/usr/lib/wsl/lib/nvidia-smi
```

## Project Configuration

Add this to the target project in `config/projects.toml`:

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

- `sandbox_enabled` turns the sandbox on. Set `sandbox_gpu = true` only for CUDA/JAX work; that is when `/dev/dxg` and the WSL CUDA library path are added.
- `sandbox_network` defaults to `true`; set it to `false` to isolate sandbox networking.
- A project cannot enable both the Bubblewrap sandbox and the Docker container runner.
- The legacy keys `gpu_enabled`, `gpu_network`, `gpu_timeout_seconds`, and the `[projects.<alias>.gpu]` table are still read as `sandbox_enabled = true` plus `sandbox_gpu = true`. Do not mix them with the new keys.
- Restart AgentMessage after changing the configuration.

## Verification and Use

```bash
cd ~/workspace/AgentMessage
uv run agent-message doctor --sandbox your_proj_name
```

`--gpu` is the legacy alias for the same check. With `sandbox_gpu` enabled, the check must report a real GPU backend and devices.

After a task starts, inspect status and logs from Feishu:

```text
/status a1b2c3d4
/logs a1b2c3d4 50 sandbox
```

`/logs a1b2c3d4 50 gpu` still works and is equivalent to `sandbox`. If the agent falls back to the normal shell, send:

```text
请通过 agent_message_sandbox 的 sandbox_run 重新执行，不要使用普通 shell。
```

## Git commits and SSH push/pull

From the repository root, run `uv run python scripts/configure_sandbox_git.py` in a local terminal. It detects the global Git identity, current SSH agent and `~/.ssh/known_hosts`, preferring existing settings; Enter reuses detected values. Use `--config PATH` for another local configuration. Input is hidden; other TOML settings are preserved and the saved file has mode `0600`. Choosing SSH `n` removes both existing forwarding settings. The script does not create or load keys, start an agent, download host keys, or restart the service. For first-time setup, prepare a dedicated agent and verified known_hosts as described below, then restart the service after active tasks finish.

A local commit needs a committer identity, not an SSH key. Add these settings once to the existing global `[service]` table in `config/projects.toml`. Every sandbox project shares them; no per-repository setup is needed:

```toml
[service]
sandbox_git_user_name = "Alice"
sandbox_git_user_email = "alice@example.com"
# SSH authentication uses these two global absolute paths and requires both:
sandbox_ssh_agent_socket = "/run/user/1000/agent-message-ssh.sock"
sandbox_ssh_known_hosts = "/home/alice/.ssh/known_hosts"
```

`sandbox_enabled`, `sandbox_gpu`, `sandbox_network`, and the timeout stay in `[projects.<alias>]`; SSH remote operations need networking enabled for the project. The four Git/SSH settings belong only in `[service]`; placing them under a project raises a migration error. The legacy `gpu_git_*` and `gpu_ssh_*` names are still read.

Adjust the absolute paths for the service user. `sandbox_ssh_agent_socket` must be a Unix socket owned by that user; every run checks that the socket and the known-hosts file exist. Without configuration the runner does not inherit `SSH_AUTH_SOCK`. Invalid configuration stops that run with an explicit error.

Start a dedicated agent as the user that runs AgentMessage, then add the keys the project needs:

```bash
ssh-agent -a "$XDG_RUNTIME_DIR/agent-message-ssh.sock"
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh.sock" ssh-add ~/.ssh/id_ed25519
```

Private key passphrases are entered only in the host terminal. Keep the agent running; after a machine restart, start it again and reload the keys. A fixed socket path means the systemd service never needs to inherit the terminal's `SSH_AUTH_SOCK`. Verify host fingerprints for known_hosts through a trusted channel beforehand; unknown or changed host keys are rejected, and disabling host verification is not a fix.

After restarting AgentMessage, run `git add`, `git commit`, and authorized `git push`/`git pull` through `sandbox_run`. The sandbox does not load the host `~/.ssh/config`, so remotes that rely on host aliases or custom ports must use an SSH URL with the actual host and port. It also does not load the host global Git configuration; existing commit-signing settings in the repository are preserved, and any signing key or configuration must be provided inside the project. Private keys can sign over SSH through the agent.

Forwarding the socket lets the sandbox authenticate and sign with every key loaded into that agent. The private keys stay unreadable, but a dedicated agent holding only the required keys is still recommended. This configuration applies to Bubblewrap sandbox projects only; Docker containers need their own authentication setup.

## Security Boundary

- The sandbox mounts only the current project and the required system files; `/dev/dxg` is added only when `sandbox_gpu` is enabled.
- The project and its internal `.git` are writable, and Git writes go through `sandbox_run`. External worktree Git metadata and private keys are not mounted; only an explicitly configured SSH agent socket and known-hosts file are exposed, and remote operations still require enabled networking.
- The host HOME, Windows directories, other projects, Feishu credentials, and the AgentMessage database are not visible.
- Bubblewrap uses `--clearenv`, so sandbox commands never inherit Feishu service environment variables.
- Each command is bounded by the project's timeout, cancellation, and logging policy.
