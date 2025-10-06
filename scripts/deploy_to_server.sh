#!/usr/bin/env bash
# Be safe.
set -eo pipefail

# Get script directory and source TUI utilities
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
source "${script_dir}/tui_utils.sh"

SERVER_HOSTNAME="$1"
PROTOCOL="${2:-trojan}"
VERBOSE=false

usage() {
    echo
    echo "${BOLD}$(_t 'Usage'):${RESET}"
    echo "${INDENT}$0 <ssh-hostname> [protocol]"
    echo
    echo "${BOLD}$(_t 'Options'):${RESET}"
    echo "${INDENT}ssh-hostname                SSH hostname to deploy to"
    echo "${INDENT}protocol                    $(_t 'Specify which protocol to install'):"
    echo "${INDENT}                            hysteria2 trojan (default: hysteria2)"
    echo
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
    error "$(_t 'Usage'): $0 <ssh-hostname> [hysteria2|trojan]"
    usage
    exit 1
fi

case "$PROTOCOL" in
    hysteria2|trojan) ;;
    *)
        error "$(_t 'Unsupported protocol'): ${PROTOCOL}. $(_t 'Supported protocols'): hysteria2 trojan"
        exit 1
        ;;
esac

# Print banner
echo
echo "${BOLD}${BLUE}Sing-box VPN Server Deployment${RESET}"
echo "${DIM}${GREY}Version 1.0${RESET}"
echo

# Show what we're about to do
echo "${BOLD}$(_t 'This script will deploy the VPN server service'):${RESET}"
echo "${INDENT}${BULLET} Server: ${GREEN}${SERVER_HOSTNAME}${RESET}"
echo "${INDENT}${BULLET} Protocol: ${GREEN}${PROTOCOL}${RESET}"
echo

# Load environment and prepare release
env_file="${script_dir}/../.env"
set -o allexport && source "${env_file}" && set +o allexport
RELEASE_DIR="sing-box-v${SING_BOX_VERSION}-${CONFIG_GIT_HASH}"
cd "${script_dir}/../releases/${RELEASE_DIR}"
RELEASE_TAR="sing-box-v${SING_BOX_VERSION}-${CONFIG_GIT_HASH}-server.tar.gz"

if [[ ! -f ${RELEASE_TAR} ]]; then
    error "${RELEASE_TAR} $(_t 'not found')."
    exit 1
fi

# Upload to server
progress "Uploading to server" "${SERVER_HOSTNAME}"
if ! scp "${RELEASE_TAR}" "${SERVER_HOSTNAME}:~/" >/dev/null 2>&1; then
    error "$(_t 'SCP failed')"
    exit 1
fi
success "Upload complete"

# Extract on server
progress "Extracting on server"
if ! ssh "${SERVER_HOSTNAME}" -t "cd ~ && tar -xf ${RELEASE_TAR} && rm ${RELEASE_TAR}" 2>/dev/null; then
    error "$(_t 'Extraction failed')"
    exit 1
fi
success "Extraction complete"

# Install on server
progress "Installing on server"
if ! ssh "${SERVER_HOSTNAME}" -t "cd ${RELEASE_DIR}-server/ && ./install.sh -p ${PROTOCOL} --no-rc" 2>/dev/null; then
    error "$(_t 'Install with systemd failed')"
    exit 1
fi
success "Installation complete"

# Restart service
progress "Restarting service" "sing-box-${PROTOCOL}.service"
if ! ssh "${SERVER_HOSTNAME}" -t "sudo systemctl restart sing-box-${PROTOCOL}.service" 2>/dev/null; then
    error "$(_t 'Service restart failed')"
    exit 1
fi
success "Service restarted"

# Final success message
echo
success "${BOLD}$(_t 'Deployment complete')!${RESET}"
success "$(_t 'Service is now running'): ${GREEN}sing-box-${PROTOCOL}.service${RESET} on ${GREEN}${SERVER_HOSTNAME}${RESET}"
echo
