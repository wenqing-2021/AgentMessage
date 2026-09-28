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
        info "Skipping git pull (--no-pull)."
        return 0
    fi
    if [[ ! -d $INSTALL_DIR/.git ]]; then
        info "This directory is not a Git checkout; skipping git pull."
        return 0
    fi
    if [[ -n $(git -C "$INSTALL_DIR" status --porcelain) ]]; then
        warn "Uncommitted changes found; skipping git pull. Commit or stash them before pulling updates."
        return 0
    fi
    info "Pulling the latest code ..."
    git -C "$INSTALL_DIR" pull --ff-only
}

sync_dependencies() {
    command -v uv >/dev/null 2>&1 || die "uv was not found; cannot sync dependencies."
    info "Syncing Python dependencies (uv sync --frozen) ..."
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
    info "Managed service files need updating; reinstalling them ..."
    bash "$INSTALL_DIR/install.sh" --install-dir "$INSTALL_DIR" --refresh-service
}

restart_unit() {
    local unit=$1
    info "Restarting $unit ..."
    if ! systemctl --user restart "$unit"; then
        warn "Failed to restart $unit. Run this in a terminal with an active systemd user session: systemctl --user restart $unit"
        FAILED=true
    fi
}

print_key_hint() {
    cat <<EOF

[AgentMessage] Note: restarting the dedicated SSH agent clears manually unlocked keys.
  Unlock passphrase-protected private keys again with:
      SSH_AUTH_SOCK="$SSH_AGENT_SOCKET" ssh-add ~/.ssh/your_encrypted_key
EOF
}

print_service_status() {
    local -a states=()
    mapfile -t states < <(systemctl --user is-active "$SSH_AGENT_SERVICE" "$BRIDGE_SERVICE" 2>/dev/null || true)
    info "Service status:"
    printf '  %s: %s\n' "$SSH_AGENT_SERVICE" "${states[0]:-unknown}"
    printf '  %s: %s\n' "$BRIDGE_SERVICE" "${states[1]:-unknown}"

    if ! command -v ssh-add >/dev/null 2>&1; then
        printf '  Loaded keys: unknown (ssh-add is missing)\n'
        return 0
    fi
    local output status
    output=$(SSH_AUTH_SOCK="$SSH_AGENT_SOCKET" ssh-add -l 2>/dev/null) && status=0 || status=$?
    if [[ $status == 0 ]]; then
        local loaded
        loaded=$(printf '%s\n' "$output" | sed '/^$/d' | wc -l | tr -d ' ')
        printf '  Loaded keys: %s\n' "$loaded"
    elif [[ $status == 1 ]]; then
        printf '  Loaded keys: 0\n'
    else
        printf '  Loaded keys: unknown (agent is not ready)\n'
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
        die "$INSTALL_DIR is not an AgentMessage checkout; cannot update."

    pull_latest
    sync_dependencies
    refresh_units

    restart_unit "$SSH_AGENT_SERVICE"
    restart_unit "$BRIDGE_SERVICE"
    local credential app_id unit
    for credential in "$CONFIG_HOME/agent-message/bots"/cli_*.env; do
        [[ -f $credential ]] || continue
        app_id=${credential##*/}
        app_id=${app_id%.env}
        [[ $app_id =~ ^cli_[A-Za-z0-9_-]{1,100}$ ]] || die "Invalid bot credential filename."
        unit="agent-message-bot-$app_id.service"
        bash "$INSTALL_DIR/install.sh" --install-dir "$INSTALL_DIR" --app-id "$app_id" --refresh-service
        info "$unit: $(systemctl --user is-active "$unit" || true)"
    done

    print_service_status
    print_key_hint

    if [[ $FAILED == true ]]; then
        die "Some services failed to restart. Follow the instructions above to resolve the issue."
    fi
    info "Update complete."
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
