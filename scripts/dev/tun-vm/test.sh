#!/usr/bin/env bash
# This script changes only a disposable Ubuntu guest, never the host.
set -euo pipefail
ASSETS=/opt/tun-e2e
if [ "$(hostname)" != tun-fixture ] || [ ! -f "$ASSETS/run-id" ] || ! systemd-detect-virt --vm --quiet; then
	echo "Refusing to change a system outside the disposable TUN guest." >&2
	exit 2
fi
# Exercise the root publisher with the same private umask as an operator backup.
umask 077
export PATH="$HOME/.local/bin:$PATH"
export SBC_LANG=en
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_DATA_HOME="$HOME/.local/share"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
result=FAIL
background=()
cleanup() {
	set +e
	if [ "$result" != PASS ]; then
		df -h /
		cat "$HOME/tun-on.log" "$HOME/interrupted.log" "$HOME/interrupted-maintenance.log" "$HOME/interrupted-commit.log" 2>/dev/null
		cat /etc/sbc/tun-control/transaction.json 2>/dev/null
		sudo cat /run/sbc-fixture-systemd-run.log 2>/dev/null
		sudo /usr/bin/systemctl status sbc-tun.service --no-pager
		sudo journalctl -u sbc-tun.service -u 'sbc-tun-recover-*' --no-pager -n 50
		resolvectl status
	fi
	sbc tun off >/dev/null 2>&1
	for pid in "${background[@]}"; do kill "$pid" 2>/dev/null; done
	printf 'TUN_E2E_RESULT=%s:%s\n' "$(<"$ASSETS/run-id")" "$result"
}
trap cleanup EXIT
trap 'printf "TUN fixture failure at line %s\n" "$LINENO" >&2' ERR
step() { printf '\n=== %s ===\n' "$*"; }
absent() { if "$@"; then
	echo "unexpected success: $*" >&2
	return 1
fi; }
phase() { python3 -c 'import json; print(json.load(open("/etc/sbc/tun-control/transaction.json"))["phase"])'; }
generation() { python3 -c 'import json; print(json.load(open("/etc/sbc/tun-control/transaction.json"))["id"])'; }
assert_off() {
	[ "$(phase)" = off ]
	absent /usr/bin/systemctl is-active --quiet sbc-tun.service
	absent ip link show sbc-tun0
	absent test -e /etc/sbc/tun-control/blocked
	[ "$(/usr/bin/systemctl show -p UnitFileState --value sbc-tun.service)" = disabled ]
	sudo nft -j list ruleset | python3 -c 'import json,sys; assert not any(x.get("table",{}).get("name")=="sing-box" for x in json.load(sys.stdin)["nftables"])'
}
assert_modes() {
	sudo python3 - <<'PY'
import grp, os, pwd, stat
uid = pwd.getpwnam("sbc-tun").pw_uid
gid = grp.getgrnam("sbc-tun").gr_gid
for path, mode, owner, group in [
    ("/etc/sbc", 0o755, 0, 0),
    ("/etc/sbc/tun-control", 0o755, 0, 0),
    ("/etc/sbc/tun-control/transaction.json", 0o644, 0, 0),
    ("/etc/sbc/tun-control/recover-sbc", 0o755, 0, 0),
    ("/usr/local/lib/sbc-tun", 0o755, 0, 0),
    ("/usr/local/lib/sbc-tun/sing-box", 0o755, 0, 0),
    ("/etc/sbc/tun", 0o750, 0, gid),
    ("/etc/sbc/tun/config.json", 0o640, 0, gid),
    ("/etc/sbc/tun/rules", 0o750, 0, gid),
    ("/var/lib/sbc-tun", 0o700, uid, gid),
]:
    info = os.stat(path)
    assert (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid) == (mode, owner, group), path
PY
}
step "strict umask for the guest user and privileged publisher"
printf 'Defaults:ubuntu umask=0077,umask_override\n' >"$HOME/sudo-umask"
sudo install -m 440 "$HOME/sudo-umask" /etc/sudoers.d/sbc-fixture-umask
sudo visudo -cf /etc/sudoers.d/sbc-fixture-umask
root_umask=$(sudo sh -c umask)
printf 'user umask=%s; root umask=%s\n' "$(umask)" "$root_umask"
[ "$root_umask" = 0077 ]
step "prepare an isolated router and direct target"
for tool in ip nft dig curl python3 update-ca-certificates; do command -v "$tool" >/dev/null; done
# A restricted QEMU network supplies an address but no default route.
iface=$(ip -4 -o addr show | awk '$4 == "10.0.2.15/24" {print $2; exit}')
[ -n "$iface" ]
sudo ip netns add sbc-fixture
sudo ip link set "$iface" netns sbc-fixture
# Moving a DHCP interface can remove its dynamic address. This address belongs
# to QEMU's default user network, not to the host's network.
sudo ip -n sbc-fixture addr replace 10.0.2.15/24 dev "$iface"
sudo ip -n sbc-fixture link set "$iface" up
sudo ip -n sbc-fixture route replace default via 10.0.2.2 dev "$iface"
sudo ip link add sbcf-main type veth peer name sbcf-site
sudo ip link set sbcf-site netns sbc-fixture
sudo ip addr add 10.77.0.1/30 dev sbcf-main
sudo ip link set sbcf-main up
sudo ip -n sbc-fixture addr add 10.77.0.2/30 dev sbcf-site
sudo ip -n sbc-fixture link set sbcf-site up
sudo ip -n sbc-fixture link set lo up
sudo ip -n sbc-fixture addr add 198.18.0.10/32 dev lo
sudo ip -n sbc-fixture route add blackhole 198.18.0.20/32
sudo ip route replace default via 10.77.0.2 dev sbcf-main
sudo ip netns exec sbc-fixture sysctl -q -w net.ipv4.ip_forward=1
printf 'table ip fixture_nat {\n chain post {\n type nat hook postrouting priority srcnat;\n ip saddr 10.77.0.0/30 oifname "%s" masquerade;\n }\n}\n' "$iface" | sudo ip netns exec sbc-fixture nft -f -
# Logs belong to the unelevated test user.
# shellcheck disable=SC2024
sudo ip netns exec sbc-fixture python3 "$ASSETS/fixtures.py" direct "$ASSETS" >"$HOME/direct.log" 2>&1 &
background+=("$!")
# shellcheck disable=SC2024
sudo ip netns exec sbc-fixture python3 "$ASSETS/fixtures.py" guest-dns "$ASSETS" --bind 10.77.0.2 --port 53 --baseline >"$HOME/baseline.log" 2>&1 &
background+=("$!")
python3 "$ASSETS/fixtures.py" guest-dns "$ASSETS" >"$HOME/dns.log" 2>&1 &
background+=("$!")
sudo resolvectl dns sbcf-main 10.77.0.2
sudo resolvectl default-route sbcf-main yes
sudo install -m 644 "$ASSETS/ca.crt" /usr/local/share/ca-certificates/sbc-tun-fixture.crt
sudo update-ca-certificates >/dev/null
for _ in $(seq 30); do
	if curl --noproxy '*' --resolve www.baidu.com:443:198.18.0.10 -fsS --max-time 2 https://www.baidu.com/control | grep 'direct /control' >/dev/null; then break; fi
	sleep 1
