#!/bin/sh
# Install sbc, the sing-box client, from a subscription link:
#
#   curl -fsSL https://<portal>/install.sh | sh
#
# The link is a credential. This script reads it from the terminal and hands it
# to curl or wget on standard input, so it reaches neither shell history nor
# any process's arguments. The sbc it downloads relies on TLS; sbc checks every
# later download against the release's signed manifest.
#
# On a machine with the bash client, this moves it to sbc without a gap in the
# proxy. It trades the client's saved token for the link, starts sbc on a spare
# port, and removes the bash client only after a page loads through sbc. sbc
# then takes over the old port, route and protocol. A machine with TUN mode set
# up keeps the bash client until sbc supports TUN.
#
# Messages are in Simplified Chinese by the rule sbc follows: SBC_LANG=zh, or a
# locale of zh_CN or zh_SG in the first of LC_ALL, LC_MESSAGES and LANG that is
# set.
#
# Everything runs from main at the end, so sh has read the whole script before
# anything starts, even when the script arrives on standard input.
set -eu

chinese=""
if [ -n "${SBC_LANG:-}" ]; then
	case "$SBC_LANG" in
	zh*) chinese=1 ;;
	esac
else
	for locale in "${LC_ALL:-}" "${LC_MESSAGES:-}" "${LANG:-}"; do
		if [ -n "$locale" ]; then
			case "$locale" in
			zh_CN* | zh_SG*) chinese=1 ;;
			esac
			break
		fi
	done
fi

usage() {
	if [ -n "$chinese" ]; then
		cat <<'USAGE'
用法：install.sh [--shell bash|zsh] [--no-rc] [-p PROTOCOL]

  --shell SHELL        在这个 shell 的 rc 文件中加入 'sbc init'
  --no-rc              不改动 rc 文件
  -p, --protocol NAME  用这个协议代替订阅的默认协议
USAGE
	else
		cat <<'USAGE'
Usage: install.sh [--shell bash|zsh] [--no-rc] [-p PROTOCOL]

  --shell SHELL        Add 'sbc init' to this shell's rc file
  --no-rc              Leave rc files alone
  -p, --protocol NAME  Start with this protocol instead of the subscription's
USAGE
	fi
}

# t prints a message in the user's language. The English message is the key,
# and the translation keeps its %s values in the same order.
t() {
	if [ -n "$chinese" ]; then
		case "$1" in
		"Subscription link: ") printf '%s' "订阅链接：" && return ;;
		"Downloading sbc for %s...") printf '%s' "正在下载 sbc（%s）……" && return ;;
		"the download failed; check the link") printf '%s' "下载失败，请检查链接" && return ;;
		"curl or wget is needed") printf '%s' "需要 curl 或 wget" && return ;;
		"--shell needs bash or zsh") printf '%s' "--shell 需要 bash 或 zsh" && return ;;
		"%s needs a protocol") printf '%s' "%s 需要一个协议" && return ;;
		"sbc runs on Linux and macOS") printf '%s' "sbc 只能在 Linux 和 macOS 上运行" && return ;;
		"sbc runs on amd64 and arm64") printf '%s' "sbc 只能在 amd64 和 arm64 上运行" && return ;;
		"that is not a subscription link") printf '%s' "这不是订阅链接" && return ;;
		"this machine set up TUN mode with the bash client. Keep using the bash client until sbc supports TUN.") printf '%s' "这台机器用 bash 客户端设置了 TUN 模式。在 sbc 支持 TUN 之前，请继续使用 bash 客户端。" && return ;;
		"the bash client is here, but its uninstaller is missing from %s. Remove the bash client by hand, then run this again.") printf '%s' "这台机器装有 bash 客户端，但 %s 中缺少它的卸载程序。请手动删除 bash 客户端，然后重新运行。" && return ;;
		"The bash client's saved token did not get the subscription link. Paste the link from the portal's files page.") printf '%s' "未能用 bash 客户端保存的令牌取得订阅链接。请从门户的文件页面复制链接并粘贴。" && return ;;
		"sbc did not work here, so it is removed again. The bash client keeps running as before.") printf '%s' "sbc 在这台机器上无法工作，已将其删除。bash 客户端照常运行。" && return ;;
		"Removing the bash client...") printf '%s' "正在删除 bash 客户端……" && return ;;
		"The bash client's uninstaller stopped partway, so install.sh removed the rest of its files.") printf '%s' "bash 客户端的卸载程序中途停止，install.sh 已删除其余文件。" && return ;;
		"the bash client's uninstaller stopped. sbc is running; finish with 'bash %s', then 'sbc port %s'.") printf '%s' "bash 客户端的卸载程序中途停止。sbc 正在运行；请运行 'bash %s'，再运行 'sbc port %s' 完成迁移。" && return ;;
		"Removed the bash client's lines from %s.") printf '%s' "已从 %s 中删除 bash 客户端的配置行。" && return ;;
		"sbc stays on 127.0.0.1:%s because port %s is still taken. Run 'sbc port %s' once it is free.") printf '%s' "sbc 仍在 127.0.0.1:%s，因为端口 %s 仍被占用。端口空出后，请运行 'sbc port %s'。" && return ;;
		"The bash client's desktop proxy is off now. Run 'sbc desktop on' to point the desktop at sbc.") printf '%s' "bash 客户端设置的桌面代理已关闭。运行 'sbc desktop on' 可让桌面改用 sbc。" && return ;;
		"sbc replaced the bash client. The proxy command went with it; type sbc instead.") printf '%s' "sbc 已取代 bash 客户端。proxy 命令随之移除，请改用 sbc。" && return ;;
		"Shells opened before now still have the old 'proxy' function. Open a new shell.") printf '%s' "之前打开的 shell 仍保留旧的 'proxy' 函数，请打开新的 shell。" && return ;;
		"%s still runs the bash client on lines %s. Remove those lines.") printf '%s' "%s 的第 %s 行仍在运行 bash 客户端，请删除这些行。" && return ;;
		esac
	fi
	printf '%s' "$1"
}

