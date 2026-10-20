#!/usr/bin/env bash
# Boots a disposable Ubuntu cloud image with QEMU and runs the Linux TUN
# checks inside it. The TUN captures the VM's traffic, never the host's.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CACHE="${SBC_TUN_E2E_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/sbc-tun-e2e}"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/sbc-tun-e2e.XXXXXX")
ASSETS="$WORK/assets"
TOKEN=e2eToken-e2eToken-e2eT
PORTAL_PORT=18080
UPSTREAM_PORT=1081
DNS_PORT=5353
IMAGE_URL=https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
IMAGE_SUMS=https://cloud-images.ubuntu.com/noble/current/SHA256SUMS
IMAGE="$CACHE/noble-server-cloudimg-amd64.img"

cleanup() {
	rm -rf "$WORK"
}
trap cleanup EXIT

need() {
	command -v "$1" >/dev/null || {
		echo "missing $1; install qemu-system-x86, cloud-image-utils, curl and Go" >&2
		exit 1
	}
}
for command in go curl python3 qemu-system-x86_64 cloud-localds; do
	need "$command"
done

mkdir -p "$ASSETS" "$CACHE" "$ASSETS/portal/sub" "$ASSETS/site"

version=$(sed -n 's/^sing_box_version:[[:space:]]*\([^[:space:]]*\).*/\1/p' "$ROOT/config/inventory/example.yaml" | head -n1)
[ -n "$version" ] || {
	echo "cannot read sing_box_version from example.yaml" >&2
	exit 1
}

echo "building sbc"
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o "$ASSETS/sbc" "$ROOT/cmd/sbc"

echo "fetching sing-box $version"
archive="$CACHE/sing-box-$version-linux-amd64.tar.gz"
if [ ! -f "$archive" ]; then
	auth=()
	if [ -n "${GITHUB_TOKEN:-}" ]; then
		auth=(-H "Authorization: Bearer $GITHUB_TOKEN")
	fi
	curl -fsSL "${auth[@]}" \
		"https://api.github.com/repos/SagerNet/sing-box/releases/tags/v$version" -o "$WORK/release.json"
	name=$(
		python3 - "$WORK/release.json" "$version" <<'PY'
import json, sys
release = json.load(open(sys.argv[1]))
want = f"sing-box-{sys.argv[2]}-linux-amd64.tar.gz"
for asset in release["assets"]:
    if asset["name"] == want:
        print(asset["browser_download_url"])
        print(asset.get("digest", "").removeprefix("sha256:"))
        break
else:
    raise SystemExit("sing-box release has no linux-amd64 archive")
PY
	)
	url=$(printf '%s\n' "$name" | sed -n '1p')
	digest=$(printf '%s\n' "$name" | sed -n '2p')
	[ -n "$url" ] && [ -n "$digest" ] || {
		echo "sing-box release metadata is incomplete" >&2
		exit 1
	}
	curl -fsSL --retry 3 "$url" -o "$archive"
	echo "$digest  $archive" | sha256sum --check
fi
tar -xzf "$archive" -C "$WORK"
sing_box_dir=$(dirname "$(find "$WORK" -type f -name sing-box -perm -u+x | head -n1)")
[ -n "$sing_box_dir" ] || {
	echo "the sing-box archive has no binary" >&2
	exit 1
}
install -m 755 "$sing_box_dir/sing-box" "$ASSETS/sing-box"
if [ -f "$sing_box_dir/libcronet.so" ]; then
	install -m 644 "$sing_box_dir/libcronet.so" "$ASSETS/libcronet.so"
fi
printf '%s\n' "$version" >"$ASSETS/sing-box.version"

