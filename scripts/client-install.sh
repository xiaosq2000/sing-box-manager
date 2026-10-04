#!/usr/bin/env bash
set -eo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
# shellcheck source=/dev/null
source "${script_dir}/lib/ui.sh"
# shellcheck source=/dev/null
source "${script_dir}/lib/platform.sh"
# shellcheck source=/dev/null
source "${script_dir}/lib/tun.sh"

# Domain translations for zh_CN output.
# A case lookup instead of an associative array keeps this compatible with
# bash 3.2, the stock /bin/bash on macOS.
_translate_zh() {
	case "$1" in
	"Installation Script") printf '%s\n' "安装脚本" ;;
	"ERROR") printf '%s\n' "错误" ;;
	"WARNING") printf '%s\n' "警告" ;;
	"INFO") printf '%s\n' "信息" ;;
	"DEBUG") printf '%s\n' "调试" ;;
	"SUCCESS") printf '%s\n' "成功" ;;
	"FAILED") printf '%s\n' "失败" ;;
	"Installation cancelled") printf '%s\n' "安装已取消" ;;
	"Created file") printf '%s\n' "已创建文件" ;;
	"Added line") printf '%s\n' "已添加行" ;;
	"Adding proxy functions to RC file") printf '%s\n' "添加代理功能到配置文件" ;;
	"Shell integration updated") printf '%s\n' "Shell 集成已更新" ;;
	"Shell integration already configured") printf '%s\n' "Shell 集成已配置" ;;
	"Shell integration skipped") printf '%s\n' "Shell 集成已跳过" ;;
	"RC file updated") printf '%s\n' "配置文件已更新" ;;
	"RC file unchanged") printf '%s\n' "配置文件未更改" ;;
	"RC file unchanged for") printf '%s\n' "配置文件未更改" ;;
	"No shell RC file found") printf '%s\n' "未找到 Shell 配置文件" ;;
	"Shell RC configuration disabled") printf '%s\n' "Shell 配置文件修改已禁用" ;;
	"Creating shell RC file") printf '%s\n' "创建 Shell 配置文件" ;;
	"Usage") printf '%s\n' "用法" ;;
	"Examples") printf '%s\n' "示例" ;;
	"Options") printf '%s\n' "选项" ;;
	"Display help messages") printf '%s\n' "显示帮助信息" ;;
	"Debug logging") printf '%s\n' "调试日志" ;;
	"Specify which protocol to install") printf '%s\n' "指定要安装的协议" ;;
	"Specify which shell RC file to target") printf '%s\n' "指定要写入的 Shell 配置文件" ;;
	"Persist portal base URL for \`proxy check quota\`") printf '%s\n' "为 \`proxy check quota\` 保存 portal 地址" ;;
	"Skip shell RC configuration") printf '%s\n' "跳过 Shell 配置文件修改" ;;
	"Install default client") printf '%s\n' "安装默认客户端" ;;
	"Install a specific protocol") printf '%s\n' "安装指定协议" ;;
	"Install targeting a specific shell RC file") printf '%s\n' "安装并写入指定的 Shell 配置文件" ;;
	"Install without modifying shell configuration") printf '%s\n' "安装但不修改 Shell 配置" ;;
	"Install with debug logging") printf '%s\n' "安装并启用调试日志" ;;
	"Unknown argument") printf '%s\n' "未知参数" ;;
	"Missing value for option") printf '%s\n' "选项缺少值" ;;
	"Unsupported shell") printf '%s\n' "不支持的 Shell" ;;
	"Supported shells") printf '%s\n' "支持的 Shell" ;;
	"Supported protocols") printf '%s\n' "支持的协议" ;;
	"Selected protocol") printf '%s\n' "当前协议" ;;
	"Selected route") printf '%s\n' "当前路由策略" ;;
	"Service file path") printf '%s\n' "服务文件路径" ;;
	"Binary file not found") printf '%s\n' "二进制文件未找到" ;;
	"Configuration file not found") printf '%s\n' "配置文件未找到" ;;
	"Required script not found") printf '%s\n' "所需脚本未找到" ;;
	"systemd is required but not found") printf '%s\n' "需要 systemd 但未找到" ;;
	"This system does not use systemd") printf '%s\n' "此系统不使用 systemd" ;;
	"launchd is required but not found") printf '%s\n' "需要 launchd 但未找到" ;;
	"This system does not use launchd") printf '%s\n' "此系统不使用 launchd" ;;
	"Cleared quarantine attribute") printf '%s\n' "已清除隔离属性" ;;
	"Service lifetime") printf '%s\n' "服务生命周期" ;;
	"This LaunchAgent starts at login and stops at logout.") printf '%s\n' "此 LaunchAgent 在登录时启动，注销时停止。" ;;
	"Running before login or after logout needs a root LaunchDaemon, which this installer does not create.") printf '%s\n' "在登录前或注销后运行需要 root 级 LaunchDaemon，此安装脚本不会创建。" ;;
	"This installer must be run as the target user without sudo") printf '%s\n' "请以目标用户身份直接运行此安装脚本，不要使用 sudo" ;;
	"Administrator privileges required") printf '%s\n' "需要管理员权限" ;;
	"Please enter your password") printf '%s\n' "请输入您的密码" ;;
	"Removing existing sing-box client installation first") printf '%s\n' "先移除已有的 sing-box 客户端安装" ;;
	"Failed to remove existing client installation") printf '%s\n' "移除已有客户端安装失败" ;;
	"Failed to start service") printf '%s\n' "启动服务失败" ;;
	"Service is not active after start") printf '%s\n' "服务启动后未处于活动状态" ;;
	"Service installed") printf '%s\n' "服务已安装" ;;
	"systemd-resolved is not running, so the ai route cannot resolve direct traffic.") printf '%s\n' "systemd-resolved 未运行，ai 路由无法解析直连流量。" ;;
	"Every other route is unaffected.") printf '%s\n' "其他路由策略不受影响。" ;;
	"Service is now running") printf '%s\n' "服务正在运行" ;;
	"quick start") printf '%s\n' "快速开始" ;;
	"Run \`./client-install.sh --help\` to see available options.") printf '%s\n' "运行 \`./client-install.sh --help\` 查看可用选项。" ;;
	"is not supported yet") printf '%s\n' "尚不支持" ;;
	"not found") printf '%s\n' "未找到" ;;
	"Optional persistence after logout") printf '%s\n' "退出登录后的可选持久运行" ;;
	"systemd user services usually stop when your last login session ends.") printf '%s\n' "systemd 用户服务通常会在最后一个登录会话结束时停止。" ;;
	"Enable linger to let this sing-box client keep running after logout.") printf '%s\n' "启用 linger 后，此 sing-box 客户端可在退出登录后继续运行。" ;;
	"This requires administrator privileges and may prompt for your sudo password.") printf '%s\n' "此操作需要管理员权限，并且可能会提示输入 sudo 密码。" ;;
	"Enable linger now for") printf '%s\n' "现在为以下用户启用 linger" ;;
	"Linger is already enabled") printf '%s\n' "linger 已启用" ;;
	"Linger enabled") printf '%s\n' "linger 已启用" ;;
	"Linger enable skipped") printf '%s\n' "已跳过启用 linger" ;;
	"Failed to enable linger") printf '%s\n' "启用 linger 失败" ;;
	"Run later if needed") printf '%s\n' "如有需要可稍后执行" ;;
	"Portal base URL must use HTTPS unless it points to localhost") printf '%s\n' "Portal 地址必须使用 HTTPS，除非它指向 localhost" ;;
	"Machine-wide TUN mode is not safely off") printf '%s\n' "全局 TUN 模式尚未安全关闭" ;;
	"Bundled rule files are missing or invalid. Download the client package again.") printf '%s\n' "随附的规则文件缺失或无效。请重新下载客户端安装包。" ;;
	"Run \`proxy tun off\` first, then run the installer again.") printf '%s\n' "请先运行 \`proxy tun off\`，然后重新运行安装脚本。" ;;
	*) return 1 ;;
	esac
}

