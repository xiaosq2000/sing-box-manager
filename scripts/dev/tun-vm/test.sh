#!/usr/bin/env bash
# Runs inside the nested Ubuntu VM as the default user. It stages a client
# layout, starts local fixtures, and exercises the real root TUN service.
set -euo pipefail

ASSETS=/opt/tun-e2e
TOKEN=e2eToken-e2eToken-e2eT
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
export PATH="$HOME/.local/bin:$PATH"

result=FAIL
background=()
cleanup() {
	set +e
	sbc tun off >/dev/null 2>&1 || true
	for pid in "${background[@]}"; do
		kill "$pid" 2>/dev/null || true
	done
	rm -f "$HOME/.local/bin/sudo" "$HOME/sudo-wrapper.pid"
	echo "TUN_E2E_RESULT=$result"
}
trap cleanup EXIT

step() {
	echo
	echo "=== $* ==="
}

absent() {
	if "$@"; then
		echo "still present: $*"
		exit 1
	fi
}

step "stage the client layout"
install -d -m 700 "$XDG_CONFIG_HOME/sbc" "$XDG_DATA_HOME/sbc/bin" "$XDG_DATA_HOME/sbc/rules" "$HOME/.local/bin"
install -m 755 "$ASSETS/sbc" "$HOME/.local/bin/sbc"
install -m 755 "$ASSETS/sing-box" "$XDG_DATA_HOME/sbc/bin/sing-box"
if [ -f "$ASSETS/libcronet.so" ]; then
	install -m 644 "$ASSETS/libcronet.so" "$XDG_DATA_HOME/sbc/bin/libcronet.so"
fi
cp "$ASSETS/sing-box.version" "$XDG_DATA_HOME/sbc/sing-box.version"
install -m 600 "$ASSETS/subscription" "$XDG_CONFIG_HOME/sbc/subscription"

step "start local fixtures"
python3 "$ASSETS/dns.py" 5353 >"$HOME/dns.log" 2>&1 &
background+=("$!")
python3 -m http.server 18080 --bind 127.0.0.1 --directory "$ASSETS/portal" >"$HOME/portal.log" 2>&1 &
background+=("$!")
python3 -m http.server 8080 --bind 127.0.0.1 --directory "$ASSETS/site" >"$HOME/site.log" 2>&1 &
background+=("$!")
for _ in $(seq 30); do
	if curl -fsS "http://127.0.0.1:18080/sub/$TOKEN" >/dev/null 2>&1 &&
		curl -fsS http://127.0.0.1:8080/ >/dev/null 2>&1; then
		break
	fi
	sleep 1
done
curl -fsS "http://127.0.0.1:18080/sub/$TOKEN" | grep -q '"config_tun"'

step "activate"
echo "HOME=$HOME XDG_CONFIG_HOME=$XDG_CONFIG_HOME XDG_DATA_HOME=$XDG_DATA_HOME"
ls -la "$XDG_CONFIG_HOME/sbc" "$XDG_DATA_HOME/sbc"
cat "$XDG_CONFIG_HOME/sbc/subscription"
cat "$XDG_DATA_HOME/sbc/sing-box.version"
sbc link
if ! sbc tun on; then
	sudo systemctl status sbc-tun.service --no-pager || true
	sudo journalctl -u sbc-tun.service --no-pager -n 100 || true
	exit 1
fi
sbc status | grep -q 'tun:      active (sbc-tun0)'
ip link show sbc-tun0 >/dev/null
systemctl is-active --quiet sbc-tun.service
test -e /run/sbc-tun/active
sudo nft list ruleset >/dev/null

step "DNS answers in every Clash mode"
port=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["api_port"])' "$XDG_CONFIG_HOME/sbc/tun.json")
secret=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["api_secret"])' "$XDG_CONFIG_HOME/sbc/tun.json")
for mode in direct ai-only china-direct gfw-proxy global; do
	curl -fsS -X PATCH -H "Authorization: Bearer $secret" -H 'Content-Type: application/json' \
		-d "{\"mode\":\"$mode\"}" "http://127.0.0.1:$port/configs" >/dev/null
	answer=$(dig +short test.example A | head -n1)
	[ "$answer" = "127.0.0.1" ] || {
		echo "mode $mode answered $answer"
		false
	}
done

step "local site and tunnel"
curl -fsS http://test.example:8080/ | grep -q "tun-e2e"
curl -fsS -X PATCH -H "Authorization: Bearer $secret" -H 'Content-Type: application/json' \
	-d '{"mode":"global"}' "http://127.0.0.1:$port/configs" >/dev/null
curl -fsS http://example.com/ | grep -q "Example Domain"

step "deactivate"
sbc tun off
absent systemctl is-active --quiet sbc-tun.service
absent ip link show sbc-tun0
absent test -e /run/sbc-tun/active
absent test -e "$XDG_CONFIG_HOME/sbc/tun.json"

step "failed activation rolls back"
cp "$ASSETS/tun-failed.json" "$ASSETS/portal/sub/$TOKEN"
if sbc tun on; then
	echo "activation with a dead proxy succeeded"
	false
fi
sudo systemctl status sbc-tun.service --no-pager || true
sudo journalctl -u sbc-tun.service --no-pager -n 80 || true
absent systemctl is-active --quiet sbc-tun.service
absent ip link show sbc-tun0
absent test -e /run/sbc-tun/active
sleep 1
sbc tun on
sbc tun off

step "interrupted activation cleans up"
cat >"$HOME/.local/bin/sudo" <<'EOF'
#!/bin/sh
echo $$ > "$HOME/sudo-wrapper.pid"
sleep 2
exec /usr/bin/sudo "$@"
EOF
chmod 755 "$HOME/.local/bin/sudo"
sbc tun on &
tun_pid=$!
sleep 1
kill -INT "$tun_pid" 2>/dev/null || true
wait "$tun_pid" 2>/dev/null || true
if [ -f "$HOME/sudo-wrapper.pid" ]; then
	kill "$(cat "$HOME/sudo-wrapper.pid")" 2>/dev/null || true
fi
pkill -f "$HOME/.local/bin/sudo" 2>/dev/null || true
rm -f "$HOME/.local/bin/sudo" "$HOME/sudo-wrapper.pid"
sbc tun off
absent systemctl is-active --quiet sbc-tun.service
absent ip link show sbc-tun0
absent test -e /run/sbc-tun/active
sbc tun on
sbc tun off
absent systemctl is-active --quiet sbc-tun.service
absent ip link show sbc-tun0

result=PASS