# fail and say print a message from a printf format in English and its values.
fail() {
	format=$1
	shift
	# shellcheck disable=SC2059 # The format is one of this script's messages.
	printf "install.sh: $(t "$format")\n" "$@" >&2
	exit 1
}

say() {
	format=$1
	shift
	# shellcheck disable=SC2059 # The format is one of this script's messages.
	printf "$(t "$format")\n" "$@" >&2
}

# first_line prints a file's first line without its line ending, or nothing.
first_line() {
	line=""
	if [ -f "$1" ]; then
		IFS= read -r line <"$1" || true
	fi
	printf '%s' "$line" | tr -d '\r'
}

# preference prints a key's value from the bash client's install-preferences.
preference() {
	if [ -f "$old_data/install-preferences" ]; then
		sed -n "s/^$1=//p" "$old_data/install-preferences" | head -n 1
	fi
}

# tun_set_up succeeds when the bash client set up TUN mode, by the signs its
# own uninstaller looks for.
tun_set_up() {
	root=${SBM_TUN_ROOT:-}
	if [ -f "$root/etc/systemd/system/sing-box-manager-tun.service" ] ||
		[ -f "$root/var/lib/sing-box-manager-tun.transaction" ]; then
		return 0
	fi
	case "$(first_line "$root/var/lib/sing-box-manager-tun.status")" in
	"" | safe-off) return 1 ;;
	esac
	return 0
}

# config_port prints the port the bash client's configs listen on.
config_port() {
	for name in trojan hysteria2 naive; do
		if [ -f "$old_config/$name/config.json" ]; then
			port=$(sed -n '/"listen_port"/{s/[^0-9]*\([0-9][0-9]*\).*/\1/p;q;}' "$old_config/$name/config.json")
			if [ -n "$port" ]; then
				printf '%s' "$port"
				return 0
			fi
		fi
	done
}

# read_bash_client sets what the move keeps: the port, the route and the
# protocol the bash client uses, and its rc file preferences.
read_bash_client() {
	listen_port=$(config_port)
	old_port=$(first_line "$old_config/selected-port")
	case "$old_port" in
	"" | *[!0-9]*) old_port=${listen_port:-1080} ;;
	esac
	route=$(first_line "$old_config/selected-route")
	case "$route" in
	china | gfw | ai | global) ;;
	*) route="" ;;
	esac
	if [ -z "$protocol" ]; then
		protocol=$(first_line "$old_config/selected-protocol")
	fi
	case "$protocol" in
	trojan | hysteria2 | naive) ;;
	*) protocol="" ;;
	esac
	if [ -z "$no_rc" ] && [ "$(preference rc_enabled)" = false ]; then
		no_rc=1
	fi
	if [ -z "$shell_name" ]; then
		case "$(preference shell)" in
		bash | zsh) shell_name=$(preference shell) ;;
		esac
	fi
}