DEFAULT_PROTOCOL="trojan"
DEFAULT_PROTOCOL_FILE="${script_dir}/default-protocol"
SUPPORTED_PROTOCOLS="trojan hysteria2 naive"
SUPPORTED_SHELLS="bash zsh"
INSTALL_COMMAND="./client-install.sh"
CONFIGURE_RC=true
VERBOSE=false
PROTOCOL=""
INSTALL_PROTOCOLS=()
SHELL_OVERRIDE=""
PORTAL_BASE_URL="${SBM_PORTAL_BASE_URL:-}"
shell_name=""
rc_file=""
RC_FILE_MODIFIED=false
RC_MODIFICATIONS=()

if [[ -f "$DEFAULT_PROTOCOL_FILE" ]]; then
	if IFS= read -r configured_default_protocol <"$DEFAULT_PROTOCOL_FILE"; then
		configured_default_protocol="${configured_default_protocol%$'\r'}"
		if [[ -n "$configured_default_protocol" ]]; then
			DEFAULT_PROTOCOL="$configured_default_protocol"
		fi
	fi
fi

if [[ "${LANG:-}${LC_ALL:-}" == *"UTF-8"* ]]; then
	INSTALL_OK_ICON="✓"
	INSTALL_WARN_ICON="!"
else
	INSTALL_OK_ICON="[OK]"
	INSTALL_WARN_ICON="[!]"
fi

step_ok() {
	printf '  %s%s%s %s\n' "$GREEN" "$INSTALL_OK_ICON" "$RESET" "$1"
}

step_warn() {
	printf '  %s%s%s %s\n' "$YELLOW" "$INSTALL_WARN_ICON" "$RESET" "$1"
}

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

_client_config_root() {
	printf '%s/sing-box\n' "$(_client_current_config_home)"
}

_client_data_root() {
	printf '%s/sing-box\n' "$(_client_current_data_home)"
}

_client_state_root() {
	printf '%s/sing-box\n' "$(_client_current_state_home)"
}

_client_launch_agent_dir() {
	printf '%s/Library/LaunchAgents\n' "${TARGET_HOME:-$HOME}"
}

_client_log_dir() {
	printf '%s/Library/Logs/sing-box\n' "${TARGET_HOME:-$HOME}"
}

_client_launchd_label() {
	printf 'io.sing-box.%s\n' "$1"
}

_client_launchd_domain() {
	printf 'gui/%s\n' "$(id -u)"
}

_client_launchd_target() {
	printf '%s/%s\n' "$(_client_launchd_domain)" "$(_client_launchd_label "$1")"
}

_client_user_service_dir() {
	if plat_is_macos; then
		_client_launch_agent_dir
		return
	fi

	printf '%s/systemd/user\n' "$(_client_current_config_home)"
}

# Name of the unit file on disk.
_client_service_file_name() {
	if plat_is_macos; then
		printf '%s.plist\n' "$(_client_launchd_label "$1")"
		return
	fi

	printf 'sing-box-%s.service\n' "$1"
}

# Name shown to the user, and the name they would pass to launchctl/systemctl.
_client_service_unit_name() {
	if plat_is_macos; then
		_client_launchd_label "$1"
		return
	fi

	printf 'sing-box-%s.service\n' "$1"
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

_client_setup_path() {
	printf '%s/setup.sh\n' "$(_client_data_root)"
}

_client_selected_protocol_path() {
	printf '%s/selected-protocol\n' "$(_client_config_root)"
}

_client_selected_route_path() {
	printf '%s/selected-route\n' "$(_client_config_root)"
}

_client_protocol_config_source() {
	printf '%s/%s-client.json\n' "$script_dir" "$1"
}

_client_protocol_route_config_source() {
	local protocol="$1"
	local route="$2"

	case "$route" in
	china) _client_protocol_config_source "$protocol" ;;
	gfw | ai | global) printf '%s/%s-%s-client.json\n' "$script_dir" "$protocol" "$route" ;;
	esac
}

# The self-managed Linux tunnel `proxy tun on` provisions. Absent from the
# macOS and Windows packages, where TUN is not supported yet.
_client_protocol_tun_config_source() {
	printf '%s/%s-linux-tun-client.json\n' "$script_dir" "$1"
}

