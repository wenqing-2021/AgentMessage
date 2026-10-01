#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

INSTALL_DIR="${AGENT_MESSAGE_INSTALL_DIR:-$HOME/workspace/AgentMessage}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_DIR="$CONFIG_HOME/agent-message"
ENV_FILE="$CONFIG_DIR/feishu.env"
UNIT_FILE="$CONFIG_HOME/systemd/user/agent-message.service"
KEEP_DATA=
DATA_CHOICE_SET=false
ASSUME_YES=false
BACKUP_DIR=
APP_ID=
APP_ID_SET=false
BOT_STATE_DIR=
BOT_LOG_DIR=
PRIMARY_BOT_ID=
INTERACTIVE_SELECTION=false
REMOVE_ALL=false
INSTALLED_BOT_IDS=()
SELECTED_BOT_IDS=()
BOT_BACKUP_DIRS=()

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

# The mode bits on /dev/tty stay readable without a controlling terminal, so
# check that it can actually be opened.
has_terminal() {
    { exec 3<>/dev/tty; } 2>/dev/null || return 1
    exec 3>&-
    return 0
}

read_env_value() {
    local key=$1
    [[ -f $ENV_FILE ]] || return 0
    awk -v wanted="$key" '
        index($0, wanted "=") == 1 {
            value = substr($0, length(wanted) + 2)
            sub(/\r$/, "", value)
            print value
            exit
        }
    ' "$ENV_FILE"
}

usage() {
    cat <<'EOF'
Usage: bash uninstall.sh [options]

Options:
  --install-dir PATH  AgentMessage installation to remove or modify.
  --app-id ID         Remove one additional bot only: stop and delete its
                      service, credentials, and local state, return its projects
                      to the primary bot, and keep the shared checkout. Its data
                      is backed up first unless --purge-data is given.
  --keep-data         Back up data before removing it.
  --purge-data        Delete data instead of backing it up (the interactive
                      default for a full uninstall).
  --yes               Skip the interactive bot selection and confirmations, and
                      remove the whole shared installation, including all bots.
                      Pair it with --keep-data or --purge-data for unattended use.
  -h, --help          Show this help.

Interactive (default): lists every installed bot and asks whether to remove
selected bots or the whole shared installation. The primary bot is tied to the
shared installation, so removing it means removing everything; choose "all" or
the primary bot's number to do that.
systemd, uv, Codex, Docker, and Bubblewrap are not removed.
EOF
}

set_data_choice() {
    [[ $DATA_CHOICE_SET == false ]] || die "Choose only one of --keep-data or --purge-data."
    KEEP_DATA=$1
    DATA_CHOICE_SET=true
}

format_bot_list() {
    local output= separator= app_id
    for app_id in "$@"; do
        output+="$separator$app_id"
        separator=', '
    done
    printf '%s' "$output"
}

