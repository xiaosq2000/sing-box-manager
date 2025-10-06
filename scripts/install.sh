#!/usr/bin/env bash
# Be safe.
set -eo pipefail

# Get script directory and source TUI utilities
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
source "${script_dir}/tui_utils.sh"

# Detect user's shell
detect_shell() {
    local shell_name=""
    local rc_file=""

    # First try to get the shell from SHELL environment variable
    if [[ -n "${SHELL}" ]]; then
        shell_name=$(basename "${SHELL}")
    fi

    # If that fails, try to get it from passwd
    if [[ -z "$shell_name" ]] && command -v getent >/dev/null 2>&1; then
        shell_name=$(getent passwd "$USER" | cut -d: -f7 | xargs basename)
    fi

    # Determine RC file based on shell
    case "$shell_name" in
        bash)
            rc_file="${HOME}/.bashrc"
            ;;
        zsh)
            rc_file="${HOME}/.zshrc"
            ;;
        *)
            # Fallback: check which files exist
            if [[ -f "${HOME}/.zshrc" ]]; then
                shell_name="zsh"
                rc_file="${HOME}/.zshrc"
            elif [[ -f "${HOME}/.bashrc" ]]; then
                shell_name="bash"
                rc_file="${HOME}/.bashrc"
            fi
            ;;
    esac

    echo "$shell_name:$rc_file"
}

# Configure shell RC file
configure_shell_rc() {
    local rc_file="$1"
    local shell_name="$2"
    local script_path="${script_dir}/setup.sh"

    if [[ ! -f "$script_path" ]]; then
        error "$(_t 'Shell RC configuration disabled') - setup.sh $(_t 'not found')"
        return 1
    fi

    # Create RC file if it doesn't exist
    if [[ ! -f "$rc_file" ]]; then
        progress "Creating shell RC file" "$rc_file"
        touch "$rc_file"
    fi

    # Create backup
    local backup
    backup="${rc_file}.backup.$(date +%Y%m%d_%H%M%S)"
    progress "Backing up RC file" "$backup"
    cp "$rc_file" "$backup"

    # Check if already configured
    local source_line="[ -f \"${script_path}\" ] && source \"${script_path}\""
    if grep -qF "source \"${script_path}\"" "$rc_file" 2>/dev/null; then
        info "$(_t 'RC file already configured')"
        return 0
    fi

    # Add configuration
    progress "Adding proxy functions to RC file"

    # Add newline if file doesn't end with one
    [[ -s "$rc_file" && -z "$(tail -c1 "$rc_file")" ]] || echo "" >> "$rc_file"

    {
        echo ""
        echo "# Network proxy management configuration"
        echo "# Added by sing-box installer on $(date)"
        echo "$source_line"
        echo ""
    } >> "$rc_file"

    success "$(_t 'Shell RC configuration complete')"
    return 0
}

# Check if running with sudo
check_sudo() {
    if [[ $EUID -eq 0 ]]; then
        error "$(_t 'This script should not be run as root')"
        error "$(_t 'Please run without sudo'): ./install.sh"
        exit 1
    fi
}

# Run command with sudo, prompting for password if needed
run_with_sudo() {
    # Check if we can run sudo without password
    if sudo -n true 2>/dev/null; then
        sudo "$@"
    else
        echo
        echo "${BOLD}${YELLOW}${LOCK} $(_t 'Administrator privileges required')${RESET}"
        echo "$(_t 'Please enter your password'):"
        if ! sudo "$@"; then
            error "$(_t 'Installation cancelled')"
            exit 1
        fi
    fi
}

DEFAULT_PROTOCOL="trojan"
SUPPORTED_PROTOCOL="trojan hysteria2"
CONFIGURE_RC=true  # Default to configuring RC files
VERBOSE=false