_client_validate_rule_files() {
	local rule_file
	local rules_dir="${script_dir}/rules"
	local manifest="${rules_dir}/files.txt"
	if [[ -L "$rules_dir" || ! -d "$rules_dir" || -L "$manifest" || ! -f "$manifest" || ! -s "$manifest" ]]; then
		error "$(_t 'Bundled rule files are missing or invalid. Download the client package again.')"
		return 1
	fi
	while IFS= read -r rule_file || [[ -n "$rule_file" ]]; do
		if [[ ! "$rule_file" =~ ^[a-f0-9]{64}\.srs$ ]] ||
			[[ -L "${rules_dir}/${rule_file}" || ! -s "${rules_dir}/${rule_file}" || ! -f "${rules_dir}/${rule_file}" ]]; then
			error "$(_t 'Bundled rule files are missing or invalid. Download the client package again.')"
			return 1
		fi
	done <"$manifest"
}

_client_install_rule_files() {
	local rule_file
	mkdir -p "${CLIENT_STATE_ROOT}/rules"
	while IFS= read -r rule_file || [[ -n "$rule_file" ]]; do
		cp "${script_dir}/rules/${rule_file}" "${CLIENT_STATE_ROOT}/rules/${rule_file}"
	done <"${script_dir}/rules/files.txt"
	cp "${script_dir}/rules/files.txt" "${CLIENT_STATE_ROOT}/rules/files.txt"
}

_client_protocol_config_dir() {
	printf '%s/%s\n' "$(_client_config_root)" "$1"
}

_client_setup_shell_path() {
	printf '%s\n' "\${XDG_DATA_HOME:-\$HOME/.local/share}/sing-box/setup.sh"
}

_client_rc_has_setup_source() {
	local target_rc_file="$1"

	grep -Eq '(^|[[:space:]])(source|\.)[[:space:]]+["'"'"']?.*sing-box/setup\.sh(["'"'"']|[[:space:]]|$)' "$target_rc_file" 2>/dev/null
}

detect_shell() {
	local detected_shell="${SHELL_OVERRIDE:-}"
	local detected_rc_file=""
	local user_home="${TARGET_HOME:-$HOME}"
	local user_name="${TARGET_USER:-$USER}"
	local passwd_entry=""
	local passwd_shell=""

	if [[ -n "$detected_shell" ]]; then
		:
	elif command -v getent >/dev/null 2>&1; then
		passwd_entry=$(getent passwd "$user_name" 2>/dev/null || true)
		if [[ -n "$passwd_entry" ]]; then
			IFS=':' read -r _ _ _ _ _ _ passwd_shell <<<"$passwd_entry"
			if [[ -n "$passwd_shell" ]]; then
				detected_shell="${passwd_shell##*/}"
			fi
		fi
	elif command -v dscl >/dev/null 2>&1; then
		# macOS has no getent; the directory service holds the login shell.
		passwd_shell=$(dscl . -read "/Users/${user_name}" UserShell 2>/dev/null | awk '{print $2}')
		if [[ -n "$passwd_shell" ]]; then
			detected_shell="${passwd_shell##*/}"
		fi
	fi

	if [[ -z "$detected_shell" ]] && [[ -n "${SHELL:-}" ]]; then
		detected_shell="${SHELL##*/}"
	fi

	case "$detected_shell" in
	bash | zsh)
		detected_rc_file="$(_client_rc_path_for_shell "$detected_shell")"
		;;
	*)
		if [[ -f "${user_home}/.zshrc" ]]; then
			detected_shell="zsh"
			detected_rc_file="${user_home}/.zshrc"
		elif [[ -f "$(_client_rc_path_for_shell bash)" ]]; then
			detected_shell="bash"
			detected_rc_file="$(_client_rc_path_for_shell bash)"
		fi
		;;
	esac

	echo "$detected_shell:$detected_rc_file"
}