done
curl --noproxy '*' --resolve www.baidu.com:443:198.18.0.10 -fsS --max-time 3 https://www.baidu.com/control | grep 'direct /control' >/dev/null
[ "$(dig +short +time=2 +tries=1 dns-path.fixture.test A)" = 198.18.0.99 ]
step "negative control: the proxy target cannot be reached directly"
absent curl --noproxy '*' --resolve ai.fixture.test:443:198.18.0.20 -fsS --max-time 3 https://ai.fixture.test/control
step "stage the client and activate the real root service"
install -d -m 700 "$HOME/.local/bin" "$XDG_CONFIG_HOME/sbc" "$XDG_DATA_HOME/sbc/bin" "$XDG_DATA_HOME/sbc/rules" "$XDG_DATA_HOME/sbc/cli"
install -m 755 "$ASSETS/sbc" "$XDG_DATA_HOME/sbc/cli/sbc"
ln -s "$XDG_DATA_HOME/sbc/cli/sbc" "$HOME/.local/bin/sbc"
printf 'tun-e2e-old\n' >"$XDG_DATA_HOME/sbc/sbc.version"
install -m 600 "$ASSETS/mixed.json" "$XDG_CONFIG_HOME/sbc/config.json"
# Invoke the real worker explicitly, not a scheduler. Native schedule and mixed
# installation acceptance are separate from this fixture bootstrap.
printf '%s\n' '{"version":1,"scheduled":true,"refresh":{"enabled":true,"interval_seconds":21600},"upgrade":{"enabled":true,"interval_seconds":21600}}' >"$XDG_CONFIG_HOME/sbc/maintenance.json"
cp "$ASSETS"/sing-box* "$XDG_DATA_HOME/sbc/bin/"
cp "$ASSETS/sing-box.version" "$XDG_DATA_HOME/sbc/sing-box.version"
if [ -f "$ASSETS/libcronet.so" ]; then cp "$ASSETS/libcronet.so" "$XDG_DATA_HOME/sbc/bin/"; fi
cp "$ASSETS"/rules/*.srs "$XDG_DATA_HOME/sbc/rules/"
install -m 600 "$ASSETS/subscription" "$XDG_CONFIG_HOME/sbc/subscription"
portal=$(sed 's,/sub/.*$,,' "$ASSETS/subscription")
sbc tun on >"$HOME/tun-on.log" 2>&1
[ "$(phase)" = active ]
sbc status
load_api() {
	assert_modes
	port=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["api_port"])' "$XDG_CONFIG_HOME/sbc/tun.json")
	secret=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["api_secret"])' "$XDG_CONFIG_HOME/sbc/tun.json")
}
load_api
runtime_digest() {
	{
		find "$XDG_DATA_HOME/sbc/bin" "$XDG_DATA_HOME/sbc/rules" "$XDG_DATA_HOME/sbc/cli" -type f -exec sha256sum '{}' +
		sha256sum "$XDG_CONFIG_HOME/sbc/config.json" "$XDG_CONFIG_HOME/sbc/tun.json" "$XDG_DATA_HOME/sbc/sbc.version" "$XDG_DATA_HOME/sbc/sing-box.version"
	} | LC_ALL=C sort | sha256sum
}
assert_selection() {
	curl -fsS --max-time 5 -H "Authorization: Bearer $secret" "http://127.0.0.1:$port/configs" | python3 -c 'import json,sys; assert json.load(sys.stdin)["mode"]=="gfw-proxy"'
	curl -fsS --max-time 5 -H "Authorization: Bearer $secret" "http://127.0.0.1:$port/proxies/proxy" | python3 -c 'import json,sys; assert json.load(sys.stdin)["now"]=="naive"'
	sudo python3 -c 'import json; t=json.load(open("/etc/sbc/tun-control/transaction.json")); c=json.load(open("/etc/sbc/tun/config.json")); assert c["experimental"]["cache_file"]["cache_id"]=="sbc-tun-"+t["id"]'
}
set_mode() {
	curl -fsS --max-time 5 -X PATCH -H "Authorization: Bearer $secret" -H 'Content-Type: application/json' \
		-d "{\"mode\":\"$1\"}" "http://127.0.0.1:$port/configs" >/dev/null
	sudo resolvectl flush-caches
}
assert_path() {
	name=$1
	expected=$2
	nonce=$(python3 -c 'import secrets; print(secrets.token_hex(8))')
	answer=$(curl --noproxy '*' -fsS --max-time 10 "https://$name/$nonce")
	[ "$answer" = "$expected /$nonce" ] || {
		echo "$name: expected $expected, got $answer"
		return 1
	}
}
step "five modes: distinct DNS paths and direct/proxy branches"
for mode in direct ai-only china-direct gfw-proxy global; do
	set_mode "$mode"
	expected_dns=198.18.0.10
	case "$mode" in china-direct | global) expected_dns=198.18.0.20 ;; esac
	# .invalid is answered locally by resolved; .test reaches the DNS path.
	actual_dns=$(dig +short +time=2 +tries=1 dns-path.fixture.test A)
	printf 'mode=%s DNS=%s expected=%s\n' "$mode" "$actual_dns" "$expected_dns"
	[ "$actual_dns" = "$expected_dns" ]
	case "$mode" in
	direct)
		assert_path ai.fixture.test direct
		assert_path ordinary.fixture.test direct
		;;
	ai-only)
		assert_path ai.fixture.test proxy
		assert_path ordinary.fixture.test direct
		;;
	gfw-proxy)
		assert_path gfw.fixture.test proxy
		assert_path ai.fixture.test proxy
		assert_path ordinary.fixture.test direct
		;;
	china-direct)
		assert_path china.fixture.test direct
		assert_path ordinary.fixture.test proxy
		;;
	global)
		assert_path china.fixture.test proxy
		assert_path ordinary.fixture.test proxy
		;;
	esac
	assert_path portal.fixture.test direct
done
step "signed maintenance: candidate rejection must not stop the active generation"
set_mode gfw-proxy
# Change the active API selection. Native route/protocol CLI switching has its
# own acceptance scope; this case checks preservation across maintenance.
curl -fsS --max-time 5 -X PUT -H "Authorization: Bearer $secret" -H 'Content-Type: application/json' -d '{"name":"naive"}' "http://127.0.0.1:$port/proxies/proxy" >/dev/null
assert_path ordinary.fixture.test direct
before=$(generation)
before_pid=$(/usr/bin/systemctl show -p MainPID --value sbc-tun.service)
before_files=$(runtime_digest)
curl -fsS --max-time 5 -X POST "$portal/fixture/manifest/tampered"
absent sbc upgrade
[ "$(generation)" = "$before" ]
[ "$(/usr/bin/systemctl show -p MainPID --value sbc-tun.service)" = "$before_pid" ]
[ "$(runtime_digest)" = "$before_files" ]
curl -fsS --max-time 5 -X POST "$portal/fixture/manifest/valid"
step "upgrade the signed client through the real root transaction"
sbc upgrade
[ "$(sbc version)" = 'sbc tun-e2e-new' ]
[ "$(generation)" != "$before" ]
[ "$(phase)" = active ]
load_api
assert_selection
assert_path gfw.fixture.test proxy-alt
step "automatic workers only report a signed rule snapshot change"
curl -fsS --max-time 5 -X POST "$portal/fixture/sub/changed"
before=$(generation)
before_pid=$(/usr/bin/systemctl show -p MainPID --value sbc-tun.service)
before_files=$(runtime_digest)
sbc _maintenance refresh
sbc _maintenance upgrade
[ "$(generation)" = "$before" ]
[ "$(/usr/bin/systemctl show -p MainPID --value sbc-tun.service)" = "$before_pid" ]
[ "$(runtime_digest)" = "$before_files" ]
test -f "$XDG_CONFIG_HOME/sbc/tun-update-available"
sbc status | grep 'updates are available' >/dev/null
assert_path ordinary.fixture.test direct
step "refresh signed rule snapshots and preserve route and protocol selections"
sbc refresh
[ "$(generation)" != "$before" ]
[ "$(phase)" = active ]
load_api
assert_selection
assert_path ordinary.fixture.test proxy-alt
assert_path portal.fixture.test direct
sbc _maintenance refresh
test ! -e "$XDG_CONFIG_HOME/sbc/tun-update-available"
step "a committed update stays committed after controller death and explicit off"
cat >"$HOME/sudo-wrapper" <<'SH'
#!/bin/sh
if [ -e /run/sbc-fixture-pause-commit ] && [ "$1" = -- ] && [ "$3" = _tun-commit ]; then
	/usr/bin/sudo "$@" || exit $?
	/usr/bin/sudo touch /run/sbc-fixture-committed
	sleep 120
	exit 0
fi
exec /usr/bin/sudo "$@"
SH
sudo install -m 755 "$HOME/sudo-wrapper" /usr/local/bin/sudo
sudo touch /run/sbc-fixture-pause-commit
curl -fsS --max-time 5 -X POST "$portal/fixture/sub/good"
sbc refresh >"$HOME/interrupted-commit.log" 2>&1 &
controller=$!
committed=
for _ in $(seq 600); do
	kill -0 "$controller" 2>/dev/null || break
	if [ -e /run/sbc-fixture-committed ]; then
		committed=yes
		break
	fi
	sleep 0.1
done
[ "$committed" = yes ]
[ "$(phase)" = active ]
kill -KILL "$controller"
wait "$controller" 2>/dev/null || true
published=$(sha256sum "$XDG_DATA_HOME/sbc/rules/"*.srs | LC_ALL=C sort | sha256sum)
sbc tun off
assert_off
python3 -c 'import json; assert json.load(open("/etc/sbc/tun-control/transaction.json"))["committed"] is True'
test -f "$XDG_DATA_HOME/sbc/upgrade-recovery.json"
absent sbc refresh
[ "$(sha256sum "$XDG_DATA_HOME/sbc/rules/"*.srs | LC_ALL=C sort | sha256sum)" = "$published" ]
test ! -e "$XDG_DATA_HOME/sbc/upgrade-recovery.json"
[ "$(sbc version)" = 'sbc tun-e2e-new' ]
assert_off
sudo rm /run/sbc-fixture-pause-commit /run/sbc-fixture-committed /usr/local/bin/sudo
sbc tun on
step "failed active maintenance stops capture without restarting the old generation"
curl -fsS --max-time 5 -X POST "$portal/fixture/sub/failed"
absent sbc refresh
assert_off
test ! -e "$XDG_CONFIG_HOME/sbc/tun.json"
test ! -e "$XDG_DATA_HOME/sbc/upgrade-recovery.json"
curl -fsS --max-time 5 -X POST "$portal/fixture/sub/good"
sbc tun on
step "native stop failure must remain dirty and block mixed mode"
cat >"$HOME/systemctl-wrapper" <<'SH'
#!/bin/sh
if [ -e /run/sbc-fixture-fail-stop ] && [ "$1" = disable ] && [ "$2" = --now ]; then exit 1; fi
if [ -e /run/sbc-fixture-pause ] && [ "$1" = start ] && [ "$2" = sbc-tun.service ]; then
	/usr/bin/systemctl "$@" || exit $?
	touch /run/sbc-fixture-started
	sleep 120
	exit 0
fi
exec /usr/bin/systemctl "$@"
SH
sudo install -m 755 "$HOME/systemctl-wrapper" /usr/local/bin/systemctl
sudo touch /run/sbc-fixture-fail-stop
absent sbc tun off
[ "$(phase)" = dirty ]
/usr/bin/systemctl is-active --quiet sbc-tun.service
test -e /etc/sbc/tun-control/blocked
absent sbc on
sudo rm /run/sbc-fixture-fail-stop
sbc tun off
assert_off
step "failed proxy activation rolls back; restoring the fixture permits recovery"
curl -fsS --max-time 5 -X POST "$portal/fixture/sub/failed"
absent sbc tun on
assert_off
curl -fsS --max-time 5 -X POST "$portal/fixture/sub/good"
sbc tun on
sbc tun off
assert_off
step "direct activation does not require an unused proxy"
curl -fsS --max-time 5 -X POST "$portal/fixture/sub/direct-failed"
sbc tun on
sbc tun off
assert_off
curl -fsS --max-time 5 -X POST "$portal/fixture/sub/good"
step "kill the root controller after startup; observe independent recovery before cleanup"
cat >"$HOME/systemd-run-wrapper" <<'SH'
#!/bin/bash
args=()
for arg in "$@"; do
	if [ "$arg" = --on-active=180s ]; then arg=--on-active=15s; fi
	args+=("$arg")
done
exec /usr/bin/systemd-run "${args[@]}" >>/run/sbc-fixture-systemd-run.log 2>&1
SH
sudo install -m 755 "$HOME/systemd-run-wrapper" /usr/local/bin/systemd-run
sudo touch /run/sbc-fixture-pause
sbc tun on >"$HOME/interrupted.log" 2>&1 &
controller=$!
started=
# TCG needs more startup time. This does not change the recovery timer.
for _ in $(seq 600); do
	kill -0 "$controller" 2>/dev/null || break
	if [ -e /run/sbc-fixture-started ]; then
		started=yes
		break
	fi
	sleep 0.1
done
[ "$started" = yes ]
/usr/bin/systemctl is-active --quiet sbc-tun.service
helper=$(pgrep -f '^/[^[:space:]]*/sbc _tun-apply ')
case "$helper" in '' | *[!0-9]*)
	echo "expected one root apply PID" >&2
	exit 1
	;;
