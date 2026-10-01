# 重复安装与多机器人

在安装目录运行 `bash install.sh`；首次安装中断时会从断点继续。

自定义安装目录时使用：

```bash
bash /tmp/agent-message-install.sh --install-dir /absolute/path/AgentMessage
```

如果 systemd 已安装但尚未在 WSL 中启用，脚本会暂停并给出 `/etc/wsl.conf` 和 `wsl --shutdown` 提示；重新打开 WSL 后再次运行即可继续。

重复运行安装脚本时，已有安装会显示三个选项：**覆盖凭证并重装服务**、**新建机器人**、**显示安装信息并退出**（默认）。覆盖保留项目配置、授权和历史数据。

选择“新建机器人”，输入 App ID / App Secret。只有 `agent_message` 可以共享给新机器人，其他项目仍归原机器人；脚本会自动保存凭证和项目绑定，所有机器人共用当前代码及 `config/projects.toml`，无需手动填写机器人配置。共享后原机器人的默认项目会自动切换到它仍管理的项目，每个项目只归属一个机器人；调整分配时会重启原服务。

没有终端时（例如由其他程序调用）无法交互选择，可用 `bash install.sh --show` 直接打印安装信息。

每个机器人分别授权用户，本地命令通过 `--app-id` 选择：

```bash
uv run agent-message pending-senders --app-id cli_second
uv run agent-message authorize ou_xxx --app-id cli_second
uv run agent-message tasks --app-id cli_second
```

不指定 `--app-id` 时使用默认机器人。更新只需在当前仓库运行 `bash update.sh`，会更新共用代码并刷新各机器人服务。`bash uninstall.sh` 卸载整个共用安装（含所有机器人）；其保留数据选项会一并备份凭证和状态。单独修改新增机器人的凭证可运行 `bash install.sh --app-id cli_second`，选择覆盖。

新增机器人的服务名为 `agent-message-bot-<AppID>.service`，可用 `systemctl --user restart`、`status` 或 `journalctl --user -u` 管理。

[返回 README](../../README_CN.md)
