#!/usr/bin/env bash
set -eo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
# shellcheck source=/dev/null
source "${script_dir}/lib/ui.sh"
# shellcheck source=/dev/null
source "${script_dir}/lib/platform.sh"
# shellcheck source=/dev/null
source "${script_dir}/lib/system-proxy.sh"
# shellcheck source=/dev/null
source "${script_dir}/lib/tun.sh"

# Domain translations for zh_CN output.
# A case lookup instead of an associative array keeps this compatible with
# bash 3.2, the stock /bin/bash on macOS.
_translate_zh() {
	case "$1" in
	"Uninstallation Script") printf '%s\n' "卸载脚本" ;;
	"ERROR") printf '%s\n' "错误" ;;
	"WARNING") printf '%s\n' "警告" ;;
	"INFO") printf '%s\n' "信息" ;;
	"DEBUG") printf '%s\n' "调试" ;;
	"SUCCESS") printf '%s\n' "成功" ;;
	"FAILED") printf '%s\n' "失败" ;;
	"Administrator privileges required") printf '%s\n' "需要管理员权限" ;;
	"Please enter your password") printf '%s\n' "请输入您的密码" ;;
	"Uninstallation cancelled") printf '%s\n' "卸载已取消" ;;
	"This script will completely remove sing-box client files and services") printf '%s\n' "此脚本将完全删除 sing-box 客户端文件与服务" ;;
	"Detecting shell environment") printf '%s\n' "检测 Shell 环境" ;;
	"Configured shell") printf '%s\n' "配置的 Shell" ;;
	"Some lines in shell RC file") printf '%s\n' "Shell RC 文件中的部分配置" ;;
	"Shell RC cleanup disabled") printf '%s\n' "Shell 配置文件清理已禁用" ;;
	"Removing sing-box lines from RC file") printf '%s\n' "从配置文件中移除 sing-box 配置" ;;
	"Shell RC cleanup complete") printf '%s\n' "Shell 配置文件清理完成" ;;
	"No shell RC file found") printf '%s\n' "未找到 Shell 配置文件" ;;
	"RC file has no sing-box configuration") printf '%s\n' "配置文件中没有 sing-box 配置" ;;
	"Stopping and disabling user services") printf '%s\n' "停止并禁用用户服务" ;;
	"Stopping and removing launch agents") printf '%s\n' "停止并移除启动代理" ;;
	"Reverting the system proxy") printf '%s\n' "还原系统代理" ;;
	"System proxy") printf '%s\n' "系统代理" ;;
	"Failed to revert the system proxy. Clear it in your system network settings.") printf '%s\n' "还原系统代理失败，请在系统网络设置中手动清除。" ;;
	"The system proxy points somewhere else and was left unchanged.") printf '%s\n' "系统代理指向其他地址，已保持不变。" ;;
	"Launch agent") printf '%s\n' "启动代理" ;;
	"Removing log directory") printf '%s\n' "移除日志目录" ;;
	"Logs") printf '%s\n' "日志" ;;
	"Removing user service files") printf '%s\n' "移除用户服务文件" ;;
	"Reloading user systemd") printf '%s\n' "重新加载用户 systemd" ;;
	"Removing binary") printf '%s\n' "移除二进制文件" ;;
	"Removing runtime library") printf '%s\n' "移除运行库" ;;
	"Removing configuration") printf '%s\n' "移除配置文件" ;;
	"Removing data directory") printf '%s\n' "移除数据目录" ;;
	"Removing state directory") printf '%s\n' "移除状态目录" ;;
	"Removing legacy system installation") printf '%s\n' "移除旧的系统级安装" ;;
	"Nothing to uninstall") printf '%s\n' "没有需要卸载的内容" ;;
	"Are you sure you want to continue?") printf '%s\n' "确定要继续吗？" ;;
	"Usage") printf '%s\n' "用法" ;;
	"Examples") printf '%s\n' "示例" ;;
	"Options") printf '%s\n' "选项" ;;
	"Display help messages") printf '%s\n' "显示帮助信息" ;;
	"Debug logging") printf '%s\n' "调试日志" ;;
	"Specify which shell RC file to clean") printf '%s\n' "指定要清理的 Shell 配置文件" ;;
	"Skip shell RC cleanup") printf '%s\n' "跳过 Shell 配置文件清理" ;;
	"Skip confirmation prompt") printf '%s\n' "跳过确认提示" ;;
	"Remove using auto-detected shell RC files") printf '%s\n' "使用自动检测的 Shell 配置文件进行卸载" ;;
	"Remove using a specific shell RC file") printf '%s\n' "使用指定的 Shell 配置文件进行卸载" ;;
	"Remove without modifying shell configuration") printf '%s\n' "卸载但不修改 Shell 配置" ;;
	"Unknown argument") printf '%s\n' "未知参数" ;;
	"Missing value for option") printf '%s\n' "选项缺少值" ;;
	"Unsupported shell") printf '%s\n' "不支持的 Shell" ;;
	"Supported shells") printf '%s\n' "支持的 Shell" ;;
	"Run this script as the target user without sudo") printf '%s\n' "请以目标用户身份直接运行此脚本，不要使用 sudo" ;;
	"Uninstallation complete") printf '%s\n' "卸载完成" ;;
	"sing-box client files have been fully removed") printf '%s\n' "sing-box 客户端文件已被完全移除" ;;
	"User service") printf '%s\n' "用户服务" ;;
	"Legacy system service") printf '%s\n' "旧的系统级服务" ;;
	"Binary") printf '%s\n' "二进制文件" ;;
	"Runtime library") printf '%s\n' "运行库" ;;
	"Legacy binary") printf '%s\n' "旧的系统级二进制文件" ;;
	"Config") printf '%s\n' "配置" ;;
	"Legacy config") printf '%s\n' "旧的系统级配置" ;;
	"Data") printf '%s\n' "数据" ;;
	"State") printf '%s\n' "状态" ;;
	"Legacy data") printf '%s\n' "旧的系统级数据" ;;
	"Skip privileged TUN cleanup") printf '%s\n' "跳过特权 TUN 清理" ;;
	"Remove without touching machine-wide TUN mode") printf '%s\n' "卸载但不改动全局 TUN 模式" ;;
	"Machine-wide TUN") printf '%s\n' "全局 TUN" ;;
	"Removing machine-wide TUN mode") printf '%s\n' "正在移除全局 TUN 模式" ;;
	"Machine-wide TUN mode preserved") printf '%s\n' "已保留全局 TUN 模式" ;;
	"Machine-wide TUN mode is owned by another local user (uid {})") printf '%s\n' "全局 TUN 模式属于另一位本地用户（uid {}）" ;;
	"Machine-wide TUN mode was left in place") printf '%s\n' "已保留全局 TUN 模式" ;;
	"The TUN service did not stop; nothing was removed") printf '%s\n' "TUN 服务未能停止，未删除任何内容" ;;
	"TUN interface, route, or rule artifacts are still present; nothing was removed") printf '%s\n' "仍残留 TUN 接口、路由或策略规则，未删除任何内容" ;;
	"Uninstallation incomplete") printf '%s\n' "卸载未完成" ;;
	"Proxy settings") printf '%s\n' "代理设置" ;;
	"Client-owned global Git, Docker, and current shell settings") printf '%s\n' "属于此客户端的全局 Git、Docker 和当前终端代理设置" ;;
	"Machine-wide TUN mode removed") printf '%s\n' "全局 TUN 模式已移除" ;;
	"Another TUN operation holds the transaction lock") printf '%s\n' "另一个 TUN 操作正持有事务锁" ;;
	"sudo is required to remove machine-wide TUN mode") printf '%s\n' "移除全局 TUN 模式需要 sudo" ;;
	*) return 1 ;;
	esac
}