_install_show_help_hint() {
	hint "$(_t "Run \`./client-install.sh --help\` to see available options.")"
}

_install_reset_rc_modifications() {
	RC_FILE_MODIFIED=false
	RC_MODIFICATIONS=()
}

_install_record_rc_modification() {
	RC_FILE_MODIFIED=true
	RC_MODIFICATIONS+=("$1")
}

_install_show_rc_modifications() {
	local current_rc_file="$1"
	local modification

	if [[ "$RC_FILE_MODIFIED" != "true" ]]; then
		debug "$(_t 'RC file unchanged'): ${current_rc_file}"
		return 0
	fi

	debug "$(_t 'RC file updated'): ${current_rc_file}"
	for modification in "${RC_MODIFICATIONS[@]}"; do
		debug "$modification"
	done
}

_install_require_option_value() {
	local option="$1"
	local value="${2:-}"

	if [[ -z "$value" || "$value" == -* ]]; then
		error "$(_t 'Missing value for option'): ${option}"
		_install_show_help_hint
		exit 1
	fi
}

_install_validate_shell_name() {
	local shell_name="$1"

	case "$shell_name" in
	bash | zsh)
		return 0
		;;
	*)
		error "$(_t 'Unsupported shell'): ${shell_name}. $(_t 'Supported shells'): ${SUPPORTED_SHELLS}"
		_install_show_help_hint
		return 1
		;;
	esac
}

_install_protocol_is_supported() {
	local protocol="$1"
	local supported_protocol

	for supported_protocol in $SUPPORTED_PROTOCOLS; do
		if [[ "$supported_protocol" == "$protocol" ]]; then
			return 0
		fi
	done

	return 1
}

_install_collect_packaged_protocols() {
	local protocol config_source

	INSTALL_PROTOCOLS=()
	for protocol in $SUPPORTED_PROTOCOLS; do
		config_source=$(_client_protocol_config_source "$protocol")
		if [[ -f "$config_source" ]]; then
			INSTALL_PROTOCOLS+=("$protocol")
		fi
	done
}

_install_package_includes_protocol() {
	local requested_protocol="$1"
	local protocol

	for protocol in "${INSTALL_PROTOCOLS[@]}"; do
		if [[ "$protocol" == "$requested_protocol" ]]; then
			return 0
		fi
	done

	return 1
}

_client_normalize_portal_base_url() {
	local url="${1:-}"

	url=${url%$'\r'}
	while [[ "$url" == */ ]]; do
		url=${url%/}
	done

	if [[ -z "$url" || "$url" == *[[:space:]]* || "$url" == *"?"* || "$url" == *"#"* ]]; then
		return 1
	fi

	case "$url" in
	https://* | http://localhost* | http://127.0.0.1* | http://[[]::1[]]*)
		printf '%s\n' "$url"
		return 0
		;;
	esac

	return 1
}

_client_packaged_version_field() {
	local key="$1"
	local metadata_file="${script_dir}/client-version"
	local line

	[[ -f "$metadata_file" ]] || return 1
	while IFS= read -r line; do
		case "$line" in
		"${key}="*)
			printf '%s\n' "${line#*=}"
			return 0
			;;
		esac
	done <"$metadata_file"
	return 1
}

configure_shell_rc() {
	local target_rc_file="$1"
	local script_path
	local comment_line="# Network proxy management configuration (sing-box)"
	local script_shell_path
	local source_line=""

	script_path=$(_client_setup_path)
	script_shell_path=$(_client_setup_shell_path)
	source_line="[ -f \"${script_shell_path}\" ] && source \"${script_shell_path}\""

	_install_reset_rc_modifications

	if [[ ! -f "$script_path" ]]; then
		error "$(_t 'Shell RC configuration disabled') - setup.sh $(_t 'not found')"
		return 1
	fi

	if [[ ! -f "$target_rc_file" ]]; then
		debug "$(_t 'Creating shell RC file'): ${target_rc_file}"
		touch "$target_rc_file"
		_install_record_rc_modification "$(_t 'Created file'): ${target_rc_file}"
	fi

	if _client_rc_has_setup_source "$target_rc_file"; then
		step_ok "$(_t 'Shell integration already configured'): $(display_path "$target_rc_file")"
		_install_show_rc_modifications "$target_rc_file"
		return 0
	fi

	debug "$(_t 'Adding proxy functions to RC file'): ${target_rc_file}"
	[[ -s "$target_rc_file" && -z "$(tail -c1 "$target_rc_file")" ]] || echo "" >>"$target_rc_file"

	{
		echo ""
		echo "$comment_line"
		echo "$source_line"
		echo ""
	} >>"$target_rc_file"

	_install_record_rc_modification "$(_t 'Added line'): ${comment_line}"
	_install_record_rc_modification "$(_t 'Added line'): ${source_line}"

	step_ok "$(_t 'Shell integration updated'): $(display_path "$target_rc_file")"
	_install_show_rc_modifications "$target_rc_file"
	return 0
}

_install_show_quick_start() {
	local source_target

	if [[ "$CONFIGURE_RC" == "true" ]] && [[ -n "$rc_file" ]]; then
		source_target=$(display_path "$rc_file")
	else
		source_target="\"$(_client_setup_shell_path)\""
	fi

	echo
	echo "$INDENT$BOLD$UNDERLINE$CYAN$(_t 'quick start')$RESET"
	echo "${INDENT}${INDENT}source $source_target"
	echo "${INDENT}${INDENT}proxy on"
	echo "${INDENT}${INDENT}proxy check ip"
	echo "${INDENT}${INDENT}proxy off"
	echo "${INDENT}${INDENT}proxy check ip"
	echo
}

_client_require_non_root() {
	if [[ $EUID -eq 0 || -n "${SUDO_USER:-}" ]]; then
		error "$(_t 'This installer must be run as the target user without sudo')"
		exit 1
	fi
}

_client_warn_user_bin_path() {
	local user_bin_dir
	user_bin_dir=$(_client_bin_dir)

	case ":${PATH:-}:" in
	*":${user_bin_dir}:"*)
		return 0
		;;
	esac

	step_warn "${user_bin_dir} is not on PATH; add it if you want to run sing-box directly."
}

_client_run_with_sudo() {
	if [[ $EUID -eq 0 ]]; then
		"$@"
		return
	fi

	if ! command -v sudo >/dev/null 2>&1; then
		return 1
	fi

	if sudo -n true 2>/dev/null; then
		sudo "$@"
		return
	fi

	echo
	echo "${BOLD}${YELLOW}${LOCK} $(_t 'Administrator privileges required')${RESET}"
	echo "$(_t 'Please enter your password'):"
	sudo "$@"
}

_client_linger_is_enabled() {
	local linger_status=""

	if ! command -v loginctl >/dev/null 2>&1; then
		return 1
	fi

	linger_status=$(loginctl show-user "${TARGET_USER}" --property=Linger --value 2>/dev/null || true)
	[[ "$linger_status" == "yes" ]]
}

_client_show_linger_manual_command() {
	echo "${INDENT}$(_t 'Run later if needed'): sudo loginctl enable-linger ${TARGET_USER}"
}

# The Linux ai route resolves direct traffic through systemd-resolved's stub:
# sing-box's own local transport binds its DNS socket to the link, and kernels
# that require CAP_NET_RAW for that bind fail every lookup in an unprivileged
# user service. No other route needs the stub, so this is a note, not an error.
_client_warn_missing_resolved_stub() {
	if plat_is_macos || ! command -v systemctl >/dev/null 2>&1; then
		return 0
	fi
	if [[ "$(systemctl is-active systemd-resolved 2>/dev/null)" == "active" ]]; then
		return 0
	fi

	step_warn "$(_t 'systemd-resolved is not running, so the ai route cannot resolve direct traffic.')"
	echo "${INDENT}$(_t 'Every other route is unaffected.')"
	echo
}

_client_maybe_enable_linger() {
	local answer=""

	if _client_linger_is_enabled; then
		step_ok "$(_t 'Linger is already enabled'): ${TARGET_USER}"
		echo
		return 0
	fi

	hint "$(_t 'Optional persistence after logout'):"
	echo "${INDENT}$(_t 'systemd user services usually stop when your last login session ends.')"
	echo "${INDENT}$(_t 'Enable linger to let this sing-box client keep running after logout.')"
	echo "${INDENT}$(_t 'This requires administrator privileges and may prompt for your sudo password.')"
	echo

	if [[ "${INTERACTIVE:-false}" != "true" ]]; then
		_client_show_linger_manual_command
		echo
		return 0
	fi

	printf '%s' "${BOLD}${YELLOW}${QUESTION_MARK} $(_t 'Enable linger now for') ${TARGET_USER}?${RESET} [Y/n] "
	if ! read -r answer; then
		answer=""
	fi

	case "$answer" in
	"" | [Yy] | [Yy][Ee][Ss])
		if _client_run_with_sudo loginctl enable-linger "${TARGET_USER}"; then
			step_ok "$(_t 'Linger enabled'): ${TARGET_USER}"
		else
			step_warn "$(_t 'Failed to enable linger'): ${TARGET_USER}"
			_client_show_linger_manual_command
		fi
		;;
	*)
		step_warn "$(_t 'Linger enable skipped'): ${TARGET_USER}"
		_client_show_linger_manual_command
		;;
	esac

	echo
}

_client_show_launchd_lifetime_note() {
	hint "$(_t 'Service lifetime'):"
	echo "${INDENT}$(_t 'This LaunchAgent starts at login and stops at logout.')"
	echo "${INDENT}$(_t 'Running before login or after logout needs a root LaunchDaemon, which this installer does not create.')"
	echo
}

_client_launchd_bootout() {
	launchctl bootout "$(_client_launchd_target "$1")" >/dev/null 2>&1 || true
}

_client_launchd_bootstrap() {
	local protocol="$1"
	local plist_file="$2"

	# Clears any disabled state a previous install or a manual launchctl left.
	launchctl enable "$(_client_launchd_target "$protocol")" >/dev/null 2>&1 || true
	launchctl bootstrap "$(_client_launchd_domain)" "$plist_file" >/dev/null 2>&1
}

# launchd brings a job up asynchronously, so poll instead of checking once.
_client_launchd_wait_running() {
	local protocol="$1"
	local attempt=0

	while ((attempt < 5)); do
		if launchctl print "$(_client_launchd_target "$protocol")" 2>/dev/null |
			grep -q 'state = running'; then
			return 0
		fi
		attempt=$((attempt + 1))
		sleep 1
	done

	return 1
}

_client_write_launch_agent() {
	local plist_file="$1"
	local protocol="$2"
	local binary_path="$3"
	local state_dir="$4"
	local config_root="$5"
	local log_file="$6"

	# KeepAlive/SuccessfulExit=false is the launchd equivalent of
	# systemd's Restart=on-failure. `-c <file>` rather than `-C <directory>`
	# keeps this loading exactly one profile, as the Linux unit does.
	cat >"$plist_file" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$(_client_launchd_label "$protocol")</string>
	<key>ProgramArguments</key>
	<array>
		<string>${binary_path}</string>
		<string>run</string>
		<string>-D</string>
		<string>${state_dir}</string>
		<string>-c</string>
		<string>${config_root}/${protocol}/config.json</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>Umask</key>
	<integer>63</integer>
	<key>KeepAlive</key>
	<dict>
		<key>SuccessfulExit</key>
		<false/>
	</dict>
	<key>StandardOutPath</key>
	<string>${log_file}</string>
	<key>StandardErrorPath</key>
	<string>${log_file}</string>
</dict>
</plist>
EOF
}

_client_write_user_service() {
	local unit_file="$1"
	local protocol="$2"
	local binary_path="$3"
	local state_dir="$4"
	local config_root="$5"
	local description=""
	local transaction_marker

	transaction_marker=$(tun_transaction_marker_path)

	case "$protocol" in
	trojan)
		description="sing-box Trojan client"
		;;
	hysteria2)
		description="sing-box Hysteria2 client"
		;;
	naive)
		description="sing-box Naive client"
		;;
	esac

	# `-c <file>`, not `-C <directory>`: the protocol directory also holds the
	# self-managed TUN profile, and loading both would merge two inbounds into
	# one instance. ExecStartPre refuses to start the mixed inbound while the
	# machine-wide tunnel is up, so a delayed or manual `systemctl --user start`
	# cannot race a TUN transaction.
	cat >"$unit_file" <<EOF
[Unit]
Description=${description}
Documentation=https://sing-box.sagernet.org

[Service]
Type=simple
UMask=0077
ExecStartPre=/bin/sh -c '! test -e ${transaction_marker}'
ExecStart="${binary_path}" run -D "${state_dir}" -c "${config_root}/${protocol}/config.json"
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=5s
LimitNOFILE=infinity

[Install]
WantedBy=default.target
EOF
}

usage() {
	echo
	echo "  ${BOLD}${CYAN}${UNDERLINE}Sing-box VPN Client Installer${RESET}"
	echo
	echo "  ${BOLD}$(_t 'Usage')${RESET}"
	echo "${INDENT}${GREEN}${BOLD}${INSTALL_COMMAND} [options]${RESET}"
	echo
	echo "  ${BOLD}$(_t 'Examples')${RESET}"
	echo "${INDENT}${GREEN}${BOLD}${INSTALL_COMMAND}${RESET}                         $(_t 'Install default client')"
	echo "${INDENT}${GREEN}${BOLD}${INSTALL_COMMAND} -p hysteria2${RESET}           $(_t 'Install a specific protocol')"
	echo "${INDENT}${GREEN}${BOLD}${INSTALL_COMMAND} -p naive${RESET}               $(_t 'Install a specific protocol')"
	echo "${INDENT}${GREEN}${BOLD}${INSTALL_COMMAND} --shell bash${RESET}           $(_t 'Install targeting a specific shell RC file')"
	echo "${INDENT}${GREEN}${BOLD}${INSTALL_COMMAND} --no-rc${RESET}                $(_t 'Install without modifying shell configuration')"
	echo "${INDENT}${GREEN}${BOLD}${INSTALL_COMMAND} -V${RESET}                     $(_t 'Install with debug logging')"
	echo
	echo "  ${BOLD}$(_t 'Options')${RESET}"
	echo "${INDENT}${GREEN}${BOLD}-h, --help${RESET}                  $(_t 'Display help messages')"
	echo "${INDENT}${GREEN}${BOLD}-V, --verbose${RESET}               $(_t 'Debug logging')"
	echo "${INDENT}${GREEN}${BOLD}-p, --protocol PROTOCOL${RESET}     $(_t 'Specify which protocol to install')"
	echo "${INDENT}${GREEN}${BOLD}--shell SHELL${RESET}               $(_t 'Specify which shell RC file to target')"
	echo "${INDENT}${GREEN}${BOLD}--portal-base-url URL${RESET}       $(_t \"Persist portal base URL for \`proxy check quota\`\")"
	echo "${INDENT}${GREEN}${BOLD}--no-rc${RESET}                     $(_t 'Skip shell RC configuration')"
	echo
	echo "${INDENT}${DIM}$(_t 'Supported protocols'): ${SUPPORTED_PROTOCOLS}${RESET}"
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
	-p | --protocol)
		_install_require_option_value "$1" "${2:-}"
		PROTOCOL="$2"
		shift 2
		;;
	--shell)
		_install_require_option_value "$1" "${2:-}"
		SHELL_OVERRIDE="${2##*/}"
		if ! _install_validate_shell_name "$SHELL_OVERRIDE"; then
			exit 1
		fi
		shift 2
		;;
	--portal-base-url)
		_install_require_option_value "$1" "${2:-}"
		PORTAL_BASE_URL="$2"
		shift 2
		;;
	-V | --verbose)
		VERBOSE=true
		shift 1
		;;
	--no-rc)
		CONFIGURE_RC=false
		shift 1
		;;
	*)
		error "$(_t 'Unknown argument'): $1"
		_install_show_help_hint
		exit 1
		;;
	esac
done

_client_require_non_root
detect_target_user

if plat_is_macos; then
	if ! command -v launchctl >/dev/null 2>&1; then
		error "$(_t 'launchd is required but not found')"
		error "$(_t 'This system does not use launchd')"
		exit 1
	fi
elif ! command -v systemctl >/dev/null 2>&1; then
	error "$(_t 'systemd is required but not found')"
	error "$(_t 'This system does not use systemd')"
	exit 1
fi

if [[ -z "$PROTOCOL" ]]; then
	PROTOCOL="$DEFAULT_PROTOCOL"
fi

if ! _install_protocol_is_supported "$PROTOCOL"; then
	error "$PROTOCOL $(_t 'is not supported yet'). $(_t 'Supported protocols'): ${SUPPORTED_PROTOCOLS}"
	exit 1
fi

if [[ -z "$PORTAL_BASE_URL" ]]; then
	PORTAL_BASE_URL=$(_client_packaged_version_field portal_base_url || true)
fi

if [[ -n "$PORTAL_BASE_URL" ]]; then
	PORTAL_BASE_URL=$(_client_normalize_portal_base_url "$PORTAL_BASE_URL" || true)
	if [[ -z "$PORTAL_BASE_URL" ]]; then
		error "$(_t 'Portal base URL must use HTTPS unless it points to localhost')"
		exit 1
	fi
fi

cd "${script_dir}"

_install_collect_packaged_protocols
if ! _install_package_includes_protocol "$PROTOCOL"; then
	error "$(_t 'Configuration file not found'): $(_client_protocol_config_source "$PROTOCOL")"
	exit 1
fi

USER_BIN_DIR=$(_client_bin_dir)
CLIENT_CONFIG_ROOT=$(_client_config_root)
CLIENT_DATA_ROOT=$(_client_data_root)
CLIENT_STATE_ROOT=$(_client_state_root)
USER_SERVICE_DIR=$(_client_user_service_dir)
SERVICE_NAME="$(_client_service_unit_name "${PROTOCOL}")"
BINARY_SOURCE="${script_dir}/sing-box"
BINARY_TARGET="${USER_BIN_DIR}/sing-box"
TMP_BINARY="${USER_BIN_DIR}/.sing-box.tmp.$$"
CRONET_SOURCE="${script_dir}/libcronet.so"
CRONET_TARGET="${USER_BIN_DIR}/libcronet.so"
TMP_CRONET="${USER_BIN_DIR}/.libcronet.so.tmp.$$"
SETUP_SOURCE="${script_dir}/setup.sh"
SETUP_TARGET="$(_client_setup_path)"
UI_SOURCE="${script_dir}/lib/ui.sh"
UI_TARGET="${CLIENT_DATA_ROOT}/lib/ui.sh"
PLATFORM_SOURCE="${script_dir}/lib/platform.sh"
PLATFORM_TARGET="${CLIENT_DATA_ROOT}/lib/platform.sh"
SYSTEM_PROXY_SOURCE="${script_dir}/lib/system-proxy.sh"
SYSTEM_PROXY_TARGET="${CLIENT_DATA_ROOT}/lib/system-proxy.sh"
TUN_LIB_SOURCE="${script_dir}/lib/tun.sh"
TUN_LIB_TARGET="${CLIENT_DATA_ROOT}/lib/tun.sh"
PORTAL_BASE_URL_TARGET="${CLIENT_DATA_ROOT}/portal-base-url"
CLIENT_VERSION_SOURCE="${script_dir}/client-version"
CLIENT_VERSION_TARGET="${CLIENT_DATA_ROOT}/client-version"
INSTALL_PREFERENCES_TARGET="${CLIENT_DATA_ROOT}/install-preferences"
SELECTED_PROTOCOL_TARGET="$(_client_selected_protocol_path)"
SELECTED_ROUTE_TARGET="$(_client_selected_route_path)"
UNINSTALL_SCRIPT="${script_dir}/client-uninstall.sh"

debug "Validating installation files"
if [[ ! -f "${BINARY_SOURCE}" ]]; then
	error "$(_t 'Binary file not found'): ${BINARY_SOURCE}"
	exit 1
fi

if [[ ! -f "${SETUP_SOURCE}" ]]; then
	error "$(_t 'Required script not found'): ${SETUP_SOURCE}"
	exit 1
fi

if [[ ! -f "${UI_SOURCE}" ]]; then
	error "$(_t 'Required script not found'): ${UI_SOURCE}"
	exit 1
fi

if [[ ! -f "${PLATFORM_SOURCE}" ]]; then
	error "$(_t 'Required script not found'): ${PLATFORM_SOURCE}"
	exit 1
fi

if [[ ! -f "${SYSTEM_PROXY_SOURCE}" ]]; then
	error "$(_t 'Required script not found'): ${SYSTEM_PROXY_SOURCE}"
	exit 1
fi

if [[ ! -f "${TUN_LIB_SOURCE}" ]]; then
	error "$(_t 'Required script not found'): ${TUN_LIB_SOURCE}"
	exit 1
fi

if [[ ! -f "${UNINSTALL_SCRIPT}" ]]; then
	error "$(_t 'Required script not found'): ${UNINSTALL_SCRIPT}"
	exit 1
fi

# Keep the user's last valid mixed inbound choice across installer-driven
# cleanup. A standalone uninstall still removes the marker with the config
# root; only this reinstall path reads it before invoking the uninstaller and
# writes it back after the fresh configs are in place.
# shellcheck source=/dev/null
source "${SYSTEM_PROXY_SOURCE}"
PRESERVED_PORT=$(_system_proxy_read_selected_port "${CLIENT_CONFIG_ROOT}")

for install_protocol in "${INSTALL_PROTOCOLS[@]}"; do
	for install_route in china gfw ai global; do
		route_config_source=$(_client_protocol_route_config_source "$install_protocol" "$install_route")
		if [[ ! -f "$route_config_source" ]]; then
			error "$(_t 'Configuration file not found'): ${route_config_source}"
			exit 1
		fi
	done
done

_client_validate_rule_files || exit 1

# Replacing the user-scope binary and configs under a running machine-wide
# tunnel would leave the privileged service pointing at stale files, and the
# cleanup below would start mixed mode beneath it. Stopping first is the user's
# call, so this refuses rather than stopping the tunnel for them.
if ! tun_is_safely_off; then
	error "$(_t 'Machine-wide TUN mode is not safely off')"
	hint "$(_t "Run \`proxy tun off\` first, then run the installer again.")"
	exit 1
fi

step_warn "$(_t 'Removing existing sing-box client installation first')"
# Inactive privileged TUN files survive a reinstall; the next `proxy tun on`
# re-stages the new binary and config by content hash.
cleanup_args=(--yes --preserve-tun)
if [[ "$VERBOSE" == "true" ]]; then
	cleanup_args+=(-V)
fi
if [[ "$CONFIGURE_RC" != "true" ]]; then
	cleanup_args+=(--no-rc)
fi
if ! bash "${UNINSTALL_SCRIPT}" "${cleanup_args[@]}"; then
	error "$(_t 'Failed to remove existing client installation')"
	exit 1
fi

mkdir -p "${USER_BIN_DIR}"
mkdir -p "${CLIENT_DATA_ROOT}/lib"
mkdir -p "${CLIENT_STATE_ROOT}"
chmod 0700 "${CLIENT_STATE_ROOT}"
_client_install_rule_files
mkdir -p "${USER_SERVICE_DIR}"
if plat_is_macos; then
	mkdir -p "$(_client_log_dir)"
fi

rm -f "${TMP_BINARY}"
cp "${BINARY_SOURCE}" "${TMP_BINARY}"
chmod +x "${TMP_BINARY}"
mv -f "${TMP_BINARY}" "${BINARY_TARGET}"
debug "${BINARY_SOURCE} ${ARROW} ${BINARY_TARGET}"

# A browser-downloaded archive propagates com.apple.quarantine to the extracted
# binary, and Gatekeeper then refuses to launch it.
if plat_is_macos && command -v xattr >/dev/null 2>&1; then
	if xattr -d com.apple.quarantine "${BINARY_TARGET}" >/dev/null 2>&1; then
		debug "$(_t 'Cleared quarantine attribute'): ${BINARY_TARGET}"
	fi
fi

if [[ -f "${CRONET_SOURCE}" ]]; then
	rm -f "${TMP_CRONET}"
	cp "${CRONET_SOURCE}" "${TMP_CRONET}"
	mv -f "${TMP_CRONET}" "${CRONET_TARGET}"
	debug "${CRONET_SOURCE} ${ARROW} ${CRONET_TARGET}"
fi

for install_protocol in "${INSTALL_PROTOCOLS[@]}"; do
	protocol_dir=$(_client_protocol_config_dir "$install_protocol")
	# These hold the user's credentials, so the modes are applied explicitly
	# rather than left to the archive's or the caller's umask.
	mkdir -p "${protocol_dir}"
	chmod 0700 "${protocol_dir}"
	rm -f "${protocol_dir}/config-china.json" \
		"${protocol_dir}/config-gfw.json" "${protocol_dir}/config-ai.json" "${protocol_dir}/config-global.json"
	for install_route in china gfw ai global; do
		route_config_source=$(_client_protocol_route_config_source "$install_protocol" "$install_route")
		cp "$route_config_source" "${protocol_dir}/config-${install_route}.json"
		chmod 0600 "${protocol_dir}/config-${install_route}.json"
		debug "${route_config_source} ${ARROW} ${protocol_dir}/config-${install_route}.json"
	done
	cp "${protocol_dir}/config-china.json" "${protocol_dir}/config.json"
	chmod 0600 "${protocol_dir}/config.json"
	debug "${protocol_dir}/config-china.json ${ARROW} ${protocol_dir}/config.json"

	tun_config_source=$(_client_protocol_tun_config_source "$install_protocol")
	if [[ -f "${tun_config_source}" ]]; then
		cp "${tun_config_source}" "${protocol_dir}/config-tun.json"
		chmod 0600 "${protocol_dir}/config-tun.json"
		debug "${tun_config_source} ${ARROW} ${protocol_dir}/config-tun.json"
	else
		# A package without the self-managed profile leaves nothing behind for
		# `proxy tun on` to stage from a previous install.
		rm -f "${protocol_dir}/config-tun.json"
	fi
done

printf '%s\n' "$PROTOCOL" >"${SELECTED_PROTOCOL_TARGET}"
debug "$(_t 'Selected protocol'): ${PROTOCOL} ${ARROW} ${SELECTED_PROTOCOL_TARGET}"
printf '%s\n' "china" >"${SELECTED_ROUTE_TARGET}"
chmod 0600 "${SELECTED_ROUTE_TARGET}"
debug "$(_t 'Selected route'): china ${ARROW} ${SELECTED_ROUTE_TARGET}"
if [[ -n "${PRESERVED_PORT}" ]]; then
	printf '%s\n' "${PRESERVED_PORT}" >"$(_system_proxy_selected_port_file "${CLIENT_CONFIG_ROOT}")"
	chmod 0600 "$(_system_proxy_selected_port_file "${CLIENT_CONFIG_ROOT}")"
fi

cp "${SETUP_SOURCE}" "${SETUP_TARGET}"
cp "${UNINSTALL_SCRIPT}" "${CLIENT_DATA_ROOT}/client-uninstall.sh"
debug "${SETUP_SOURCE} ${ARROW} ${SETUP_TARGET}"
cp "${UI_SOURCE}" "${UI_TARGET}"
debug "${UI_SOURCE} ${ARROW} ${UI_TARGET}"
cp "${PLATFORM_SOURCE}" "${PLATFORM_TARGET}"
debug "${PLATFORM_SOURCE} ${ARROW} ${PLATFORM_TARGET}"
cp "${SYSTEM_PROXY_SOURCE}" "${SYSTEM_PROXY_TARGET}"
debug "${SYSTEM_PROXY_SOURCE} ${ARROW} ${SYSTEM_PROXY_TARGET}"
cp "${TUN_LIB_SOURCE}" "${TUN_LIB_TARGET}"
debug "${TUN_LIB_SOURCE} ${ARROW} ${TUN_LIB_TARGET}"
rm -f "${CLIENT_VERSION_TARGET}"
if [[ -f "${CLIENT_VERSION_SOURCE}" ]]; then
	cp "${CLIENT_VERSION_SOURCE}" "${CLIENT_VERSION_TARGET}"
	chmod 0644 "${CLIENT_VERSION_TARGET}"
	debug "${CLIENT_VERSION_SOURCE} ${ARROW} ${CLIENT_VERSION_TARGET}"
fi
rm -f "${PORTAL_BASE_URL_TARGET}"
if [[ -n "$PORTAL_BASE_URL" ]]; then
	printf '%s\n' "$PORTAL_BASE_URL" >"${PORTAL_BASE_URL_TARGET}"
	debug "${PORTAL_BASE_URL} ${ARROW} ${PORTAL_BASE_URL_TARGET}"
fi

for install_protocol in "${INSTALL_PROTOCOLS[@]}"; do
	service_file="${USER_SERVICE_DIR}/$(_client_service_file_name "${install_protocol}")"
	if plat_is_macos; then
		_client_write_launch_agent \
			"${service_file}" \
			"${install_protocol}" \
			"${BINARY_TARGET}" \
			"${CLIENT_STATE_ROOT}" \
			"${CLIENT_CONFIG_ROOT}" \
			"$(_client_log_dir)/${install_protocol}.log"
	else
		_client_write_user_service \
			"${service_file}" \
			"${install_protocol}" \
			"${BINARY_TARGET}" \
			"${CLIENT_STATE_ROOT}" \
			"${CLIENT_CONFIG_ROOT}"
	fi
done

if ! plat_is_macos; then
	if ! systemctl --user daemon-reload >/dev/null 2>&1; then
		error "$(_t 'Failed to start service'): ${SERVICE_NAME}"
		exit 1
	fi
fi

for install_protocol in "${INSTALL_PROTOCOLS[@]}"; do
	if [[ "$install_protocol" == "$PROTOCOL" ]]; then
		continue
	fi
	if plat_is_macos; then
		_client_launchd_bootout "${install_protocol}"
	else
		systemctl --user disable --now "sing-box-${install_protocol}.service" >/dev/null 2>&1 || true
	fi
done

if plat_is_macos; then
	# bootstrap refuses a label that is already loaded.
	_client_launchd_bootout "${PROTOCOL}"
	if ! _client_launchd_bootstrap \
		"${PROTOCOL}" \
		"${USER_SERVICE_DIR}/$(_client_service_file_name "${PROTOCOL}")"; then
		error "$(_t 'Failed to start service'): ${SERVICE_NAME}"
		exit 1
	fi

	if ! _client_launchd_wait_running "${PROTOCOL}"; then
		error "$(_t 'Service is not active after start'): ${SERVICE_NAME}"
		exit 1
	fi
else
	if ! systemctl --user enable --now "${SERVICE_NAME}" >/dev/null 2>&1; then
		error "$(_t 'Failed to start service'): ${SERVICE_NAME}"
		exit 1
	fi

	if ! systemctl --user is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
		error "$(_t 'Service is not active after start'): ${SERVICE_NAME}"
		exit 1
	fi
fi

for install_protocol in "${INSTALL_PROTOCOLS[@]}"; do
	step_ok "$(_t 'Service installed'): $(_client_service_unit_name "${install_protocol}")"
done
step_ok "$(_t 'Service is now running'): ${SERVICE_NAME}"

if [[ "$CONFIGURE_RC" == "true" ]]; then
	shell_info=$(detect_shell)
	shell_name="${shell_info%%:*}"
	rc_file="${shell_info#*:}"
	if [[ -n "$shell_name" ]] && [[ -n "$rc_file" ]]; then
		configure_shell_rc "$rc_file" "$shell_name"
	else
		step_warn "$(_t 'Shell integration skipped'): $(_t 'No shell RC file found')"
	fi
else
	step_ok "$(_t 'Shell integration skipped')"
fi

{
	printf 'rc_enabled=%s\n' "$CONFIGURE_RC"
	printf 'shell=%s\n' "$shell_name"
} >"${INSTALL_PREFERENCES_TARGET}"
chmod 0600 "${INSTALL_PREFERENCES_TARGET}"

echo
_client_warn_user_bin_path
if plat_is_macos; then
	_client_show_launchd_lifetime_note
else
	_client_maybe_enable_linger
	_client_warn_missing_resolved_stub
fi
_install_show_quick_start
