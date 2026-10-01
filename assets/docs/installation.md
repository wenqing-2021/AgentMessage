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

不带参数运行 `bash uninstall.sh` 会先列出所有已安装机器人：默认机器人排在最前，并标注删除它意味着删除整个共用安装，之后是各个新增机器人。输入一个或多个编号（用空格或逗号分隔）只删除选中的新增机器人，输入 `a`/`all` 删除整个共用安装及所有机器人，输入 `q` 加回车（或直接回车）则退出且不做任何改动。

只删除新增机器人时，脚本先询问是否备份它们的凭证和本地数据（默认备份），随后要求一次确认；删除会停止并移除对应的 systemd 服务、凭证和本地数据，把它们管理的 `agent_message` 归还给默认机器人，保留共用代码并重启默认机器人服务。选中默认机器人或输入 `a`/`all` 时，脚本会提示默认机器人与共用安装绑定，然后进入原有的完整卸载流程（是否备份默认不备份，最后再确认），一并删除代码、凭证及所有机器人的本地数据。不带 `--app-id` 时加 `--yes` 会跳过菜单并直接卸载整个共用安装；`bash uninstall.sh --app-id cli_second`（可加 `--purge-data` / `--keep-data` / `--yes`）仍只删除该新增机器人且不显示交互菜单；`--install-dir PATH` 仍用于指定安装目录。

每个机器人分别授权用户，本地命令通过 `--app-id` 选择：

```bash
uv run agent-message pending-senders --app-id cli_second
uv run agent-message authorize ou_xxx --app-id cli_second
uv run agent-message tasks --app-id cli_second
```

不指定 `--app-id` 时使用默认机器人。更新只需在当前仓库运行 `bash update.sh`，会更新共用代码并刷新各机器人服务。不带 `--app-id` 运行 `bash uninstall.sh` 会进入上面的交互式选择，加 `--yes` 则跳过选择并卸载整个共用安装（含所有机器人）；其保留数据选项会一并备份凭证和状态。单独修改新增机器人的凭证可运行 `bash install.sh --app-id cli_second`，选择覆盖。

新增机器人的服务名为 `agent-message-bot-<AppID>.service`，可用 `systemctl --user restart`、`status` 或 `journalctl --user -u` 管理。

[返回 README](../../README_CN.md)