UNINSTALL_COMMAND="./client-uninstall.sh"
if [[ "${SBM_UNINSTALL_CLEAN_PROXY:-}" == 1 ]]; then
	UNINSTALL_COMMAND="proxy uninstall"
fi
CONFIGURE_RC=true
PRESERVE_TUN=false
VERBOSE=false
SKIP_CONFIRM=false
SUPPORTED_SHELLS="bash zsh"
SHELL_OVERRIDE=""
rc_cleanup_targets=()

detect_target_user() {
	TARGET_USER="${USER}"
	TARGET_HOME="${HOME}"
	export TARGET_USER TARGET_HOME
}

_client_bin_dir() {
	printf '%s/.local/bin\n' "${TARGET_HOME:-$HOME}"
}

_client_default_config_home() {
	printf '%s/.config\n' "${TARGET_HOME:-$HOME}"
}

_client_current_config_home() {
	printf '%s\n' "${XDG_CONFIG_HOME:-$(_client_default_config_home)}"
}

_client_default_data_home() {
	printf '%s/.local/share\n' "${TARGET_HOME:-$HOME}"
}

_client_current_data_home() {
	printf '%s\n' "${XDG_DATA_HOME:-$(_client_default_data_home)}"
}

_client_default_state_home() {
	printf '%s/.local/state\n' "${TARGET_HOME:-$HOME}"
}

_client_current_state_home() {
	printf '%s\n' "${XDG_STATE_HOME:-$(_client_default_state_home)}"
}

_client_launch_agent_dir() {
	printf '%s/Library/LaunchAgents\n' "${TARGET_HOME:-$HOME}"
}

_client_log_dir() {
	printf '%s/Library/Logs/sing-box\n' "${TARGET_HOME:-$HOME}"
}