usage() {
    echo
    echo "${BOLD}$(_t 'Usage'):${RESET}"
    echo "${INDENT}$0 [options]"
    echo
    echo "${BOLD}$(_t 'Options'):${RESET}"
    echo "${INDENT}-h, --help                  $(_t 'Display help messages')"
    echo "${INDENT}-V, --verbose               $(_t 'Debug logging')"
    echo "${INDENT}-p, --protocol PROTOCOL     $(_t 'Specify which protocol to install'):"
    echo "${INDENT}                            ${SUPPORTED_PROTOCOL}"
    echo "${INDENT}--no-rc                     $(_t 'Skip shell RC configuration')"
    echo
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        -p | --protocol)
            PROTOCOL="$2"
            shift 2
            ;;
        -V | --verbose)
            VERBOSE=true
            shift 1
            ;;
        --no-rc)
            CONFIGURE_RC=false
            shift 1
            ;;
        *)
            error "$(_t 'Unknown argument'): $1"
            usage
            exit 1
            ;;
    esac
done

# Print banner
echo
echo "${BOLD}${BLUE}Sing-box VPN Client Installer${RESET}"
echo "${DIM}${GREY}Version 1.0${RESET}"
echo

# Check prerequisites
progress "Checking prerequisites"

# Check if systemd is available
if ! command -v systemctl >/dev/null 2>&1; then
    error "$(_t 'systemd is required but not found')"
    error "$(_t 'This system does not use systemd')"
    exit 1
fi

# Set default protocol if not specified
if [[ -z $PROTOCOL ]]; then
    PROTOCOL=$DEFAULT_PROTOCOL
fi

# Validate protocol
good=$(
    IFS=" "
    for p in $SUPPORTED_PROTOCOL; do
        if [[ "${p}" = "${PROTOCOL}" ]]; then
            printf 1
            break
        fi
    done
)

if [[ "${good}" != "1" ]]; then
    error "$PROTOCOL $(_t 'is not supported yet'). $(_t 'Supported protocols'): ${SUPPORTED_PROTOCOL}"
    exit 1
fi

# Change to script directory
cd "${script_dir}"

# Check that we're NOT running as root
check_sudo

# Show what we're about to do
echo "${BOLD}$(_t 'This script will install the VPN client service'):${RESET}"
echo "${INDENT}${BULLET} Protocol: ${GREEN}${PROTOCOL}${RESET}"
echo "${INDENT}${BULLET} Service: ${GREEN}sing-box-${PROTOCOL}.service${RESET}"
if [[ "$CONFIGURE_RC" == "true" ]]; then
    shell_info=$(detect_shell)
    shell_name="${shell_info%%:*}"
    rc_file="${shell_info#*:}"
    if [[ -n "$shell_name" ]] && [[ -n "$rc_file" ]]; then
        echo "${INDENT}${BULLET} Shell: ${GREEN}${shell_name}${RESET} (${rc_file})"
    fi
fi
echo

# Define paths
XDG_PREFIX_DIR="/usr/local"
SERVICE_DIR="/etc/systemd/system"
SERVICE_NAME="sing-box-${PROTOCOL}.service"
SERVICE_FILE="${SERVICE_DIR}/${SERVICE_NAME}"

# Stop and remove existing services
progress "Stopping existing services"
set +e # Temporarily disable exit on error for service removal operations
for service_path in "${SERVICE_DIR}/sing-box-${PROTOCOL}"*.service; do
    # Check if the glob found actual files (and not just the literal pattern if no matches)
    if [[ -e "${service_path}" ]]; then
        service_name=$(basename "${service_path}")
        debug "Stopping and disabling ${service_name}"
        # Use '|| true' to prevent script from exiting if systemctl commands fail (e.g., service not active/enabled)
        run_with_sudo systemctl stop "${service_name}" >/dev/null 2>&1 || true
        run_with_sudo systemctl disable "${service_name}" >/dev/null 2>&1 || true
        run_with_sudo rm "${service_path}" >/dev/null 2>&1 || true
    fi
done
set -e # Re-enable exit on error

