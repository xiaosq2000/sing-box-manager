#!/usr/bin/env bash
# Scan reachable history by default, staged changes with --staged, or a clean
# working tree with --working-tree.
# The binary version and archive digest are pinned independently of download metadata.
set -euo pipefail

version=8.30.1
archive_sha256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb

case "${1:-}" in
"") mode=git ;;
--working-tree) mode=dir ;;
--staged) mode=staged ;;
*)
	printf 'Usage: bash scripts/dev/check-secrets.sh [--staged|--working-tree]\n' >&2
	exit 2
	;;
esac
if [ "$#" -gt 1 ]; then
	printf 'Only one optional argument is supported.\n' >&2
	exit 2
fi

# A fixed archive is used only on the supported Linux x86-64 build environment.
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != x86_64 ]; then
	printf 'The pinned scanner requires Linux x86-64.\n' >&2
	exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Dependency downloads must not use an operator's metered proxy.
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
	-u ftp_proxy -u FTP_PROXY -u socks_proxy -u SOCKS_PROXY \
	-u all_proxy -u ALL_PROXY \
	curl --fail --location --silent --show-error --connect-timeout 10 --max-time 120 \
	"https://github.com/gitleaks/gitleaks/releases/download/v${version}/gitleaks_${version}_linux_x64.tar.gz" \
	--output "$tmp/gitleaks.tar.gz"
printf '%s  %s\n' "$archive_sha256" "$tmp/gitleaks.tar.gz" | sha256sum --check

tar -xzf "$tmp/gitleaks.tar.gz" -C "$tmp" gitleaks

# Fail closed if an allowlist accidentally hides new credentials in fixture files.
mkdir -p "$tmp/control/tests"
control_value=$(head -c 32 /dev/urandom | base64)
printf 'api_key = "%s"\n' "$control_value" >"$tmp/control/tests/test_config_loader.py"
control_status=0
"$tmp/gitleaks" dir "$tmp/control" --config "$root/.gitleaks.toml" \
	--redact --no-banner --ignore-gitleaks-allow \
	--report-format json --report-path "$tmp/control-report.json" >/dev/null 2>&1 || control_status=$?
if [ "$control_status" -ne 1 ]; then
	printf 'Secret-scanner negative control failed; no scan result can be trusted.\n' >&2
	exit 1
fi
printf 'Secret-scanner negative control passed.\n'

case "$mode" in
git)
	"$tmp/gitleaks" git . --config .gitleaks.toml --redact --no-banner \
		--ignore-gitleaks-allow --log-opts=--all
	;;
staged)
	"$tmp/gitleaks" git . --config .gitleaks.toml --redact --no-banner \
		--ignore-gitleaks-allow --pre-commit --staged
	;;
dir)
	"$tmp/gitleaks" dir . --config .gitleaks.toml --redact --no-banner \
		--ignore-gitleaks-allow
	;;
esac