# macOS bash login shells read .bash_profile, not .bashrc.
_client_rc_path_for_shell() {
	local target_shell="$1"
	local user_home="${TARGET_HOME:-$HOME}"

	case "$target_shell" in
	bash)
		if plat_is_macos; then
			printf '%s/.bash_profile\n' "$user_home"
		else
			printf '%s/.bashrc\n' "$user_home"
		fi
		;;
	zsh)
		printf '%s/.zshrc\n' "$user_home"
		;;
	esac
}

_client_rc_has_sing_box_config() {
	local rc_file="$1"

	[[ -f "$rc_file" ]] || return 1
	grep -q "sing-box" "$rc_file" 2>/dev/null
}

_client_collect_rc_cleanup_targets() {
	local target_home="${TARGET_HOME:-$HOME}"
	local target_rc_file=""
	local -a candidate_rc_files=()

	rc_cleanup_targets=()

	if [[ "$CONFIGURE_RC" != "true" ]]; then
		return 0
	fi

	if [[ -n "$SHELL_OVERRIDE" ]]; then
		target_rc_file="$(_client_rc_path_for_shell "$SHELL_OVERRIDE")"
		if _client_rc_has_sing_box_config "$target_rc_file"; then
			rc_cleanup_targets+=("$target_rc_file")
		fi
		return 0
	fi

	candidate_rc_files=("${target_home}/.bashrc" "${target_home}/.zshrc")
	if plat_is_macos; then
		candidate_rc_files+=("${target_home}/.bash_profile")
	fi

	for target_rc_file in "${candidate_rc_files[@]}"; do
		if _client_rc_has_sing_box_config "$target_rc_file"; then
			rc_cleanup_targets+=("$target_rc_file")
		fi
	done
}

detect_shell() {
	local shell_name="${SHELL_OVERRIDE:-}"
	local rc_file=""
	local user_home="${TARGET_HOME:-$HOME}"
	local user_name="${TARGET_USER:-$USER}"
	local passwd_entry=""
	local passwd_shell=""

	if [[ -n "$shell_name" ]]; then
		:
	elif command -v getent >/dev/null 2>&1; then
		passwd_entry=$(getent passwd "$user_name" 2>/dev/null || true)
		if [[ -n "$passwd_entry" ]]; then
			IFS=':' read -r _ _ _ _ _ _ passwd_shell <<<"$passwd_entry"
			if [[ -n "$passwd_shell" ]]; then
				shell_name="${passwd_shell##*/}"
			fi
		fi
	elif command -v dscl >/dev/null 2>&1; then
		# macOS has no getent; the directory service holds the login shell.
		passwd_shell=$(dscl . -read "/Users/${user_name}" UserShell 2>/dev/null | awk '{print $2}')
		if [[ -n "$passwd_shell" ]]; then
			shell_name="${passwd_shell##*/}"
		fi
	fi

	if [[ -z "$shell_name" ]] && [[ -n "${SHELL:-}" ]]; then
		shell_name="${SHELL##*/}"
	fi

	case "$shell_name" in
	bash | zsh)
		rc_file="$(_client_rc_path_for_shell "$shell_name")"
		;;
	*)
		if [[ -f "${user_home}/.zshrc" ]]; then
			shell_name="zsh"
			rc_file="${user_home}/.zshrc"
		elif [[ -f "$(_client_rc_path_for_shell bash)" ]]; then
			shell_name="bash"
			rc_file="$(_client_rc_path_for_shell bash)"
		fi
		;;
	esac

	echo "$shell_name:$rc_file"
}

clean_shell_rc() {
	local rc_file="$1"

	if [[ ! -f "$rc_file" ]]; then
		return 0
	fi

	if ! grep -q "sing-box" "$rc_file" 2>/dev/null; then
		return 0
	fi

	debug "$(_t 'Removing sing-box lines from RC file'): $(display_path "$rc_file")"

	local tmp_file
	local setup_source_pattern='(^|[[:space:]])(source|\.)[[:space:]]+["'"'"']?.*sing-box/setup\.sh(["'"'"']|[[:space:]]|$)'
	tmp_file=$(mktemp)
	grep -v "# Network proxy management configuration (sing-box)" "$rc_file" |
		grep -Ev "$setup_source_pattern" \
			>"$tmp_file"

	sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' "$tmp_file"
	cp "$tmp_file" "$rc_file"
	rm -f "$tmp_file"

	step_ok "$(_t 'Shell RC cleanup complete'): $(display_path "$rc_file")"
	return 0
}

# Machine-wide TUN cleanup (docs/TUN_MODE.md section 11.2).
#
# Removes exactly the paths this feature records and nothing else: no glob over
# /etc/systemd/system, no /usr/local/bin/sing-box, no whole-directory sweep of
# /etc. If the service will not stop or its routing artifacts survive, every
# managed file stays put and the uninstall reports itself incomplete -- deleting
# the management path while a root-managed tunnel may still be running would
# leave no way to turn it off.

