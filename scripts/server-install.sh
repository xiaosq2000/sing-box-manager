#!/usr/bin/env bash
# Install one node config and migrate the three managed legacy units.
set -eo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
source "${script_dir}/lib/ui.sh"
VERBOSE=false
_translate_zh() {
	case "$1" in
	'Run the server installer as root.') printf '%s\n' '请以 root 身份运行服务端安装脚本。' ;;
	'Missing or unsupported protocol') printf '%s\n' '协议缺失或不受支持' ;;
	'Installation failed; restored the previous files and service state.') printf '%s\n' '安装失败；已恢复之前的文件和服务状态。' ;;
	'sing-box check failed; no services were changed.') printf '%s\n' 'sing-box 配置检查失败；未更改任何服务。' ;;
	'Installed sing-box.service with one node config.') printf '%s\n' '已安装使用单一节点配置的 sing-box.service。' ;;
	'Rollback failed; recovery files retained at') printf '%s\n' '回滚失败；恢复文件保留于' ;;
	*) return 1 ;;
	esac
}
protocols=()
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return 0; fi
while [[ $# -gt 0 ]]; do
	case "$1" in
	-h | --help)
		printf '%s\n' 'Usage: ./server-install.sh [-p trojan|hysteria2|naive]... [-V]' 'Without -p, installs all packaged protocol inbounds in sing-box.service.'
		exit 0
		;;
	-p | --protocol)
		case "${2:-}" in
		trojan | hysteria2 | naive) protocols+=("$2") ;;
		*)
			error "$(_t 'Missing or unsupported protocol')"
			exit 1
			;;
		esac
		shift 2
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
if [[ $EUID -ne 0 ]]; then
	error "$(_t 'Run the server installer as root.')"
	exit 1
fi
command -v systemctl >/dev/null
command -v python3 >/dev/null
# An override confines fixture tests to a scratch filesystem.
root="${SBM_SERVER_ROOT:-}"
prefix="${root}/usr/local"
units="${root}/etc/systemd/system"
state="${root}/var/lib/sing-box"
config_dir="${prefix}/etc/sing-box"
binary="${prefix}/bin/sing-box"
unit='sing-box.service'
legacy=('sing-box-trojan.service' 'sing-box-hysteria2.service' 'sing-box-naive.service')
for file in sing-box config.json sing-box.service; do
	[[ -f "${script_dir}/${file}" ]] || {
		error "Installation file missing: ${file}"
		exit 1
	}
done
mkdir -p "${config_dir}" "${prefix}/bin" "${units}" "${state}"
chmod 700 "${config_dir}"
work=$(mktemp -d "${config_dir}/.install.XXXXXXXX")
active=()
enabled=()
changed=false
success=false
rollback() {
	local status=$?
	local rollback_failed=false
	if [[ "$changed" == true && "$success" != true ]]; then
		if [[ -f "${units}/${unit}" ]]; then
			systemctl stop "$unit" >/dev/null 2>&1 || rollback_failed=true
			systemctl disable "$unit" >/dev/null 2>&1 || rollback_failed=true
		fi
		for name in binary config unit; do
			case "$name" in
			binary) target="$binary" ;;
			config) target="${config_dir}/config.json" ;;
			unit) target="${units}/${unit}" ;;
			esac
			if [[ -f "${work}/${name}.old" ]]; then
				cp -p "${work}/${name}.old" "$target" || rollback_failed=true
			else
				rm -f "$target" || rollback_failed=true
			fi
		done
		for old in "${legacy[@]}"; do
			if [[ -f "${work}/${old}" ]]; then
				cp -p "${work}/${old}" "${units}/${old}" || rollback_failed=true
			fi
		done
		systemctl daemon-reload || rollback_failed=true
		for old in "${enabled[@]}"; do systemctl enable "$old" || rollback_failed=true; done
		for old in "${active[@]}"; do systemctl restart "$old" || rollback_failed=true; done
		if [[ "$rollback_failed" == true ]]; then
			error "$(_t 'Rollback failed; recovery files retained at'): ${work}"
			return "$status"
		fi
		error "$(_t 'Installation failed; restored the previous files and service state.')"
	fi
	rm -rf "$work"
	return "$status"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cp "${script_dir}/sing-box" "${work}/sing-box"
chmod 755 "${work}/sing-box"
python3 - "${script_dir}/config.json" "${work}/config.json" "${protocols[@]}" <<'PY'
import json, os, sys
with open(sys.argv[1], encoding='utf-8') as source:
    config = json.load(source)
if sys.argv[3:]:
    selected = set(sys.argv[3:])
    config['inbounds'] = [entry for entry in config['inbounds'] if entry['type'] in selected]
    if {entry['type'] for entry in config['inbounds']} != selected:
        missing = sorted(selected - {entry['type'] for entry in config['inbounds']})
        raise SystemExit('Selected protocol missing from server config: ' + ', '.join(missing))
fd = os.open(sys.argv[2], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as output:
    json.dump(config, output)
PY
# Check the real node TLS paths before stopping any service. Do not print
# checker output, which can contain credentials from a rejected config.
if ! "${work}/sing-box" check -D "$state" -c "${work}/config.json" >"${work}/check.log" 2>&1; then
	error "$(_t 'sing-box check failed; no services were changed.')"
	exit 1
fi
[[ ! -f "$binary" ]] || cp -p "$binary" "${work}/binary.old"
[[ ! -f "${config_dir}/config.json" ]] || cp -p "${config_dir}/config.json" "${work}/config.old"
[[ ! -f "${units}/${unit}" ]] || cp -p "${units}/${unit}" "${work}/unit.old"
for old in "$unit" "${legacy[@]}"; do
	if systemctl is-active --quiet "$old"; then active+=("$old"); fi
	if systemctl is-enabled --quiet "$old"; then enabled+=("$old"); fi
	if [[ "$old" != "$unit" && -f "${units}/${old}" ]]; then cp -p "${units}/${old}" "${work}/${old}"; fi
done
changed=true
for old in "${active[@]}"; do systemctl stop "$old"; done
for old in "${legacy[@]}"; do
	if [[ -f "${units}/${old}" ]]; then
		systemctl disable "$old"
		rm -f "${units}/${old}"
	fi
done
mv -f "${work}/sing-box" "$binary"
mv -f "${work}/config.json" "${config_dir}/config.json"
install -m 644 "${script_dir}/sing-box.service" "${units}/${unit}"
# Hysteria2 keeps the same QUIC buffer tuning in the consolidated process.
if [[ ${#protocols[@]} -eq 0 || " ${protocols[*]} " == *' hysteria2 '* ]]; then
	mkdir -p "${root}/etc/sysctl.d"
	printf '%s\n' '# Managed by sing-box-manager.' 'net.core.rmem_max=16777216' 'net.core.wmem_max=16777216' >"${root}/etc/sysctl.d/99-sing-box-hysteria2.conf"
	if [[ -z "$root" ]]; then sysctl -p /etc/sysctl.d/99-sing-box-hysteria2.conf >/dev/null 2>&1 || true; fi
fi
systemctl daemon-reload
systemctl enable "$unit"
systemctl restart "$unit"
sleep 1
systemctl is-active --quiet "$unit"
success=true
# Legacy configs are removed only after the new service passes its health check.
for protocol in trojan hysteria2 naive; do rm -rf "${config_dir:?}/${protocol}"; done
info "$(_t 'Installed sing-box.service with one node config.')"
