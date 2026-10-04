#!/usr/bin/env bash

# Shared state model for the privileged Linux TUN service (docs/TUN_MODE.md).
#
# setup.sh drives the transaction, but client-install.sh and client-uninstall.sh
# need the same answers -- where the managed files live, who owns them, and
# whether the tunnel is safely off -- so those answers live here rather than in
# whichever script happened to need them first.
#
# The status attestation and the transaction marker are deliberately non-secret
# and world-readable. That is what lets a mixed-mode guard refuse without
# prompting for a sudo password it does not otherwise need.
#
# SBM_TUN_ROOT prefixes every managed absolute path. It exists so tests and
# staging runs can point the whole layout at a scratch directory. It grants
# nothing: these paths are only ever written through the caller's own sudo, and
# the trust model already treats the provisioning user as sudo-capable.

# Must stay bash 3.2 compatible; see tests/test_shell_compat.py.

TUN_UNIT_NAME="sing-box-manager-tun.service"
TUN_SAFEGUARD_UNIT_NAME="sing-box-manager-tun-safeguard"
TUN_RUNTIME_ACCOUNT="sing-box-tun"
TUN_INTERFACE_NAME="sbm-tun0"
TUN_INET4_PREFIX="172.19.0.0/30"
TUN_INET6_PREFIX="fdfe:dcba:9876::/126"
TUN_UNIT_VERSION=1
TUN_MANIFEST_SCHEMA_VERSION=1

tun_root_prefix() {
	printf '%s\n' "${SBM_TUN_ROOT:-}"
}

tun_libexec_dir() {
	printf '%s/usr/local/libexec/sing-box-manager-tun\n' "$(tun_root_prefix)"
}

tun_binary_path() {
	printf '%s/sing-box\n' "$(tun_libexec_dir)"
}

tun_cronet_path() {
	printf '%s/libcronet.so\n' "$(tun_libexec_dir)"
}

tun_config_dir() {
	printf '%s/etc/sing-box-manager/tun\n' "$(tun_root_prefix)"
}

tun_config_path() {
	printf '%s/config.json\n' "$(tun_config_dir)"
}

tun_manifest_path() {
	printf '%s/manifest\n' "$(tun_config_dir)"
}

tun_rules_dir() {
	printf '%s/rules\n' "$(tun_config_dir)"
}

tun_unit_dir() {
	printf '%s/etc/systemd/system\n' "$(tun_root_prefix)"
}

tun_unit_path() {
	printf '%s/%s\n' "$(tun_unit_dir)" "$TUN_UNIT_NAME"
}

tun_state_dir() {
	printf '%s/var/lib/sing-box-manager-tun\n' "$(tun_root_prefix)"
}

tun_control_dir() {
	printf '%s/var/lib/sing-box-manager-tun-control\n' "$(tun_root_prefix)"
}

tun_journal_path() {
	printf '%s/journal\n' "$(tun_control_dir)"
}

tun_rollback_dir() {
	printf '%s/previous\n' "$(tun_control_dir)"
}

# Staging happens beside the destination, never in a shared scratch directory:
# /usr/local, /etc, and /var/lib can be separate filesystems, and only a
# same-filesystem rename is atomic.
tun_staged_path() {
	local destination="$1"
	printf '%s/.%s.staged\n' "${destination%/*}" "${destination##*/}"
}

tun_lock_dir() {
	printf '%s/lock\n' "$(tun_control_dir)"
}

tun_lock_owner_path() {
	printf '%s/owner\n' "$(tun_lock_dir)"
}

tun_transaction_marker_path() {
	printf '%s/var/lib/sing-box-manager-tun.transaction\n' "$(tun_root_prefix)"
}

tun_status_path() {
	printf '%s/var/lib/sing-box-manager-tun.status\n' "$(tun_root_prefix)"
}

tun_runtime_account() {
	printf '%s\n' "$TUN_RUNTIME_ACCOUNT"
}