_client_tun_present() {
	local status_value

	if tun_unit_installed || tun_transaction_in_progress; then
		return 0
	fi

	# No unit and no transaction: only an attestation that routing may still be
	# unclean counts. A `safe-off` marker left by a rolled-back transaction is
	# not something to escalate for.
	status_value=$(tun_status)
	case "$status_value" in
	"" | safe-off)
		return 1
		;;
	esac

	return 0
}

_client_tun_authenticate_sudo() {
	if [[ $EUID -eq 0 ]]; then
		return 0
	fi

	if ! command -v sudo >/dev/null 2>&1; then
		error "$(_t 'sudo is required to remove machine-wide TUN mode')"
		return 1
	fi

	if sudo -n true 2>/dev/null; then
		return 0
	fi

	echo
	echo "${BOLD}${YELLOW}${LOCK} $(_t 'Administrator privileges required')${RESET}"
	echo "$(_t 'Please enter your password'):"
	sudo -v
}

_client_tun_wait_inactive() {
	local attempt=0

	while ((attempt < 15)); do
		case "$(tun_unit_active_state 2>/dev/null)" in
		inactive | failed | "")
			return 0
			;;
		esac
		attempt=$((attempt + 1))
		sleep 1
	done

	return 1
}

_client_remove_tun_installation() {
	local owner_uid account_created msg userdel_command

	if ! _client_tun_authenticate_sudo; then
		return 1
	fi

	if ! tun_acquire_lock; then
		error "$(_t 'Another TUN operation holds the transaction lock')"
		return 1
	fi

	owner_uid=$(tun_manifest_value owner_uid 2>/dev/null || true)
	if [[ -n "$owner_uid" ]] && [[ "$owner_uid" != "$(id -u)" ]]; then
		msg=$(_t 'Machine-wide TUN mode is owned by another local user (uid {})')
		error "${msg//\{\}/$owner_uid}"
		tun_release_lock
		return 1
	fi

	# Recorded before the manifest goes away, because it is the only proof that
	# this client created the account rather than adopting an existing one.
	account_created=$(tun_manifest_value runtime_account_created 2>/dev/null || true)

	tun_run_privileged systemctl disable "$(tun_unit_name)" >/dev/null 2>&1 || true
	tun_run_privileged systemctl stop "$(tun_unit_name)" >/dev/null 2>&1 || true

	if ! _client_tun_wait_inactive; then
		error "$(_t 'The TUN service did not stop; nothing was removed')"
		tun_release_lock
		return 1
	fi

	if tun_network_artifacts_present; then
		error "$(_t 'TUN interface, route, or rule artifacts are still present; nothing was removed')"
		tun_release_lock
		return 1
	fi

	tun_run_privileged rm -f "$(tun_unit_path)" >/dev/null 2>&1 || true
	tun_run_privileged rm -f "$(tun_binary_path)" "$(tun_cronet_path)" >/dev/null 2>&1 || true
	tun_run_privileged rm -f "$(tun_config_path)" "$(tun_manifest_path)" >/dev/null 2>&1 || true
	tun_run_privileged rm -rf "$(tun_rules_dir)" >/dev/null 2>&1 || true
	# rmdir, not rm -rf: anything else living in these directories is not ours.
	tun_run_privileged rmdir "$(tun_libexec_dir)" >/dev/null 2>&1 || true
	tun_run_privileged rmdir "$(tun_config_dir)" >/dev/null 2>&1 || true
	tun_run_privileged rmdir "${SBM_TUN_ROOT:-}/etc/sing-box-manager" >/dev/null 2>&1 || true
	tun_run_privileged rm -f "$(tun_transaction_marker_path)" "$(tun_status_path)" \
		>/dev/null 2>&1 || true

	tun_run_privileged systemctl daemon-reload >/dev/null 2>&1 || true
	tun_run_privileged systemctl reset-failed "$(tun_unit_name)" >/dev/null 2>&1 || true

	if [[ "$account_created" == "true" ]] && ! tun_unit_installed; then
		tun_run_privileged rm -rf "$(tun_state_dir)" >/dev/null 2>&1 || true
		if userdel_command=$(tun_resolve_admin_command userdel); then
			tun_run_privileged "$userdel_command" "$(tun_runtime_account)" \
				>/dev/null 2>&1 || true
		fi
	fi

	# Holds the lock itself, so this is deliberately the last removal.
	tun_run_privileged rm -rf "$(tun_control_dir)" >/dev/null 2>&1 || true
	tun_release_lock
	return 0
}

