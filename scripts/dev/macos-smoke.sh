#!/usr/bin/env bash

# Verify the client scripts against a real macOS host.
#
# The test suite covers the macOS code paths on Linux by shimming uname,
# launchctl, networksetup and friends onto PATH. That proves the logic but not
# the environment, so this script checks the parts a shim cannot:
#
#   - the stock /bin/bash 3.2, not whatever newer bash is on PATH
#   - the BSD userland: sed, awk and grep differ from GNU
#   - dscl, which only runs here because macOS has no getent
#   - real networksetup output, which the service-name parser has to handle
#
# Read-only on purpose. It never loads a LaunchAgent (a CI runner has no Aqua
# session) and never writes proxy settings (the runner's own networking has to
# keep working).

set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1

# Overridable so this can be rehearsed on Linux against a bash 3.2 build.
STOCK_BASH="${SBM_STOCK_BASH:-/bin/bash}"
failures=0

pass() { printf '  ok    %s\n' "$1"; }

fail() {
	printf '  FAIL  %s\n' "$1"
	if [ -n "${2:-}" ]; then
		printf '%s\n' "$2" | sed 's/^/          /' | head -15
	fi
	failures=$((failures + 1))
}

# Run a snippet under the stock bash and require a pattern in its output.
expect_output() {
	local label="$1" pattern="$2" snippet="$3"
	local output status

	output=$(LANG=C.UTF-8 "$STOCK_BASH" -c "$snippet" 2>&1)
	status=$?

	if [ "$status" -ne 0 ]; then
		fail "$label (exit ${status})" "$output"
		return
	fi

	if printf '%s\n' "$output" | grep -q "$pattern"; then
		pass "$label"
	else
		fail "$label (expected: ${pattern})" "$output"
	fi
}

CLIENT_SCRIPTS="
scripts/client-install.sh
scripts/client-uninstall.sh
scripts/setup.sh
scripts/lib/ui.sh
scripts/lib/platform.sh
scripts/lib/system-proxy.sh
"

printf '\n== environment ==\n'

bash_version=$("$STOCK_BASH" --version | head -1)
case "$bash_version" in
*"version 3."*)
	pass "stock bash is 3.x: ${bash_version}"
	;;
*)
	# Not a failure of the scripts, but the job would stop testing what it
	# claims to test, so say so loudly.
	fail "expected /bin/bash to be 3.x, got: ${bash_version}"
	;;
esac

expect_output "uname reports Darwin" "Darwin" "uname -s"

printf '\n== parse under stock bash 3.2 ==\n'

for script in $CLIENT_SCRIPTS; do
	if output=$("$STOCK_BASH" -n "$script" 2>&1); then
		pass "parses: ${script}"
	else
		fail "parses: ${script}" "$output"
	fi
done

printf '\n== platform detection ==\n'

expect_output "plat_id resolves" "darwin-" \
	'source scripts/lib/platform.sh; plat_id'
expect_output "plat_is_macos is true" "yes" \
	'source scripts/lib/platform.sh; plat_is_macos && echo yes'

printf '\n== installer help ==\n'

expect_output "client-install.sh --help" "Sing-box VPN Client Installer" \
	'bash scripts/client-install.sh --help'
expect_output "client-uninstall.sh --help" "Sing-box VPN Client Uninstaller" \
	'bash scripts/client-uninstall.sh --help'
expect_output "client-install.sh --help in zh_CN" "安装脚本\|用法" \
	'FORCE_LANG=zh_CN bash scripts/client-install.sh --help'

printf '\n== login shell detection via dscl ==\n'

# macOS has no getent, so this is the only place the dscl branch runs.
expect_output "detect_shell finds a login shell" "^\(bash\|zsh\):/" \
	'source scripts/client-install.sh; detect_target_user; detect_shell'

printf '\n== proxy helper ==\n'

expect_output "setup.sh sources cleanly" "Toggling Commands" \
	'source scripts/setup.sh >/dev/null 2>&1; proxy help'
expect_output "runtime is macos" "Runtime: macos" \
	'source scripts/setup.sh >/dev/null 2>&1; proxy check system'
expect_output "desktop is aqua" "Desktop: aqua" \
	'source scripts/setup.sh >/dev/null 2>&1; proxy check system'
expect_output "launchctl is detected" "launchctl:" \
	'source scripts/setup.sh >/dev/null 2>&1; proxy check system'
expect_output "networksetup is detected" "networksetup:" \
	'source scripts/setup.sh >/dev/null 2>&1; proxy check system'
expect_output "proxy target resolves" "Proxy target: 127.0.0.1:1080" \
	'source scripts/setup.sh >/dev/null 2>&1; proxy check system'

printf '\n== system proxy parsing against real networksetup ==\n'

# The parser has to survive BSD sed and awk plus this machine's real service
# list. A runner always has at least one network service.
expect_output "resolves a network service name" "." \
	'source scripts/lib/platform.sh
	 source scripts/lib/system-proxy.sh
	 macos_proxy_network_service'
# shellcheck disable=SC2016  # the snippet expands in the inner shell, not here
expect_output "classifies the current system proxy" "^\(ours\|other\|off\)$" \
	'source scripts/lib/platform.sh
	 source scripts/lib/system-proxy.sh
	 service=$(macos_proxy_network_service) || exit 1
	 macos_proxy_classify "$service"'
expect_output "check desktop names the service" "Network service:" \
	'source scripts/setup.sh >/dev/null 2>&1; proxy check desktop'

printf '\n'
if [ "$failures" -gt 0 ]; then
	printf '%s check(s) failed\n\n' "$failures"
	exit 1
fi

printf 'all checks passed\n\n'