# bash_client_desktop prints ours, other or off, for whether the desktop proxy
# is the bash client's. It asks the client's own library, which its uninstaller
# asks before it turns that proxy off.
bash_client_desktop() {
	if [ ! -f "$old_data/lib/platform.sh" ] || [ ! -f "$old_data/lib/system-proxy.sh" ]; then
		return 0
	fi
	# shellcheck disable=SC2016
	bash -c '. "$1/lib/platform.sh" && . "$1/lib/system-proxy.sh" &&
		target=$(system_proxy_target_name) && system_proxy_classify "$target"' \
		sh "$old_data" </dev/null 2>/dev/null || true
}

# trade_token prints the subscription link the portal gives for the bash
# client's saved machine token. The token goes to curl on standard input.
trade_token() {
	base=$(first_line "$old_data/portal-base-url")
	base=${base%/}
	token=$(first_line "$old_config/portal-token")
	case "$base" in
	https://* | http://localhost* | http://127.0.0.1*) ;;
	*) return 1 ;;
	esac
	case "$base" in
	*[!A-Za-z0-9._:/-]*) return 1 ;;
	esac
	case "$token" in
	"" | *[!A-Za-z0-9._@:+-]*) return 1 ;;
	esac
	command -v curl >/dev/null 2>&1 || return 1
	response=$(printf 'url = "%s/api/sub"\nheader = "Authorization: Bearer %s"\n' "$base" "$token" |
		curl -fsS --noproxy '*' --max-time 30 -d '' -K - 2>/dev/null) || return 1
	printf '%s' "$response" | sed -n 's/.*"url" *: *"\([^"]*\)".*/\1/p'
}

ask_link() {
	# Tests point this at a file; people answer on the terminal.
	tty=${SBC_INSTALL_TTY:-/dev/tty}
	t 'Subscription link: ' >&2
	stty -echo <"$tty" 2>/dev/null || true
	answer=""
	IFS= read -r answer <"$tty" || true
	stty echo <"$tty" 2>/dev/null || true
	printf '\n' >&2
	printf '%s' "$answer"
}

download_sbc() {
	url="$link/files/sbc/$os-$arch/sbc"
	say 'Downloading sbc for %s...' "$os-$arch"
	if command -v curl >/dev/null 2>&1; then
		printf 'url = "%s"\n' "$url" | curl -fsSL --noproxy '*' -K - -o "$sbc" ||
			fail "the download failed; check the link"
	elif command -v wget >/dev/null 2>&1; then
		printf '%s\n' "$url" | wget -q --no-proxy -O "$sbc" -i - ||
			fail "the download failed; check the link"
	else
		fail "curl or wget is needed"
	fi
	chmod +x "$sbc"
}

# forget_git_proxy removes global git proxy settings the bash client made. git
# reads https_proxy, which sbc sets in shells.
forget_git_proxy() {
	command -v git >/dev/null 2>&1 || return 0
	for port in "$@"; do
		for key in http.proxy https.proxy; do
			if git config --global --get-all "$key" 2>/dev/null | grep -Fxq "http://127.0.0.1:$port"; then
				git config --global --unset-all "$key" "^http://127[.]0[.]0[.]1:$port\$" || true
			fi
		done
	done
}

# take_over_port moves sbc to the bash client's port, which frees up once the
# old service has stopped.
take_over_port() {
	current=$("$sbc" port </dev/null 2>/dev/null) || current=""
	[ "$current" != "$old_port" ] || return 0
	tries=0
	until "$sbc" port "$old_port" </dev/null >/dev/null 2>&1; do
		tries=$((tries + 1))
		if [ "$tries" -ge 10 ]; then
			say "sbc stays on 127.0.0.1:%s because port %s is still taken. Run 'sbc port %s' once it is free." "$current" "$old_port" "$old_port"
			return 0
		fi
		sleep 1
	done
}