run_with_sudo() {
	if [[ $EUID -eq 0 ]]; then
		"$@"
		return
	fi

	if sudo -n true 2>/dev/null; then
		sudo "$@"
	else
		echo
		echo "${BOLD}${YELLOW}${LOCK} $(_t 'Administrator privileges required')${RESET}"
		echo "$(_t 'Please enter your password'):"
		if ! sudo "$@"; then
			error "$(_t 'Uninstallation cancelled')"
			exit 1
		fi
	fi
}

_client_require_non_root() {
	if [[ $EUID -eq 0 || -n "${SUDO_USER:-}" ]]; then
		error "$(_t 'Run this script as the target user without sudo')"
		exit 1
	fi
}

_uninstall_require_option_value() {
	local option="$1"
	local value="${2:-}"

	if [[ -z "$value" || "$value" == -* ]]; then
		error "$(_t 'Missing value for option'): ${option}"
		usage
		exit 1
	fi
}

_uninstall_validate_shell_name() {
	local shell_name="$1"

	case "$shell_name" in
	bash | zsh)
		return 0
		;;
	*)
		error "$(_t 'Unsupported shell'): ${shell_name}. $(_t 'Supported shells'): ${SUPPORTED_SHELLS}"
		usage
		return 1
		;;
	esac
}

_uninstall_print_target() {
	local label="$1"
	local value="$2"

	printf '%s%s %s: %s\n' "$INDENT$INDENT" "$BULLET" "$(_t "$label")" "$value"
}

usage() {
	echo
	echo "  ${BOLD}${CYAN}${UNDERLINE}Sing-box VPN Client Uninstaller${RESET}"
	echo
	echo "  ${BOLD}$(_t 'Usage')${RESET}"
	echo "${INDENT}${GREEN}${BOLD}${UNINSTALL_COMMAND} [options]${RESET}"
	echo
	echo "  ${BOLD}$(_t 'Examples')${RESET}"
	echo "${INDENT}${GREEN}${BOLD}${UNINSTALL_COMMAND}${RESET}                   $(_t 'Remove using auto-detected shell RC files')"
	echo "${INDENT}${GREEN}${BOLD}${UNINSTALL_COMMAND} --shell bash${RESET}    $(_t 'Remove using a specific shell RC file')"
	echo "${INDENT}${GREEN}${BOLD}${UNINSTALL_COMMAND} --no-rc${RESET}         $(_t 'Remove without modifying shell configuration')"
	echo "${INDENT}${GREEN}${BOLD}${UNINSTALL_COMMAND} --preserve-tun${RESET}  $(_t 'Remove without touching machine-wide TUN mode')"
	echo
	echo "  ${BOLD}$(_t 'Options')${RESET}"
	echo "${INDENT}${GREEN}${BOLD}-h, --help${RESET}                  $(_t 'Display help messages')"
	echo "${INDENT}${GREEN}${BOLD}-V, --verbose${RESET}               $(_t 'Debug logging')"
	echo "${INDENT}${GREEN}${BOLD}-y, --yes${RESET}                   $(_t 'Skip confirmation prompt')"
	echo "${INDENT}${GREEN}${BOLD}--shell SHELL${RESET}               $(_t 'Specify which shell RC file to clean')"
	echo "${INDENT}${GREEN}${BOLD}--no-rc${RESET}                     $(_t 'Skip shell RC cleanup')"
	echo "${INDENT}${GREEN}${BOLD}--preserve-tun${RESET}              $(_t 'Skip privileged TUN cleanup')"
	echo
	echo "${INDENT}${DIM}$(_t 'Supported shells'): ${SUPPORTED_SHELLS}${RESET}"
	echo
}

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
	return 0
fi

while [[ $# -gt 0 ]]; do
	case "$1" in
	-h | --help)
		usage
		exit 0
		;;
	-V | --verbose)
		# shellcheck disable=SC2034
		VERBOSE=true
		shift 1
		;;
	-y | --yes)
		SKIP_CONFIRM=true
		shift 1
		;;
	--shell)
		_uninstall_require_option_value "$1" "${2:-}"
		SHELL_OVERRIDE="${2##*/}"
		if ! _uninstall_validate_shell_name "$SHELL_OVERRIDE"; then
			exit 1
		fi
		shift 2
		;;
	--no-rc)
		CONFIGURE_RC=false
		shift 1
		;;
	--preserve-tun)
		PRESERVE_TUN=true
		shift 1
		;;
	*)
		error "$(_t 'Unknown argument'): $1"
		usage
		exit 1
		;;
	esac
done

_client_require_non_root
detect_target_user

USER_BIN_DIR=$(_client_bin_dir)
CURRENT_CONFIG_HOME=$(_client_current_config_home)
DEFAULT_CONFIG_HOME=$(_client_default_config_home)
CURRENT_DATA_HOME=$(_client_current_data_home)
DEFAULT_DATA_HOME=$(_client_default_data_home)
CURRENT_STATE_HOME=$(_client_current_state_home)
DEFAULT_STATE_HOME=$(_client_default_state_home)

