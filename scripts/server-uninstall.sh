#!/usr/bin/env bash
set -eo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
source "${script_dir}/lib/ui.sh"
VERBOSE=false
_translate_zh() {
	case "$1" in
	'Run the server uninstaller as root.') printf '%s\n' '请以 root 身份运行服务端卸载脚本。' ;;
	'Removed the sing-box node service.') printf '%s\n' '已卸载 sing-box 节点服务。' ;;
	*) return 1 ;;
	esac
}
confirm=true
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return 0; fi
while [[ $# -gt 0 ]]; do
	case "$1" in
	-h | --help)
		printf '%s\n' 'Usage: ./server-uninstall.sh [--yes] [-V]'
		exit 0
		;;
	-y | --yes)
		confirm=false
		shift
		;;
	-V | --verbose)
		# shellcheck disable=SC2034
		VERBOSE=true
		shift
		;;
	*)
		error "Unknown argument: $1"
		exit 1
		;;
	esac
done
[[ $EUID -eq 0 ]] || {
	error "$(_t 'Run the server uninstaller as root.')"
	exit 1
}
if [[ "$confirm" == true ]]; then
	read -r -p 'Remove the sing-box node service, binary, config and state? [y/N] ' answer
	[[ "$answer" == y || "$answer" == Y ]] || exit 0
fi
root="${SBM_SERVER_ROOT:-}"
# Never glob sing-box-*: that also matches the portal and traffic services.
for unit in sing-box.service sing-box-trojan.service sing-box-hysteria2.service sing-box-naive.service; do
	systemctl disable --now "$unit" >/dev/null 2>&1 || true
	rm -f "${root}/etc/systemd/system/${unit}"
done
systemctl daemon-reload
rm -f "${root}/usr/local/bin/sing-box" "${root}/etc/sysctl.d/99-sing-box-hysteria2.conf"
rm -rf "${root}/usr/local/etc/sing-box" "${root}/var/lib/sing-box"
info "$(_t 'Removed the sing-box node service.')"