tun_interface_name() {
	printf '%s\n' "$TUN_INTERFACE_NAME"
}

tun_inet4_prefix() {
	printf '%s\n' "$TUN_INET4_PREFIX"
}

tun_inet6_prefix() {
	printf '%s\n' "$TUN_INET6_PREFIX"
}

tun_unit_version() {
	printf '%s\n' "$TUN_UNIT_VERSION"
}

tun_manifest_schema_version() {
	printf '%s\n' "$TUN_MANIFEST_SCHEMA_VERSION"
}

tun_unit_name() {
	printf '%s\n' "$TUN_UNIT_NAME"
}

tun_safeguard_unit_name() {
	printf '%s\n' "$TUN_SAFEGUARD_UNIT_NAME"
}

tun_has_command() {
	command -v "$1" >/dev/null 2>&1
}

# ip, useradd, and userdel live in sbin, which plenty of interactive PATHs omit
# for a non-root user. Falling back to the standard locations keeps a missing
# PATH entry from being reported as a missing tool -- and, worse, keeps a
# cleanup check from passing vacuously because `ip` could not be found.
# shellcheck disable=SC2086  # SBM_TUN_SBIN_DIRS is a deliberate list.
tun_resolve_admin_command() {
	local name="$1"
	local directory

	if tun_has_command "$name"; then
		command -v "$name"
		return 0
	fi

	for directory in ${SBM_TUN_SBIN_DIRS:-/usr/local/sbin /usr/sbin /sbin}; do
		if [ -x "${directory}/${name}" ]; then
			printf '%s\n' "${directory}/${name}"
			return 0
		fi
	done

	return 1
}

tun_ip_command() {
	tun_resolve_admin_command ip
}

# Privilege helpers ###########################################################

tun_run_privileged() {
	if [ "$(id -u)" -eq 0 ]; then
		"$@"
		return
	fi

	if ! tun_has_command sudo; then
		return 1
	fi

	sudo "$@"
}

# Never prompts. Diagnostics use this so displaying a root-only field cannot
# turn a read-only report into a password prompt.
tun_run_privileged_quiet() {
	if [ "$(id -u)" -eq 0 ]; then
		"$@" 2>/dev/null
		return
	fi

	if ! tun_has_command sudo; then
		return 1
	fi

	sudo -n "$@" 2>/dev/null
}

tun_can_sudo_noninteractive() {
	if [ "$(id -u)" -eq 0 ]; then
		return 0
	fi

	tun_has_command sudo && sudo -n true 2>/dev/null
}

# Content hashing #############################################################

tun_sha256_command() {
	if tun_has_command sha256sum; then
		printf 'sha256sum\n'
		return 0
	fi

	if tun_has_command shasum; then
		printf 'shasum -a 256\n'
		return 0
	fi

	return 1
}

# Hash a path the caller can already read.
tun_sha256_of() {
	local file_path="$1"
	local hash_command digest

	if [ ! -f "$file_path" ]; then
		return 1
	fi

	hash_command=$(tun_sha256_command) || return 1
	# shellcheck disable=SC2086
	digest=$($hash_command <"$file_path" 2>/dev/null) || return 1
	printf '%s\n' "${digest%% *}"
}

# Hash a root-only path. Reads through sudo rather than relaxing its mode.
tun_privileged_sha256_of() {
	local file_path="$1"
	local hash_command digest

	hash_command=$(tun_sha256_command) || return 1
	# shellcheck disable=SC2086
	digest=$(tun_run_privileged cat "$file_path" 2>/dev/null | $hash_command 2>/dev/null) || return 1
	digest="${digest%% *}"
	if [ -z "$digest" ]; then
		return 1
	fi

	printf '%s\n' "$digest"
}

# Manifest ####################################################################

tun_manifest_exists() {
	tun_run_privileged_quiet test -f "$(tun_manifest_path)"
}