cat >"$ASSETS/tun.json" <<JSON
{
  "log": {"level": "warn"},
  "dns": {
    "servers": [
      {"type": "udp", "tag": "local", "server": "127.0.0.1", "server_port": $DNS_PORT}
    ],
    "rules": [
      {"clash_mode": "direct", "server": "local"},
      {"clash_mode": "ai-only", "server": "local"},
      {"clash_mode": "china-direct", "server": "local"},
      {"clash_mode": "gfw-proxy", "server": "local"},
      {"clash_mode": "global", "server": "local"}
    ],
    "final": "local",
    "strategy": "prefer_ipv4"
  },
  "inbounds": [
    {
      "type": "tun",
      "tag": "tun-in",
      "interface_name": "sbc-tun0",
      "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
      "auto_route": true,
      "strict_route": true,
      "auto_redirect": true,
      "dns_mode": "disabled"
    }
  ],
  "outbounds": [
    {"type": "selector", "tag": "proxy", "outbounds": ["socks"], "default": "socks"},
    {"type": "socks", "tag": "socks", "server": "127.0.0.1", "server_port": $UPSTREAM_PORT},
    {"type": "direct", "tag": "direct", "domain_resolver": {"server": "local"}}
  ],
  "route": {
    "auto_detect_interface": true,
    "final": "proxy",
    "rules": [
      {"action": "sniff"},
      {"type": "logical", "mode": "or", "rules": [{"protocol": "dns"}, {"port": 53}], "action": "hijack-dns"},
      {"ip_is_private": true, "outbound": "direct"},
      {"network": "udp", "port": 123, "outbound": "direct"},
      {"network": "udp", "action": "reject"},
      {"clash_mode": "direct", "outbound": "direct"},
      {"clash_mode": "ai-only", "outbound": "direct"},
      {"clash_mode": "china-direct", "outbound": "proxy"},
      {"clash_mode": "gfw-proxy", "outbound": "proxy"},
      {"clash_mode": "global", "outbound": "proxy"}
    ]
  },
  "experimental": {
    "cache_file": {"enabled": true, "store_dns": true},
    "clash_api": {"default_mode": "china-direct"}
  }
}
JSON
python3 - "$ASSETS/tun.json" "$ASSETS/portal/sub/$TOKEN" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
json.dump({"version": 1, "config": {}, "config_tun": config}, open(sys.argv[2], "w"))
PY
printf 'http://127.0.0.1:%d/sub/%s\n' "$PORTAL_PORT" "$TOKEN" >"$ASSETS/subscription"

cat >"$ASSETS/upstream.json" <<JSON
{"log": {"level": "warn"},
 "inbounds": [{"type": "socks", "tag": "in", "listen": "127.0.0.1", "listen_port": $UPSTREAM_PORT}],
 "outbounds": [{"type": "direct", "tag": "direct"}]}
JSON
printf '<!doctype html><title>tun-e2e</title>tun-e2e\n' >"$ASSETS/site/index.html"
install -m 755 "$ROOT/scripts/dev/tun-vm/test.sh" "$ASSETS/test.sh"
install -m 644 "$ROOT/scripts/dev/tun-vm/dns.py" "$ASSETS/dns.py"

echo "fetching the Ubuntu cloud image"
if [ ! -f "$IMAGE" ]; then
	curl -fsSL --retry 3 "$IMAGE_URL" -o "$IMAGE.part"
	expected=$(curl -fsSL "$IMAGE_SUMS" | awk '{
		name = $2
		sub(/^\*/, "", name)
		if (name == "noble-server-cloudimg-amd64.img") {
			print $1
			exit
		}
	}')
	[ -n "$expected" ] || {
		echo "the Ubuntu image checksum is missing" >&2
		exit 1
	}
	echo "$expected  $IMAGE.part" | sha256sum --check
	mv "$IMAGE.part" "$IMAGE"
fi

cat >"$WORK/user-data" <<'CLOUD'
#cloud-config
package_update: true
packages:
  - nftables
  - dnsutils
  - curl
runcmd:
  - modprobe 9p 9pnet 9pnet_virtio
  - mkdir -p /mnt/assets
  - mount -t 9p -o trans=virtio,version=9p2000.L assets /mnt/assets
  - cp -a /mnt/assets /opt/tun-e2e
  - chown -R ubuntu:ubuntu /opt/tun-e2e
  - su - ubuntu -c 'bash /opt/tun-e2e/test.sh' 2>&1 | tee /dev/ttyS0
  - poweroff
CLOUD
cat >"$WORK/meta-data" <<'META'
instance-id: sbc-tun-e2e
local-hostname: sbc-tun-e2e
META
cloud-localds "$WORK/seed.iso" "$WORK/user-data" "$WORK/meta-data"

cp "$IMAGE" "$WORK/disk.qcow2"
accel=tcg
if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
	accel=kvm
fi
echo "booting the nested VM with $accel acceleration"
set +e
timeout 1800 qemu-system-x86_64 \
	-machine "accel=$accel" -cpu max -m 2048 -smp 2 \
	-drive "file=$WORK/disk.qcow2,if=virtio,format=qcow2" \
	-drive "file=$WORK/seed.iso,if=virtio,format=raw" \
	-virtfs "local,path=$ASSETS,mount_tag=assets,security_model=none" \
	-netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
	-display none -serial "file:$WORK/serial.log" -monitor none -no-reboot
status=$?
set -e
if [ "$status" -eq 124 ]; then
	echo "the nested VM timed out" >&2
fi
if ! grep -q 'TUN_E2E_RESULT=PASS' "$WORK/serial.log"; then
	echo "the nested VM did not pass the Linux TUN checks" >&2
	tail -n 200 "$WORK/serial.log" >&2
	exit 1
fi
grep 'TUN_E2E_RESULT=' "$WORK/serial.log"