user_service_dirs=("${CURRENT_CONFIG_HOME}/systemd/user")
if [[ "${DEFAULT_CONFIG_HOME}" != "${CURRENT_CONFIG_HOME}" ]]; then
	user_service_dirs+=("${DEFAULT_CONFIG_HOME}/systemd/user")
fi

client_config_roots=("${CURRENT_CONFIG_HOME}/sing-box")
if [[ "${DEFAULT_CONFIG_HOME}" != "${CURRENT_CONFIG_HOME}" ]]; then
	client_config_roots+=("${DEFAULT_CONFIG_HOME}/sing-box")
fi

client_data_roots=("${CURRENT_DATA_HOME}/sing-box")
if [[ "${DEFAULT_DATA_HOME}" != "${CURRENT_DATA_HOME}" ]]; then
	client_data_roots+=("${DEFAULT_DATA_HOME}/sing-box")
fi

client_state_roots=("${CURRENT_STATE_HOME}/sing-box")
if [[ "${DEFAULT_STATE_HOME}" != "${CURRENT_STATE_HOME}" ]]; then
	client_state_roots+=("${DEFAULT_STATE_HOME}/sing-box")
fi

user_services=()
for service_dir in "${user_service_dirs[@]}"; do
	for service_path in "${service_dir}/sing-box-"*.service; do
		if [[ -e "${service_path}" ]]; then
			user_services+=("${service_path}")
		fi
	done
done

launch_agents=()
if plat_is_macos; then
	for plist_path in "$(_client_launch_agent_dir)"/io.sing-box.*.plist; do
		if [[ -e "${plist_path}" ]]; then
			launch_agents+=("${plist_path}")
		fi
	done
fi

has_user_logs=false
if plat_is_macos && [[ -d "$(_client_log_dir)" ]]; then
	has_user_logs=true
fi

# Only revert a system proxy this client set. Clearing one the user configured
# for something else would be worse than leaving ours behind.
system_proxy_target=""
system_proxy_state="off"
if system_proxy_target=$(system_proxy_target_name); then
	system_proxy_state=$(system_proxy_classify "$system_proxy_target")
else
	system_proxy_target=""
fi

has_system_proxy=false
if [[ "$system_proxy_state" == "ours" ]]; then
	has_system_proxy=true
fi

# Exact names, never a glob: /etc/systemd/system also holds the server package's
# units and the machine-wide TUN singleton, and neither is this client's to
# remove.
legacy_services=()
for legacy_protocol in trojan hysteria2 naive; do
	service_path="/etc/systemd/system/sing-box-${legacy_protocol}.service"
	if [[ -e "${service_path}" ]]; then
		legacy_services+=("${service_path}")
	fi
done

has_user_binary=false
has_user_cronet=false
has_user_config=false
has_user_data=false
has_user_state=false
[[ -f "${USER_BIN_DIR}/sing-box" ]] && has_user_binary=true
[[ -f "${USER_BIN_DIR}/libcronet.so" ]] && has_user_cronet=true

for path in "${client_config_roots[@]}"; do
	if [[ -d "$path" ]]; then
		has_user_config=true
		break
	fi
done

for path in "${client_data_roots[@]}"; do
	if [[ -d "$path" ]]; then
		has_user_data=true
		break
	fi
done

for path in "${client_state_roots[@]}"; do
	if [[ -d "$path" ]]; then
		has_user_state=true
		break
	fi
done