tun_manifest_value() {
	local key="$1"
	local line

	line=$(tun_run_privileged cat "$(tun_manifest_path)" 2>/dev/null | grep "^${key}=" | head -n 1) || return 1
	if [ -z "$line" ]; then
		return 1
	fi

	printf '%s\n' "${line#*=}"
}

tun_manifest_value_quiet() {
	local key="$1"
	local line

	line=$(tun_run_privileged_quiet cat "$(tun_manifest_path)" | grep "^${key}=" | head -n 1) || return 1
	if [ -z "$line" ]; then
		return 1
	fi

	printf '%s\n' "${line#*=}"
}

tun_manifest_owner_uid() {
	tun_manifest_value owner_uid
}

# True when the manifest exists and records this UID as the owner. A different
# UID never takes over a machine-wide tunnel automatically.
tun_current_uid_owns_manifest() {
	local owner_uid

	owner_uid=$(tun_manifest_value owner_uid 2>/dev/null) || return 1
	[ -n "$owner_uid" ] && [ "$owner_uid" = "$(id -u)" ]
}

# systemd state ###############################################################

tun_systemd_available() {
	tun_has_command systemctl && [ -d /run/systemd/system ]
}

tun_unit_installed() {
	[ -f "$(tun_unit_path)" ]
}

tun_unit_property() {
	local property="$1"
	local value

	if ! tun_has_command systemctl; then
		return 1
	fi

	value=$(systemctl show "$TUN_UNIT_NAME" --property="$property" --value 2>/dev/null) || return 1
	printf '%s\n' "$value"
}

tun_unit_active_state() {
	tun_unit_property ActiveState
}

tun_unit_sub_state() {
	tun_unit_property SubState
}

tun_unit_main_pid() {
	tun_unit_property MainPID
}

tun_unit_restart_count() {
	tun_unit_property NRestarts
}

tun_unit_load_state() {
	tun_unit_property LoadState
}

tun_unit_enabled_state() {
	if ! tun_has_command systemctl; then
		return 1
	fi

	systemctl is-enabled "$TUN_UNIT_NAME" 2>/dev/null || true
}

tun_unit_is_active() {
	[ "$(tun_unit_active_state 2>/dev/null)" = "active" ]
}

tun_unit_is_enabled() {
	[ "$(tun_unit_enabled_state)" = "enabled" ]
}

# Public attestations #########################################################

# `safe-off`, `active`, `dirty`, or empty when never written. Root maintains it;
# everyone can read it.
tun_status() {
	local status_file value

	status_file=$(tun_status_path)
	if [ ! -f "$status_file" ]; then
		return 0
	fi

	IFS= read -r value <"$status_file" 2>/dev/null || true
	printf '%s\n' "${value%%[[:space:]]*}"
}

tun_transaction_in_progress() {
	[ -f "$(tun_transaction_marker_path)" ]
}

# The predicate every mixed-mode command applies (docs/TUN_MODE.md section 7).
# `is-active` alone is not enough: a half-finished transaction, a restart loop,
# and a masked unit all leave routing in a state mixed mode must not start under.
tun_is_safely_off() {
	local active_state enabled_state status_value

	if tun_transaction_in_progress; then
		return 1
	fi

	status_value=$(tun_status)

	if ! tun_unit_installed; then
		# Nothing installed and nothing claiming otherwise. A leftover
		# `active` or `dirty` attestation means routing was never verified
		# clean, so refuse rather than assume.
		case "$status_value" in
		"" | safe-off)
			return 0
			;;
		*)
			return 1
			;;
		esac
	fi

	if [ "$status_value" != "safe-off" ]; then
		return 1
	fi

	if ! tun_has_command systemctl; then
		return 1
	fi

	active_state=$(tun_unit_active_state 2>/dev/null)
	if [ "$active_state" != "inactive" ]; then
		return 1
	fi

	if [ "$(tun_unit_sub_state 2>/dev/null)" != "dead" ]; then
		return 1
	fi

	enabled_state=$(tun_unit_enabled_state)
	if [ "$enabled_state" != "disabled" ]; then
		return 1
	fi

	return 0
}