# Install service file
progress "Installing service files"
if [[ -f "${script_dir}/${SERVICE_NAME}" ]]; then
    run_with_sudo cp "${script_dir}/${SERVICE_NAME}" "${SERVICE_FILE}"
    debug "${script_dir}/${SERVICE_NAME} ${ARROW} ${SERVICE_FILE}"
else
    error "$(_t 'Service file not found'): ${script_dir}/${SERVICE_NAME}"
    error "$(_t 'Contact your admin')"
    exit 1
fi

# Create directories
progress "Creating directories"
run_with_sudo mkdir -p "${XDG_PREFIX_DIR}/bin"
run_with_sudo mkdir -p "${XDG_PREFIX_DIR}/etc/sing-box/${PROTOCOL}"
run_with_sudo mkdir -p "/var/lib/sing-box/"

# Install binary
progress "Copying files" "sing-box binary"
if [[ -f "${script_dir}/sing-box" ]]; then
    run_with_sudo cp "${script_dir}/sing-box" "${XDG_PREFIX_DIR}/bin/sing-box"
    run_with_sudo chmod +x "${XDG_PREFIX_DIR}/bin/sing-box"
    debug "${script_dir}/sing-box ${ARROW} ${XDG_PREFIX_DIR}/bin/sing-box"
else
    error "$(_t 'Binary file not found'): ${script_dir}/sing-box"
    exit 1
fi

# Install configuration
progress "Copying files" "configuration"
if [[ -f "${script_dir}/${PROTOCOL}-client.json" ]]; then
    run_with_sudo cp "${script_dir}/${PROTOCOL}-client.json" "${XDG_PREFIX_DIR}/etc/sing-box/${PROTOCOL}/config.json"
    debug "${script_dir}/${PROTOCOL}-client.json ${ARROW} ${XDG_PREFIX_DIR}/etc/sing-box/${PROTOCOL}/config.json"
elif [[ -f "${script_dir}/${PROTOCOL}-server.json" ]]; then
    run_with_sudo cp "${script_dir}/${PROTOCOL}-server.json" "${XDG_PREFIX_DIR}/etc/sing-box/${PROTOCOL}/config.json"
    debug "${script_dir}/${PROTOCOL}-server.json ${ARROW} ${XDG_PREFIX_DIR}/etc/sing-box/${PROTOCOL}/config.json"
else
    warning "$(_t 'Configuration file not found')"
fi

# Reload systemd and enable service
progress "Reloading systemd"
run_with_sudo systemctl daemon-reload >/dev/null 2>&1

progress "Enabling service"
run_with_sudo systemctl enable --now "${SERVICE_NAME}" >/dev/null 2>&1 &
spin $!

# Configure shell RC if requested
if [[ "$CONFIGURE_RC" == "true" ]]; then
    echo
    progress "Detecting shell environment"
    shell_info=$(detect_shell)
    shell_name="${shell_info%%:*}"
    rc_file="${shell_info#*:}"

    if [[ -n "$shell_name" ]] && [[ -n "$rc_file" ]]; then
        info "$(_t 'Detected shell'): ${GREEN}${shell_name}${RESET}"
        info "$(_t 'Shell RC file'): ${GREEN}${rc_file}${RESET}"
        configure_shell_rc "$rc_file" "$shell_name"
    else
        warning "$(_t 'No shell RC file found')"
    fi
else
    info "$(_t 'Shell RC configuration disabled')"
fi

# Final success message
echo
success "${BOLD}$(_t 'Installation complete')!${RESET}"
success "$(_t 'Service is now running'): ${GREEN}${SERVICE_NAME}${RESET}"

if [[ "$CONFIGURE_RC" == "true" ]] && [[ -n "$rc_file" ]]; then
    echo
    info "$(_t 'To apply shell changes, run'):"
    echo "${INDENT}${GREEN}${BOLD}\$${RESET} source ${rc_file}"
    info "$(_t 'Or open a new terminal')"
fi

echo
