# Rerunning the Installer and Adding Bots

Run `bash install.sh` in the installation directory. Interrupted first-time installs resume from the last completed stage.

To use another install directory:

```bash
bash /tmp/agent-message-install.sh --install-dir /absolute/path/AgentMessage
```

If systemd is installed but inactive in WSL, the installer pauses with the required `/etc/wsl.conf` and `wsl --shutdown` instructions. Reopen WSL and run it again to resume.

When an installation exists, rerunning the installer offers **overwrite credentials and reinstall services**, **add a bot**, or **show installation info and exit** (default). Overwrite preserves project configuration, authorizations, and history.

Choose **add a bot** and enter its App ID / App Secret. Only `agent_message` can be shared with the new bot; every other project stays with the primary bot. The script saves credentials and assignments automatically, and all bots share the current checkout and `config/projects.toml`. After sharing, the primary bot switches its default project to one it still manages. Each project belongs to one bot, and the primary service restarts when assignments change.

Without a terminal (for example when invoked by another program), interactive selection is unavailable; run `bash install.sh --show` to print the installation info.

Running `bash uninstall.sh` without arguments lists every installed bot: the primary bot comes first, marked that removing it means removing the whole shared installation, followed by each additional bot. Enter one or more numbers separated by spaces or commas to remove just those additional bots, `a`/`all` to remove the whole shared installation and every bot, or `q` plus Enter (or just Enter) to quit without changes.

When only additional bots are selected, the script asks once whether to back up their credentials and local state (default: yes) and then asks for one confirmation. Removing a bot stops and deletes its systemd service, credentials, and local state, returns its `agent_message` project to the primary bot, keeps the shared checkout, and restarts the primary service. Selecting the primary bot or entering `a`/`all` warns that the primary bot is tied to the shared installation and continues with the existing full-uninstall prompts (backup question defaults to no, then the final confirmation), removing the checkout, credentials, and local state of every bot. Adding `--yes` without `--app-id` skips the menu and removes the whole shared installation; `bash uninstall.sh --app-id cli_second` (with optional `--purge-data` / `--keep-data` / `--yes`) still removes exactly that one additional bot without the interactive menu; `--install-dir PATH` still selects the installation.

Authorize users separately for each bot and select it with `--app-id`:

```bash
uv run agent-message pending-senders --app-id cli_second
uv run agent-message authorize ou_xxx --app-id cli_second
uv run agent-message tasks --app-id cli_second
```

Without `--app-id`, commands select the primary bot. Run `bash update.sh` once in the shared checkout to update the code and refresh all bot services. Without `--app-id`, `bash uninstall.sh` opens the interactive selection described above; `--yes` skips it and removes the entire shared installation, including all bots; its keep-data option backs up all credentials and state. To edit an additional bot's credentials, run `bash install.sh --app-id cli_second` and choose overwrite.

Additional bot services are named `agent-message-bot-<AppID>.service`. Manage them with `systemctl --user restart`, `status`, or `journalctl --user -u`.

[Back to README](../../README.md)