resolve_primary_bot_id() {
    # The credentials file is authoritative; the registry is the fallback.
    local primary_id
    primary_id=$(read_env_value AGENT_MESSAGE_FEISHU_APP_ID)
    if [[ -z $primary_id ]]; then
        local python="$INSTALL_DIR/.venv/bin/python"
        local project_config="$INSTALL_DIR/config/projects.toml"
        if [[ -x $python && -f $project_config ]]; then
            primary_id=$("$python" -c 'import sys
from agent_message.core.config import ConfigError, load_config
try:
    config = load_config(sys.argv[1])
except (ConfigError, OSError, ValueError):
    raise SystemExit(1)
print(config.primary_app_id or "")' "$project_config" 2>/dev/null) || primary_id=
        fi
    fi
    printf '%s' "$primary_id"
}

registered_bot_ids() {
    local python="$INSTALL_DIR/.venv/bin/python"
    local project_config="$INSTALL_DIR/config/projects.toml"
    [[ -x $python && -f $project_config ]] || return 1
    "$python" -c 'import sys
from agent_message.core.config import ConfigError, load_config
try:
    config = load_config(sys.argv[1])
except (ConfigError, OSError, ValueError):
    raise SystemExit(1)
for app_id in config.bots:
    print(app_id)' "$project_config"
}

collect_installed_bots() {
    # The shared registry is authoritative; credential files are only used when
    # no readable registry exists, and leftovers are reported instead of listed.
    INSTALLED_BOT_IDS=()
    PRIMARY_BOT_ID=$(resolve_primary_bot_id)
    local output= app_id= credential= registered=true
    local -A seen=()
    if ! output=$(registered_bot_ids 2>/dev/null); then
        registered=false
    fi
    if [[ $registered == true ]]; then
        while IFS= read -r app_id; do
            [[ $app_id =~ ^cli_[A-Za-z0-9_-]{1,100}$ ]] || continue
            [[ $app_id == "$PRIMARY_BOT_ID" ]] && continue
            seen[$app_id]=1
        done <<<"$output"
    fi
    for credential in "$CONFIG_DIR/bots"/cli_*.env; do
        [[ -f $credential ]] || continue
        app_id=${credential##*/}
        app_id=${app_id%.env}
        [[ $app_id =~ ^cli_[A-Za-z0-9_-]{1,100}$ ]] || continue
        [[ $app_id == "$PRIMARY_BOT_ID" ]] && continue
        if [[ $registered == true && -z ${seen[$app_id]:-} ]]; then
            warn "$credential is not registered in the shared project configuration; remove it manually."
            continue
        fi
        seen[$app_id]=1
    done
    if ((${#seen[@]})); then
        mapfile -t INSTALLED_BOT_IDS < <(printf '%s\n' "${!seen[@]}" | LC_ALL=C sort)
    fi
}

prompt_bot_backup_choice() {
    has_terminal || die "Interactive selection requires a terminal; use --keep-data or --purge-data."
    local answer
    printf 'Back up the credentials and local state of the removed bots? [Y/n]: ' >/dev/tty
    if ! IFS= read -r answer </dev/tty; then
        printf '\n' >/dev/tty
        exit 130
    fi
    case $answer in
        n|N|no|NO|No)
            KEEP_DATA=false
            ;;
        *)
            KEEP_DATA=true
            ;;
    esac
    DATA_CHOICE_SET=true
}

parse_bot_selection() {
    local answer=$1
    shift
    local -a menu_ids=("$@")
    local selection=${answer//,/ }
    local -a tokens=()
    local token position app_id reply
    local -a chosen_ids=()
    local -A chosen=()
    IFS=$' \t\n' read -r -a tokens <<<"$selection" || true
    if ((${#tokens[@]} == 0)); then
        info "Nothing was selected; no bots were removed."
        exit 0
    fi
    for token in "${tokens[@]}"; do
        case ${token,,} in
            a|all)
                REMOVE_ALL=true
                return 0
                ;;
            q|quit)
                info "Uninstall cancelled; nothing was removed."
                exit 0
                ;;
        esac
        [[ $token =~ ^[0-9]+$ ]] || die "Invalid selection: $token"
        position=$((10#$token))
        ((position >= 1 && position <= ${#menu_ids[@]})) ||
            die "Selection $token is not in the list."
        chosen[${menu_ids[position - 1]}]=1
    done
    for app_id in "${menu_ids[@]}"; do
        [[ -n ${chosen[$app_id]:-} ]] || continue
        if [[ -n $PRIMARY_BOT_ID && $app_id == "$PRIMARY_BOT_ID" ]]; then
            printf '[AgentMessage] %s is the primary bot; removing it removes the shared checkout and every bot.\n' \
                "$app_id" >/dev/tty
            printf 'Remove the whole shared installation? [y/N]: ' >/dev/tty
            if ! IFS= read -r reply </dev/tty; then
                printf '\n' >/dev/tty
                exit 130
            fi
            case $reply in
                y|Y|yes|YES|Yes)
                    REMOVE_ALL=true
                    ;;
                *)
                    info "Nothing was removed."
                    exit 0
                    ;;
            esac
            return 0
        fi
        chosen_ids+=("$app_id")
    done
    if ((${#chosen_ids[@]} == 0)); then
        info "Nothing was selected; no bots were removed."
        exit 0
    fi
    SELECTED_BOT_IDS=("${chosen_ids[@]}")
}

prompt_bot_selection() {
    collect_installed_bots
    local -a menu_ids=()
    local index entry answer
    if [[ -n $PRIMARY_BOT_ID ]]; then
        menu_ids+=("$PRIMARY_BOT_ID")
    fi
    if ((${#INSTALLED_BOT_IDS[@]})); then
        menu_ids+=("${INSTALLED_BOT_IDS[@]}")
    fi
    if ((${#menu_ids[@]} == 0)); then
        info "No installed bot credentials were found; continuing with the full uninstall prompts."
        REMOVE_ALL=true
        return 0
    fi
    printf 'Installed bots:\n' >/dev/tty
    for index in "${!menu_ids[@]}"; do
        entry=${menu_ids[index]}
        if [[ -n $PRIMARY_BOT_ID && $entry == "$PRIMARY_BOT_ID" ]]; then
            printf '  %d) %s (primary; removing it removes the whole installation)\n' \
                "$((index + 1))" "$entry" >/dev/tty
        else
            printf '  %d) %s (service: agent-message-bot-%s.service)\n' \
                "$((index + 1))" "$entry" "$entry" >/dev/tty
        fi
    done
    printf '  a) all: remove the shared installation and every bot\n' >/dev/tty
    printf '  q) quit without removing anything\n' >/dev/tty
    printf 'Select bots to remove (numbers separated by spaces or commas, "a" for all, "q" to quit) [q]: ' >/dev/tty
    if ! IFS= read -r answer </dev/tty; then
        printf '\n' >/dev/tty
        exit 130
    fi
    parse_bot_selection "$answer" "${menu_ids[@]}"
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
    has_terminal || die "Interactive selection requires a terminal; use --keep-data or --purge-data."
    local answer
    printf 'Back up project configuration, task databases, logs, and Feishu credentials? [y/N]: ' >/dev/tty
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
    has_terminal || die "Final confirmation requires a terminal; use --yes for automation."
    local answer action
    if [[ $KEEP_DATA == true ]]; then
        action="Back up data and remove AgentMessage"
    else
        action="Remove AgentMessage and all its local data"
    fi
    printf '%s. Continue? [y/N]: ' "$action" >/dev/tty
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
    # Additional bots share this checkout. Stop them before deleting any code/data.
    local credential app_id bot_unit bot_file bot_dropin
    for credential in "$CONFIG_DIR/bots"/cli_*.env; do
        [[ -f $credential ]] || continue
        app_id=${credential##*/}
        app_id=${app_id%.env}
        [[ $app_id =~ ^cli_[A-Za-z0-9_-]{1,100}$ ]] || die "Invalid bot credential filename."
        bot_unit="agent-message-bot-$app_id.service"
        bot_file="${UNIT_FILE%/*}/$bot_unit"
        [[ -f $bot_file ]] || continue
        grep -q '^# Managed by AgentMessage install.sh$' "$bot_file" || die "Unmanaged bot unit: $bot_unit"
        systemctl --user stop "$bot_unit" || die "Cannot stop $bot_unit; no code was removed."
        systemctl --user disable "$bot_unit" >/dev/null 2>&1 || true
        bot_dropin="$bot_file.d/ssh-agent.conf"
        if [[ -f $bot_dropin ]] && grep -q '^# Managed by AgentMessage:' "$bot_dropin"; then
            rm -f -- "$bot_dropin"
            rmdir --ignore-fail-on-non-empty "$bot_file.d" 2>/dev/null || true
        fi
        rm -f -- "$bot_file"
    done
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

confirm_bot_removal() {
    [[ $ASSUME_YES == true ]] && return 0
    has_terminal || die "Final confirmation requires a terminal; use --yes for automation."
    local answer
    printf 'Remove bot %s (service, credentials, and local state)? [y/N]: ' "$APP_ID" >/dev/tty
    if ! IFS= read -r answer </dev/tty; then
        printf '\n' >/dev/tty
        exit 130
    fi
    case $answer in
        y|Y|yes|YES|Yes)
            ;;
        *)
            info "Bot removal cancelled; nothing was removed."
            exit 0
            ;;
    esac
}

resolve_bot_paths() {
    local project_config="$INSTALL_DIR/config/projects.toml"
    local python="$INSTALL_DIR/.venv/bin/python"
    [[ -x $python && -f $project_config ]] || die "$INSTALL_DIR is not a complete installation."
    local output status
    if output=$("$python" -c 'import sys
from agent_message.core.config import ConfigError, load_config
try:
    config = load_config(sys.argv[1], app_id=sys.argv[2])
except ConfigError:
    raise SystemExit(2)
except (OSError, ValueError):
    raise SystemExit(3)
print(config.service.state_dir)
print(config.service.log_dir)' "$project_config" "$APP_ID" 2>/dev/null); then
        status=0
    else
        status=$?
    fi
    case $status in
        0) ;;
        2) die "Bot $APP_ID is not registered in $project_config." ;;
        *) die "Cannot read the shared project configuration: $project_config" ;;
    esac
    local -a paths=()
    mapfile -t paths <<<"$output"
    [[ ${#paths[@]} == 2 && -n ${paths[0]} && -n ${paths[1]} ]] ||
        die "Cannot resolve the local paths for bot $APP_ID."
    BOT_STATE_DIR=${paths[0]}
    BOT_LOG_DIR=${paths[1]}
    local target
    for target in "$BOT_STATE_DIR" "$BOT_LOG_DIR"; do
        [[ $target == */bots/"$APP_ID" && $target != /bots/* ]] ||
            die "Refusing to remove unexpected bot path: $target"
    done
}

backup_bot() {
    local app_id=$1 credential=$2 state_dir=$3 log_dir=$4
    local backup_base="$DATA_HOME/agent-message/backups"
    mkdir -p "$backup_base"
    chmod 700 "$DATA_HOME/agent-message" "$backup_base"
    BACKUP_DIR=$(mktemp -d "$backup_base/bot-${app_id}-$(date +%Y%m%d-%H%M%S).XXXXXX")
    chmod 700 "$BACKUP_DIR"
    if [[ -f $credential ]]; then
        mkdir -p "$BACKUP_DIR/credentials"
        cp -a -- "$credential" "$BACKUP_DIR/credentials/"
    fi
    if [[ -d $state_dir ]]; then
        cp -a -- "$state_dir" "$BACKUP_DIR/state"
    fi
    if [[ -d $log_dir ]]; then
        cp -a -- "$log_dir" "$BACKUP_DIR/logs"
    fi
    {
        printf '%s\n' "AgentMessage bot removal backup: $app_id"
        printf '%s\n' 'credentials/: Feishu credentials for this bot (when present)'
        printf '%s\n' 'state/: SQLite task state for this bot (when present)'
        printf '%s\n' 'logs/: task logs for this bot (when present)'
    } >"$BACKUP_DIR/CONTENTS.txt"
    chmod -R go-rwx "$BACKUP_DIR"
    BOT_BACKUP_DIRS+=("$BACKUP_DIR")
    info "Bot data backup completed: $BACKUP_DIR"
}

confirm_selected_bots() {
    [[ $ASSUME_YES == true ]] && return 0
    has_terminal || die "Final confirmation requires a terminal; use --yes for automation."
    local answer
    printf 'Remove %d bot(s) (%s)? [y/N]: ' \
        "${#SELECTED_BOT_IDS[@]}" "$(format_bot_list "${SELECTED_BOT_IDS[@]}")" >/dev/tty
    if ! IFS= read -r answer </dev/tty; then
        printf '\n' >/dev/tty
        exit 130
    fi
    case $answer in
        y|Y|yes|YES|Yes)
            ;;
        *)
            info "Bot removal cancelled; nothing was removed."
            exit 0
            ;;
    esac
}

remove_bots() {
    # Validate every selected bot before touching anything so an invalid or
    # unmanaged bot stops the whole batch instead of half-removing a bot.
    local -a bots=("$@")
    ((${#bots[@]})) || die "No bots were selected."
    if [[ -z $PRIMARY_BOT_ID ]]; then
        PRIMARY_BOT_ID=$(resolve_primary_bot_id)
    fi
    [[ -n $PRIMARY_BOT_ID ]] || die "Primary credentials are missing in $ENV_FILE."
    local -A state_dirs=() log_dirs=() credentials=() units=()
    local app_id credential unit dropin
    for app_id in "${bots[@]}"; do
        [[ $app_id =~ ^cli_[A-Za-z0-9_-]{1,100}$ ]] || die "Invalid App ID: $app_id"
        if [[ -n $PRIMARY_BOT_ID && $app_id == "$PRIMARY_BOT_ID" ]]; then
            die "The primary bot cannot be removed on its own; remove the whole installation instead."
        fi
        APP_ID=$app_id
        resolve_bot_paths
        credential="$CONFIG_DIR/bots/$app_id.env"
        unit="${UNIT_FILE%/*}/agent-message-bot-$app_id.service"
        if [[ -f $unit ]] && ! grep -q '^# Managed by AgentMessage install.sh$' "$unit"; then
            die "Unmanaged unit file: $unit"
        fi
        state_dirs[$app_id]=$BOT_STATE_DIR
        log_dirs[$app_id]=$BOT_LOG_DIR
        credentials[$app_id]=$credential
        units[$app_id]=$unit
    done

    if [[ $DATA_CHOICE_SET == false ]]; then
        if [[ $INTERACTIVE_SELECTION == true ]]; then
            prompt_bot_backup_choice
        else
            KEEP_DATA=true
            DATA_CHOICE_SET=true
        fi
    fi
    if [[ $ASSUME_YES == false ]]; then
        if [[ $INTERACTIVE_SELECTION == true ]]; then
            confirm_selected_bots
        else
            APP_ID=${bots[0]}
            confirm_bot_removal
        fi
    fi

    local systemd_available=false
    if command -v systemctl >/dev/null 2>&1 &&
        systemctl --user show-environment >/dev/null 2>&1; then
        systemd_available=true
    fi
    if [[ $systemd_available == true ]]; then
        for app_id in "${bots[@]}"; do
            [[ -f ${units[$app_id]} ]] || continue
            systemctl --user stop "agent-message-bot-$app_id.service" ||
                die "Failed to stop agent-message-bot-$app_id.service; no bot files were removed."
        done
    else
        warn "The systemd user manager is unavailable; the selected bot services cannot be stopped."
    fi

    for app_id in "${bots[@]}"; do
        credential=${credentials[$app_id]}
        if [[ $KEEP_DATA == true ]] &&
            [[ -f $credential || -d ${state_dirs[$app_id]} || -d ${log_dirs[$app_id]} ]]; then
            backup_bot "$app_id" "$credential" "${state_dirs[$app_id]}" "${log_dirs[$app_id]}"
        fi
    done

    for app_id in "${bots[@]}"; do
        if [[ $systemd_available == true ]]; then
            systemctl --user disable "agent-message-bot-$app_id.service" >/dev/null 2>&1 || true
        fi
        unit=${units[$app_id]}
        if [[ -f $unit ]]; then
            dropin="$unit.d/ssh-agent.conf"
            if [[ -f $dropin ]] && grep -q '^# Managed by AgentMessage:' "$dropin"; then
                rm -f -- "$dropin"
                rmdir --ignore-fail-on-non-empty "$unit.d" 2>/dev/null || true
            fi
            rm -f -- "$unit"
        fi
        rm -f -- "${credentials[$app_id]}"
        rm -rf -- "${state_dirs[$app_id]}" "${log_dirs[$app_id]}"
        rmdir --ignore-fail-on-non-empty "${state_dirs[$app_id]%/*}" 2>/dev/null || true
        rmdir --ignore-fail-on-non-empty "${log_dirs[$app_id]%/*}" 2>/dev/null || true
    done

    for app_id in "${bots[@]}"; do
        "$INSTALL_DIR/.venv/bin/python" -m agent_message.core.bot_registry \
            --config "$INSTALL_DIR/config/projects.toml" --primary-app-id "$PRIMARY_BOT_ID" \
            --app-id "$app_id" --remove ||
            die "Failed to update the shared configuration after removing $app_id; rerun this command to finish."
    done

    if [[ $systemd_available == true ]]; then
        systemctl --user daemon-reload
        for app_id in "${bots[@]}"; do
            systemctl --user reset-failed "agent-message-bot-$app_id.service" >/dev/null 2>&1 || true
        done
        # The shared project returns to the primary bot, so reload its project list.
        systemctl --user restart agent-message.service ||
            warn "Restart agent-message.service to apply the updated project list."
    else
        warn "Restart agent-message.service later to apply the updated project list."
    fi

    info "Removed $(format_bot_list "${bots[@]}"); the shared checkout and other bots were kept."
    if [[ $KEEP_DATA == true && ${#BOT_BACKUP_DIRS[@]} -gt 0 ]]; then
        info "Retained data: $(format_bot_list "${BOT_BACKUP_DIRS[@]}")"
    elif [[ $KEEP_DATA == false ]]; then
        info "Bot credentials and local state were deleted."
    fi
}

print_summary() {
    info "AgentMessage service, checkout, and local configuration were removed."
    if [[ $KEEP_DATA == true ]]; then
        info "Retained data: $BACKUP_DIR"
    else
        info "Project configuration, task state, logs, and Feishu credentials were deleted."
    fi
    info "systemd, uv, Codex, Docker, and Bubblewrap were left installed."
}

main() {
    while (($#)); do
        case $1 in
            --install-dir)
                (($# >= 2)) || die "--install-dir requires a path."
                INSTALL_DIR=$2
                shift 2
                ;;
            --app-id)
                (($# >= 2)) || die "--app-id requires an App ID."
                [[ -n $2 ]] || die "--app-id requires a non-empty App ID."
                APP_ID=$2
                APP_ID_SET=true
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
    if [[ $APP_ID_SET == true ]]; then
        remove_bots "$APP_ID"
        return 0
    fi
    if [[ $ASSUME_YES == false ]]; then
        has_terminal ||
            die "Interactive bot selection requires a terminal; use --app-id ID to remove one bot, or --yes to remove the whole installation."
        INTERACTIVE_SELECTION=true
        prompt_bot_selection
    fi
    if [[ $REMOVE_ALL == false && ${#SELECTED_BOT_IDS[@]} -gt 0 ]]; then
        remove_bots "${SELECTED_BOT_IDS[@]}"
        return 0
    fi
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