quote() {
	printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# The bash client's `proxy upgrade` runs this script, then sources its own
# setup.sh to restore the route, port and desktop proxy. That file is gone with
# the bash client, so this leaves one in its place for that single source. It
# tells `proxy upgrade` that nothing is left to restore, then removes itself.
leave_setup_for_proxy_upgrade() {
	setup=$old_data/setup.sh
	quoted_setup=$(quote "$setup")
	quoted_dir=$(quote "$old_data")
	mkdir -p "$old_data"
	cat >"$setup" <<EOF
# install.sh moved this machine to sbc and left this file for the bash
# client's 'proxy upgrade', which sources it once. It removes itself.
case " \${FUNCNAME[*]-} \${funcstack[*]-} " in
*" _proxy_upgrade_client "*)
	route="" port="" was_active=true desktop_state=""
	_proxy_check_update() { :; }
	;;
esac
rm -f $quoted_setup
rmdir $quoted_dir 2>/dev/null || :
EOF
}

# forget_rc_lines removes the lines the bash client's installer added to rc
# files, matched the way its uninstaller matches them. That uninstaller edits
# with GNU sed options that fail on macOS, so this does the edit instead.
forget_rc_lines() {
	for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.bash_profile"; do
		if [ ! -f "$rc" ] || ! grep -q 'sing-box' "$rc"; then
			continue
		fi
		grep -v -F '# Network proxy management configuration (sing-box)' "$rc" |
			grep -v -E "(^|[[:space:]])(source|\\.)[[:space:]]+[\"']?.*sing-box/setup\\.sh([\"']|[[:space:]]|\$)" >"$work/rc" || true
		if ! cmp -s "$work/rc" "$rc"; then
			# cat keeps the file's mode and writes through a symlink.
			cat "$work/rc" >"$rc"
			say "Removed the bash client's lines from %s." "$rc"
		fi
	done
}

# warn_about_rc_lines names rc file lines that still load the bash client.
warn_about_rc_lines() {
	for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.zshrc" "$HOME/.profile"; do
		[ -f "$rc" ] || continue
		lines=$(grep -n -E '^[^#]*(sing-box/setup\.sh|proxy shell)' "$rc" 2>/dev/null | cut -d: -f1 | tr '\n' ' ') || true
		if [ -n "$lines" ]; then
			say '%s still runs the bash client on lines %s. Remove those lines.' "$rc" "${lines% }"
		fi
	done
}

# finish_removal deletes what the bash client's uninstaller left when it
# stopped after removing the client's services. On an NFS home, a file sing-box
# had open lingers for a moment after sing-box exits, so the uninstaller's
# `rm -rf` can find a directory that is not empty yet.
finish_removal() {
	for unit in "${XDG_CONFIG_HOME:-$HOME/.config}"/systemd/user/sing-box-*.service \
		"$HOME"/Library/LaunchAgents/io.sing-box.*.plist; do
		if [ -e "$unit" ]; then
			return 1
		fi
	done
	tries=0
	while :; do
		rm -rf "$old_config" "$old_data" "$old_state" \
			"$HOME/.local/bin/sing-box" "$HOME/.local/bin/libcronet.so" 2>/dev/null || true
		if [ ! -e "$old_config" ] && [ ! -e "$old_data" ] && [ ! -e "$old_state" ]; then
			return 0
		fi
		tries=$((tries + 1))
		if [ "$tries" -ge 10 ]; then
			return 1
		fi
		sleep 1
	done
}

