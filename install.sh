#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

REPO_URL="https://github.com/wenqing-2021/AgentMessage.git"
INSTALL_DIR="${AGENT_MESSAGE_INSTALL_DIR:-$HOME/workspace/AgentMessage}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_DIR="$CONFIG_HOME/agent-message"
ENV_FILE="$CONFIG_DIR/feishu.env"
SYSTEMD_USER_DIR="$CONFIG_HOME/systemd/user"
UNIT_FILE="$SYSTEMD_USER_DIR/agent-message.service"
SSH_AGENT_UNIT_FILE="$SYSTEMD_USER_DIR/agent-message-ssh-agent.service"
SSH_AGENT_DROPIN_DIR="$SYSTEMD_USER_DIR/agent-message.service.d"
SSH_AGENT_DROPIN_FILE="$SSH_AGENT_DROPIN_DIR/ssh-agent.conf"
SSH_AGENT_LOADER_DIR="$HOME/.local/libexec/agent-message"
SSH_AGENT_LOADER_FILE="$SSH_AGENT_LOADER_DIR/load_ssh_keys.py"
PROJECT_CONFIG="$INSTALL_DIR/config/projects.toml"
STATE_FILE="$INSTALL_DIR/.git/agent-message-install-stage"
REFRESH_SERVICE=false

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
Usage: bash install.sh [--install-dir PATH] [--refresh-service]

Clone and install AgentMessage, collect Feishu credentials, and enable its
systemd user services. The default install directory is ~/workspace/AgentMessage.
The bridge unit plus the dedicated SSH agent service and its key loader are
installed and enabled together. Re-running the same command resumes from the
last completed stage.
Use --refresh-service to reinstall the systemd units from this script and the
deploy/ templates without repeating the other stages.
EOF
}

set_install_dir() {
    INSTALL_DIR=$1
    PROJECT_CONFIG="$INSTALL_DIR/config/projects.toml"
    STATE_FILE="$INSTALL_DIR/.git/agent-message-install-stage"
}

