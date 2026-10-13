#!/usr/bin/env bash
# Fixture for an installed legacy client's upgrade call. This is not a client.
# Keep the caller's local state and post-install source step: the hosted
# installer must clear that state through its one-use setup.sh replacement.

_proxy_check_update() {
	printf 'unexpected legacy update check\n' >&2
	return 99
}

_proxy_upgrade_client() {
	local data="${XDG_DATA_HOME:-$HOME/.local/share}/sing-box"
	local config="${XDG_CONFIG_HOME:-$HOME/.config}/sing-box"
	local protocol route port was_active=false desktop_state=ours
	local installer target_shell
	protocol=$(cat "$config/selected-protocol")
	route=$(cat "$config/selected-route")
	port=$(cat "$config/selected-port")
	target_shell=$(sed -n 's/^shell=//p' "$data/install-preferences")
	installer=$(mktemp "${TMPDIR:-/tmp}/legacy-upgrade.XXXXXX") || return 1
	if ! curl -fsSL --output "$installer" "$(cat "$data/portal-base-url")/install/linux.sh"; then
		rm -f "$installer"
		return 1
	fi
	if ! bash "$installer" -p "$protocol" --shell "$target_shell"; then
		rm -f "$installer"
		return 1
	fi
	rm -f "$installer"
	# shellcheck source=/dev/null
	source "$data/setup.sh" || return 1
	if [[ -n "$route" || -n "$port" || "$was_active" != true || -n "$desktop_state" ]]; then
		printf 'unexpected legacy state restoration\n' >&2
		return 99
	fi
	_proxy_check_update
}

proxy() {
	[[ "$1" == upgrade ]] || return 2
	shift
	_proxy_upgrade_client "$@"
}
