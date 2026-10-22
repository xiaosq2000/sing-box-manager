#!/usr/bin/env bash
# Boot a disposable guest. The host's services and network stay unchanged.
set -euo pipefail
umask 077
suite=${1:-core}
case "$suite" in core | lifecycle) ;; *)
	echo "Usage: $0 [core|lifecycle]" >&2
	exit 2
	;;
esac
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ftp_proxy FTP_PROXY socks_proxy SOCKS_PROXY all_proxy ALL_PROXY
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CACHE=${SBC_TUN_E2E_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/sbc-tun-e2e}
IMAGE=${SBC_TUN_E2E_IMAGE:-$CACHE/noble-server-cloudimg-amd64.img}
BINARY=${SBC_TUN_E2E_SING_BOX:-$ROOT/.cache/sing-box-manager/bin/sing-box}
mkdir -p "$CACHE/runs"
WORK=$(mktemp -d "$CACHE/runs/run-XXXXXXXX")
ASSETS=$WORK/assets
mkdir -m 700 "$ASSETS"
# shellcheck disable=SC2329 # Called by the EXIT trap.
cleanup() {
	if [ -n "${fixture_pid:-}" ]; then kill "$fixture_pid" 2>/dev/null || true; fi
	if [ -n "${vm_pid:-}" ]; then kill "$vm_pid" 2>/dev/null || true; fi
	echo "TUN test artifacts: $WORK"
}
trap cleanup EXIT
for tool in go python3 curl openssl qemu-system-x86_64 qemu-img nc; do
	command -v "$tool" >/dev/null || {
		echo "missing $tool" >&2
		exit 1
	}
done
if [ ! -f "$IMAGE" ]; then
	echo "Missing cloud image: $IMAGE. See docs/DEVELOPMENT.md for verified image preparation." >&2
	exit 1
fi
if [ ! -x "$BINARY" ]; then
	echo "Missing sing-box: run pixi run fetch-sing-box first." >&2
	exit 1
fi
BINARY=$(realpath "$BINARY")
# Only disposable binaries trust this ephemeral key. Production source and
# signature checks stay unchanged. The private key is not in the guest archive.
openssl genpkey -algorithm ED25519 -out "$WORK/signing.key"
openssl pkey -in "$WORK/signing.key" -pubout -outform DER -out "$WORK/signing-public.der"
python3 - "$ROOT" "$WORK" <<'PY'
import base64, json, sys
from pathlib import Path
root, work = map(Path, sys.argv[1:])
public = base64.b64encode((work / "signing-public.der").read_bytes()[-32:]).decode()
key = work / "key.go"
key.write_text("package trust\nfunc init() { Keys = append(Keys, " + json.dumps(public) + ") }\n")
(work / "overlay.json").write_text(json.dumps({"Replace": {str(root / "internal/trust/zz_e2e_key.go"): str(key)}}))
PY
CGO_ENABLED=0 GOTOOLCHAIN=local go build -trimpath -overlay "$WORK/overlay.json" -ldflags '-X main.version=tun-e2e-old' -o "$ASSETS/sbc" "$ROOT/cmd/sbc"
mkdir -p "$ASSETS/release/sbc/linux-amd64"
CGO_ENABLED=0 GOTOOLCHAIN=local go build -trimpath -overlay "$WORK/overlay.json" -ldflags '-X main.version=tun-e2e-new' -o "$ASSETS/release/sbc/linux-amd64/sbc" "$ROOT/cmd/sbc"
install -m 755 "$BINARY" "$ASSETS/sing-box"
if [ -f "$(dirname "$BINARY")/libcronet.so" ]; then
	install -m 644 "$(dirname "$BINARY")/libcronet.so" "$ASSETS/libcronet.so"
fi
"$BINARY" version | awk 'NR == 1 {print $3}' >"$ASSETS/sing-box.version"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=TUN-fixture \
	-addext 'subjectAltName=DNS:www.baidu.com,DNS:www.gstatic.com,DNS:dns.fixture.test,DNS:ai.fixture.test,DNS:gfw.fixture.test,DNS:china.fixture.test,DNS:ordinary.fixture.test,DNS:portal.fixture.test,DNS:api.ipify.org,DNS:api6.ipify.org,DNS:4.ident.me,DNS:6.ident.me,DNS:ipwho.is,DNS:speed.cloudflare.com' \
	-keyout "$ASSETS/ca.key" -out "$ASSETS/ca.crt" >"$WORK/certificate.log" 2>&1
python3 "$ROOT/scripts/dev/tun-vm/fixtures.py" host "$ASSETS" >"$WORK/host-fixture.log" 2>&1 &
fixture_pid=$!
for _ in $(seq 50); do
	if [ -f "$ASSETS/ports.json" ]; then break; fi
	kill -0 "$fixture_pid" || {
		echo "host fixtures failed" >&2
		exit 1
	}
	sleep 0.1
done
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/scripts/dev/tun-vm/prepare.py" "$ASSETS" "$BINARY"
if [ "$suite" = lifecycle ]; then
	python3 "$ROOT/scripts/dev/tun-vm/lifecycle-host.py" "$ASSETS" "$CACHE/engines"
else
	python3 "$ROOT/scripts/dev/tun-vm/release.py" "$ASSETS"