# Network artifacts ###########################################################

tun_interface_exists() {
	local ip_command

	ip_command=$(tun_ip_command) || return 1
	"$ip_command" -o link show dev "$TUN_INTERFACE_NAME" >/dev/null 2>&1
}

# Routes still pointing at our interface, in any table, v4 and v6.
tun_managed_routes_present() {
	local routes ip_command

	ip_command=$(tun_ip_command) || return 1

	routes=$(
		{
			"$ip_command" -4 route show table all 2>/dev/null
			"$ip_command" -6 route show table all 2>/dev/null
		} | grep -c "[[:space:]]dev[[:space:]]${TUN_INTERFACE_NAME}\([[:space:]]\|$\)" || true
	)
	[ "${routes:-0}" -gt 0 ]
}

tun_managed_rules_present() {
	local rules ip_command

	ip_command=$(tun_ip_command) || return 1

	rules=$(
		{
			"$ip_command" -4 rule show 2>/dev/null
			"$ip_command" -6 rule show 2>/dev/null
		} | grep -c "${TUN_INTERFACE_NAME}" || true
	)
	[ "${rules:-0}" -gt 0 ]
}

tun_network_artifacts_present() {
	tun_interface_exists || tun_managed_routes_present || tun_managed_rules_present
}

# An interface or address range we intend to claim that something else already
# holds. Our own live interface is not a conflict; re-sync reclaims it.
tun_foreign_interface_conflict() {
	local link_line ip_command

	ip_command=$(tun_ip_command) || return 1

	link_line=$("$ip_command" -o link show dev "$TUN_INTERFACE_NAME" 2>/dev/null) || return 1
	if [ -z "$link_line" ]; then
		return 1
	fi

	# The interface exists. It is ours only when the manifest says a managed
	# generation is live; anything else owns the name and we must not take it.
	if tun_unit_installed && tun_unit_is_active; then
		return 1
	fi

	return 0
}

tun_address_conflict() {
	local addresses ip_command

	ip_command=$(tun_ip_command) || return 1

	addresses=$(
		{
			"$ip_command" -o -4 addr show 2>/dev/null
			"$ip_command" -o -6 addr show 2>/dev/null
		} | grep -v "[[:space:]]${TUN_INTERFACE_NAME}[[:space:]]" |
			grep -c '172\.19\.0\.\|fdfe:dcba:9876:' || true
	)
	[ "${addresses:-0}" -gt 0 ]
}

# Transaction lock ############################################################
#
# The lock has to survive across many separate sudo invocations, so it is an
# atomically created root-owned directory rather than an flock on one shell's
# file descriptor. Its owner stamp records the boot id and the holder's process
# start time so a lock left behind by a killed shell can be told apart from a
# live one and reclaimed instead of blocking forever.

tun_boot_id() {
	if [ -r /proc/sys/kernel/random/boot_id ]; then
		cat /proc/sys/kernel/random/boot_id 2>/dev/null
		return 0
	fi

	printf 'unknown\n'
}

tun_process_start_time() {
	local pid="$1"
	local stat_line

	if [ ! -r "/proc/${pid}/stat" ]; then
		return 1
	fi

	stat_line=$(cat "/proc/${pid}/stat" 2>/dev/null) || return 1
	# Field 22 is starttime. The comm field can contain spaces and
	# parentheses, so count from the last ')' rather than from the start.
	printf '%s\n' "${stat_line##*) }" | awk '{ print $20 }'
}

tun_lock_owner_field() {
	local key="$1"
	local line

	line=$(grep "^${key}=" "$(tun_lock_owner_path)" 2>/dev/null | head -n 1) || return 1
	if [ -z "$line" ]; then
		return 1
	fi

	printf '%s\n' "${line#*=}"
}

