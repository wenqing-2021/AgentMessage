#!/usr/bin/env bash
# Managed by AgentMessage: update an installed checkout and restart its services.
set -Eeuo pipefail
IFS=$'\n\t'

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
SYSTEMD_USER_DIR="$CONFIG_HOME/systemd/user"
UNIT_FILE="$SYSTEMD_USER_DIR/agent-message.service"
SSH_AGENT_UNIT_FILE="$SYSTEMD_USER_DIR/agent-message-ssh-agent.service"
SSH_AGENT_DROPIN_FILE="$SYSTEMD_USER_DIR/agent-message.service.d/ssh-agent.conf"
SSH_AGENT_LOADER_FILE="$HOME/.local/libexec/agent-message/load_ssh_keys.py"
SSH_AGENT_SOCKET="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/agent-message-ssh/agent.sock"
BRIDGE_SERVICE="agent-message.service"
SSH_AGENT_SERVICE="agent-message-ssh-agent.service"

PULL=true
FAILED=false

info() {
    printf '[AgentMessage] %s\n' "$*"
}

warn() {
    printf '[AgentMessage] WARNING: %s\n' "$*" >&2
}

die() {
    printf '[AgentMessage] ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: bash update.sh [--no-pull]

Update the installed AgentMessage checkout and restart its user services:
pull the latest code, sync Python dependencies, and reinstall the managed
systemd files with "install.sh --refresh-service" when a service in deploy/
changed. It then restarts the dedicated SSH agent and the bridge. The SSH agent
restarts empty, so it ends by printing both service states, the number of loaded
keys, and how to unlock passphrase-protected keys again.
EOF
}

pull_latest() {
    if [[ $PULL != true ]]; then
        info "已跳过 git pull（--no-pull）。"
        return 0
    fi
    if [[ ! -d $INSTALL_DIR/.git ]]; then
        info "该目录不是 git 检出，跳过 git pull。"
        return 0
    fi
    if [[ -n $(git -C "$INSTALL_DIR" status --porcelain) ]]; then
        warn "检测到未提交的本地改动，已跳过 git pull；需要更新时请先提交或 git stash。"
        return 0
    fi
    info "拉取最新代码 ..."
    git -C "$INSTALL_DIR" pull --ff-only
}

sync_dependencies() {
    command -v uv >/dev/null 2>&1 || die "找不到 uv，无法同步依赖。"
    info "同步 Python 依赖（uv sync --frozen）..."
    (cd "$INSTALL_DIR" && uv sync --frozen)
}

# install.sh owns the managed systemd files; they are reinstalled as soon as a
# service in deploy/ (or the key loader) differs from the installed copy.
unit_templates_current() {
    cmp -s "$INSTALL_DIR/deploy/agent-message-ssh-agent.service" "$SSH_AGENT_UNIT_FILE" || return 1
    cmp -s "$INSTALL_DIR/deploy/agent-message-ssh-agent.conf" "$SSH_AGENT_DROPIN_FILE" || return 1
    cmp -s "$INSTALL_DIR/scripts/load_ssh_keys.py" "$SSH_AGENT_LOADER_FILE" || return 1
    # The bridge unit is rendered with install-specific paths, so compare timestamps.
    [[ -f $UNIT_FILE ]] || return 1
    [[ $INSTALL_DIR/deploy/agent-message.service -nt $UNIT_FILE ]] && return 1
    [[ $INSTALL_DIR/install.sh -nt $UNIT_FILE ]] && return 1
    return 0
}

refresh_units() {
    if unit_templates_current; then
        return 0
    fi
    info "deploy/ 中的 service 有改动，重新安装受管文件 ..."
    bash "$INSTALL_DIR/install.sh" --install-dir "$INSTALL_DIR" --refresh-service
}

restart_unit() {
    local unit=$1
    info "重启 $unit ..."
    if ! systemctl --user restart "$unit"; then
        warn "重启 $unit 失败；请在 systemd 用户会话可用的终端中执行：systemctl --user restart $unit"
        FAILED=true
    fi
}

print_key_hint() {
    cat <<EOF

[AgentMessage] 提示：重启专用 SSH agent 会清空已手动解锁的密钥。
  有口令的私钥需要重新解锁：
      SSH_AUTH_SOCK="$SSH_AGENT_SOCKET" ssh-add ~/.ssh/your_encrypted_key
EOF
}

print_service_status() {
    local -a states=()
    mapfile -t states < <(systemctl --user is-active "$SSH_AGENT_SERVICE" "$BRIDGE_SERVICE" 2>/dev/null || true)
    info "服务状态："
    printf '  %s: %s\n' "$SSH_AGENT_SERVICE" "${states[0]:-未知}"
    printf '  %s: %s\n' "$BRIDGE_SERVICE" "${states[1]:-未知}"

    if ! command -v ssh-add >/dev/null 2>&1; then
        printf '  已加载密钥：无法确认（缺少 ssh-add）\n'
        return 0
    fi
    local output status
    output=$(SSH_AUTH_SOCK="$SSH_AGENT_SOCKET" ssh-add -l 2>/dev/null) && status=0 || status=$?
    if [[ $status == 0 ]]; then
        local loaded
        loaded=$(printf '%s\n' "$output" | sed '/^$/d' | wc -l | tr -d ' ')
        printf '  已加载密钥：%s 把\n' "$loaded"
    elif [[ $status == 1 ]]; then
        printf '  已加载密钥：0 把\n'
    else
        printf '  已加载密钥：无法确认（agent 未就绪）\n'
    fi
}

main() {
    while (($#)); do
        case $1 in
            --no-pull)
                PULL=false
                ;;
            -h|--help)
                usage
                return 0
                ;;
            *)
                die "Unknown argument: $1"
                ;;
        esac
        shift
    done

    [[ -d $INSTALL_DIR/src/agent_message ]] ||
        die "$INSTALL_DIR 不是 AgentMessage 仓库，无法更新。"

    pull_latest
    sync_dependencies
    refresh_units

    restart_unit "$SSH_AGENT_SERVICE"
    restart_unit "$BRIDGE_SERVICE"

    print_service_status
    print_key_hint

    if [[ $FAILED == true ]]; then
        die "部分服务未重启成功，请按上面的提示手动处理。"
    fi
    info "更新完成。"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
