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
sandbox_ssh_agent_socket = "/run/user/1000/agent-message-ssh/agent.sock"
sandbox_ssh_known_hosts = "/home/alice/.ssh/known_hosts"
```

`sandbox_enabled`, `sandbox_gpu`, `sandbox_network`, and the timeout stay in `[projects.<alias>]`; SSH remote operations need networking enabled for the project. The four Git/SSH settings belong only in `[service]`; placing them under a project raises a migration error. The legacy `gpu_git_*` and `gpu_ssh_*` names are still read.

Adjust the absolute paths for the service user. `sandbox_ssh_agent_socket` must be a Unix socket owned by that user; every run checks that the socket and the known-hosts file exist. Without configuration the runner does not inherit `SSH_AUTH_SOCK`. Invalid configuration stops that run with an explicit error.

### Start the SSH agent automatically with WSL

Enable `[boot]` / `systemd=true` in `/etc/wsl.conf` first. Only enabling systemd for the first time requires `wsl --shutdown` from Windows and reopening the distribution.

`install.sh` now installs and enables this dedicated user service by default; it listens on the fixed socket `/run/user/<uid>/agent-message-ssh/agent.sock`, so a normal installation needs no manual step. In the `service` stage it copies `deploy/agent-message-ssh-agent.service` to `~/.config/systemd/user/agent-message-ssh-agent.service`, `deploy/agent-message-ssh-agent.conf` to `~/.config/systemd/user/agent-message.service.d/ssh-agent.conf`, and `scripts/load_ssh_keys.py` to `~/.local/libexec/agent-message/load_ssh_keys.py`, all with mode `600`. It then runs `systemctl --user daemon-reload`, enables and starts the agent, and enables and restarts `agent-message.service`. Repository systemd templates all live in `deploy/`; `install.sh` renders the bridge unit from `deploy/agent-message.service`, replacing its placeholders with the real install path, credentials file, executable and PATH. Only a manual deployment or a unit change needs the managed files reinstalled from the latest templates:

```bash
cd ~/workspace/AgentMessage && bash install.sh --refresh-service
```

Linger starts the user service manager without a terminal login. `install.sh` never enables it, so turn it on yourself once if you need that:

```bash
sudo loginctl enable-linger "$(id -un)"
```

Verify the installed agent:

```bash
systemctl --user status agent-message-ssh-agent.service
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh/agent.sock" ssh-add -l
```

For a manual deployment without `install.sh`, copy the same files from the repository yourself:

```bash
cd ~/workspace/AgentMessage
mkdir -p ~/.config/systemd/user/agent-message.service.d ~/.local/libexec/agent-message
install -m 600 scripts/load_ssh_keys.py ~/.local/libexec/agent-message/
install -m 600 deploy/agent-message-ssh-agent.service ~/.config/systemd/user/
install -m 600 deploy/agent-message-ssh-agent.conf ~/.config/systemd/user/agent-message.service.d/ssh-agent.conf
systemctl --user daemon-reload
systemctl --user enable --now agent-message-ssh-agent.service
```

At each startup, the service scans the top level of `~/.ssh` and loads every private key owned by the current user with no group/other permissions that unlocks without a passphrase. It recognizes OpenSSH, RSA, DSA, EC, and PKCS#8 headers regardless of filename; matching `.pub` files are not required. Symlinks, directories, public keys, configuration, and known-host files are ignored. Encrypted or malformed keys are skipped without prompting or modifying them; a failed key does not block the remaining keys.

Sandboxes can authenticate/sign with all automatically loaded keys. Logs report only loaded/skipped counts, not key material, paths, or fingerprints. Unlock encrypted keys manually in a host terminal:

```bash
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh/agent.sock" ssh-add ~/.ssh/your_encrypted_key
```

After adding keys, restart the dedicated agent when no sandbox tasks are running to rescan. Restarting loses previously unlocked encrypted keys, so unlock those again:

```bash
systemctl --user restart agent-message-ssh-agent.service
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh/agent.sock" ssh-add -l
```

Update global `sandbox_ssh_agent_socket` to `/run/user/<your uid>/agent-message-ssh/agent.sock`, keeping the verified known-hosts file. You can also run the setup wizard once the agent is active:

```bash
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh/agent.sock" uv run python scripts/configure_sandbox_git.py
# If a previous socket is configured, enter the new path instead of accepting the old value.
# Wait for current tasks to finish before restarting:
systemctl --user restart agent-message.service
```

Linger starts the user service manager when the distribution boots. The dependency orders AgentMessage after agent startup and key loading. A crashed agent restarts and reloads its keys. `Wants` avoids terminating active AgentMessage tasks when the agent restarts; new sandbox commands fail explicitly while its socket is unavailable.

Verify with:

```bash
loginctl show-user "$(id -un)" -p Linger
systemctl --user is-enabled agent-message.service agent-message-ssh-agent.service
systemctl --user is-active agent-message.service agent-message-ssh-agent.service
SSH_AUTH_SOCK="$XDG_RUNTIME_DIR/agent-message-ssh/agent.sock" ssh-add -l
```

This starts services when the WSL **distribution starts**; it does not launch the distribution on Windows login. Verify known-host fingerprints through a trusted channel. Each managed file carries a `# Managed by AgentMessage:` marker on its first line; `uninstall.sh` stops and removes all three of them, and keeps your SSH keys, other agents, and the shared user linger setting.

After restarting AgentMessage, run `git add`, `git commit`, and authorized `git push`/`git pull` through `sandbox_run`. The sandbox does not load the host `~/.ssh/config`, so remotes that rely on host aliases or custom ports must use an SSH URL with the actual host and port. It also does not load the host global Git configuration; existing commit-signing settings in the repository are preserved, and any signing key or configuration must be provided inside the project. Private keys can sign over SSH through the agent.

Forwarding the socket lets the sandbox authenticate and sign with every key loaded into that agent. The private keys stay unreadable, but a dedicated agent holding only the required keys is still recommended. This configuration applies to Bubblewrap sandbox projects only; Docker containers need their own authentication setup.

## Security Boundary

- The sandbox mounts only the current project and the required system files; `/dev/dxg` is added only when `sandbox_gpu` is enabled.
- The project and its internal `.git` are writable, and Git writes go through `sandbox_run`. External worktree Git metadata and private keys are not mounted; only an explicitly configured SSH agent socket and known-hosts file are exposed, and remote operations still require enabled networking.
- The host HOME, Windows directories, other projects, Feishu credentials, and the AgentMessage database are not visible.
- Bubblewrap uses `--clearenv`, so sandbox commands never inherit Feishu service environment variables.
- Each command is bounded by the project's timeout, cancellation, and logging policy.