fi
portal=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["portal"])' "$ASSETS/ports.json")
socks=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["socks"])' "$ASSETS/ports.json")
socks_alt=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["socks_alt"])' "$ASSETS/ports.json")
echo "Preflight: bounded SOCKS/TLS fixture, without public DNS or internet requests"
response=$(curl --noproxy '' --socks5-hostname "127.0.0.1:$socks" --cacert "$ASSETS/ca.crt" \
	-fsS --max-time 10 https://www.gstatic.com/generate_204)
[ "$response" = 'proxy /generate_204' ]
"$BINARY" check -D "$ASSETS" -c "$ASSETS/tun.json"
cp "$ROOT/scripts/dev/tun-vm/fixtures.py" "$ASSETS/fixtures.py"
cp "$ROOT/scripts/dev/tun-vm/test.sh" "$ASSETS/test.sh"
python3 - "$ASSETS" "$portal" "$suite" <<'PY'
import json, secrets, sys
from pathlib import Path
assets, port, suite = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
(assets / "run-id").write_text(secrets.token_hex(16))
config = {
    # Noble cloud images include the required tools. The guest checks them;
    # acceptance never installs packages or makes public network requests.
    "package_update": False,
    "runcmd": [
        ["sh", "-c", f"mkdir -p /opt/tun-e2e; curl --noproxy '*' -fsS http://10.0.2.100:{port}/assets.tar | tar -x -C /opt/tun-e2e; chown -R ubuntu:ubuntu /opt/tun-e2e"],
        ["su", "-", "ubuntu", "-c", "bash /opt/tun-e2e/test.sh"],
        ["poweroff"],
    ],
}
if suite == "lifecycle":
    config["runcmd"][1:] = [["python3", "/opt/tun-e2e/lifecycle.py", "bootstrap"]]
(assets / "user-data").write_text("#cloud-config\n" + json.dumps(config))
(assets / "meta-data").write_text("instance-id: tun-" + (assets / "run-id").read_text() + "\nlocal-hostname: tun-fixture\n")
PY
tar -C "$ASSETS" --exclude=assets.tar -cf "$ASSETS/assets.tar" .
qemu-img create -f qcow2 -F qcow2 -b "$(realpath "$IMAGE")" "$WORK/disk.qcow2"
# Several retained root generations exceed the cloud image's small root disk.
# This grows only the sparse guest disk; cloud-init expands its root filesystem.
qemu-img resize "$WORK/disk.qcow2" 12G
accel=${SBC_TUN_E2E_ACCEL:-tcg}
if [ -z "${SBC_TUN_E2E_ACCEL:-}" ] && [ -r /dev/kvm ] && [ -w /dev/kvm ]; then accel=kvm; fi
case "$accel" in kvm | tcg) ;; *)
	echo "SBC_TUN_E2E_ACCEL must be kvm or tcg" >&2
	exit 1
	;;
esac
echo "Booting disposable guest ($accel, $suite); serial log: $WORK/serial.log"
# A lifecycle run reboots the same disk. Every boot needs its own marker.
boots=1
boot_timeout=1800
if [ "$suite" = lifecycle ]; then
	boots=4
	boot_timeout=900
fi
for boot in $(seq "$boots"); do
	serial=$WORK/serial.log
	if [ "$suite" = lifecycle ]; then serial=$WORK/serial-$boot.log; fi
	trace=$WORK/network.pcap
	if [ "$suite" = lifecycle ]; then trace=$WORK/network-$boot.pcap; fi
	# Each guest socket needs its own relay. A fixed TCP chardev is one stream.
	timeout "$boot_timeout" qemu-system-x86_64 \
		-machine "accel=$accel" -cpu max -m 2048 -smp 2 \
		-drive "file=$WORK/disk.qcow2,if=virtio,format=qcow2" \
		-smbios "type=1,serial=ds=nocloud-net;s=http://10.0.2.100:$portal/" \
		-netdev "user,id=net0,restrict=on,guestfwd=tcp:10.0.2.100:$portal-cmd:nc -N 127.0.0.1 $portal,guestfwd=tcp:10.0.2.100:$socks-cmd:nc -N 127.0.0.1 $socks,guestfwd=tcp:10.0.2.100:$socks_alt-cmd:nc -N 127.0.0.1 $socks_alt" -device virtio-net-pci,netdev=net0 \
		-object "filter-dump,id=trace,netdev=net0,file=$trace" \
		-display none -serial "file:$serial" -monitor none -no-reboot &
	vm_pid=$!
	status=0
	wait "$vm_pid" || status=$?
	vm_pid=
	result=$(python3 "$ROOT/scripts/dev/tun-vm/lifecycle-host.py" result "$serial" "$(<"$ASSETS/run-id")" "$boot" "$boots")
	if [ "$status" -eq 0 ] && [ "$result" = PASS ]; then
		grep -E 'TUN_E2E_RESULT=.*:PASS' "$serial"
		exit 0
	fi
	if [ "$status" -eq 0 ] && [ "$result" = REBOOT ]; then
		grep -E 'TUN_LIFECYCLE_REBOOT=' "$serial"
		echo "Restarting the same guest disk for the next acceptance phase"
		continue
	fi
	echo "TUN acceptance failed (QEMU status $status, boot $boot)." >&2
	grep -E '=== |sbc: |TUN fixture failure|TUN_E2E_RESULT|Error:|Traceback|curl:' "$serial" >&2 || true
	tail -n 120 "$serial" >&2
	exit 1
done
exit 1
