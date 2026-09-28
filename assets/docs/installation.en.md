# Rerunning the Installer and Adding Bots

Run `bash install.sh` in the installation directory. Interrupted first-time installs resume from the last completed stage.

To use another install directory:

```bash
bash /tmp/agent-message-install.sh --install-dir /absolute/path/AgentMessage
```

If systemd is installed but inactive in WSL, the installer pauses with the required `/etc/wsl.conf` and `wsl --shutdown` instructions. Reopen WSL and run it again to resume.

When an installation exists, rerunning the installer offers **overwrite credentials and reinstall services**, **add a bot**, or **show installation info and exit** (default). Overwrite preserves project configuration, authorizations, and history.

Choose **add a bot**, enter its App ID / App Secret, and select existing projects. The script saves the credentials and project assignments automatically; all bots share the current checkout and `config/projects.toml`. Each project belongs to one bot, and project directories assigned to different bots must not overlap. The primary bot keeps its default project and restarts when assignments change.

Authorize users separately for each bot and select it with `--app-id`:

```bash
uv run agent-message pending-senders --app-id cli_second
uv run agent-message authorize ou_xxx --app-id cli_second
uv run agent-message tasks --app-id cli_second
```

Without `--app-id`, commands select the primary bot. Run `bash update.sh` once in the shared checkout to update the code and refresh all bot services. `bash uninstall.sh` removes the entire shared installation, including all bots; its keep-data option backs up all credentials and state. To edit an additional bot's credentials, run `bash install.sh --app-id cli_second` and choose overwrite.

Additional bot services are named `agent-message-bot-<AppID>.service`. Manage them with `systemctl --user restart`, `status`, or `journalctl --user -u`.

[Back to README](../../README.md)
