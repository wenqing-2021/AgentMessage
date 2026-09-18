#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

INSTALL_DIR="${AGENT_MESSAGE_INSTALL_DIR:-$HOME/workspace/AgentMessage}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_DIR="$CONFIG_HOME/agent-message"
UNIT_FILE="$CONFIG_HOME/systemd/user/agent-message.service"
KEEP_DATA=
DATA_CHOICE_SET=false
ASSUME_YES=false
BACKUP_DIR=

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
Usage: bash uninstall.sh [options]

Options:
  --install-dir PATH  AgentMessage installation to remove.
  --keep-data         Back up project configuration, state, logs, and credentials.
  --purge-data        Delete all AgentMessage data (the interactive default).
  --yes               Skip the final uninstall confirmation.
  -h, --help          Show this help.

systemd, uv, Codex, Qoder, Docker, and Bubblewrap are not removed.
EOF
}

set_data_choice() {
    [[ $DATA_CHOICE_SET == false ]] || die "Choose only one of --keep-data or --purge-data."
    KEEP_DATA=$1
    DATA_CHOICE_SET=true
}

normalize_and_validate_paths() {
    command -v realpath >/dev/null 2>&1 || die "realpath is required to validate removal paths."
    [[ $INSTALL_DIR == /* ]] || die "The install directory must be an absolute path."
    [[ $INSTALL_DIR != *$'\n'* && $INSTALL_DIR != *$'\r'* ]] ||
        die "The install directory cannot contain line breaks."

    local resolved_home resolved_install
    resolved_home=$(realpath -m -- "$HOME")
    resolved_install=$(realpath -m -- "$INSTALL_DIR")
    [[ $resolved_install != / && $resolved_install != "$resolved_home" ]] ||
        die "Refusing to remove an unsafe install directory: $resolved_install"
    INSTALL_DIR=$resolved_install

    if [[ -e $INSTALL_DIR ]]; then
        [[ -f $INSTALL_DIR/pyproject.toml && -d $INSTALL_DIR/src/agent_message ]] ||
            die "$INSTALL_DIR does not look like an AgentMessage checkout."
    fi

    local resolved_config
    resolved_config=$(realpath -m -- "$CONFIG_DIR")
    [[ $resolved_config != / && $resolved_config != "$resolved_home" ]] ||
        die "Refusing to remove an unsafe configuration directory: $resolved_config"
    CONFIG_DIR=$resolved_config
}

prompt_data_choice() {
    [[ -r /dev/tty ]] || die "Interactive selection requires a terminal; use --keep-data or --purge-data."
    local answer
    printf '保留项目配置、任务数据库、日志和飞书凭证？[y/N]: ' >/dev/tty
    if ! IFS= read -r answer </dev/tty; then
        printf '\n' >/dev/tty
        exit 130
    fi
    case $answer in
        y|Y|yes|YES|Yes)
            KEEP_DATA=true
            ;;
        *)
            KEEP_DATA=false
            ;;
    esac
    DATA_CHOICE_SET=true
}

confirm_uninstall() {
    [[ $ASSUME_YES == true ]] && return 0
    [[ -r /dev/tty ]] || die "Final confirmation requires a terminal; use --yes for automation."
    local answer action
    if [[ $KEEP_DATA == true ]]; then
        action="备份数据后删除 AgentMessage"
    else
        action="删除 AgentMessage 及其全部本地数据"
    fi
    printf '%s。确认继续？[y/N]: ' "$action" >/dev/tty
    if ! IFS= read -r answer </dev/tty; then
        printf '\n' >/dev/tty
        exit 130
    fi
    case $answer in
        y|Y|yes|YES|Yes)
            ;;
        *)
            info "Uninstall cancelled; nothing was removed."
            exit 0
            ;;
    esac
}

stop_and_remove_service() {
    if command -v systemctl >/dev/null 2>&1 &&
        systemctl --user show-environment >/dev/null 2>&1; then
        if systemctl --user is-active --quiet agent-message.service; then
            systemctl --user stop agent-message.service ||
                die "Failed to stop agent-message.service; no files were removed."
        fi
        systemctl --user disable agent-message.service >/dev/null 2>&1 || true
    else
        warn "The systemd user manager is unavailable; no managed service could be stopped."
    fi

    # Remove only our optional dedicated agent, never the user's other SSH agents.
    local ssh_unit="${UNIT_FILE%/*}/agent-message-ssh-agent.service"
    local ssh_dropin="${UNIT_FILE}.d/ssh-agent.conf"
    if [[ -f $ssh_unit ]] && grep -q '^# Managed by AgentMessage:' "$ssh_unit"; then
        if command -v systemctl >/dev/null 2>&1 &&
            systemctl --user show-environment >/dev/null 2>&1; then
            systemctl --user stop agent-message-ssh-agent.service ||
                die "Failed to stop agent-message-ssh-agent.service; no files were removed."
            systemctl --user disable agent-message-ssh-agent.service >/dev/null 2>&1 || true
        fi
        rm -f -- "$ssh_unit"
    fi
    if [[ -f $ssh_dropin ]] && grep -q '^# Managed by AgentMessage:' "$ssh_dropin"; then
        rm -f -- "$ssh_dropin"
        rmdir --ignore-fail-on-non-empty "${UNIT_FILE}.d" 2>/dev/null || true
    fi
    local ssh_loader="$HOME/.local/libexec/agent-message/load_ssh_keys.py"
    if [[ -f $ssh_loader ]] && grep -q '^# Managed by AgentMessage: SSH key loader$' "$ssh_loader"; then
        rm -f -- "$ssh_loader"
        rmdir --ignore-fail-on-non-empty "$HOME/.local/libexec/agent-message" 2>/dev/null || true
    fi
    # User linger is shared with other services and must not be disabled here.
    rm -f -- "$UNIT_FILE"
    if command -v systemctl >/dev/null 2>&1 &&
        systemctl --user show-environment >/dev/null 2>&1; then
        systemctl --user daemon-reload
        systemctl --user reset-failed agent-message.service >/dev/null 2>&1 || true
        systemctl --user reset-failed agent-message-ssh-agent.service >/dev/null 2>&1 || true
    fi
}

backup_data() {
    local backup_base repository_backup
    backup_base="$DATA_HOME/agent-message/backups"
    mkdir -p "$backup_base"
    chmod 700 "$DATA_HOME/agent-message" "$backup_base"
    BACKUP_DIR=$(mktemp -d "$backup_base/uninstall-$(date +%Y%m%d-%H%M%S).XXXXXX")
    chmod 700 "$BACKUP_DIR"
    repository_backup="$BACKUP_DIR/repository"

    if [[ -f $INSTALL_DIR/config/projects.toml ]]; then
        mkdir -p "$repository_backup/config"
        cp -a -- "$INSTALL_DIR/config/projects.toml" "$repository_backup/config/"
    fi
    if [[ -d $INSTALL_DIR/var ]]; then
        mkdir -p "$repository_backup"
        cp -a -- "$INSTALL_DIR/var" "$repository_backup/"
    fi
    if [[ -d $CONFIG_DIR ]]; then
        cp -a -- "$CONFIG_DIR" "$BACKUP_DIR/user-config"
    fi
    {
        printf '%s\n' 'AgentMessage uninstall backup'
        printf '%s\n' 'repository/config/projects.toml: project registry (when present)'
        printf '%s\n' 'repository/var/: SQLite state and logs (when present)'
        printf '%s\n' 'user-config/: Feishu credentials and related user configuration (when present)'
    } >"$BACKUP_DIR/CONTENTS.txt"
    chmod -R go-rwx "$BACKUP_DIR"
    info "Data backup completed: $BACKUP_DIR"
}

remove_files() {
    rm -rf -- "$INSTALL_DIR"
    rm -rf -- "$CONFIG_DIR"
}

print_summary() {
    info "AgentMessage service, checkout, and local configuration were removed."
    if [[ $KEEP_DATA == true ]]; then
        info "Retained data: $BACKUP_DIR"
    else
        info "Project configuration, task state, logs, and Feishu credentials were deleted."
    fi
    info "systemd, uv, Codex, Qoder, Docker, and Bubblewrap were left installed."
}

main() {
    while (($#)); do
        case $1 in
            --install-dir)
                (($# >= 2)) || die "--install-dir requires a path."
                INSTALL_DIR=$2
                shift 2
                ;;
            --keep-data)
                set_data_choice true
                shift
                ;;
            --purge-data)
                set_data_choice false
                shift
                ;;
            --yes)
                ASSUME_YES=true
                shift
                ;;
            -h|--help)
                usage
                return 0
                ;;
            *)
                die "Unknown argument: $1"
                ;;
        esac
    done

    normalize_and_validate_paths
    [[ $DATA_CHOICE_SET == true ]] || prompt_data_choice
    confirm_uninstall
    stop_and_remove_service
    [[ $KEEP_DATA == false ]] || backup_data
    remove_files
    print_summary
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