tun_lock_is_stale() {
	local lock_pid lock_boot lock_start current_start

	if [ ! -f "$(tun_lock_owner_path)" ]; then
		# A lock directory with no owner stamp cannot be attributed, so it
		# is treated as abandoned rather than blocking every future run.
		return 0
	fi

	lock_boot=$(tun_lock_owner_field boot 2>/dev/null)
	if [ -n "$lock_boot" ] && [ "$lock_boot" != "$(tun_boot_id)" ]; then
		return 0
	fi

	lock_pid=$(tun_lock_owner_field pid 2>/dev/null)
	if [ -z "$lock_pid" ] || [ ! -d "/proc/${lock_pid}" ]; then
		return 0
	fi

	# The pid is live, but it may be a different process that reused it.
	lock_start=$(tun_lock_owner_field start 2>/dev/null)
	current_start=$(tun_process_start_time "$lock_pid" 2>/dev/null)
	if [ -n "$lock_start" ] && [ -n "$current_start" ] && [ "$lock_start" != "$current_start" ]; then
		return 0
	fi

	return 1
}

tun_write_lock_owner() {
	local owner_path
	owner_path=$(tun_lock_owner_path)

	printf 'pid=%s\nboot=%s\nstart=%s\n' \
		"$$" "$(tun_boot_id)" "$(tun_process_start_time "$$" 2>/dev/null)" |
		tun_run_privileged tee "$owner_path" >/dev/null || return 1
	tun_run_privileged chmod 0644 "$owner_path"
}

tun_acquire_lock() {
	local lock_dir attempt=0

	lock_dir=$(tun_lock_dir)
	tun_run_privileged mkdir -p -m 0700 "$(tun_control_dir)" >/dev/null 2>&1 || return 1
	tun_run_privileged chown root:root "$(tun_control_dir)" >/dev/null 2>&1 || true

	while [ "$attempt" -lt 2 ]; do
		if tun_run_privileged mkdir -m 0700 "$lock_dir" >/dev/null 2>&1; then
			tun_write_lock_owner || return 1
			return 0
		fi

		if ! tun_lock_is_stale; then
			return 1
		fi

		tun_run_privileged rm -rf "$lock_dir" >/dev/null 2>&1 || return 1
		attempt=$((attempt + 1))
	done

	return 1
}

tun_release_lock() {
	tun_run_privileged rm -rf "$(tun_lock_dir)" >/dev/null 2>&1
}

# Unit rendering ##############################################################

# Generated from this fixed template rather than copied from anywhere the user
# can write, so an activated unit can only ever be one this code produced.
tun_render_unit() {
	local binary_path config_path state_dir account
	binary_path=$(tun_binary_path)
	config_path=$(tun_config_path)
	state_dir=$(tun_state_dir)
	account=$(tun_runtime_account)

	cat <<EOF
[Unit]
Description=sing-box-manager TUN client
Documentation=https://sing-box.sagernet.org
After=network-online.target nss-lookup.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=exec
User=${account}
Group=${account}
UMask=0077
StateDirectory=sing-box-manager-tun
StateDirectoryMode=0700
CapabilityBoundingSet=CAP_NET_ADMIN
AmbientCapabilities=CAP_NET_ADMIN
NoNewPrivileges=true
PrivateTmp=true
ProtectControlGroups=true
ProtectHome=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectSystem=strict
DevicePolicy=closed
DeviceAllow=/dev/net/tun rw
ExecStartPre=${binary_path} check -c ${config_path}
ExecStart=${binary_path} run -D ${state_dir} -c ${config_path}
Restart=on-failure
RestartSec=5s
TimeoutStartSec=30s
TimeoutStopSec=30s
LimitNOFILE=infinity

[Install]
WantedBy=multi-user.target
EOF
}
