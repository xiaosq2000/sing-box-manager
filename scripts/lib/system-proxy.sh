#!/usr/bin/env bash

# System proxy helpers shared by setup.sh and client-uninstall.sh: networksetup
# on macOS, dconf on GNOME. Nothing here prints UI, so callers own the messaging
# and can capture stdout safely.

# The target this client configures. Used to tell our own system proxy settings
# apart from ones the user set for something else.
SYSTEM_PROXY_HOST="127.0.0.1"
SYSTEM_PROXY_DEFAULT_PORT="1080"

# `proxy on` picks the mixed inbound's port at run time and writes it into the
# client configs, so the port has to be read back rather than assumed.
#
# Resolved on demand, never at source time: setup.sh sources this file from the
# user's shell rc, so an eager lookup would fork a subshell into every new
# terminal for a value only the two classify functions below ever read -- and it
# would go stale the moment `proxy on --port` moved the inbound in that same
# shell, making the client's own settings classify as someone else's.
#
# Where the client keeps its per-protocol configs. setup.sh already owns this
# path as SING_BOX_CONFIG_ROOT and assigns it after sourcing; the default keeps
# client-uninstall.sh, which sources this file alone, self-contained. Only a
# default, so a caller that set it before sourcing keeps its value.
: "${SYSTEM_PROXY_CONFIG_ROOT:="${XDG_CONFIG_HOME:-$HOME/.config}/sing-box"}"

_system_proxy_detect_port() {
	local config_root="${1:-$SYSTEM_PROXY_CONFIG_ROOT}"
	local protocol cfg port

	for protocol in trojan hysteria2 naive; do
		cfg="${config_root}/${protocol}/config.json"
		if [ -f "$cfg" ]; then
			# First listen_port wins: the mixed inbound is the only one these
			# client configs declare, and quitting there keeps this to a
			# single process on a file read on every classify.
			port=$(sed -n '/"listen_port"/{s/[^0-9]*\([0-9][0-9]*\).*/\1/p;q;}' "$cfg")
			if [ -n "$port" ]; then
				printf '%s\n' "$port"
				return 0
			fi
		fi
	done

	printf '%s\n' "$SYSTEM_PROXY_DEFAULT_PORT"
}

# The port `proxy on` last chose, recorded next to selected-route and
# selected-protocol. The configs are installer-owned -- an upgrade rewrites them
# from the shipped templates, resetting listen_port to the default -- so the
# marker is what survives to say which port this client is meant to serve.
_system_proxy_selected_port_file() {
	printf '%s/selected-port\n' "${1:-$SYSTEM_PROXY_CONFIG_ROOT}"
}

_system_proxy_read_selected_port() {
	local marker port=""

	marker=$(_system_proxy_selected_port_file "$@")
	[ -f "$marker" ] || return 0

	IFS= read -r port <"$marker" || true
	port=${port%$'\r'}
	case "$port" in
	'' | *[!0-9]*) return 0 ;;
	esac
	[ "$port" -ge 1024 ] 2>/dev/null && [ "$port" -le 65535 ] 2>/dev/null || return 0

	printf '%s\n' "$port"
}

# Ports a system proxy setting written by this client may name: the one the
# configs declare now, plus the one the marker records. Those legitimately
# disagree between an upgrade resetting the configs and the next `proxy on`
# reconciling them, and a setting pointing at either is still ours to revert.
_system_proxy_owned_ports() {
	local config_port selected_port

	config_port=$(_system_proxy_detect_port "$@")
	selected_port=$(_system_proxy_read_selected_port "$@")

	if [ -n "$selected_port" ] && [ "$selected_port" != "$config_port" ]; then
		printf '%s %s\n' "$config_port" "$selected_port"
		return 0
	fi
	printf '%s\n' "$config_port"
}

_system_proxy_port_is_ours() {
	local candidate="$1"
	local owned="$2"

	[ -n "$candidate" ] || return 1
	case " $owned " in
	*" $candidate "*) return 0 ;;
	esac
	return 1
}

# --- macOS -----------------------------------------------------------------

macos_proxy_run_with_sudo() {
	if [ "${EUID:-$(id -u)}" -eq 0 ]; then
		"$@"
		return
	fi

	if ! command -v sudo >/dev/null 2>&1; then
		return 1
	fi

	sudo "$@"
}

