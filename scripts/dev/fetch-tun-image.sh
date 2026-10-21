#!/usr/bin/env bash
# Fetch a cloud image directly, outside any metered proxy. Verify cached images.
set -euo pipefail
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ftp_proxy FTP_PROXY socks_proxy SOCKS_PROXY all_proxy ALL_PROXY
CACHE=${SBC_TUN_E2E_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/sbc-tun-e2e}
IMAGE=$CACHE/noble-server-cloudimg-amd64.img
BASE=https://cloud-images.ubuntu.com/noble/current
mkdir -p "$CACHE"
if [ -f "$IMAGE" ] && [ -f "$IMAGE.sha256" ]; then
	sha256sum --check "$IMAGE.sha256"
	exit 0
fi
curl -fsSL --connect-timeout 15 --max-time 30 "$BASE/SHA256SUMS" -o "$CACHE/SHA256SUMS"
expected=$(awk '{ name=$2; sub(/^\*/, "", name); if (name=="noble-server-cloudimg-amd64.img") print $1 }' "$CACHE/SHA256SUMS")
[ -n "$expected" ] || {
	echo 'The cloud image checksum is missing.' >&2
	exit 1
}
if [ -f "$IMAGE" ]; then
	printf '%s  %s\n' "$expected" "$IMAGE" | sha256sum --check
else
	curl -fSL --connect-timeout 15 --max-time 600 "$BASE/noble-server-cloudimg-amd64.img" -o "$IMAGE.part"
	printf '%s  %s\n' "$expected" "$IMAGE.part" | sha256sum --check
	mv "$IMAGE.part" "$IMAGE"
fi
printf '%s  %s\n' "$expected" "$IMAGE" >"$IMAGE.sha256"