run_as_root() {
    if [[ $EUID -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        die "Installing system packages requires root or sudo."
    fi
}

install_system_requirements() {
    local -a packages=()
    command -v git >/dev/null 2>&1 || packages+=(git)
    command -v curl >/dev/null 2>&1 || packages+=(curl)
    command -v systemctl >/dev/null 2>&1 || packages+=(systemd)
    ((${#packages[@]})) || return 0

    info "Installing missing system packages: ${packages[*]}"
    if command -v apt-get >/dev/null 2>&1; then
        run_as_root apt-get update
        run_as_root apt-get install -y ca-certificates "${packages[@]}"
    elif command -v dnf >/dev/null 2>&1; then
        run_as_root dnf install -y ca-certificates "${packages[@]}"
    elif command -v pacman >/dev/null 2>&1; then
        run_as_root pacman -S --needed --noconfirm ca-certificates "${packages[@]}"
    else
        die "No supported package manager found. Install git, curl, and systemd, then rerun."
    fi

    command -v git >/dev/null 2>&1 || die "git installation did not provide git."
    command -v curl >/dev/null 2>&1 || die "curl installation did not provide curl."
    command -v systemctl >/dev/null 2>&1 || die "systemd installation did not provide systemctl."
}

install_uv() {
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi

    local installer
    installer=$(mktemp "${TMPDIR:-/tmp}/agent-message-uv.XXXXXX")
    info "Installing uv from the official HTTPS installer."
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$installer"; then
        rm -f "$installer"
        die "Failed to download the uv installer."
    fi
    UV_NO_MODIFY_PATH=1 sh "$installer"
    rm -f "$installer"
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv was installed but is not available on PATH."
}

checkout_project() {
    local parent
    parent=$(dirname "$INSTALL_DIR")
    mkdir -p "$parent"

    if [[ ! -e $INSTALL_DIR ]]; then
        info "Cloning AgentMessage over HTTPS into $INSTALL_DIR"
        git clone "$REPO_URL" "$INSTALL_DIR"
        return 0
    fi
    [[ -d $INSTALL_DIR/.git ]] || die "$INSTALL_DIR exists but is not a Git checkout."

    if [[ -n $(git -C "$INSTALL_DIR" status --porcelain) ]]; then
        warn "The existing checkout has local changes; keeping them and skipping git pull."
    else
        info "Updating the existing checkout with a fast-forward-only pull."
        git -C "$INSTALL_DIR" pull --ff-only
    fi
}

toml_escape() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    printf '%s' "$value"
}

create_project_config() {
    [[ -f $PROJECT_CONFIG ]] && return 0

    local default_agent allowed_agents escaped_path temporary
    if command -v codex >/dev/null 2>&1; then
        default_agent=codex
    elif command -v qodercli >/dev/null 2>&1; then
        default_agent=qoder
    else
        default_agent=codex
        warn "Neither codex nor qodercli is installed; install and log in to one before sending tasks."
    fi
    if command -v codex >/dev/null 2>&1 && command -v qodercli >/dev/null 2>&1; then
        allowed_agents='["codex", "qoder"]'
    elif [[ $default_agent == qoder ]]; then
        allowed_agents='["qoder"]'
    else
        allowed_agents='["codex"]'
    fi

    escaped_path=$(toml_escape "$INSTALL_DIR")
    temporary=$(mktemp "$INSTALL_DIR/config/projects.toml.XXXXXX")
    umask 077
    {
        printf '%s\n' '[service]'
        printf '%s\n' 'state_dir = "var"'
        printf '%s\n' 'log_dir = "var/logs"'
        printf '%s\n' 'default_chat_project = "agent_message"'
        printf '%s\n' 'codex_tool_network = false'
        printf '%s\n' 'stop_grace_seconds = 10'
        printf '%s\n' 'final_message_limit = 3500'
        printf '\n%s\n' '[projects.agent_message]'
        printf 'path = "%s"\n' "$escaped_path"
        printf 'default_agent = "%s"\n' "$default_agent"
        printf 'allowed_agents = %s\n' "$allowed_agents"
    } >"$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$PROJECT_CONFIG"
    info "Created $PROJECT_CONFIG"
}

bootstrap_project() {
    install_system_requirements
    install_uv
    checkout_project
    info "Installing locked Python dependencies."
    (cd "$INSTALL_DIR" && uv sync --frozen)
    create_project_config
}

read_stage() {
    if [[ -f $STATE_FILE ]]; then
        head -n 1 "$STATE_FILE"
    else
        printf '%s' bootstrap
    fi
}

write_stage() {
    local temporary
    [[ -d $INSTALL_DIR/.git ]] || die "Cannot save installer state before cloning the repository."
    temporary=$(mktemp "$INSTALL_DIR/.git/agent-message-install-stage.XXXXXX")
    umask 077
    printf '%s\n' "$1" >"$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$STATE_FILE"
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

validate_credential() {
    local label=$1 value=$2
    [[ -n $value ]] || die "$label cannot be empty."
    [[ $value =~ ^[A-Za-z0-9._-]+$ ]] || die "$label contains unsupported characters."
}

write_credentials_file() {
    local app_id=$1 app_secret=$2 allowed_open_ids=$3 temporary
    mkdir -p "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"
    temporary=$(mktemp "$CONFIG_DIR/feishu.env.XXXXXX")
    umask 077
    {
        printf 'AGENT_MESSAGE_FEISHU_APP_ID=%s\n' "$app_id"
        printf 'AGENT_MESSAGE_FEISHU_APP_SECRET=%s\n' "$app_secret"
        printf 'AGENT_MESSAGE_ALLOWED_OPEN_IDS=%s\n' "$allowed_open_ids"
    } >"$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$ENV_FILE"
}

prompt_plain() {
    local label=$1
    printf '%s: ' "$label" >/dev/tty
    if ! IFS= read -r PROMPT_VALUE </dev/tty; then
        printf '\n' >/dev/tty
        exit 130
    fi
}

prompt_secret() {
    local label=$1
    printf '%s: ' "$label" >/dev/tty
    if ! IFS= read -r -s PROMPT_VALUE </dev/tty; then
        printf '\n' >/dev/tty
        exit 130
    fi
    printf '\n' >/dev/tty
}

collect_credentials() {
    [[ -r /dev/tty ]] || die "Credential input requires an interactive terminal."
    local app_id app_secret allowed_open_ids
    app_id=$(read_env_value AGENT_MESSAGE_FEISHU_APP_ID)
    app_secret=$(read_env_value AGENT_MESSAGE_FEISHU_APP_SECRET)
    allowed_open_ids=$(read_env_value AGENT_MESSAGE_ALLOWED_OPEN_IDS)

    if [[ -z $app_id ]]; then
        prompt_plain "AGENT_MESSAGE_FEISHU_APP_ID"
        app_id=$PROMPT_VALUE
        validate_credential AGENT_MESSAGE_FEISHU_APP_ID "$app_id"
        write_credentials_file "$app_id" "$app_secret" "$allowed_open_ids"
    fi
    if [[ -z $app_secret ]]; then
        prompt_secret "AGENT_MESSAGE_FEISHU_APP_SECRET (input hidden)"
        app_secret=$PROMPT_VALUE
        validate_credential AGENT_MESSAGE_FEISHU_APP_SECRET "$app_secret"
        write_credentials_file "$app_id" "$app_secret" "$allowed_open_ids"
    fi
    chmod 600 "$ENV_FILE"
    info "Feishu credentials saved to $ENV_FILE with mode 600."
}

systemd_quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//%/%%}
    printf '"%s"' "$value"
}

systemd_path_value() {
    local value=$1
    value=${value//\\/\\x5c}
    value=${value// /\\x20}
    value=${value//$'\t'/\\x09}
    value=${value//\"/\\x22}
    value=${value//%/%%}
    printf '%s' "$value"
}

service_path() {
    local value="$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    local executable directory
    for executable in codex qodercli uv; do
        if command -v "$executable" >/dev/null 2>&1; then
            directory=$(dirname "$(command -v "$executable")")
            if [[ :$value: != *":$directory:"* ]]; then
                value="$directory:$value"
            fi
        fi
    done
    printf '%s' "$value"
}

require_running_systemd_user() {
    if systemctl --user show-environment >/dev/null 2>&1; then
        return 0
    fi
    if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
        cat >&2 <<'EOF'
[AgentMessage] systemd is installed but is not active in this WSL instance.
Add the following to /etc/wsl.conf:

[boot]
systemd=true

Then run `wsl --shutdown` in Windows PowerShell, reopen WSL, and rerun this installer.
It will resume directly from the systemd-service stage.
EOF
    else
        warn "systemd is installed but the user service manager is unavailable. Start a systemd session and rerun."
    fi
    return 1
}

# deploy/agent-message.service is the single source of truth for the bridge unit.
# The @AGENT_MESSAGE_*@ tokens are replaced here because the real install path,
# the credentials file, the executable and PATH are only known while installing.
write_user_service_file() {
    local template="$INSTALL_DIR/deploy/agent-message.service"
    local temporary escaped_install escaped_env quoted_executable quoted_config quoted_path path_value line
    [[ -f $template ]] || die "Missing systemd template: $template"
    escaped_install=$(systemd_path_value "$INSTALL_DIR")
    escaped_env=$(systemd_path_value "$ENV_FILE")
    quoted_executable=$(systemd_quote "$INSTALL_DIR/.venv/bin/agent-message")
    quoted_config=$(systemd_quote "$PROJECT_CONFIG")
    path_value=$(service_path)
    quoted_path=$(systemd_quote "PATH=$path_value")

    mkdir -p "$SYSTEMD_USER_DIR"
    chmod 700 "$SYSTEMD_USER_DIR"
    temporary=$(mktemp "$SYSTEMD_USER_DIR/agent-message.service.XXXXXX")
    umask 077
    {
        printf '%s\n' '# Managed by AgentMessage install.sh'
        while IFS= read -r line || [[ -n $line ]]; do
            line=${line//@AGENT_MESSAGE_WORKING_DIRECTORY@/$escaped_install}
            line=${line//@AGENT_MESSAGE_ENV_FILE@/$escaped_env}
            line=${line//@AGENT_MESSAGE_PATH@/$quoted_path}
            line=${line//@AGENT_MESSAGE_EXEC_START@/$quoted_executable run --config $quoted_config}
            printf '%s\n' "$line"
        done <"$template"
    } >"$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$UNIT_FILE"
}

install_ssh_agent_service() {
    local unit_source="$INSTALL_DIR/deploy/agent-message-ssh-agent.service"
    local dropin_source="$INSTALL_DIR/deploy/agent-message-ssh-agent.conf"
    local loader_source="$INSTALL_DIR/scripts/load_ssh_keys.py"
    local required

    for required in "$unit_source" "$dropin_source" "$loader_source"; do
        [[ -f $required ]] || die "Missing SSH agent installation file: $required"
    done

    mkdir -p "$SSH_AGENT_DROPIN_DIR" "$SSH_AGENT_LOADER_DIR"
    chmod 700 "$SSH_AGENT_DROPIN_DIR" "$SSH_AGENT_LOADER_DIR"
    # 0600 keeps the loader and units private to the service user; the marker on
    # the first lines lets uninstall.sh remove only files this installer owns.
    install -m 600 "$loader_source" "$SSH_AGENT_LOADER_FILE"
    install -m 600 "$unit_source" "$SSH_AGENT_UNIT_FILE"
    install -m 600 "$dropin_source" "$SSH_AGENT_DROPIN_FILE"
    info "Installed the dedicated SSH agent service, its drop-in and the key loader."
}

install_user_service() {
    require_running_systemd_user
    write_user_service_file
    install_ssh_agent_service
    systemctl --user daemon-reload
    if ! systemctl --user enable --now agent-message-ssh-agent.service; then
        warn "The dedicated SSH agent service did not start; sandbox SSH push/pull needs a running agent"
        warn "Check that ssh-agent is installed and ~/.ssh is readable, then rerun with --refresh-service."
    fi
    systemctl --user enable agent-message.service
    systemctl --user restart agent-message.service
    info "systemd user services enabled and restarted."
}

credentials_are_complete() {
    [[ -n $(read_env_value AGENT_MESSAGE_FEISHU_APP_ID) ]] &&
        [[ -n $(read_env_value AGENT_MESSAGE_FEISHU_APP_SECRET) ]]
}

print_summary() {
    cat <<EOF

AgentMessage installation is complete.
  Repository:  $INSTALL_DIR
  Projects:    $PROJECT_CONFIG
  Credentials: $ENV_FILE
  SSH agent:   $SSH_AGENT_UNIT_FILE

Next:
  1. Log in to Codex with "codex login" or Qoder with "qodercli login".
  2. Edit the project allowlist: vim "$PROJECT_CONFIG"
  3. Send /help to the Feishu bot, then authorize yourself with:
       cd "$INSTALL_DIR" && uv run agent-message pending-senders
       cd "$INSTALL_DIR" && uv run agent-message authorize ou_xxx
  4. Update later with: cd "$INSTALL_DIR" && bash update.sh
EOF
}

main() {
    while (($#)); do
        case $1 in
            --install-dir)
                (($# >= 2)) || die "--install-dir requires a path."
                set_install_dir "$2"
                shift 2
                ;;
            --refresh-service)
                REFRESH_SERVICE=true
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

    [[ $INSTALL_DIR == /* ]] || die "The install directory must be an absolute path."
    [[ $INSTALL_DIR != *$'\n'* && $INSTALL_DIR != *$'\r'* ]] ||
        die "The install directory cannot contain line breaks."
    if [[ $REFRESH_SERVICE == true ]]; then
        [[ -d $INSTALL_DIR/.git ]] || die "$INSTALL_DIR is not an installed AgentMessage checkout."
        install_system_requirements
        credentials_are_complete || die "Feishu credentials are incomplete in $ENV_FILE"
        install_user_service
        write_stage complete
        print_summary
        return 0
    fi

    while true; do
        case $(read_stage) in
            bootstrap)
                bootstrap_project
                write_stage credentials
                ;;
            credentials)
                collect_credentials
                write_stage service
                ;;
            service)
                install_user_service
                write_stage complete
                ;;
            complete)
                if [[ ! -x $INSTALL_DIR/.venv/bin/agent-message || ! -f $PROJECT_CONFIG ]]; then
                    write_stage bootstrap
                    continue
                fi
                if ! credentials_are_complete; then
                    write_stage credentials
                    continue
                fi
                if [[ ! -f $UNIT_FILE ]]; then
                    write_stage service
                    continue
                fi
                if [[ ! -f $SSH_AGENT_UNIT_FILE || ! -f $SSH_AGENT_DROPIN_FILE ||
                    ! -f $SSH_AGENT_LOADER_FILE ]]; then
                    # Installations created before the dedicated agent existed
                    # get it on the next run instead of silently staying behind.
                    write_stage service
                    continue
                fi
                print_summary
                return 0
                ;;
            *)
                die "Invalid installer checkpoint in $STATE_FILE"
                ;;
        esac
    done
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