move_from_bash_client() {
	say "Removing the bash client..."
	# Without SBM_UNINSTALL_CLEAN_PROXY the uninstaller keeps the Docker
	# settings, which keep working because sbc takes over the same port.
	if ! bash "$old_data/client-uninstall.sh" --yes --no-rc </dev/null; then
		if ! finish_removal; then
			fail "the bash client's uninstaller stopped. sbc is running; finish with 'bash %s', then 'sbc port %s'." "$old_data/client-uninstall.sh" "$old_port"
		fi
		say "The bash client's uninstaller stopped partway, so install.sh removed the rest of its files."
	fi
	if [ -z "$no_rc" ]; then
		forget_rc_lines
	fi
	forget_git_proxy "$old_port" ${listen_port:+"$listen_port"}
	take_over_port
	if [ "$old_desktop" = ours ] && ! "$sbc" desktop on </dev/null; then
		say "The bash client's desktop proxy is off now. Run 'sbc desktop on' to point the desktop at sbc."
	fi
	if [ -n "$from_proxy_upgrade" ]; then
		leave_setup_for_proxy_upgrade
	fi
	printf '\n' >&2
	say "sbc replaced the bash client. The proxy command went with it; type sbc instead."
	say "Shells opened before now still have the old 'proxy' function. Open a new shell."
	warn_about_rc_lines
	"$sbc" status </dev/null || true
}

main() {
	shell_name=""
	no_rc=""
	protocol=""
	from_proxy_upgrade=""
	while [ $# -gt 0 ]; do
		case "$1" in
		--shell)
			[ $# -ge 2 ] || fail '--shell needs bash or zsh'
			shell_name=$2
			shift 2
			;;
		--no-rc)
			no_rc=1
			shift
			;;
		-p | --protocol)
			[ $# -ge 2 ] || fail '%s needs a protocol' "$1"
			protocol=$2
			# The bash client's `proxy upgrade` always passes -p.
			from_proxy_upgrade=1
			shift 2
			;;
		-h | --help)
			usage
			exit 0
			;;
		*)
			usage >&2
			exit 2
			;;
		esac
	done

	case "$(uname -s)" in
	Linux) os=linux ;;
	Darwin) os=darwin ;;
	*) fail "sbc runs on Linux and macOS" ;;
	esac
	case "$(uname -m)" in
	x86_64 | amd64) arch=amd64 ;;
	aarch64 | arm64) arch=arm64 ;;
	*) fail "sbc runs on amd64 and arm64" ;;
	esac

	old_config=${XDG_CONFIG_HOME:-$HOME/.config}/sing-box
	old_data=${XDG_DATA_HOME:-$HOME/.local/share}/sing-box
	old_state=${XDG_STATE_HOME:-$HOME/.local/state}/sing-box
	moving=""
	if [ -f "$old_config/selected-protocol" ] || [ -f "$old_data/setup.sh" ]; then
		if tun_set_up; then
			fail "this machine set up TUN mode with the bash client. Keep using the bash client until sbc supports TUN."
		fi
		if [ ! -f "$old_data/client-uninstall.sh" ]; then
			fail 'the bash client is here, but its uninstaller is missing from %s. Remove the bash client by hand, then run this again.' "$old_data"
		fi
		moving=1
		read_bash_client
		old_desktop=$(bash_client_desktop)
	fi

	link=""
	if [ -n "$moving" ]; then
		link=$(trade_token) || link=""
		if [ -z "$link" ]; then
			say "The bash client's saved token did not get the subscription link. Paste the link from the portal's files page."
		fi
	fi
	if [ -z "$link" ]; then
		link=$(ask_link)
	fi
	link=$(printf '%s' "$link" | tr -d '[:space:]')
	link=${link%/}
	case "$link" in
	https://*/sub/* | http://*/sub/*) ;;
	*) fail "that is not a subscription link" ;;
	esac

	work=$(mktemp -d)
	trap 'rm -rf "$work"' EXIT HUP INT TERM
	sbc="$work/sbc"
	download_sbc

	set --
	if [ -n "$shell_name" ]; then
		set -- --shell "$shell_name"
	fi
	if [ -n "$no_rc" ]; then
		set -- "$@" --no-rc
	fi
	if [ -n "${route:-}" ]; then
		set -- "$@" --route "$route"
	fi
	if [ -n "$protocol" ]; then
		set -- "$@" --protocol "$protocol"
	fi
	if ! printf '%s\n' "$link" | "$sbc" install "$@"; then
		if [ -n "$moving" ]; then
			"$sbc" uninstall --yes </dev/null >/dev/null 2>&1 || true
			fail "sbc did not work here, so it is removed again. The bash client keeps running as before."
		fi
		exit 1
	fi
	if [ -n "$moving" ]; then
		move_from_bash_client
	fi
}

main "$@"