# These paths are shared with the server package and with third-party installs,
# so their existence alone proves nothing. One of this client's own legacy units
# has to be present before any of them is claimed.
has_legacy_binary=false
has_legacy_config=false
has_legacy_data=false
if [[ ${#legacy_services[@]} -gt 0 ]]; then
	[[ -f "/usr/local/bin/sing-box" ]] && has_legacy_binary=true
	[[ -d "/usr/local/etc/sing-box" ]] && has_legacy_config=true
	[[ -d "/var/lib/sing-box" ]] && has_legacy_data=true
fi

has_tun=false
if _client_tun_present; then
	if [[ "$PRESERVE_TUN" == "true" ]]; then
		step_warn "$(_t 'Machine-wide TUN mode preserved')"
	else
		has_tun=true
	fi
fi

_client_collect_rc_cleanup_targets
has_rc_targets=false
if [[ ${#rc_cleanup_targets[@]} -gt 0 ]]; then
	has_rc_targets=true
fi

if [[ ${#user_services[@]} -eq 0 ]] && [[ ${#legacy_services[@]} -eq 0 ]] &&
	[[ ${#launch_agents[@]} -eq 0 ]] && [[ "$has_user_logs" == "false" ]] &&
	[[ "$has_system_proxy" == "false" ]] &&
	[[ "$has_user_binary" == "false" ]] && [[ "$has_user_config" == "false" ]] &&
	[[ "$has_user_cronet" == "false" ]] &&
	[[ "$has_user_data" == "false" ]] && [[ "$has_user_state" == "false" ]] &&
	[[ "$has_legacy_binary" == "false" ]] && [[ "$has_legacy_config" == "false" ]] &&
	[[ "$has_legacy_data" == "false" ]] && [[ "$has_rc_targets" == "false" ]] &&
	[[ "$has_tun" == "false" ]]; then
	step_ok "$(_t 'Nothing to uninstall')"
	exit 0
fi

step_warn "$(_t 'This script will completely remove sing-box client files and services')"
if [[ "${SBM_UNINSTALL_CLEAN_PROXY:-}" == 1 ]]; then
	_uninstall_print_target "Proxy settings" "$(_t 'Client-owned global Git, Docker, and current shell settings')"
fi
for service_path in "${user_services[@]}"; do
	_uninstall_print_target "User service" "$(basename "${service_path}")"
done
for plist_path in "${launch_agents[@]}"; do
	_uninstall_print_target "Launch agent" "$(basename "${plist_path}")"
done
for service_path in "${legacy_services[@]}"; do
	_uninstall_print_target "Legacy system service" "$(basename "${service_path}")"
done
[[ "$has_user_binary" == "true" ]] && _uninstall_print_target "Binary" "$(display_path "${USER_BIN_DIR}/sing-box")"
[[ "$has_user_cronet" == "true" ]] && _uninstall_print_target "Runtime library" "$(display_path "${USER_BIN_DIR}/libcronet.so")"
[[ "$has_legacy_binary" == "true" ]] && _uninstall_print_target "Legacy binary" "$(display_path "/usr/local/bin/sing-box")"
[[ "$has_user_config" == "true" ]] && _uninstall_print_target "Config" "$(display_path "${CURRENT_CONFIG_HOME}/sing-box")"
[[ "$has_legacy_config" == "true" ]] && _uninstall_print_target "Legacy config" "$(display_path "/usr/local/etc/sing-box")"
[[ "$has_user_data" == "true" ]] && _uninstall_print_target "Data" "$(display_path "${CURRENT_DATA_HOME}/sing-box")"
[[ "$has_user_state" == "true" ]] && _uninstall_print_target "State" "$(display_path "${CURRENT_STATE_HOME}/sing-box")"
[[ "$has_user_logs" == "true" ]] && _uninstall_print_target "Logs" "$(display_path "$(_client_log_dir)")"
[[ "$has_system_proxy" == "true" ]] && _uninstall_print_target "System proxy" "${system_proxy_target}"
[[ "$has_legacy_data" == "true" ]] && _uninstall_print_target "Legacy data" "$(display_path "/var/lib/sing-box")"
[[ "$has_tun" == "true" ]] && _uninstall_print_target "Machine-wide TUN" "$(tun_unit_name)"
for rc_cleanup_target in "${rc_cleanup_targets[@]}"; do
	_uninstall_print_target "Some lines in shell RC file" "$(display_path "$rc_cleanup_target")"
done
echo

if [[ "$SKIP_CONFIRM" != "true" ]]; then
	printf '%s' "${INDENT}${BOLD}${YELLOW}${QUESTION_MARK} $(_t 'Are you sure you want to continue?')${RESET} [y/N] "
	read -r answer
	if [[ "$answer" != "y" ]] && [[ "$answer" != "Y" ]]; then
		step_warn "$(_t 'Uninstallation cancelled')"
		exit 0
	fi
	echo
fi

if [[ "$has_tun" == "true" ]]; then
	debug "$(_t 'Removing machine-wide TUN mode')"
	if ! _client_remove_tun_installation; then
		echo
		step_fail "$(_t 'Uninstallation incomplete')"
		step_warn "$(_t 'Machine-wide TUN mode was left in place')"
		exit 1
	fi
	step_ok "$(_t 'Machine-wide TUN mode removed')"
fi

# Reinstall keeps integrations so the replacement client can use them again.
if [[ "${SBM_UNINSTALL_CLEAN_PROXY:-}" == 1 ]]; then
	if ! SBM_SKIP_UPDATE_CHECK=1 bash -c 'source "$1"; _proxy_uninstall_settings' bash "${script_dir}/setup.sh"; then
		step_fail "$(_t 'Uninstallation incomplete')"
		exit 1
	fi
fi

if [[ ${#user_services[@]} -gt 0 ]]; then
	debug "$(_t 'Stopping and disabling user services')"
	set +e
	for service_path in "${user_services[@]}"; do
		service_name=$(basename "${service_path}")
		debug "Stopping and disabling ${service_name}"
		systemctl --user stop "${service_name}" >/dev/null 2>&1 || true
		systemctl --user disable "${service_name}" >/dev/null 2>&1 || true
	done
	set -e

	debug "$(_t 'Removing user service files')"
	for service_path in "${user_services[@]}"; do
		debug "Removing ${service_path}"
		rm -f "${service_path}"
	done

	debug "$(_t 'Reloading user systemd')"
	systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

if [[ ${#launch_agents[@]} -gt 0 ]]; then
	debug "$(_t 'Stopping and removing launch agents')"
	set +e
	for plist_path in "${launch_agents[@]}"; do
		launch_agent_label=$(basename "${plist_path}" .plist)
		debug "Stopping ${launch_agent_label}"
		launchctl bootout "gui/$(id -u)/${launch_agent_label}" >/dev/null 2>&1 || true
	done
	set -e

	for plist_path in "${launch_agents[@]}"; do
		debug "Removing ${plist_path}"
		rm -f "${plist_path}"
	done
fi

if [[ "$has_user_logs" == "true" ]]; then
	debug "$(_t 'Removing log directory')"
	debug "$(_client_log_dir)"
	rm -rf "$(_client_log_dir)"
fi

if [[ "$has_system_proxy" == "true" ]]; then
	debug "$(_t 'Reverting the system proxy')"
	debug "${system_proxy_target}"
	if system_proxy_clear_needs_sudo && [[ $EUID -ne 0 ]] && ! sudo -n true 2>/dev/null; then
		echo
		echo "${BOLD}${YELLOW}${LOCK} $(_t 'Administrator privileges required')${RESET}"
		echo "$(_t 'Please enter your password'):"
	fi
	# A failure here must not abort the rest of the cleanup.
	if ! system_proxy_clear "${system_proxy_target}"; then
		step_warn "$(_t 'Failed to revert the system proxy. Clear it in your system network settings.')"
	fi
elif [[ "$system_proxy_state" == "other" ]]; then
	step_warn "$(_t 'The system proxy points somewhere else and was left unchanged.')"
fi

if [[ "$has_user_binary" == "true" ]]; then
	debug "$(_t 'Removing binary')"
	debug "${USER_BIN_DIR}/sing-box"
	rm -f "${USER_BIN_DIR}/sing-box"
fi

if [[ "$has_user_cronet" == "true" ]]; then
	debug "$(_t 'Removing runtime library')"
	debug "${USER_BIN_DIR}/libcronet.so"
	rm -f "${USER_BIN_DIR}/libcronet.so"
fi

if [[ "$has_user_config" == "true" ]]; then
	debug "$(_t 'Removing configuration')"
	for path in "${client_config_roots[@]}"; do
		if [[ -d "$path" ]]; then
			debug "$path"
			rm -rf "$path"
		fi
	done
fi

if [[ "$has_user_data" == "true" ]]; then
	debug "$(_t 'Removing data directory')"
	for path in "${client_data_roots[@]}"; do
		if [[ -d "$path" ]]; then
			debug "$path"
			rm -rf "$path"
		fi
	done
fi

if [[ "$has_user_state" == "true" ]]; then
	debug "$(_t 'Removing state directory')"
	for path in "${client_state_roots[@]}"; do
		if [[ -d "$path" ]]; then
			debug "$path"
			rm -rf "$path"
		fi
	done
fi

if [[ ${#legacy_services[@]} -gt 0 || "$has_legacy_binary" == "true" || "$has_legacy_config" == "true" || "$has_legacy_data" == "true" ]]; then
	debug "$(_t 'Removing legacy system installation')"
	set +e
	for service_path in "${legacy_services[@]}"; do
		service_name=$(basename "${service_path}")
		debug "Stopping and disabling ${service_name}"
		run_with_sudo systemctl stop "${service_name}" >/dev/null 2>&1 || true
		run_with_sudo systemctl disable "${service_name}" >/dev/null 2>&1 || true
	done
	set -e

	for service_path in "${legacy_services[@]}"; do
		debug "Removing ${service_path}"
		run_with_sudo rm -f "${service_path}"
	done
	run_with_sudo systemctl daemon-reload >/dev/null 2>&1 || true

	if [[ "$has_legacy_binary" == "true" ]]; then
		run_with_sudo rm -f "/usr/local/bin/sing-box"
	fi
	if [[ "$has_legacy_config" == "true" ]]; then
		run_with_sudo rm -rf "/usr/local/etc/sing-box"
	fi
	if [[ "$has_legacy_data" == "true" ]]; then
		run_with_sudo rm -rf "/var/lib/sing-box"
	fi
fi

if [[ "$CONFIGURE_RC" == "true" ]]; then
	for rc_cleanup_target in "${rc_cleanup_targets[@]}"; do
		clean_shell_rc "$rc_cleanup_target"
	done
else
	step_warn "$(_t 'Shell RC cleanup disabled')"
fi

echo
step_ok "$(_t 'Uninstallation complete')"
echo