# Resolve the network service that currently holds the default route. With a VPN
# or a virtualization adapter up this may not be the one the user expects, which
# is why callers report the name they resolved.
macos_proxy_network_service() {
	local interface network_service

	interface=$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')
	if [ -z "$interface" ]; then
		return 1
	fi

	network_service=$(networksetup -listnetworkserviceorder 2>/dev/null |
		awk -v want="Device: ${interface})" '
			index($0, want) { print previous; exit }
			{ previous = $0 }
		' | sed 's/^(\*\{0,1\}[0-9]*) //')

	if [ -z "$network_service" ]; then
		return 1
	fi

	printf '%s\n' "$network_service"
}

# Parse one `networksetup -get*proxy` block into "host port", or fail when that
# proxy is disabled.
macos_proxy_read() {
	local network_service="$1"
	local getter="$2"
	local line enabled="" server="" port=""

	while IFS= read -r line; do
		case "$line" in
		"Enabled: "*) enabled="${line#Enabled: }" ;;
		"Server: "*) server="${line#Server: }" ;;
		"Port: "*) port="${line#Port: }" ;;
		esac
	done < <(networksetup "$getter" "$network_service" 2>/dev/null)

	if [ "$enabled" != "Yes" ] || [ -z "$server" ]; then
		return 1
	fi

	printf '%s %s\n' "$server" "$port"
}

macos_proxy_classify() {
	local network_service="$1"
	local getter pair pair_host pair_port found_ours=false
	local owned

	owned=$(_system_proxy_owned_ports "$SYSTEM_PROXY_CONFIG_ROOT")

	for getter in -getwebproxy -getsecurewebproxy -getsocksfirewallproxy; do
		if ! pair=$(macos_proxy_read "$network_service" "$getter"); then
			continue
		fi

		pair_host=${pair%% *}
		pair_port=${pair##* }
		if [ "$pair_host" = "$SYSTEM_PROXY_HOST" ] &&
			_system_proxy_port_is_ours "$pair_port" "$owned"; then
			found_ours=true
		else
			printf 'other\n'
			return 0
		fi
	done

	if [ "$found_ours" = true ]; then
		printf 'ours\n'
	else
		printf 'off\n'
	fi
}

macos_proxy_clear() {
	local network_service="$1"

	macos_proxy_run_with_sudo networksetup -setwebproxystate "$network_service" off || return 1
	macos_proxy_run_with_sudo networksetup -setsecurewebproxystate "$network_service" off || return 1
	macos_proxy_run_with_sudo networksetup -setsocksfirewallproxystate "$network_service" off || return 1
	return 0
}

# --- GNOME -----------------------------------------------------------------

# dconf returns strings single-quoted and numbers bare.
gnome_proxy_read() {
	local dconf_key="$1"
	local value

	value=$(dconf read "$dconf_key" 2>/dev/null || true)
	case "$value" in
	\'*\')
		value=${value#\'}
		value=${value%\'}
		;;
	esac

	printf '%s\n' "$value"
}

gnome_proxy_classify() {
	local protocol host port found_ours=false
	local owned

	if [ "$(gnome_proxy_read /system/proxy/mode)" != "manual" ]; then
		printf 'off\n'
		return 0
	fi

	owned=$(_system_proxy_owned_ports "$SYSTEM_PROXY_CONFIG_ROOT")

	for protocol in http https socks; do
		host=$(gnome_proxy_read "/system/proxy/${protocol}/host")
		if [ -z "$host" ]; then
			continue
		fi

		port=$(gnome_proxy_read "/system/proxy/${protocol}/port")
		if [ "$host" = "$SYSTEM_PROXY_HOST" ] && _system_proxy_port_is_ours "$port" "$owned"; then
			found_ours=true
		else
			printf 'other\n'
			return 0
		fi
	done

	if [ "$found_ours" = true ]; then
		printf 'ours\n'
	else
		printf 'off\n'
	fi
}

# Matches what `proxy desktop off` does: the hosts stay and only the mode goes
# back to none, so re-enabling later does not have to re-enter them.
gnome_proxy_clear() {
	dconf write /system/proxy/mode "'none'" >/dev/null 2>&1
}

# --- Platform-agnostic entry points ----------------------------------------

# Print a display name for what would be changed, and fail when this platform has
# no usable system proxy mechanism.
system_proxy_target_name() {
	if plat_is_macos; then
		command -v networksetup >/dev/null 2>&1 || return 1
		macos_proxy_network_service
		return
	fi

	command -v dconf >/dev/null 2>&1 || return 1
	printf 'GNOME desktop\n'
}

# One of `ours`, `other`, or `off`. `other` wins over `ours` on purpose: clearing
# a proxy we did not set would be worse than leaving one of ours behind.
system_proxy_classify() {
	if plat_is_macos; then
		macos_proxy_classify "$1"
		return
	fi

	gnome_proxy_classify
}

system_proxy_clear() {
	if plat_is_macos; then
		macos_proxy_clear "$1"
		return
	fi

	gnome_proxy_clear
}

# Only macOS needs elevation to change the system proxy.
system_proxy_clear_needs_sudo() {
	plat_is_macos
}