esac
sudo kill -KILL "$helper"
wait "$controller" 2>/dev/null || true
recovered=
for _ in $(seq 45); do
	if [ "$(phase)" = off ]; then
		recovered=yes
		break
	fi
	sleep 1
done
[ "$recovered" = yes ]
assert_off
sudo rm /run/sbc-fixture-pause /run/sbc-fixture-started
step "kill both maintenance controllers; observe the timer before user-file recovery"
sbc tun on
sudo touch /run/sbc-fixture-pause
sbc refresh >"$HOME/interrupted-maintenance.log" 2>&1 &
controller=$!
started=
for _ in $(seq 600); do
	kill -0 "$controller" 2>/dev/null || break
	if [ -e /run/sbc-fixture-started ]; then
		started=yes
		break
	fi
	sleep 0.1
done
[ "$started" = yes ]
/usr/bin/systemctl is-active --quiet sbc-tun.service
helper=$(pgrep -f '^/[^[:space:]]*/sbc _tun-apply ')
case "$helper" in '' | *[!0-9]*)
	echo "expected one root apply PID" >&2
	exit 1
	;;
esac
kill -KILL "$controller"
sudo kill -KILL "$helper"
wait "$controller" 2>/dev/null || true
recovered=
for _ in $(seq 45); do
	if [ "$(phase)" = off ]; then
		recovered=yes
		break
	fi
	sleep 1
done
[ "$recovered" = yes ]
assert_off
test -f "$XDG_DATA_HOME/sbc/upgrade-recovery.json"
sudo rm /run/sbc-fixture-pause /run/sbc-fixture-started
absent sbc on
absent sbc tun on
test -f "$XDG_DATA_HOME/sbc/upgrade-recovery.json"
assert_off
absent sbc refresh
test ! -e "$XDG_DATA_HOME/sbc/upgrade-recovery.json"
test ! -e "$XDG_CONFIG_HOME/sbc/tun.json"
assert_off
sudo rm /usr/local/bin/systemctl /usr/local/bin/systemd-run
step "the client can activate again after independent recovery"
sbc tun on
assert_modes
sbc tun off
assert_off
result=PASS
