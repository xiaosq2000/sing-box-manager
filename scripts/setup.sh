#!/usr/bin/env bash

# Load UI utilities for consistent CLI output
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE:-$0}")" >/dev/null 2>&1 && pwd)"
if [ -f "$SCRIPT_DIR/lib/ui.sh" ]; then
	# shellcheck source=/dev/null
	source "$SCRIPT_DIR/lib/ui.sh"
else
	echo "error: $SCRIPT_DIR/lib/ui.sh not found"
fi

if [ -f "$SCRIPT_DIR/lib/platform.sh" ]; then
	# shellcheck source=/dev/null
	source "$SCRIPT_DIR/lib/platform.sh"
else
	# This file is sourced from an interactive shell, so degrade to the Linux
	# behavior rather than leaving every plat_* call undefined.
	plat_os() { echo linux; }
	plat_arch() { echo unknown; }
	plat_id() { echo linux-unknown; }
	plat_is_linux() { return 0; }
	plat_is_macos() { return 1; }
fi

if [ -f "$SCRIPT_DIR/lib/system-proxy.sh" ]; then
	# shellcheck source=/dev/null
	source "$SCRIPT_DIR/lib/system-proxy.sh"
else
	macos_proxy_network_service() { return 1; }
	macos_proxy_read() { return 1; }
	macos_proxy_classify() { printf 'off\n'; }
	gnome_proxy_read() { printf '\n'; }
	macos_proxy_clear() { return 1; }
	macos_proxy_run_with_sudo() { return 1; }
	_system_proxy_detect_port() { printf '%s\n' "$DEFAULT_PROXY_PORT"; }
	_system_proxy_read_selected_port() { return 0; }
fi

if [ -f "$SCRIPT_DIR/lib/tun.sh" ]; then
	# shellcheck source=/dev/null
	source "$SCRIPT_DIR/lib/tun.sh"
else
	# Without the library there is no managed TUN, so the guard has to report
	# safely-off or every mixed-mode command would refuse for no reason.
	tun_is_safely_off() { return 0; }
	tun_unit_installed() { return 1; }
	tun_unit_name() { printf 'sing-box-manager-tun.service\n'; }
	tun_status() { printf '\n'; }
	tun_transaction_in_progress() { return 1; }
fi

# Script configuration
VERBOSE=${VERBOSE:-false}
VPN_PROTOCOL=${VPN_PROTOCOL:-}
DEFAULT_PROXY_HOST="127.0.0.1"
DEFAULT_PROXY_PORT=1080
DEFAULT_NO_PROXY="localhost,127.0.0.0/8,::1,host.docker.internal"
TIMEOUT=3
SUPPORTED_PROTOCOLS=(trojan hysteria2 naive)
SUPPORTED_ROUTES=(china gfw ai global)
# What a route probe fetches. Plain HTTP, a tiny body, and reachable from the
# mainland, Hong Kong and Macau alike, so a failed probe says something about
# the route rather than about the destination.
ROUTE_PROBE_URL="http://captive.apple.com"
ROUTE_PROBE_TIMEOUT=20
USER_SYSTEMD_SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SING_BOX_CONFIG_ROOT="${XDG_CONFIG_HOME:-$HOME/.config}/sing-box"
# lib/system-proxy.sh is sourced above, before this path exists, so it carries
# its own default for client-uninstall.sh; hand it ours now that we have one.
# shellcheck disable=SC2034  # Read by the sourced lib/system-proxy.sh.
SYSTEM_PROXY_CONFIG_ROOT="$SING_BOX_CONFIG_ROOT"
SING_BOX_DATA_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/sing-box"
SING_BOX_STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/sing-box"
LEGACY_SYSTEMD_SERVICE_DIR="/etc/systemd/system"
LEGACY_SING_BOX_CONFIG_ROOT="/usr/local/etc/sing-box"

# Domain translations for zh_CN output.
# A case lookup instead of an associative array keeps this compatible with
# bash 3.2, the stock /bin/bash on macOS.
_translate_zh() {
	case "$1" in
	"Network Proxy") printf '%s\n' "网络代理" ;;
	"ERROR") printf '%s\n' "错误" ;;
	"WARNING") printf '%s\n' "警告" ;;
	"INFO") printf '%s\n' "信息" ;;
	"DEBUG") printf '%s\n' "调试" ;;
	"VPN Service Status") printf '%s\n' "VPN 服务状态" ;;
	"System Environment") printf '%s\n' "系统环境" ;;
	"Runtime Environment") printf '%s\n' "运行环境" ;;
	"Desktop Environment") printf '%s\n' "桌面环境" ;;
	"Support Level") printf '%s\n' "支持级别" ;;
	"System") printf '%s\n' "系统" ;;
	"Runtime") printf '%s\n' "运行环境" ;;
	"Desktop") printf '%s\n' "桌面环境" ;;
	"Support") printf '%s\n' "支持级别" ;;
	"systemctl") printf '%s\n' "systemctl" ;;
	"dconf") printf '%s\n' "dconf" ;;
	"launchctl") printf '%s\n' "launchctl" ;;
	"networksetup") printf '%s\n' "networksetup" ;;
	"macos") printf '%s\n' "macOS" ;;
	"aqua") printf '%s\n' "Aqua" ;;
	"Network service") printf '%s\n' "网络服务" ;;
	"launchctl is not available. Use -f/--force to set proxy anyway.") printf '%s\n' "launchctl 不可用。如需仍然设置代理，请使用 -f/--force。" ;;
	"Cannot determine VPN status - launchctl not available") printf '%s\n' "无法确定 VPN 状态，launchctl 不可用。" ;;
	"networksetup is not available. Skipping system proxy settings.") printf '%s\n' "networksetup 不可用，跳过系统代理设置。" ;;
	"Could not determine the active network service. Skipping system proxy settings.") printf '%s\n' "无法确定当前活动的网络服务，跳过系统代理设置。" ;;
	"Set macOS system proxy settings.") printf '%s\n' "设置 macOS 系统代理。" ;;
	"Unset macOS system proxy settings.") printf '%s\n' "清除 macOS 系统代理。" ;;
	"Changing the macOS system proxy requires administrator privileges.") printf '%s\n' "修改 macOS 系统代理需要管理员权限。" ;;
	"Failed to change the macOS system proxy settings.") printf '%s\n' "修改 macOS 系统代理设置失败。" ;;
	"macOS users can install jq with: brew install jq") printf '%s\n' "macOS 用户可直接运行：brew install jq" ;;
	"Docker Desktop manages the daemon proxy in its own settings. Only the Docker client config was updated.") printf '%s\n' "Docker Desktop 在其自身设置中管理守护进程代理，此处仅更新了 Docker 客户端配置。" ;;
	"Proxy target") printf '%s\n' "代理目标" ;;
	"Service") printf '%s\n' "服务" ;;
	"Services") printf '%s\n' "服务" ;;
	"Protocol") printf '%s\n' "协议" ;;
	"Active protocol") printf '%s\n' "活动协议" ;;
	"Protocols") printf '%s\n' "协议" ;;
	"Selected protocol") printf '%s\n' "当前协议" ;;
	"Selected route") printf '%s\n' "当前路由策略" ;;
	"Active TUN route") printf '%s\n' "当前 TUN 路由" ;;
	"Status") printf '%s\n' "状态" ;;
	"Shell") printf '%s\n' "Shell" ;;
	"HTTP(S)") printf '%s\n' "HTTP(S)" ;;
	"SOCKS") printf '%s\n' "SOCKS" ;;
	"No proxy") printf '%s\n' "不代理" ;;
	"Network") printf '%s\n' "网络" ;;
	"Internet") printf '%s\n' "互联网" ;;
	"Internet (CN)") printf '%s\n' "互联网（CN）" ;;
	"LAN") printf '%s\n' "局域网" ;;
	"Portal") printf '%s\n' "Portal" ;;
	"VPS") printf '%s\n' "VPS" ;;
	"User") printf '%s\n' "用户" ;;
	"Hostname") printf '%s\n' "主机名" ;;
	"Location") printf '%s\n' "位置" ;;
	"Cycle") printf '%s\n' "周期" ;;
	"Upload") printf '%s\n' "上传" ;;
	"Download") printf '%s\n' "下载" ;;
	"Bandwidth") printf '%s\n' "带宽" ;;
	"Quota") printf '%s\n' "配额" ;;
	"Total") printf '%s\n' "总计" ;;
	"Plan usage") printf '%s\n' "套餐占用" ;;
	"Last updated") printf '%s\n' "最近更新" ;;
	"Reset date") printf '%s\n' "重置日期" ;;
	"Resolved Proxy Target") printf '%s\n' "代理目标" ;;
	"systemctl Availability") printf '%s\n' "systemctl 可用性" ;;
	"dconf Availability") printf '%s\n' "dconf 可用性" ;;
	"Related Environment Variables") printf '%s\n' "涉及的环境变量" ;;
	"Detected Protocols") printf '%s\n' "检测到的协议" ;;
	"Detected Service Status") printf '%s\n' "检测到的服务状态" ;;
	"Git") printf '%s\n' "Git" ;;
	"Global") printf '%s\n' "全局" ;;
	"Local") printf '%s\n' "本地" ;;
	"Mode") printf '%s\n' "模式" ;;
	"Internet:") printf '%s\n' "互联网：" ;;
	"Internet (CN):") printf '%s\n' "互联网（CN）：" ;;
	"LAN:") printf '%s\n' "局域网：" ;;
	"This platform is not supported.") printf '%s\n' "此脚本不支持当前操作系统。" ;;
	"Make sure the VPN client is working on host.") printf '%s\n' "正在 WSL2/Docker 中运行。请确保代理客户端正在您的主机上运行。" ;;
	"Set shell proxy environment variables.") printf '%s\n' "正在设置当前 shell 的代理环境变量。" ;;
	"Unset shell proxy environment variables.") printf '%s\n' "正在取消当前 shell 的代理环境变量。" ;;
	"Set git network proxy.") printf '%s\n' "正在应用 git 代理配置。" ;;
	"Unset git network proxy.") printf '%s\n' "正在移除 git 代理配置。" ;;
	"Set GNOME desktop proxy settings.") printf '%s\n' "正在应用 GNOME 桌面代理设置。" ;;
	"Unset GNOME desktop proxy settings.") printf '%s\n' "正在移除 GNOME 桌面代理设置。" ;;
	"GNOME desktop environment not detected. Skipping desktop proxy settings.") printf '%s\n' "未检测到 GNOME 桌面环境，跳过桌面代理设置。" ;;
	"dconf is not available. Skipping desktop proxy settings.") printf '%s\n' "dconf 不可用，跳过桌面代理设置。" ;;
	"The shell is using network proxy.") printf '%s\n' "代理在此 shell 会话中已激活。" ;;
	"The shell is {NOT} using network proxy.") printf '%s\n' "代理在此 shell 会话中未激活。" ;;
	"The shell is not exporting any proxy-related environment variables.") printf '%s\n' "当前 shell 未导出任何代理相关环境变量。" ;;
	"Unknown. For WSL2, the VPN client is probably running on the host machine. Please check manually.") printf '%s\n' "无法在 WSL2 中确定 VPN 状态。请手动检查主机。" ;;
	"Unknown. For a Docker container, the VPN client is probably running on the host machine. Please check manually.") printf '%s\n' "无法在 Docker 容器中确定 VPN 状态。请手动检查主机。" ;;
	"Cannot determine VPN status - systemctl not available") printf '%s\n' "无法确定 VPN 状态，systemctl 不可用。" ;;
	"Failed to detect public IP in {} seconds.") printf '%s\n' "在 {} 秒内检测公共 IP 失败。请检查您的网络连接。" ;;
	"Failed to get private IP") printf '%s\n' "获取私有 IP 失败。" ;;
	"Set Docker daemon proxy settings.") printf '%s\n' "正在配置 Docker 守护进程代理设置。" ;;
	"Set Docker client proxy settings.") printf '%s\n' "正在配置 Docker 客户端代理设置。" ;;
	"Unset Docker daemon proxy settings.") printf '%s\n' "正在移除 Docker 守护进程代理设置。" ;;
	"Unset Docker client proxy settings.") printf '%s\n' "正在移除 Docker 客户端代理设置。" ;;
	"Docker daemon restarted successfully.") printf '%s\n' "Docker 守护进程重启成功。" ;;
	"Failed to restart Docker daemon.") printf '%s\n' "Docker 守护进程重启失败。" ;;
	"Docker is not installed or not available.") printf '%s\n' "Docker 未安装或不可用。" ;;
	"Docker client config is not valid JSON.") printf '%s\n' "Docker 客户端配置不是有效的 JSON。" ;;
	"Failed to update Docker client proxy settings.") printf '%s\n' "更新 Docker 客户端代理设置失败。" ;;
	"Failed to clear Docker client proxy settings.") printf '%s\n' "移除 Docker 客户端代理设置失败。" ;;
	"Git is not installed or not available.") printf '%s\n' "Git 未安装或不可用。" ;;
	"jq is required for this command.") printf '%s\n' "此命令需要 jq。" ;;
	"Install jq first: https://jqlang.org/download/") printf '%s\n' "请先安装 jq：https://jqlang.org/download/" ;;
	"Ubuntu users can install jq with: sudo apt install jq") printf '%s\n' "Ubuntu 用户可直接运行：sudo apt install jq" ;;
	"Git local proxy settings require a Git repository.") printf '%s\n' "git 本地代理设置需要在 Git 仓库中执行。" ;;
	"Failed to get IP from cip.cc in {} seconds.") printf '%s\n' "在 {} 秒内从 cip.cc 获取 IP 失败。请检查您的网络连接。" ;;
	"Toggling Commands") printf '%s\n' "控制命令" ;;
	"Checking Commands") printf '%s\n' "检查命令" ;;
	"Enable composite proxy config") printf '%s\n' "启用组合代理配置" ;;
	"Remove composite proxy config") printf '%s\n' "移除组合代理配置" ;;
	"Switch active proxy protocol") printf '%s\n' "切换当前代理协议" ;;
	"Switch mixed-mode routing strategy") printf '%s\n' "切换混合模式路由策略" ;;
	"Show saved mixed-mode routing strategy") printf '%s\n' "显示已保存的混合模式路由策略" ;;
	"Enable user proxy systemd service") printf '%s\n' "启用用户代理 systemd 服务" ;;
	"Disable user proxy systemd service") printf '%s\n' "停用用户代理 systemd 服务" ;;
	"Enable shell proxy") printf '%s\n' "启用 shell 代理" ;;
	"Remove shell proxy") printf '%s\n' "移除 shell 代理" ;;
	"Enable git proxy") printf '%s\n' "启用 git 代理" ;;
	"Remove git proxy") printf '%s\n' "移除 git 代理" ;;
	"Enable desktop proxy") printf '%s\n' "启用桌面代理" ;;
	"Remove desktop proxy") printf '%s\n' "移除桌面代理" ;;
	"Enable Docker proxy") printf '%s\n' "启用 Docker 代理" ;;
	"Remove Docker proxy") printf '%s\n' "移除 Docker 代理" ;;
	"Run common proxy checks") printf '%s\n' "运行常用代理检查" ;;
	"Show runtime and desktop support status") printf '%s\n' "显示运行环境与桌面支持状态" ;;
	"Show detected protocol and service status") printf '%s\n' "显示检测到的协议和服务状态" ;;
	"Show or select mixed inbound port") printf '%s\n' "显示或选择混合入站端口" ;;
	"Show proxy environment variables for current shell") printf '%s\n' "显示当前 shell 的代理环境变量" ;;
	"Show git proxy settings") printf '%s\n' "显示 git 代理设置" ;;
	"Show GNOME desktop proxy settings") printf '%s\n' "显示 GNOME 桌面代理设置" ;;
	"Check public IP (ipinfo.io)") printf '%s\n' "检查公网 IP（ipinfo.io）" ;;
	"Check public IP (cip.cc)") printf '%s\n' "检查公网 IP（cip.cc）" ;;
	"Show private/LAN IP") printf '%s\n' "显示私有/局域网 IP" ;;
	"Show VPS and user quota") printf '%s\n' "显示 VPS 与用户配额" ;;
	"No installed sing-box client protocol detected.") printf '%s\n' "未检测到已安装的 sing-box 客户端协议。" ;;
	"Multiple installed sing-box client protocols detected. Run \`proxy protocol <protocol>\` first.") printf '%s\n' "检测到多个已安装的 sing-box 客户端协议。请先运行 \`proxy protocol <protocol>\`。" ;;
	"Missing protocol. Supported protocols: {}") printf '%s\n' "缺少协议。支持的协议：{}" ;;
	"Unsupported protocol {}. Supported protocols: {}") printf '%s\n' "不支持协议 {}。支持的协议：{}" ;;
	"Protocol {} is not installed for this user. Reinstall from the portal if you have access.") printf '%s\n' "当前用户未安装协议 {}。如果您有权限，请从 portal 重新安装。" ;;
	"Protocol service {} is not installed for this user. Reinstall from the portal if you have access.") printf '%s\n' "当前用户未安装协议服务 {}。如果您有权限，请从 portal 重新安装。" ;;
	"Failed to save selected proxy protocol.") printf '%s\n' "保存选中的代理协议失败。" ;;
	"Failed to switch proxy protocol to {}.") printf '%s\n' "切换代理协议到 {} 失败。" ;;
	"Proxy protocol switched to {}.") printf '%s\n' "代理协议已切换到 {}。" ;;
	"Missing route. Supported routes: {}") printf '%s\n' "缺少路由策略。支持的策略：{}" ;;
	"Unsupported route {}. Supported routes: {}") printf '%s\n' "不支持路由策略 {}。支持的策略：{}" ;;
	"No installed mixed proxy protocols were found.") printf '%s\n' "未找到已安装的混合代理协议。" ;;
	"Route variant {} is missing. Reinstall the client.") printf '%s\n' "缺少路由配置 {}。请重新安装客户端。" ;;
	"The installed sing-box binary is missing or is not executable.") printf '%s\n' "已安装的 sing-box 二进制文件缺失或不可执行。" ;;
	"Route variant {} failed sing-box validation.") printf '%s\n' "路由配置 {} 未通过 sing-box 校验。" ;;
	"Route {} cannot reach the network on this machine, so it was not installed.") printf '%s\n' "路由策略 {} 在本机无法联网，因此未安装。" ;;
	"The ai route resolves direct traffic through systemd-resolved, which is not running.") printf '%s\n' "ai 路由通过 systemd-resolved 解析直连流量，但该服务未运行。" ;;
	"Routes that proxy unknown destinations still work: proxy route gfw") printf '%s\n' "代理未知目标的路由仍可使用：proxy route gfw" ;;
	"Could not verify route {}. The current route fails the same check, so this machine may be offline.") printf '%s\n' "无法验证路由策略 {}。当前策略同样未通过该检查，本机可能处于离线状态。" ;;
	"Failed to stop active proxy services for route switching.") printf '%s\n' "切换路由策略时未能停止活动的代理服务。" ;;
	"Failed to install route {}. Restoring the previous route.") printf '%s\n' "安装路由策略 {} 失败，正在恢复先前策略。" ;;
	"Failed to restart active proxy services. Restoring the previous route.") printf '%s\n' "未能重启活动代理服务，正在恢复先前策略。" ;;
	"Route rollback did not complete.") printf '%s\n' "路由策略回滚未完成。" ;;
	"Mixed-mode route switched to {}.") printf '%s\n' "混合模式路由策略已切换到 {}。" ;;
	"fixed/global") printf '%s\n' "固定/全局" ;;
	"No active sing-box service detected. Use -f/--force to set proxy anyway.") printf '%s\n' "未检测到活动的 sing-box 服务。如需仍然设置代理，请使用 -f/--force。" ;;
	"Proxy service {} is not active. Use -f/--force to set proxy anyway.") printf '%s\n' "代理服务 {} 未激活。如需仍然设置代理，请使用 -f/--force。" ;;
	"Failed to enable proxy service {}.") printf '%s\n' "启用代理服务 {} 失败。" ;;
	"Failed to disable proxy service {}.") printf '%s\n' "停用代理服务 {} 失败。" ;;
	"systemctl is not available. Use -f/--force to set proxy anyway.") printf '%s\n' "systemctl 不可用。如需仍然设置代理，请使用 -f/--force。" ;;
	"Use -f or --force to apply proxy settings even when no active sing-box service is detected.") printf '%s\n' "即使未检测到活动的 sing-box 服务，也可使用 -f 或 --force 强制应用代理设置。" ;;
	"Could not find a free port after 20 attempts.") printf '%s\n' "尝试 20 次后仍未找到空闲端口。" ;;
	"Missing value for --port.") printf '%s\n' "缺少 --port 的值。" ;;
	"Port must be a decimal number between 1024 and 65535.") printf '%s\n' "端口必须是 1024 到 65535 之间的十进制数字。" ;;
	"Port {} is already in use.") printf '%s\n' "端口 {} 已被占用。" ;;
	"Port {} is occupied; using port {} instead.") printf '%s\n' "端口 {} 已被占用，改用端口 {}。" ;;
	"Selected port") printf '%s\n' "当前端口" ;;
	"The running mixed proxy is not listening on the selected port.") printf '%s\n' "正在运行的混合代理未监听当前所选端口。" ;;
	"Could not verify that port {} is free for both TCP and UDP.") printf '%s\n' "无法确认端口 {} 的 TCP 和 UDP 均空闲。" ;;
	"Failed to change the mixed inbound port. Restoring the previous port.") printf '%s\n' "更改混合入站端口失败，正在恢复先前端口。" ;;
	"Port rollback did not complete.") printf '%s\n' "端口回滚未完成。" ;;
	"Mixed inbound port switched to {}.") printf '%s\n' "混合入站端口已切换到 {}。" ;;
	"Unknown argument") printf '%s\n' "未知参数" ;;
	"Run \`proxy help\` to see available commands.") printf '%s\n' "运行 \`proxy help\` 查看可用命令。" ;;
	"Portal base URL is not configured. Reinstall via the hosted installer or set SBM_PORTAL_BASE_URL.") printf '%s\n' "Portal 地址未配置。请通过托管安装器重新安装，或设置 SBM_PORTAL_BASE_URL。" ;;
	"Portal base URL is invalid.") printf '%s\n' "Portal 地址无效。" ;;
	"Failed to fetch VPS info from portal.") printf '%s\n' "从 portal 获取 VPS 信息失败。" ;;
	"Portal credentials are not configured. Set SBM_TOKEN or SBM_USERNAME/SBM_PASSWORD, or re-run the hosted installer.") printf '%s\n' "未配置 portal 凭据。请设置 SBM_TOKEN 或 SBM_USERNAME/SBM_PASSWORD，或重新运行托管安装器。" ;;
	"Saved portal token expired or invalid. Set SBM_USERNAME/SBM_PASSWORD or re-run the hosted installer.") printf '%s\n' "保存的 portal token 已过期或无效。请设置 SBM_USERNAME/SBM_PASSWORD，或重新运行托管安装器。" ;;
	"Portal credentials are invalid.") printf '%s\n' "Portal 凭据无效。" ;;
	"Failed to fetch usage info from portal.") printf '%s\n' "从 portal 获取用量信息失败。" ;;
	"Usage info unavailable") printf '%s\n' "用量信息不可用" ;;
	"Not collected yet") printf '%s\n' "尚未采集" ;;
	"Client Update") printf '%s\n' "客户端更新" ;;
	"Installed version") printf '%s\n' "已安装版本" ;;
	"Portal version") printf '%s\n' "Portal 版本" ;;
	"Upgrade policy") printf '%s\n' "升级策略" ;;
	"Client is current") printf '%s\n' "客户端已是最新版本" ;;
	"Client update available") printf '%s\n' "有可用的客户端更新" ;;
	"A sing-box client update is highly suggested. Run \`proxy upgrade\`.") printf '%s\n' "强烈建议升级 sing-box 客户端。请运行 \`proxy upgrade\`。" ;;
	"A sing-box client upgrade is required. Run \`proxy upgrade\`.") printf '%s\n' "sing-box 客户端需要升级。请运行 \`proxy upgrade\`。" ;;
	"Remove the installed client") printf '%s\n' "卸载已安装的客户端" ;;
	"Installed uninstaller not found. Reinstall the client or use client-uninstall.sh from the archive.") printf '%s\n' "找不到已安装的卸载脚本。请重新安装客户端，或使用压缩包中的 client-uninstall.sh。" ;;
	"Client update metadata is unavailable.") printf '%s\n' "客户端更新元数据不可用。" ;;
	"Failed to contact the client update service.") printf '%s\n' "无法连接客户端更新服务。" ;;
	"Upgrade the installed client") printf '%s\n' "升级已安装的客户端" ;;
	"Client upgraded, but some previous state could not be restored.") printf '%s\n' "客户端已升级，但部分原有状态未能恢复。" ;;
	"Show installed and portal client versions") printf '%s\n' "显示已安装和 Portal 客户端版本" ;;
	"Show bundled sing-box and manager versions") printf '%s\n' "显示随包提供的 sing-box 和管理器版本" ;;
	"VPS info unavailable") printf '%s\n' "VPS 信息不可用" ;;
	"yes") printf '%s\n' "是" ;;
	"no") printf '%s\n' "否" ;;
	"linux") printf '%s\n' "Linux" ;;
	"docker") printf '%s\n' "Docker 容器" ;;
	"wsl2") printf '%s\n' "WSL2" ;;
	"gnome") printf '%s\n' "GNOME" ;;
	"other") printf '%s\n' "其他" ;;
	"full") printf '%s\n' "完整" ;;
	"partial") printf '%s\n' "部分" ;;
	"unsupported") printf '%s\n' "不支持" ;;
	"unknown") printf '%s\n' "未知" ;;
	"none") printf '%s\n' "无" ;;
	"Refusing {}: machine-wide TUN mode is not safely off.") printf '%s\n' "拒绝执行 {}：全局 TUN 模式尚未安全关闭。" ;;
	"a TUN transaction is in progress") printf '%s\n' "TUN 事务正在进行中" ;;
	"Run \`proxy tun off\` first, then run this command again.") printf '%s\n' "请先运行 \`proxy tun off\`，然后重新执行此命令。" ;;
	"Machine-wide TUN mode is still active; these settings do not control it.") printf '%s\n' "全局 TUN 模式仍处于活动状态，这些设置不会影响它。" ;;
	"Run \`proxy tun off\` to stop the tunnel.") printf '%s\n' "运行 \`proxy tun off\` 以停止隧道。" ;;
	"Machine-wide TUN mode is owned by another local user (uid {}).") printf '%s\n' "全局 TUN 模式属于另一位本地用户（uid {}）。" ;;
	"That user runs \`proxy tun off\`; automatic takeover is not supported.") printf '%s\n' "请由该用户运行 \`proxy tun off\`；不支持自动接管。" ;;
	"sudo is required to change machine-wide TUN mode.") printf '%s\n' "修改全局 TUN 模式需要 sudo。" ;;
	"Changing machine-wide TUN mode requires administrator privileges.") printf '%s\n' "修改全局 TUN 模式需要管理员权限。" ;;
	"Administrator authentication failed.") printf '%s\n' "管理员身份验证失败。" ;;
	"useradd is required to create the TUN service account.") printf '%s\n' "创建 TUN 服务账户需要 useradd。" ;;
	"systemd-run is required to verify TUN capabilities.") printf '%s\n' "验证 TUN 能力需要 systemd-run。" ;;
	"iproute2 (ip) is required for TUN mode.") printf '%s\n' "TUN 模式需要 iproute2（ip）。" ;;
	"Machine-wide TUN mode supports native Linux only, not {}.") printf '%s\n' "全局 TUN 模式仅支持原生 Linux，不支持 {}。" ;;
	"A running systemd system manager is required for TUN mode.") printf '%s\n' "TUN 模式需要正在运行的 systemd 系统管理器。" ;;
	"/dev/net/tun is missing; the kernel tun module is not available.") printf '%s\n' "缺少 /dev/net/tun，内核 tun 模块不可用。" ;;
	"Refusing to change machine-wide routing from a remote shell.") printf '%s\n' "拒绝从远程 shell 修改全局路由。" ;;
	"Run \`proxy tun on\` from a local session on that machine.") printf '%s\n' "请在该机器的本地会话中运行 \`proxy tun on\`。" ;;
	"Protocol {} has no installed TUN profile. Reinstall from the portal if you have access.") printf '%s\n' "协议 {} 没有已安装的 TUN 配置。如果您有权限，请从 portal 重新安装。" ;;
	"The sing-box binary {} is missing or is not a regular file.") printf '%s\n' "sing-box 二进制文件 {} 缺失或不是普通文件。" ;;
	"The TUN configuration {} is missing or is not a regular file.") printf '%s\n' "TUN 配置文件 {} 缺失或不是普通文件。" ;;
	"sha256sum or shasum is required to verify managed TUN files.") printf '%s\n' "校验受管 TUN 文件需要 sha256sum 或 shasum。" ;;
	"An unexpected account already owns the sing-box-tun name.") printf '%s\n' "已有非预期的账户占用 sing-box-tun 名称。" ;;
	"Another interface already holds the managed TUN name.") printf '%s\n' "已有其他网络接口占用受管 TUN 接口名称。" ;;
	"The managed TUN address ranges conflict with an existing interface.") printf '%s\n' "受管 TUN 地址段与现有网络接口冲突。" ;;
	"A sing-box service outside this client is active; stop it first.") printf '%s\n' "存在本客户端之外的活动 sing-box 服务，请先停止它。" ;;
	"A shell proxy this client does not own was left unchanged.") printf '%s\n' "检测到非本客户端设置的 shell 代理，已保持不变。" ;;
	"A global Git proxy this client does not own was left unchanged.") printf '%s\n' "检测到非本客户端设置的全局 Git 代理，已保持不变。" ;;
	"A desktop proxy this client does not own was left unchanged.") printf '%s\n' "检测到非本客户端设置的桌面代理，已保持不变。" ;;
	"A Docker proxy this client does not own was left unchanged.") printf '%s\n' "检测到非本客户端设置的 Docker 代理，已保持不变。" ;;
	"The previous TUN service did not stop.") printf '%s\n' "之前的 TUN 服务未能停止。" ;;
	"Failed to stop mixed proxy service {}.") printf '%s\n' "停止混合代理服务 {} 失败。" ;;
	"Shells and repositories configured earlier may still point at the stopped mixed inbound.") printf '%s\n' "此前已配置的 shell 与仓库可能仍指向已停止的混合入站。" ;;
	"Open a new shell, or run the matching \`proxy ... off\` command there.") printf '%s\n' "请打开新的 shell，或在受影响处运行对应的 \`proxy ... off\` 命令。" ;;
	"systemd-run is required to arm the TUN safeguard.") printf '%s\n' "启用 TUN 保护定时器需要 systemd-run。" ;;
	"The TUN safeguard timer could not be cancelled.") printf '%s\n' "无法取消 TUN 保护定时器。" ;;
	"Failed to stage the TUN binary.") printf '%s\n' "暂存 TUN 二进制文件失败。" ;;
	"Failed to stage the Naive runtime library.") printf '%s\n' "暂存 Naive 运行库失败。" ;;
	"Failed to stage the TUN configuration.") printf '%s\n' "暂存 TUN 配置文件失败。" ;;
	"Failed to stage the TUN service unit.") printf '%s\n' "暂存 TUN 服务单元失败。" ;;
	"The staged TUN configuration failed sing-box validation.") printf '%s\n' "暂存的 TUN 配置未通过 sing-box 校验。" ;;
	"The TUN service did not reach a stable running state.") printf '%s\n' "TUN 服务未能进入稳定运行状态。" ;;
	"The expected TUN interface {} did not appear.") printf '%s\n' "预期的 TUN 接口 {} 未出现。" ;;
	"No route was installed through the TUN interface.") printf '%s\n' "没有任何路由指向 TUN 接口。" ;;
	"DNS resolution failed through the tunnel.") printf '%s\n' "通过隧道进行 DNS 解析失败。" ;;
	"Proxy-free egress failed through the tunnel.") printf '%s\n' "在关闭代理变量的情况下通过隧道出网失败。" ;;
	"curl is not installed, so egress could not be verified.") printf '%s\n' "未安装 curl，无法验证出网。" ;;
	"The TUN interface packet counters are unavailable, so throughput was not verified.") printf '%s\n' "TUN 接口的数据包计数器不可用，未验证吞吐。" ;;
	"The TUN interface counters did not move, so traffic bypassed the tunnel.") printf '%s\n' "TUN 接口计数器没有变化，流量绕过了隧道。" ;;
	"Rolling back the TUN transaction.") printf '%s\n' "正在回滚 TUN 事务。" ;;
	"The attempted TUN service did not stop during rollback.") printf '%s\n' "回滚过程中未能停止本次尝试启动的 TUN 服务。" ;;
	"TUN interface, route, or rule artifacts survived the rollback.") printf '%s\n' "回滚后仍残留 TUN 接口、路由或策略规则。" ;;
	"Failed to restore the previous TUN generation.") printf '%s\n' "恢复上一代 TUN 文件失败。" ;;
	"Rollback did not complete; run \`proxy check tun\` before using the network.") printf '%s\n' "回滚未完成，请在使用网络前运行 \`proxy check tun\`。" ;;
	"Recovering an interrupted TUN transaction.") printf '%s\n' "正在恢复被中断的 TUN 事务。" ;;
	"Interrupted at phase {}.") printf '%s\n' "中断发生在阶段 {}。" ;;
	"A previous TUN transaction did not finish.") printf '%s\n' "上一次 TUN 事务未能完成。" ;;
	"Failed to record the TUN transaction journal.") printf '%s\n' "写入 TUN 事务日志失败。" ;;
	"Failed to create the managed TUN directories.") printf '%s\n' "创建受管 TUN 目录失败。" ;;
	"Failed to create the TUN service account.") printf '%s\n' "创建 TUN 服务账户失败。" ;;
	"The kernel refused to create a TUN device under the service sandbox.") printf '%s\n' "内核拒绝在服务沙箱中创建 TUN 设备。" ;;
	"Failed to stage and validate the TUN payload.") printf '%s\n' "暂存并校验 TUN 载荷失败。" ;;
	"Failed to arm the TUN safeguard.") printf '%s\n' "启用 TUN 保护定时器失败。" ;;
	"Failed to stop the existing proxy services.") printf '%s\n' "停止现有代理服务失败。" ;;
	"Failed to save the previous TUN generation.") printf '%s\n' "保存上一代 TUN 文件失败。" ;;
	"Failed to publish the staged TUN generation.") printf '%s\n' "发布暂存的 TUN 文件失败。" ;;
	"A mixed proxy service started again before the tunnel; refusing to overlap.") printf '%s\n' "隧道启动前混合代理服务再次启动，拒绝二者重叠运行。" ;;
	"Failed to start the TUN service.") printf '%s\n' "启动 TUN 服务失败。" ;;
	"The TUN service started but did not carry traffic.") printf '%s\n' "TUN 服务已启动，但没有承载流量。" ;;
	"The tunnel is running but could not be enabled at boot.") printf '%s\n' "隧道正在运行，但未能设置为开机自启。" ;;
	"The tunnel is running but its safeguard could not be cancelled.") printf '%s\n' "隧道正在运行，但未能取消其保护定时器。" ;;
	"Another TUN operation holds the transaction lock.") printf '%s\n' "另一个 TUN 操作正持有事务锁。" ;;
	"Machine-wide TUN mode is active with protocol {}.") printf '%s\n' "全局 TUN 模式已启用，使用协议 {}。" ;;
	"The tunnel is enabled at boot.") printf '%s\n' "隧道已设置为开机自启。" ;;
	"Machine-wide TUN mode is not provisioned.") printf '%s\n' "尚未部署全局 TUN 模式。" ;;
	"The TUN service did not stop.") printf '%s\n' "TUN 服务未能停止。" ;;
	"TUN interface, route, or rule artifacts are still present.") printf '%s\n' "仍残留 TUN 接口、路由或策略规则。" ;;
	"Machine-wide TUN mode is off.") printf '%s\n' "全局 TUN 模式已关闭。" ;;
	"Mixed mode was not restored.") printf '%s\n' "未自动恢复混合模式。" ;;
	"Run \`proxy on\` to return to the local mixed inbound.") printf '%s\n' "运行 \`proxy on\` 可返回本地混合入站。" ;;
	"conflict") printf '%s\n' "冲突" ;;
	"in progress") printf '%s\n' "进行中" ;;
	"not read; sudo would prompt") printf '%s\n' "未读取，读取需要 sudo 授权" ;;
	"Shell variables in other running processes and repository-local Git settings cannot be enumerated.") printf '%s\n' "无法枚举其他运行中进程的 shell 变量，以及仓库级 Git 设置。" ;;
	"match the manifest") printf '%s\n' "与清单一致" ;;
	"do not match the manifest") printf '%s\n' "与清单不一致" ;;
	"Enable machine-wide TUN mode") printf '%s\n' "启用全局 TUN 模式" ;;
	"Disable machine-wide TUN mode") printf '%s\n' "关闭全局 TUN 模式" ;;
	"Show machine-wide TUN capability and state") printf '%s\n' "显示全局 TUN 的能力与状态" ;;
	"TUN") printf '%s\n' "TUN" ;;
	"Supported") printf '%s\n' "是否支持" ;;
	"systemd") printf '%s\n' "systemd" ;;
	"/dev/net/tun") printf '%s\n' "/dev/net/tun" ;;
	"Interface") printf '%s\n' "网络接口" ;;
	"Unit") printf '%s\n' "服务单元" ;;
	"Installed") printf '%s\n' "已安装" ;;
	"Substate") printf '%s\n' "子状态" ;;
	"Enabled") printf '%s\n' "开机自启" ;;
	"MainPID") printf '%s\n' "主进程 PID" ;;
	"Restarts") printf '%s\n' "重启次数" ;;
	"Attestation") printf '%s\n' "状态证明" ;;
	"Transaction") printf '%s\n' "事务" ;;
	"Safely off") printf '%s\n' "安全关闭" ;;
	"Owner uid") printf '%s\n' "所有者 uid" ;;
	"Generation") printf '%s\n' "代次" ;;
	"Managed files") printf '%s\n' "受管文件" ;;
	"Mutable by you") printf '%s\n' "当前用户可修改" ;;
	"Manifest") printf '%s\n' "清单" ;;
	"Source config") printf '%s\n' "源配置" ;;
	"Provisioned config") printf '%s\n' "已部署配置" ;;
	"auto_redirect") printf '%s\n' "auto_redirect" ;;
	"Start at boot") printf '%s\n' "开机自启" ;;
	"Profile auto_redirect") printf '%s\n' "配置的 auto_redirect" ;;
	"nftables") printf '%s\n' "nftables" ;;
	"The tunnel is running but will not start after a reboot; run \`proxy tun on --persist\` to keep it.") printf '%s\n' "隧道正在运行，但重启后不会自动启动；如需保持请运行 \`proxy tun on --persist\`。" ;;
	"TUN service") printf '%s\n' "TUN 服务" ;;
	"TUN status") printf '%s\n' "TUN 状态" ;;
	"TUN enabled") printf '%s\n' "TUN 开机自启" ;;
	"on") printf '%s\n' "开启" ;;
	"off") printf '%s\n' "关闭" ;;
	*) return 1 ;;
	esac
}

_has() {
	command -v "$1" 1>/dev/null 2>&1
}

# Logging wrappers
_error() { error "$*"; }
_warning() { warning "$*"; }
_info() { info "$*"; }
_debug() { debug "$*"; }

_proxy_show_help_hint() {
	hint "$(_t "Run \`proxy help\` to see available commands.")"
}

_proxy_unknown_argument() {
	_error "$(_t 'Unknown argument'): $1"
	_proxy_show_help_hint
}

# Runtime and desktop detection helpers
_proxy_is_linux() {
	[[ "$(uname -s 2>/dev/null)" == "Linux" ]]
}

_proxy_is_docker() {
	[[ -f "/.dockerenv" ]]
}

_proxy_is_wsl2() {
	if ! _proxy_is_linux; then
		return 1
	fi

	if [[ "$(uname -r 2>/dev/null)" == *WSL2* ]]; then
		return 0
	fi

	grep -qi microsoft /proc/version 2>/dev/null
}

_proxy_is_gnome_desktop() {
	if [[ -n "${GNOME_DESKTOP_SESSION_ID:-}" ]]; then
		return 0
	fi

	case "${XDG_CURRENT_DESKTOP:-}:${DESKTOP_SESSION:-}" in
	*GNOME* | *gnome*)
		return 0
		;;
	*)
		return 1
		;;
	esac
}

_proxy_runtime_kind() {
	if plat_is_macos; then
		printf 'macos\n'
	elif ! _proxy_is_linux; then
		printf 'unsupported\n'
	elif _proxy_is_docker; then
		printf 'docker\n'
	elif _proxy_is_wsl2; then
		printf 'wsl2\n'
	else
		printf 'linux\n'
	fi
}

_proxy_desktop_kind() {
	if plat_is_macos; then
		printf 'aqua\n'
	elif _proxy_is_gnome_desktop; then
		printf 'gnome\n'
	elif [[ -n "${XDG_CURRENT_DESKTOP:-}${DESKTOP_SESSION:-}${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
		printf 'other\n'
	else
		printf 'none\n'
	fi
}

_proxy_support_level() {
	local runtime desktop

	runtime=$(_proxy_runtime_kind)
	desktop=$(_proxy_desktop_kind)

	if [[ "$runtime" == "macos" ]]; then
		printf 'full\n'
	elif [[ "$runtime" == "linux" && "$desktop" == "gnome" ]]; then
		printf 'full\n'
	elif [[ "$runtime" == "unsupported" ]]; then
		printf 'unsupported\n'
	else
		printf 'partial\n'
	fi
}

_proxy_bool_label() {
	if [[ "$1" == true ]]; then
		printf '%s\n' "${BOLD}${GREEN}$(_t 'yes')${RESET}"
	else
		printf '%s\n' "$(_t 'no')"
	fi
}

_proxy_format_proxy_value() {
	local value="$1"

	case "$value" in
	"" | "$(_t 'none')" | "$(_t 'unknown')")
		printf '%s\n' "$value"
		;;
	*)
		printf '%s\n' "${BOLD}${YELLOW}${value}${RESET}"
		;;
	esac
}

_proxy_report_section() {
	printf '%s%s%s\n' "${BOLD}" "$(_t "$1")" "${RESET}"
}

_proxy_report_row() {
	local indent="${3:-$INDENT}"
	printf '%s%s: %s\n' "$indent" "$(_t "$1")" "$2"
}

_proxy_format_service_state() {
	case "$1" in
	active)
		printf '%s\n' "${BOLD}${GREEN}active${RESET}"
		;;
	inactive)
		printf '%s\n' "${BOLD}${RED}inactive${RESET}"
		;;
	*)
		printf '%s\n' "$1"
		;;
	esac
}

_proxy_format_toggle_state() {
	case "$1" in
	on)
		printf '%s\n' "${BOLD}${GREEN}$(_t 'on')${RESET}"
		;;
	off)
		printf '%s\n' "${BOLD}${RED}$(_t 'off')${RESET}"
		;;
	*)
		printf '%s\n' "$1"
		;;
	esac
}

_proxy_first_proxy_value() {
	local value

	for value in "$@"; do
		if [[ -n "$value" ]]; then
			printf '%s\n' "$value"
			return 0
		fi
	done

	return 1
}

_proxy_format_dual_value() {
	local first_value="$1"
	local second_value="$2"
	local first_label="$3"
	local second_label="$4"
	local value

	if [[ -n "$first_value" && -n "$second_value" && "$first_value" != "$second_value" ]]; then
		printf '%s=%s %s=%s\n' "$first_label" "$first_value" "$second_label" "$second_value"
		return 0
	fi

	if value=$(_proxy_first_proxy_value "$first_value" "$second_value"); then
		printf '%s\n' "$value"
		return 0
	fi

	return 1
}

_proxy_format_host_port_value() {
	local host="$1"
	local port="$2"

	if [[ -n "$host" && -n "$port" ]]; then
		printf '%s:%s\n' "$host" "$port"
		return 0
	fi

	if [[ -n "$host" ]]; then
		printf '%s\n' "$host"
		return 0
	fi

	if [[ -n "$port" ]]; then
		printf '%s\n' "$port"
		return 0
	fi

	return 1
}

_proxy_format_byte_count() {
	local value="${1:-0}"

	awk -v bytes="$value" 'BEGIN {
		split("B KiB MiB GiB TiB", suffixes, " ")
		unit_index = 1
		while (bytes >= 1024 && unit_index < 5) {
			bytes /= 1024
			unit_index += 1
		}
		if (unit_index == 1) {
			printf "%d %s\n", bytes, suffixes[unit_index]
		} else {
			printf "%.2f %s\n", bytes, suffixes[unit_index]
		}
	}'
}

# Project measured traffic onto the host's bandwidth counter.
#
# A relayed byte crosses the VPS interface twice, so a host billing inbound plus
# outbound charges roughly `relay_multiplier` times what sing-box recorded for
# the user. Printing only the measured total makes users read their share of the
# plan as half what it is. The multiplier is named in the output so the larger
# number is obviously derived rather than a competing measurement.
_proxy_format_plan_usage() {
	local total_bytes="${1:-0}"
	local multiplier="${2:-}"
	local plan_total_bytes="${3:-}"

	if [[ -z "$multiplier" ]]; then
		return 1
	fi

	awk -v total="$total_bytes" -v multiplier="$multiplier" -v plan="${plan_total_bytes:-0}" '
		function human(bytes,   suffixes, unit_index) {
			split("B KiB MiB GiB TiB", suffixes, " ")
			unit_index = 1
			while (bytes >= 1024 && unit_index < 5) {
				bytes /= 1024
				unit_index += 1
			}
			if (unit_index == 1) {
				return sprintf("%d %s", bytes, suffixes[unit_index])
			}
			return sprintf("%.2f %s", bytes, suffixes[unit_index])
		}
		BEGIN {
			billed = total * multiplier
			out = human(billed)
			if (plan > 0) {
				out = out " / " human(plan) sprintf(" (%.1f%%", billed * 100 / plan)
				if (multiplier != 1) {
					out = out sprintf(", x%g relay", multiplier)
				}
				out = out ")"
			} else if (multiplier != 1) {
				out = out sprintf(" (x%g relay)", multiplier)
			} else {
				# At a multiplier of 1 with no plan total the projection is
				# the measured total, and a second row repeating it reads as
				# a different quantity that happens to agree.
				exit 0
			}
			print out
		}'
}

_proxy_expect_no_args() {
	if [[ $# -gt 0 ]]; then
		_proxy_unknown_argument "$1"
		return 1
	fi
}

_proxy_os_release_value() {
	local field_name="$1"
	local key value

	if [[ ! -r /etc/os-release ]]; then
		return 1
	fi

	while IFS='=' read -r key value; do
		if [[ "$key" != "$field_name" ]]; then
			continue
		fi

		case "$value" in
		\"*\")
			value=${value#\"}
			value=${value%\"}
			;;
		esac

		printf '%s\n' "$value"
		return 0
	done </etc/os-release

	return 1
}

_proxy_linux_distro_id() {
	_proxy_os_release_value ID
}

_proxy_linux_distro_like() {
	_proxy_os_release_value ID_LIKE
}

_proxy_is_ubuntu_like() {
	local value distro_name

	for value in "$(_proxy_linux_distro_id || true)" "$(_proxy_linux_distro_like || true)"; do
		for distro_name in $value; do
			if [[ "$distro_name" == "ubuntu" ]]; then
				return 0
			fi
		done
	done

	return 1
}

_proxy_show_jq_install_hints() {
	if plat_is_macos; then
		hint "$(_t 'macOS users can install jq with: brew install jq')"
	elif _proxy_is_ubuntu_like; then
		hint "$(_t 'Ubuntu users can install jq with: sudo apt install jq')"
	else
		hint "$(_t 'Install jq first: https://jqlang.org/download/')"
	fi
}

_proxy_require_jq() {
	if _has jq; then
		return 0
	fi

	_error "$(_t 'jq is required for this command.')"
	_proxy_show_jq_install_hints
	return 1
}

_proxy_launchd_label_for_protocol() {
	printf 'io.sing-box.%s\n' "$1"
}

_proxy_launch_agent_dir() {
	printf '%s/Library/LaunchAgents\n' "$HOME"
}

_proxy_launchd_domain() {
	printf 'gui/%s\n' "$(id -u)"
}

_proxy_launchd_target_for_protocol() {
	printf '%s/%s\n' "$(_proxy_launchd_domain)" "$(_proxy_launchd_label_for_protocol "$1")"
}

# The service manager this platform uses, for availability checks and reporting.
_proxy_service_manager() {
	if plat_is_macos; then
		printf 'launchctl\n'
	else
		printf 'systemctl\n'
	fi
}

_proxy_service_manager_available() {
	_has "$(_proxy_service_manager)"
}

_proxy_service_manager_unavailable_warning() {
	if plat_is_macos; then
		_t 'launchctl is not available. Use -f/--force to set proxy anyway.'
	else
		_t 'systemctl is not available. Use -f/--force to set proxy anyway.'
	fi
}

_proxy_service_name_for_protocol() {
	if plat_is_macos; then
		_proxy_launchd_label_for_protocol "$1"
		return
	fi

	printf 'sing-box-%s.service\n' "$1"
}

_proxy_default_user_service_dir() {
	printf '%s/.config/systemd/user\n' "$HOME"
}

_proxy_default_user_config_root() {
	printf '%s/.config/sing-box\n' "$HOME"
}

_proxy_selected_protocol_file() {
	printf '%s/selected-protocol\n' "$SING_BOX_CONFIG_ROOT"
}

_proxy_selected_route_file() {
	printf '%s/selected-route\n' "$SING_BOX_CONFIG_ROOT"
}

_proxy_default_selected_protocol_file() {
	printf '%s/selected-protocol\n' "$(_proxy_default_user_config_root)"
}

_proxy_default_data_root() {
	printf '%s/.local/share/sing-box\n' "$HOME"
}

_proxy_docker_daemon_service_dir() {
	printf '/etc/systemd/system/docker.service.d\n'
}

_proxy_docker_daemon_proxy_file() {
	printf '%s/http-proxy.conf\n' "$(_proxy_docker_daemon_service_dir)"
}

_proxy_docker_config_dir() {
	printf '%s\n' "${DOCKER_CONFIG:-$HOME/.docker}"
}

_proxy_docker_config_file() {
	printf '%s/config.json\n' "$(_proxy_docker_config_dir)"
}

_proxy_read_portal_base_url_file() {
	local file_path="$1"
	local portal_base_url=""

	if [[ ! -f "$file_path" ]]; then
		return 1
	fi

	IFS= read -r portal_base_url <"$file_path" || true
	if [[ -z "$portal_base_url" ]]; then
		return 1
	fi

	printf '%s\n' "$portal_base_url"
}

_proxy_read_selected_protocol() {
	local file_path protocol default_file_path

	file_path=$(_proxy_selected_protocol_file)
	protocol=$(_proxy_read_portal_base_url_file "$file_path" || true)
	if [[ -n "$protocol" ]] && _proxy_is_supported_protocol "$protocol"; then
		printf '%s\n' "$protocol"
		return 0
	fi

	default_file_path=$(_proxy_default_selected_protocol_file)
	if [[ "$default_file_path" != "$file_path" ]]; then
		protocol=$(_proxy_read_portal_base_url_file "$default_file_path" || true)
		if [[ -n "$protocol" ]] && _proxy_is_supported_protocol "$protocol"; then
			printf '%s\n' "$protocol"
			return 0
		fi
	fi

	return 1
}

_proxy_selected_protocol() {
	local protocol

	protocol=$(_proxy_read_selected_protocol || true)
	if [[ -z "$protocol" ]]; then
		return 1
	fi

	if _proxy_user_protocol_config_exists "$protocol"; then
		printf '%s\n' "$protocol"
		return 0
	fi

	return 1
}

_proxy_preferred_protocol() {
	if _proxy_is_supported_protocol "${VPN_PROTOCOL:-}"; then
		printf '%s\n' "$VPN_PROTOCOL"
		return 0
	fi

	_proxy_selected_protocol
}

_proxy_save_selected_protocol() {
	local protocol="$1"
	local file_path file_dir tmp_file

	file_path=$(_proxy_selected_protocol_file)
	file_dir=${file_path%/*}
	tmp_file="${file_path}.tmp.$$"

	if ! mkdir -p "$file_dir"; then
		return 1
	fi

	if ! printf '%s\n' "$protocol" >"$tmp_file"; then
		rm -f "$tmp_file"
		return 1
	fi

	if ! mv "$tmp_file" "$file_path"; then
		rm -f "$tmp_file"
		return 1
	fi
}

_proxy_is_supported_route() {
	case "$1" in
	china | gfw | ai | global) return 0 ;;
	*) return 1 ;;
	esac
}

_proxy_read_selected_route() {
	local route=""
	local marker

	marker=$(_proxy_selected_route_file)
	if [[ -f "$marker" ]]; then
		IFS= read -r route <"$marker" || true
		route=${route%$'\r'}
		if _proxy_is_supported_route "$route"; then
			printf '%s\n' "$route"
			return 0
		fi
	fi

	# Pre-route releases shipped the China policy as config.json.
	if [[ -d "$SING_BOX_CONFIG_ROOT" ]]; then
		printf 'china\n'
		return 0
	fi
	return 1
}

_proxy_save_selected_route() {
	local route="$1"
	local marker marker_dir tmp_file

	marker=$(_proxy_selected_route_file)
	marker_dir=${marker%/*}
	tmp_file="${marker}.tmp.$$"
	mkdir -p "$marker_dir" || return 1
	if ! printf '%s\n' "$route" >"$tmp_file"; then
		rm -f "$tmp_file"
		return 1
	fi
	chmod 0600 "$tmp_file" || {
		rm -f "$tmp_file"
		return 1
	}
	if ! mv -f "$tmp_file" "$marker"; then
		rm -f "$tmp_file"
		return 1
	fi
}

_proxy_portal_token_file() {
	printf '%s/portal-token\n' "$SING_BOX_CONFIG_ROOT"
}

_proxy_default_portal_token_file() {
	printf '%s/portal-token\n' "$(_proxy_default_user_config_root)"
}

_proxy_normalize_portal_base_url() {
	local portal_base_url="${1:-}"

	portal_base_url=${portal_base_url%$'\r'}
	while [[ "$portal_base_url" == */ ]]; do
		portal_base_url=${portal_base_url%/}
	done

	if [[ -z "$portal_base_url" || "$portal_base_url" == *[[:space:]]* || "$portal_base_url" == *"?"* || "$portal_base_url" == *"#"* ]]; then
		return 1
	fi

	case "$portal_base_url" in
	https://* | http://localhost* | http://127.0.0.1* | http://[[]::1[]]*)
		printf '%s\n' "$portal_base_url"
		return 0
		;;
	esac

	return 1
}

_proxy_resolve_portal_base_url() {
	local portal_base_url file_path default_root

	if [[ -n "${SBM_PORTAL_BASE_URL:-}" ]]; then
		_proxy_normalize_portal_base_url "$SBM_PORTAL_BASE_URL"
		return $?
	fi

	file_path="${SING_BOX_DATA_ROOT}/portal-base-url"
	portal_base_url=$(_proxy_read_portal_base_url_file "$file_path" || true)
	if [[ -n "$portal_base_url" ]]; then
		_proxy_normalize_portal_base_url "$portal_base_url"
		return $?
	fi

	default_root=$(_proxy_default_data_root)
	if [[ "$default_root" != "$SING_BOX_DATA_ROOT" ]]; then
		file_path="${default_root}/portal-base-url"
		portal_base_url=$(_proxy_read_portal_base_url_file "$file_path" || true)
		if [[ -n "$portal_base_url" ]]; then
			_proxy_normalize_portal_base_url "$portal_base_url"
			return $?
		fi
	fi

	return 1
}

_proxy_resolve_portal_token() {
	local portal_token file_path default_file_path

	if [[ -n "${SBM_TOKEN:-}" ]]; then
		printf '%s\n' "$SBM_TOKEN"
		return 0
	fi

	file_path=$(_proxy_portal_token_file)
	portal_token=$(_proxy_read_portal_base_url_file "$file_path" || true)
	if [[ -n "$portal_token" ]]; then
		printf '%s\n' "$portal_token"
		return 0
	fi

	default_file_path=$(_proxy_default_portal_token_file)
	if [[ "$default_file_path" != "$file_path" ]]; then
		portal_token=$(_proxy_read_portal_base_url_file "$default_file_path" || true)
		if [[ -n "$portal_token" ]]; then
			printf '%s\n' "$portal_token"
			return 0
		fi
	fi

	return 1
}

_proxy_update_field() {
	local file_path="$1"
	local key="$2"
	local line

	[[ -f "$file_path" ]] || return 1
	while IFS= read -r line; do
		case "$line" in
		"${key}="*)
			printf '%s\n' "${line#*=}"
			return 0
			;;
		esac
	done <"$file_path"
	return 1
}

_proxy_client_version_file() {
	local file_path default_root
	file_path="${SING_BOX_DATA_ROOT}/client-version"
	if [[ ! -f "$file_path" ]]; then
		# Same fallback the portal URL and token readers use: a shell that sets
		# XDG_DATA_HOME after the install must still see the installed package.
		default_root=$(_proxy_default_data_root)
		if [[ "$default_root" != "$SING_BOX_DATA_ROOT" ]] &&
			[[ -f "${default_root}/client-version" ]]; then
			file_path="${default_root}/client-version"
		fi
	fi
	printf '%s\n' "$file_path"
}

_proxy_version() {
	local metadata_file upstream_version commit_sha dirty
	_proxy_expect_no_args "$@" || return 1
	metadata_file=$(_proxy_client_version_file)
	upstream_version=$(_proxy_update_field "$metadata_file" upstream_version || true)
	commit_sha=$(_proxy_update_field "$metadata_file" commit_sha || true)
	dirty=$(_proxy_update_field "$metadata_file" dirty || true)
	if [[ -n "$commit_sha" && "$dirty" == true ]]; then
		commit_sha="${commit_sha}-dirty"
	fi
	_proxy_report_row 'sing-box' "${upstream_version:-$(_t 'unknown')}"
	_proxy_report_row 'sing-box-manager' "${commit_sha:-$(_t 'unknown')}"
}

_proxy_update_cache_file() {
	printf '%s/client-update-cache\n' "$SING_BOX_STATE_ROOT"
}

# Records that a check was attempted, successful or not. Without it a portal the
# client cannot reach would make every new interactive shell pay the fetch
# timeout again instead of the documented once-a-day.
_proxy_update_attempt_file() {
	printf '%s/client-update-checked-at\n' "$SING_BOX_STATE_ROOT"
}

_proxy_update_suggested_marker() {
	printf '%s/client-update-suggested\n' "$SING_BOX_STATE_ROOT"
}

_proxy_update_payload_field() {
	local payload="$1"
	local key="$2"
	local line

	while IFS= read -r line; do
		case "$line" in
		"${key}="*)
			printf '%s\n' "${line#*=}"
			return 0
			;;
		esac
	done <<<"$payload"
	return 1
}

_proxy_fetch_client_update() {
	local portal_base_url="$1"
	local update_url="${portal_base_url}/api/client-update"

	if _has curl; then
		curl --fail --silent --location --connect-timeout 1 --max-time 2 "$update_url" 2>/dev/null
		return $?
	fi
	if _has wget; then
		# --tries=1: wget retries 20 times by default, which would turn an
		# unreachable portal into a minutes-long stall at shell startup.
		wget --quiet --tries=1 --timeout=2 -O - "$update_url" 2>/dev/null
		return $?
	fi
	return 1
}

_proxy_record_update_attempt() {
	local attempt_file

	attempt_file=$(_proxy_update_attempt_file)
	mkdir -p "$SING_BOX_STATE_ROOT" 2>/dev/null || return 0
	printf 'checked_at=%s\n' "$(date +%s)" >"$attempt_file" 2>/dev/null || true
}

_proxy_refresh_client_update() {
	local portal_base_url payload schema policy client_build_id now cache_file tmp_file

	_proxy_record_update_attempt
	portal_base_url=$(_proxy_resolve_portal_base_url || true)
	[[ -n "$portal_base_url" ]] || return 1
	payload=$(_proxy_fetch_client_update "$portal_base_url" || true)
	[[ -n "$payload" ]] || return 1

	# Parsed in memory: a predictable name under a world-writable /tmp is a
	# symlink target an attacker can pre-create, and this runs on shell startup.
	schema=$(_proxy_update_payload_field "$payload" schema || true)
	policy=$(_proxy_update_payload_field "$payload" policy || true)
	client_build_id=$(_proxy_update_payload_field "$payload" client_build_id || true)
	[[ "$schema" == 1 ]] || return 1
	case "$policy" in off | suggested | required) ;; *) return 1 ;; esac
	if [[ -n "$client_build_id" && ! "$client_build_id" =~ ^[0-9a-f]{64}$ ]]; then
		return 1
	fi

	now=$(date +%s)
	cache_file=$(_proxy_update_cache_file)
	mkdir -p "$SING_BOX_STATE_ROOT" || return 1
	tmp_file="${cache_file}.tmp.$$"
	{
		printf 'checked_at=%s\n' "$now"
		printf '%s\n' "$payload"
	} >"$tmp_file" || {
		rm -f "$tmp_file"
		return 1
	}
	chmod 0600 "$tmp_file" || true
	mv -f "$tmp_file" "$cache_file"
}

_proxy_update_cache_is_due() {
	local attempt_file checked_at now
	attempt_file=$(_proxy_update_attempt_file)
	checked_at=$(_proxy_update_field "$attempt_file" checked_at || true)
	[[ "$checked_at" =~ ^[0-9]+$ ]] || return 0
	now=$(date +%s)
	((now - checked_at >= 86400))
}

_proxy_update_status() {
	local installed_file cache_file installed_id portal_id policy
	installed_file=$(_proxy_client_version_file)
	cache_file=$(_proxy_update_cache_file)
	installed_id=$(_proxy_update_field "$installed_file" client_build_id || true)
	portal_id=$(_proxy_update_field "$cache_file" client_build_id || true)
	policy=$(_proxy_update_field "$cache_file" policy || true)

	if [[ -z "$installed_id" || -z "$portal_id" ]]; then
		printf 'unavailable\n'
	elif [[ "$installed_id" == "$portal_id" ]]; then
		printf 'current\n'
	elif [[ "$policy" == off ]]; then
		printf 'off\n'
	else
		printf 'available\n'
	fi
}

# The portal's `message` field stays out of the shell-startup notice; `proxy check
# update` reports it in full for anyone who wants the why.
_proxy_show_client_update_notice() {
	local cache_file update_state policy portal_id marker_file shown_id
	cache_file=$(_proxy_update_cache_file)
	update_state=$(_proxy_update_status)
	[[ "$update_state" == available ]] || return 0

	policy=$(_proxy_update_field "$cache_file" policy || true)
	portal_id=$(_proxy_update_field "$cache_file" client_build_id || true)
	case "$policy" in
	required)
		_warning "$(_t "A sing-box client upgrade is required. Run \`proxy upgrade\`.")"
		;;
	suggested)
		marker_file=$(_proxy_update_suggested_marker)
		shown_id=$(_proxy_update_field "$marker_file" client_build_id || true)
		[[ "$shown_id" == "$portal_id" ]] && return 0
		hint "$(_t "A sing-box client update is highly suggested. Run \`proxy upgrade\`.")"
		mkdir -p "$SING_BOX_STATE_ROOT" || return 0
		printf 'client_build_id=%s\n' "$portal_id" >"$marker_file" || true
		chmod 0600 "$marker_file" 2>/dev/null || true
		;;
	esac
}

_proxy_maybe_notice_client_update() {
	case "$-" in *i*) ;; *) return 0 ;; esac
	[[ "${SBM_SKIP_UPDATE_CHECK:-}" == 1 ]] && return 0
	[[ -f "$(_proxy_client_version_file)" ]] || return 0
	if _proxy_update_cache_is_due; then
		_proxy_refresh_client_update >/dev/null 2>&1 || true
	fi
	_proxy_show_client_update_notice
}

_proxy_check_update() {
	local installed_file cache_file update_state installed_sha portal_sha policy message deployed_at
	installed_file=$(_proxy_client_version_file)
	cache_file=$(_proxy_update_cache_file)
	if ! _proxy_refresh_client_update; then
		_warning "$(_t 'Failed to contact the client update service.')"
		[[ -f "$cache_file" ]] || return 1
	fi

	update_state=$(_proxy_update_status)
	installed_sha=$(_proxy_update_field "$installed_file" commit_sha || true)
	portal_sha=$(_proxy_update_field "$cache_file" commit_sha || true)
	policy=$(_proxy_update_field "$cache_file" policy || true)
	message=$(_proxy_update_field "$cache_file" message || true)
	deployed_at=$(_proxy_update_field "$cache_file" deployed_at || true)
	_proxy_report_section 'Client Update'
	_proxy_report_row 'Installed version' "${installed_sha:-$(_t 'unknown')}"
	_proxy_report_row 'Portal version' "${portal_sha:-$(_t 'unknown')}"
	_proxy_report_row 'Upgrade policy' "${policy:-$(_t 'unknown')}"
	[[ -z "$deployed_at" ]] || _proxy_report_row 'Last updated' "$deployed_at"
	[[ -z "$message" ]] || _proxy_report_row 'INFO' "$message"
	case "$update_state" in
	current) _info "$(_t 'Client is current')" ;;
	available) _warning "$(_t 'Client update available')" ;;
	off) _info "$(_t 'Client update available') ($(_t 'off'))" ;;
	*)
		_warning "$(_t 'Client update metadata is unavailable.')"
		return 1
		;;
	esac
}

_proxy_upgrade_client() {
	local protocol route port service_name was_active=false desktop_state
	local portal_base_url platform install_path setup_path preferences rc_enabled target_shell
	local previous_skip restore_ok=true
	local -a installer_args=()

	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi
	if ! tun_is_safely_off; then
		_error "$(_t 'Machine-wide TUN mode is not safely off')"
		hint "$(_t "Run \`proxy tun off\` first, then run the installer again.")"
		return 1
	fi
	portal_base_url=$(_proxy_resolve_portal_base_url || true)
	if [[ -z "$portal_base_url" ]]; then
		_warning "$(_t 'Portal base URL is not configured. Reinstall via the hosted installer or set SBM_PORTAL_BASE_URL.')"
		return 1
	fi
	protocol=$(_proxy_selected_protocol || true)
	if [[ -z "$protocol" ]]; then
		_error "$(_t 'No installed sing-box client protocol detected.')"
		return 1
	fi
	route=$(_proxy_read_selected_route || true)
	port=$(_proxy_read_selected_port)
	service_name=$(_proxy_service_name_for_protocol "$protocol")
	if _proxy_user_service_is_active "$service_name"; then
		was_active=true
	fi
	desktop_state=$(_proxy_tun_classify_desktop_proxy)

	preferences="${SING_BOX_DATA_ROOT}/install-preferences"
	rc_enabled=$(_proxy_update_field "$preferences" rc_enabled || true)
	target_shell=$(_proxy_update_field "$preferences" shell || true)
	installer_args=(-p "$protocol")
	[[ "$rc_enabled" == false ]] && installer_args+=(--no-rc)
	case "$target_shell" in bash | zsh) installer_args+=(--shell "$target_shell") ;; esac

	platform=$(plat_os)
	case "$platform" in
	linux) ;;
	darwin) platform=macos ;;
	*)
		_error "$(_t 'This platform is not supported.')"
		return 1
		;;
	esac
	install_path=$(mktemp "${TMPDIR:-/tmp}/sbm-upgrade.XXXXXX") || return 1
	if _has curl; then
		curl --fail --silent --show-error --location \
			--output "$install_path" "${portal_base_url}/install/${platform}.sh" || {
			rm -f "$install_path"
			return 1
		}
	elif _has wget; then
		wget --quiet -O "$install_path" "${portal_base_url}/install/${platform}.sh" || {
			rm -f "$install_path"
			return 1
		}
	else
		rm -f "$install_path"
		_error "curl or wget is required"
		return 1
	fi

	if ! bash "$install_path" "${installer_args[@]}"; then
		rm -f "$install_path"
		return 1
	fi
	rm -f "$install_path"

	setup_path="${SING_BOX_DATA_ROOT}/setup.sh"
	if [[ ! -f "$setup_path" ]]; then
		_error "$(_t 'Client update metadata is unavailable.')"
		return 1
	fi
	previous_skip="${SBM_SKIP_UPDATE_CHECK:-}"
	SBM_SKIP_UPDATE_CHECK=1
	# shellcheck source=/dev/null
	source "$setup_path"
	if [[ -n "$previous_skip" ]]; then
		SBM_SKIP_UPDATE_CHECK="$previous_skip"
	else
		unset SBM_SKIP_UPDATE_CHECK
	fi

	if [[ -n "$route" && "$route" != china ]]; then
		_proxy_switch_route "$route" || restore_ok=false
	fi
	if [[ -n "$port" ]]; then
		_proxy_apply_selected_port "$port" true || restore_ok=false
	fi
	if [[ "$was_active" != true ]]; then
		_proxy_user_service_stop "$protocol" || restore_ok=false
	fi
	if [[ "$desktop_state" == ours ]]; then
		_proxy_apply_desktop_proxy "$DEFAULT_PROXY_HOST" "${port:-$DEFAULT_PROXY_PORT}" || restore_ok=false
	fi

	if [[ "$restore_ok" != true ]]; then
		_warning "$(_t 'Client upgraded, but some previous state could not be restored.')"
		return 1
	fi
	_proxy_check_update >/dev/null 2>&1 || true
	return 0
}

_proxy_user_service_exists() {
	local service_name="$1"
	local default_dir

	if plat_is_macos; then
		[[ -f "$(_proxy_launch_agent_dir)/${service_name}.plist" ]]
		return
	fi

	if [[ -f "${USER_SYSTEMD_SERVICE_DIR}/${service_name}" ]]; then
		return 0
	fi

	default_dir=$(_proxy_default_user_service_dir)
	if [[ "$default_dir" != "$USER_SYSTEMD_SERVICE_DIR" ]] && [[ -f "${default_dir}/${service_name}" ]]; then
		return 0
	fi

	return 1
}

_proxy_system_service_exists() {
	local service_name="$1"
	[[ -f "${LEGACY_SYSTEMD_SERVICE_DIR}/${service_name}" ]]
}

_proxy_user_protocol_exists() {
	local protocol="$1"
	local service_name default_root

	service_name=$(_proxy_service_name_for_protocol "$protocol")
	default_root=$(_proxy_default_user_config_root)

	if [[ -d "${SING_BOX_CONFIG_ROOT}/${protocol}" ]] || _proxy_user_service_exists "$service_name"; then
		return 0
	fi

	if [[ "$default_root" != "$SING_BOX_CONFIG_ROOT" ]] && [[ -d "${default_root}/${protocol}" ]]; then
		return 0
	fi

	return 1
}

_proxy_user_protocol_config_exists() {
	local protocol="$1"
	local default_root

	default_root=$(_proxy_default_user_config_root)

	if [[ -f "${SING_BOX_CONFIG_ROOT}/${protocol}/config.json" ]]; then
		return 0
	fi

	if [[ "$default_root" != "$SING_BOX_CONFIG_ROOT" ]] && [[ -f "${default_root}/${protocol}/config.json" ]]; then
		return 0
	fi

	return 1
}

_proxy_system_protocol_exists() {
	local protocol="$1"
	local service_name

	service_name=$(_proxy_service_name_for_protocol "$protocol")
	[[ -d "${LEGACY_SING_BOX_CONFIG_ROOT}/${protocol}" ]] || _proxy_system_service_exists "$service_name"
}

_proxy_user_service_is_active() {
	if plat_is_macos; then
		_has launchctl &&
			launchctl print "$(_proxy_launchd_domain)/$1" 2>/dev/null |
			grep -q 'state = running'
		return
	fi

	_has systemctl && systemctl --user is-active --quiet "$1" 2>/dev/null
}

_proxy_system_service_is_active() {
	# launchd system daemons are out of scope; this client only installs agents.
	if plat_is_macos; then
		return 1
	fi

	_has systemctl && systemctl is-active --quiet "$1" 2>/dev/null
}

# Start the user service for a protocol, leaving an already-running one alone so
# `proxy on` does not drop live connections.
_proxy_user_service_start() {
	local protocol="$1"
	local service_name plist_file

	service_name=$(_proxy_service_name_for_protocol "$protocol")

	if ! plat_is_macos; then
		_proxy_run_systemctl_for_scope user enable --now "$service_name" >/dev/null 2>&1
		return
	fi

	if _proxy_user_service_is_active "$service_name"; then
		return 0
	fi

	plist_file="$(_proxy_launch_agent_dir)/${service_name}.plist"
	if [[ ! -f "$plist_file" ]]; then
		return 1
	fi

	launchctl enable "$(_proxy_launchd_target_for_protocol "$protocol")" >/dev/null 2>&1 || true
	# bootstrap refuses a label that is already loaded, even when not running.
	launchctl bootout "$(_proxy_launchd_target_for_protocol "$protocol")" >/dev/null 2>&1 || true
	launchctl bootstrap "$(_proxy_launchd_domain)" "$plist_file" >/dev/null 2>&1
}

_proxy_user_service_stop() {
	local protocol="$1"
	local service_name

	service_name=$(_proxy_service_name_for_protocol "$protocol")

	if ! plat_is_macos; then
		_proxy_run_systemctl_for_scope user disable --now "$service_name" >/dev/null 2>&1
		return
	fi

	launchctl bootout "$(_proxy_launchd_target_for_protocol "$protocol")" >/dev/null 2>&1 || true
}

# Opening and immediately closing a loopback TCP connection is enough to
# prove sing-box finished initialization and bound the mixed inbound.  In
# particular, a process blocked on an initial remote rule-set download can
# be "active" without accepting proxy traffic yet.  Run the Bash-specific
# /dev/tcp redirection in a subprocess because setup.sh can be sourced by zsh.
_proxy_listener_is_ready() {
	local port="${1:-$(_proxy_read_config_port)}"
	bash --noprofile --norc -c 'exec 3<>"/dev/tcp/$1/$2"' \
		_ "$DEFAULT_PROXY_HOST" "$port" 2>/dev/null
}

# A mixed inbound binds both transports. Checking only its TCP listener can
# accept a port already held by a UDP-only process, so inspect the kernel's
# socket table (or lsof on macOS) for both protocols. Returning 2 means the
# host has no safe inspection mechanism; callers reject the port rather than
# guessing and discovering the collision only after stopping the client.
_proxy_port_has_socket() {
	local port="$1"
	local hex_port table
	local -a socket_tables=()

	if _has lsof; then
		lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sed -n '2p' | grep -q . && return 0
		lsof -nP -iUDP:"$port" 2>/dev/null | sed -n '2p' | grep -q . && return 0
		return 1
	fi

	if plat_is_linux && [[ -r /proc/net/tcp && -r /proc/net/udp ]]; then
		hex_port=$(printf '%04X' "$port")
		for table in /proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6; do
			[[ -r "$table" ]] && socket_tables+=("$table")
		done
		awk -v wanted="$hex_port" '
			NR > 1 {
				split($2, address, ":")
				if (toupper(address[2]) == wanted) found = 1
			}
			END { exit(found ? 0 : 1) }
		' "${socket_tables[@]}" 2>/dev/null
		return $?
	fi

	return 2
}

_PROXY_PORT_CHECK_ERROR=""
_proxy_port_is_available() {
	local socket_status

	_PROXY_PORT_CHECK_ERROR=""
	_proxy_port_has_socket "$1"
	socket_status=$?
	case "$socket_status" in
	0) return 1 ;;
	1) return 0 ;;
	*)
		_PROXY_PORT_CHECK_ERROR=unavailable
		return 1
		;;
	esac
}

_proxy_find_free_port() {
	local port="${1:-$DEFAULT_PROXY_PORT}"
	local i=0
	while [[ $i -lt 20 ]]; do
		if _proxy_port_is_available "$port"; then
			printf '%s\n' "$port"
			return 0
		fi
		if [[ "$_PROXY_PORT_CHECK_ERROR" == unavailable ]]; then
			local msg
			msg=$(_t 'Could not verify that port {} is free for both TCP and UDP.')
			_error "${msg//\{\}/$port}"
			return 1
		fi
		port=$((port + 1))
		if [[ $port -gt 65535 ]]; then
			port="$DEFAULT_PROXY_PORT"
		fi
		i=$((i + 1))
	done
	_error "$(_t 'Could not find a free port after 20 attempts.')"
	return 1
}

# What the configs currently declare -- where sing-box is actually listening.
# One reader: the implementation lives in lib/system-proxy.sh so
# client-uninstall.sh, which sources that file alone, resolves the same port.
_proxy_read_config_port() {
	_system_proxy_detect_port "$SING_BOX_CONFIG_ROOT"
}

# What `proxy on` last chose. The configs are installer-owned and an upgrade
# rewrites them from the shipped templates, resetting listen_port to the
# default; this marker is what carries the choice across that.
_proxy_selected_port_file() {
	_system_proxy_selected_port_file "$SING_BOX_CONFIG_ROOT"
}

_proxy_read_selected_port() {
	_system_proxy_read_selected_port "$SING_BOX_CONFIG_ROOT"
}

_proxy_save_selected_port() {
	local port="$1"
	local marker marker_dir tmp_file

	marker=$(_proxy_selected_port_file)
	marker_dir=${marker%/*}
	tmp_file="${marker}.tmp.$$"
	mkdir -p "$marker_dir" || return 1
	if ! printf '%s\n' "$port" >"$tmp_file"; then
		rm -f "$tmp_file"
		return 1
	fi
	chmod 0600 "$tmp_file" || {
		rm -f "$tmp_file"
		return 1
	}
	if ! mv -f "$tmp_file" "$marker"; then
		rm -f "$tmp_file"
		return 1
	fi
}

# True when our own client is the thing answering on this port. `proxy on`
# leaves a running client alone by design, so its own inbound must not be read
# as a conflict and moved out from under it.
_proxy_client_is_serving() {
	local port="$1"

	[[ -n "$port" ]] || return 1
	[[ "$port" == "$(_proxy_read_config_port)" ]] || return 1
	_proxy_has_active_service && _proxy_listener_is_ready "$port"
}

# sing-box reads its config once at startup, so a port change only takes effect
# after a restart. Without this the daemon would keep serving the old port while
# every proxy setting we just wrote points at the new one.
_proxy_restart_for_port_change() {
	local port="$1"
	local protocol service_name
	local restart=()

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_service_is_active "$service_name"; then
			restart+=("$protocol")
		fi
	done

	if [[ ${#restart[@]} -eq 0 ]]; then
		return 0
	fi

	for protocol in "${restart[@]}"; do
		_proxy_user_service_stop "$protocol" || return 1
	done
	for protocol in "${restart[@]}"; do
		_proxy_user_service_start "$protocol" || return 1
		_proxy_user_service_wait_ready "$protocol" "$port" || return 1
	done
}

# Rewrite one config in place. These carry the user's credentials, so the temp
# file gets its mode set explicitly rather than inheriting the caller's umask --
# mv preserves whatever the file was created with.
_proxy_patch_listen_port_file() {
	local cfg="$1"
	local port="$2"
	local tmp="${cfg}.port.$$"

	[[ -f "$cfg" ]] || return 0

	rm -f "$tmp"
	if ! sed "s/\"listen_port\": *[0-9]*/\"listen_port\": ${port}/" "$cfg" >"$tmp"; then
		rm -f "$tmp"
		return 1
	fi
	if cmp -s "$cfg" "$tmp"; then
		rm -f "$tmp"
		return 0
	fi
	if ! chmod 0600 "$tmp" || ! mv "$tmp" "$cfg"; then
		rm -f "$tmp"
		return 1
	fi
}

_proxy_patch_listen_port() {
	local port="$1"
	local protocol route

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		_proxy_patch_listen_port_file \
			"${SING_BOX_CONFIG_ROOT}/${protocol}/config.json" "$port" || return 1
		for route in "${SUPPORTED_ROUTES[@]}"; do
			_proxy_patch_listen_port_file \
				"$(_proxy_route_variant_path "$protocol" "$route")" "$port" || return 1
		done
	done
}

# Port changes are transactions for the same reason route changes are: every
# installed protocol config carries the mixed inbound, and a running daemon has
# to agree with the durable marker and any proxy integrations that already
# point at it. Plain scalars and indexed arrays keep this bash 3.2 compatible.
_PROXY_PORT_SNAPSHOT_DIR=""
_PROXY_PORT_HAD_MARKER=false
_PROXY_PORT_OLD_CONFIG_PORT=""
_PROXY_PORT_OLD_SELECTED_PORT=""
_PROXY_PORT_SHELL_PORT=""
_PROXY_PORT_GIT_GLOBAL_HTTP=""
_PROXY_PORT_GIT_GLOBAL_HTTPS=""
_PROXY_PORT_GIT_LOCAL_HTTP=""
_PROXY_PORT_GIT_LOCAL_HTTPS=""
_PROXY_PORT_DESKTOP_STATE=off
_PROXY_PORT_DESKTOP_PORT=""
_PROXY_PORT_DOCKER_STATE=off
_PROXY_PORT_DOCKER_PORT=""
_PROXY_PORT_INTEGRATIONS_CHANGED=false
_PROXY_PORT_PROTOCOLS=()
_PROXY_PORT_ACTIVE_PROTOCOLS=()

_proxy_port_reset_transaction() {
	_PROXY_PORT_SNAPSHOT_DIR=""
	_PROXY_PORT_HAD_MARKER=false
	_PROXY_PORT_OLD_CONFIG_PORT=""
	_PROXY_PORT_OLD_SELECTED_PORT=""
	_PROXY_PORT_SHELL_PORT=""
	_PROXY_PORT_GIT_GLOBAL_HTTP=""
	_PROXY_PORT_GIT_GLOBAL_HTTPS=""
	_PROXY_PORT_GIT_LOCAL_HTTP=""
	_PROXY_PORT_GIT_LOCAL_HTTPS=""
	_PROXY_PORT_DESKTOP_STATE=off
	_PROXY_PORT_DESKTOP_PORT=""
	_PROXY_PORT_DOCKER_STATE=off
	_PROXY_PORT_DOCKER_PORT=""
	_PROXY_PORT_INTEGRATIONS_CHANGED=false
	_PROXY_PORT_PROTOCOLS=()
	_PROXY_PORT_ACTIVE_PROTOCOLS=()
}

_proxy_port_value_port_if_ours() {
	local value="$1"
	local candidate

	for candidate in "$_PROXY_PORT_OLD_CONFIG_PORT" "$_PROXY_PORT_OLD_SELECTED_PORT"; do
		[[ -n "$candidate" ]] || continue
		case "$value" in
		http://"$DEFAULT_PROXY_HOST":"$candidate" | https://"$DEFAULT_PROXY_HOST":"$candidate" | \
			ftp://"$DEFAULT_PROXY_HOST":"$candidate" | socks5://"$DEFAULT_PROXY_HOST":"$candidate")
			printf '%s\n' "$candidate"
			return 0
			;;
		esac
	done
	return 1
}

_proxy_port_snapshot_integrations() {
	local value network_service pair protocol candidate daemon_file

	value=$(_proxy_first_proxy_value "${http_proxy:-}" "${HTTP_PROXY:-}" || true)
	_PROXY_PORT_SHELL_PORT=$(_proxy_port_value_port_if_ours "$value" || true)

	if _has git; then
		value=$(git config --global --get http.proxy 2>/dev/null || true)
		_proxy_port_value_port_if_ours "$value" >/dev/null && _PROXY_PORT_GIT_GLOBAL_HTTP="$value"
		value=$(git config --global --get https.proxy 2>/dev/null || true)
		_proxy_port_value_port_if_ours "$value" >/dev/null && _PROXY_PORT_GIT_GLOBAL_HTTPS="$value"
		if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
			value=$(git config --local --get http.proxy 2>/dev/null || true)
			_proxy_port_value_port_if_ours "$value" >/dev/null && _PROXY_PORT_GIT_LOCAL_HTTP="$value"
			value=$(git config --local --get https.proxy 2>/dev/null || true)
			_proxy_port_value_port_if_ours "$value" >/dev/null && _PROXY_PORT_GIT_LOCAL_HTTPS="$value"
		fi
	fi

	_PROXY_PORT_DESKTOP_STATE=$(_proxy_tun_classify_desktop_proxy)
	if [[ "$_PROXY_PORT_DESKTOP_STATE" == ours ]]; then
		if plat_is_macos; then
			network_service=$(_proxy_macos_resolve_network_service 2>/dev/null || true)
			if [[ -n "$network_service" ]]; then
				for protocol in -getwebproxy -getsecurewebproxy -getsocksfirewallproxy; do
					pair=$(macos_proxy_read "$network_service" "$protocol" || true)
					[[ -n "$pair" ]] || continue
					_PROXY_PORT_DESKTOP_PORT=${pair##* }
					break
				done
			fi
		else
			for protocol in http https socks; do
				value=$(gnome_proxy_read "/system/proxy/${protocol}/host")
				[[ "$value" == "$DEFAULT_PROXY_HOST" ]] || continue
				_PROXY_PORT_DESKTOP_PORT=$(gnome_proxy_read "/system/proxy/${protocol}/port")
				[[ -n "$_PROXY_PORT_DESKTOP_PORT" ]] && break
			done
		fi
		_PROXY_PORT_DESKTOP_PORT=${_PROXY_PORT_DESKTOP_PORT:-$_PROXY_PORT_OLD_CONFIG_PORT}
	fi

	_PROXY_PORT_DOCKER_STATE=$(_proxy_tun_classify_docker_proxy)
	value=""
	if [[ -f "$(_proxy_docker_config_file)" ]] && _has jq; then
		value=$(jq -r '.proxies.default.httpProxy // empty' "$(_proxy_docker_config_file)" 2>/dev/null || true)
	fi
	if
		candidate=$(_proxy_port_value_port_if_ours "$value" || true)
		[[ -n "$candidate" ]]
	then
		_PROXY_PORT_DOCKER_STATE=ours
		_PROXY_PORT_DOCKER_PORT="$candidate"
	fi
	if [[ -z "$_PROXY_PORT_DOCKER_PORT" ]]; then
		daemon_file=$(_proxy_docker_daemon_proxy_file)
		if [[ -f "$daemon_file" ]]; then
			for candidate in "$_PROXY_PORT_OLD_CONFIG_PORT" "$_PROXY_PORT_OLD_SELECTED_PORT"; do
				[[ -n "$candidate" ]] || continue
				if grep -q "http_proxy=http://${DEFAULT_PROXY_HOST}:${candidate}" "$daemon_file" 2>/dev/null; then
					_PROXY_PORT_DOCKER_STATE=ours
					_PROXY_PORT_DOCKER_PORT="$candidate"
					break
				fi
			done
		fi
	fi
	if [[ "$_PROXY_PORT_DOCKER_STATE" == ours ]]; then
		_PROXY_PORT_DOCKER_PORT=${_PROXY_PORT_DOCKER_PORT:-$_PROXY_PORT_OLD_CONFIG_PORT}
	fi
}

_proxy_port_snapshot_transaction() {
	local protocol route source destination service_name marker
	local -a protocols=()
	local -a active_protocols=()

	_proxy_port_reset_transaction
	_PROXY_PORT_OLD_CONFIG_PORT=$(_proxy_read_config_port)
	_PROXY_PORT_OLD_SELECTED_PORT=$(_proxy_read_selected_port)
	_proxy_port_snapshot_integrations

	_PROXY_PORT_SNAPSHOT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sing-box-port.XXXXXX") || return 1
	while IFS= read -r protocol; do
		[[ -n "$protocol" ]] && protocols+=("$protocol")
	done < <(_proxy_route_installed_protocols)
	_PROXY_PORT_PROTOCOLS=("${protocols[@]}")

	for protocol in "${protocols[@]}"; do
		mkdir -p "${_PROXY_PORT_SNAPSHOT_DIR}/${protocol}" || return 1
		source="${SING_BOX_CONFIG_ROOT}/${protocol}/config.json"
		if [[ -f "$source" ]]; then
			destination="${_PROXY_PORT_SNAPSHOT_DIR}/${protocol}/${source##*/}"
			cp -p "$source" "$destination" || return 1
		fi
		for route in "${SUPPORTED_ROUTES[@]}"; do
			source=$(_proxy_route_variant_path "$protocol" "$route")
			[[ -f "$source" ]] || continue
			destination="${_PROXY_PORT_SNAPSHOT_DIR}/${protocol}/${source##*/}"
			cp -p "$source" "$destination" || return 1
		done
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_service_is_active "$service_name"; then
			active_protocols+=("$protocol")
		fi
	done
	_PROXY_PORT_ACTIVE_PROTOCOLS=("${active_protocols[@]}")

	marker=$(_proxy_selected_port_file)
	if [[ -f "$marker" ]]; then
		cp -p "$marker" "${_PROXY_PORT_SNAPSHOT_DIR}/selected-port" || return 1
		_PROXY_PORT_HAD_MARKER=true
	fi
}

_proxy_port_restore_integrations() {
	local ok=true

	if [[ -n "$_PROXY_PORT_SHELL_PORT" ]]; then
		_set_proxy_env_vars "$DEFAULT_PROXY_HOST" "$_PROXY_PORT_SHELL_PORT" || ok=false
	fi
	if _has git; then
		[[ -z "$_PROXY_PORT_GIT_GLOBAL_HTTP" ]] || git config --global http.proxy "$_PROXY_PORT_GIT_GLOBAL_HTTP" || ok=false
		[[ -z "$_PROXY_PORT_GIT_GLOBAL_HTTPS" ]] || git config --global https.proxy "$_PROXY_PORT_GIT_GLOBAL_HTTPS" || ok=false
		[[ -z "$_PROXY_PORT_GIT_LOCAL_HTTP" ]] || git config --local http.proxy "$_PROXY_PORT_GIT_LOCAL_HTTP" || ok=false
		[[ -z "$_PROXY_PORT_GIT_LOCAL_HTTPS" ]] || git config --local https.proxy "$_PROXY_PORT_GIT_LOCAL_HTTPS" || ok=false
	fi
	if [[ "$_PROXY_PORT_DESKTOP_STATE" == ours ]]; then
		_proxy_apply_desktop_proxy "$DEFAULT_PROXY_HOST" "$_PROXY_PORT_DESKTOP_PORT" || ok=false
	fi
	if [[ "$_PROXY_PORT_DOCKER_STATE" == ours ]]; then
		_proxy_apply_docker_client_proxy "$DEFAULT_PROXY_HOST" "$_PROXY_PORT_DOCKER_PORT" || ok=false
		if ! plat_is_macos && _has docker; then
			_proxy_apply_docker_daemon_proxy "$DEFAULT_PROXY_HOST" "$_PROXY_PORT_DOCKER_PORT" || ok=false
		fi
	fi
	[[ "$ok" == true ]]
}

_proxy_port_migrate_integrations() {
	local port="$1"
	local ok=true

	if [[ -n "$_PROXY_PORT_SHELL_PORT" ]]; then
		_set_proxy_env_vars "$DEFAULT_PROXY_HOST" "$port" || ok=false
	fi
	if _has git; then
		[[ -z "$_PROXY_PORT_GIT_GLOBAL_HTTP" ]] || git config --global http.proxy "http://${DEFAULT_PROXY_HOST}:${port}" || ok=false
		[[ -z "$_PROXY_PORT_GIT_GLOBAL_HTTPS" ]] || git config --global https.proxy "http://${DEFAULT_PROXY_HOST}:${port}" || ok=false
		[[ -z "$_PROXY_PORT_GIT_LOCAL_HTTP" ]] || git config --local http.proxy "http://${DEFAULT_PROXY_HOST}:${port}" || ok=false
		[[ -z "$_PROXY_PORT_GIT_LOCAL_HTTPS" ]] || git config --local https.proxy "http://${DEFAULT_PROXY_HOST}:${port}" || ok=false
	fi
	if [[ "$_PROXY_PORT_DESKTOP_STATE" == ours ]]; then
		_proxy_apply_desktop_proxy "$DEFAULT_PROXY_HOST" "$port" || ok=false
	fi
	if [[ "$_PROXY_PORT_DOCKER_STATE" == ours ]]; then
		_proxy_apply_docker_client_proxy "$DEFAULT_PROXY_HOST" "$port" || ok=false
		if ! plat_is_macos && _has docker; then
			_proxy_apply_docker_daemon_proxy "$DEFAULT_PROXY_HOST" "$port" || ok=false
		fi
	fi
	[[ "$ok" == true ]]
}

_proxy_port_rollback() {
	local protocol backup destination marker service_name
	local rollback_ok=true

	for protocol in "${_PROXY_PORT_PROTOCOLS[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_service_is_active "$service_name"; then
			_proxy_user_service_stop "$protocol" || rollback_ok=false
		fi
	done

	for protocol in "${_PROXY_PORT_PROTOCOLS[@]}"; do
		for backup in "${_PROXY_PORT_SNAPSHOT_DIR}/${protocol}"/*.json; do
			[[ -f "$backup" ]] || continue
			destination="${SING_BOX_CONFIG_ROOT}/${protocol}/${backup##*/}"
			_proxy_route_atomic_copy "$backup" "$destination" || rollback_ok=false
		done
	done

	marker=$(_proxy_selected_port_file)
	if [[ "$_PROXY_PORT_HAD_MARKER" == true ]]; then
		_proxy_route_atomic_copy "${_PROXY_PORT_SNAPSHOT_DIR}/selected-port" "$marker" || rollback_ok=false
	else
		rm -f "$marker" || rollback_ok=false
	fi

	for protocol in "${_PROXY_PORT_ACTIVE_PROTOCOLS[@]}"; do
		if ! _proxy_user_service_start "$protocol" ||
			! _proxy_user_service_wait_ready "$protocol" "$_PROXY_PORT_OLD_CONFIG_PORT"; then
			rollback_ok=false
		fi
	done
	if [[ "$_PROXY_PORT_INTEGRATIONS_CHANGED" == true ]]; then
		_proxy_port_restore_integrations || rollback_ok=false
	fi

	rm -rf "$_PROXY_PORT_SNAPSHOT_DIR"
	_proxy_port_reset_transaction
	[[ "$rollback_ok" == true ]]
}

_proxy_apply_selected_port() {
	local port="$1"
	local quiet="${2:-false}"
	local current_port selected_port port_moved=false msg

	current_port=$(_proxy_read_config_port)
	selected_port=$(_proxy_read_selected_port)
	if [[ "$current_port" != "$port" ]] ||
		[[ -n "$selected_port" && "$selected_port" != "$port" ]]; then
		port_moved=true
	fi
	if ! _proxy_port_snapshot_transaction; then
		[[ -z "${_PROXY_PORT_SNAPSHOT_DIR:-}" ]] || rm -rf "$_PROXY_PORT_SNAPSHOT_DIR"
		_proxy_port_reset_transaction
		_error "$(_t 'Failed to change the mixed inbound port. Restoring the previous port.')"
		return 1
	fi

	if ! _proxy_save_selected_port "$port" || ! _proxy_patch_listen_port "$port"; then
		_error "$(_t 'Failed to change the mixed inbound port. Restoring the previous port.')"
		if ! _proxy_port_rollback; then
			_error "$(_t 'Port rollback did not complete.')"
		fi
		return 1
	fi

	if [[ "$current_port" != "$port" ]] && ! _proxy_restart_for_port_change "$port"; then
		_error "$(_t 'Failed to change the mixed inbound port. Restoring the previous port.')"
		if ! _proxy_port_rollback; then
			_error "$(_t 'Port rollback did not complete.')"
		fi
		return 1
	fi

	if [[ "$port_moved" == true ]]; then
		_PROXY_PORT_INTEGRATIONS_CHANGED=true
	fi
	if [[ "$port_moved" == true ]] && ! _proxy_port_migrate_integrations "$port"; then
		_error "$(_t 'Failed to change the mixed inbound port. Restoring the previous port.')"
		if ! _proxy_port_rollback; then
			_error "$(_t 'Port rollback did not complete.')"
		fi
		return 1
	fi

	rm -rf "$_PROXY_PORT_SNAPSHOT_DIR"
	_proxy_port_reset_transaction
	if [[ "$quiet" != true ]]; then
		msg=$(_t 'Mixed inbound port switched to {}.')
		success "${msg//\{\}/$port}"
	fi
}

_proxy_check_port() {
	local selected_port

	selected_port=$(_proxy_read_selected_port)
	if [[ -z "$selected_port" ]]; then
		selected_port=$(_proxy_read_config_port)
	fi
	_proxy_report_row 'Selected port' "$selected_port"
	if _proxy_has_active_service && ! _proxy_listener_is_ready "$selected_port"; then
		_warning "$(_t 'The running mixed proxy is not listening on the selected port.')"
	fi
}

_proxy_switch_port() {
	local requested="${1:-}"
	local argument_count=$#
	local port msg

	if [[ $# -gt 0 ]]; then
		shift
	fi
	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi
	if [[ "$argument_count" -eq 0 ]]; then
		_proxy_check_port
		return 0
	fi
	if ! _proxy_validate_port_value "$requested"; then
		return 1
	fi
	port="$_PROXY_VALIDATED_PORT"

	if ! _proxy_port_is_available "$port" && ! _proxy_client_is_serving "$port"; then
		if [[ "$_PROXY_PORT_CHECK_ERROR" == unavailable ]]; then
			msg=$(_t 'Could not verify that port {} is free for both TCP and UDP.')
		else
			msg=$(_t 'Port {} is already in use.')
		fi
		_error "${msg//\{\}/$port}"
		return 1
	fi

	_proxy_apply_selected_port "$port"
}

_proxy_user_service_wait_ready() {
	local protocol="$1"
	local port="${2:-$(_proxy_read_config_port)}"
	local service_name attempt

	service_name=$(_proxy_service_name_for_protocol "$protocol")
	for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
		if _proxy_user_service_is_active "$service_name" && _proxy_listener_is_ready "$port"; then
			return 0
		fi
		sleep 1
	done
	return 1
}

_proxy_service_scope_for_protocol() {
	local protocol="$1"
	local service_name

	service_name=$(_proxy_service_name_for_protocol "$protocol")

	if _proxy_user_protocol_exists "$protocol"; then
		printf 'user\n'
		return 0
	fi

	if _proxy_system_protocol_exists "$protocol"; then
		printf 'system\n'
		return 0
	fi

	if _proxy_user_service_is_active "$service_name"; then
		printf 'user\n'
		return 0
	fi

	if _proxy_system_service_is_active "$service_name"; then
		printf 'system\n'
		return 0
	fi

	return 1
}

_proxy_service_state_for_protocol() {
	local protocol="$1"
	local service_name scope

	if ! _proxy_service_manager_available; then
		printf '%s\n' "$(_t 'unknown')"
		return 0
	fi

	service_name=$(_proxy_service_name_for_protocol "$protocol")

	if plat_is_macos; then
		if _proxy_user_service_is_active "$service_name"; then
			printf 'active\n'
		elif _proxy_user_service_exists "$service_name"; then
			printf 'inactive\n'
		else
			printf '%s\n' "$(_t 'unknown')"
		fi
		return 0
	fi

	scope=$(_proxy_service_scope_for_protocol "$protocol" || true)

	case "$scope" in
	user)
		systemctl --user is-active "$service_name" 2>/dev/null || true
		;;
	system)
		systemctl is-active "$service_name" 2>/dev/null || true
		;;
	*)
		printf '%s\n' "$(_t 'unknown')"
		;;
	esac
}

_proxy_is_supported_protocol() {
	case "$1" in
	trojan | hysteria2 | naive)
		return 0
		;;
	*)
		return 1
		;;
	esac
}

_proxy_list_active_protocols() {
	local protocol service_name

	if ! _has systemctl; then
		return 0
	fi

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_service_is_active "$service_name" || _proxy_system_service_is_active "$service_name"; then
			printf '%s\n' "$protocol"
		fi
	done
}

_proxy_detect_active_protocol() {
	local protocol
	local -a active_protocols=()

	while IFS= read -r protocol; do
		if [[ -n "$protocol" ]]; then
			active_protocols+=("$protocol")
		fi
	done < <(_proxy_list_active_protocols)

	if [[ ${#active_protocols[@]} -gt 0 ]]; then
		printf '%s\n' "${active_protocols[*]}"
		return 0
	fi

	return 1
}

_proxy_list_known_protocols() {
	local protocol service_name

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_protocol_exists "$protocol" || _proxy_system_protocol_exists "$protocol"; then
			printf '%s\n' "$protocol"
			continue
		fi

		if _proxy_user_service_is_active "$service_name" || _proxy_system_service_is_active "$service_name"; then
			printf '%s\n' "$protocol"
		fi
	done
}

_proxy_detect_protocol() {
	local protocol
	local selected_protocol
	local -a active_protocols=()
	local -a known_protocols=()

	if _proxy_is_supported_protocol "${VPN_PROTOCOL:-}"; then
		printf '%s\n' "$VPN_PROTOCOL"
		return 0
	fi

	selected_protocol=$(_proxy_selected_protocol || true)
	if [[ -n "$selected_protocol" ]]; then
		printf '%s\n' "$selected_protocol"
		return 0
	fi

	while IFS= read -r protocol; do
		if [[ -n "$protocol" ]]; then
			active_protocols+=("$protocol")
		fi
	done < <(_proxy_list_active_protocols)

	if [[ ${#active_protocols[@]} -eq 1 ]]; then
		printf '%s\n' "${active_protocols[0]}"
		return 0
	fi

	while IFS= read -r protocol; do
		if [[ -n "$protocol" ]]; then
			known_protocols+=("$protocol")
		fi
	done < <(_proxy_list_known_protocols)

	if [[ ${#known_protocols[@]} -eq 1 ]]; then
		printf '%s\n' "${known_protocols[0]}"
		return 0
	fi

	return 1
}

_proxy_has_active_service() {
	local protocol service_name

	if ! _proxy_service_manager_available; then
		return 1
	fi

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_service_is_active "$service_name" || _proxy_system_service_is_active "$service_name"; then
			return 0
		fi
	done

	return 1
}

# Fetch a URL to a file with optional spinner
_fetch_url() {
	local url="$1"
	local output_file="$2"
	local timeout="${3:-$TIMEOUT}"

	local curl_status
	if [ "${INTERACTIVE:-false}" = true ] && command -v spinner >/dev/null 2>&1; then
		# Suppress job-control noise while backgrounding
		if [ -n "${ZSH_VERSION:-}" ]; then
			setopt local_options nomonitor
		fi
		local restore_bash_monitor=""
		if [ -n "${BASH_VERSION:-}" ]; then
			if [[ -o monitor ]]; then
				restore_bash_monitor="on"
				set +m
			else
				restore_bash_monitor="off"
			fi
		fi
		(curl --silent --max-time "$timeout" "$url" >"$output_file" 2>/dev/null) &
		local curl_pid=$!
		spinner "$curl_pid"
		wait "$curl_pid"
		curl_status=$?
		if [ -n "${BASH_VERSION:-}" ] && [ "$restore_bash_monitor" = "on" ]; then
			set -m
		fi
	else
		curl --silent --max-time "$timeout" "$url" >"$output_file" 2>/dev/null
		curl_status=$?
	fi

	return $curl_status
}

_proxy_fetch_portal_vps_info() {
	local portal_base_url="$1"
	local timeout="${2:-$TIMEOUT}"
	local response_json

	if ! _has curl || ! _has jq; then
		return 1
	fi

	if ! response_json=$(curl --fail --silent --show-error --max-time "$timeout" "${portal_base_url}/api/vps-info" 2>/dev/null); then
		return 1
	fi

	if [[ -z "$response_json" ]]; then
		return 1
	fi

	if ! printf '%s\n' "$response_json" | jq -e 'type == "object"' >/dev/null 2>&1; then
		return 1
	fi

	printf '%s\n' "$response_json"
}

_PROXY_PORTAL_USAGE_STATUS=""
_PROXY_PORTAL_USAGE_PAYLOAD=""

_proxy_fetch_portal_usage() {
	local portal_base_url="$1"
	local portal_token="${2:-}"
	local portal_username="${3:-}"
	local portal_password="${4:-}"
	local timeout="${5:-$TIMEOUT}"
	local response_json http_code
	local -a curl_args

	_PROXY_PORTAL_USAGE_STATUS=""
	_PROXY_PORTAL_USAGE_PAYLOAD=""

	if ! _has curl || ! _has jq; then
		return 1
	fi

	if [[ -z "$portal_token" && (-z "$portal_username" || -z "$portal_password") ]]; then
		return 1
	fi

	curl_args=(
		--silent
		--show-error
		--location
		--max-time "$timeout"
		--output -
		--write-out '\n%{http_code}'
	)

	if [[ -n "$portal_token" ]]; then
		curl_args+=(-H "Authorization: Bearer ${portal_token}")
	else
		curl_args+=(--user "${portal_username}:${portal_password}")
	fi

	if ! response_json=$(curl "${curl_args[@]}" "${portal_base_url}/api/usage" 2>/dev/null); then
		return 1
	fi

	if [[ -z "$response_json" ]]; then
		return 1
	fi

	http_code=${response_json##*$'\n'}
	response_json=${response_json%$'\n'*}
	_PROXY_PORTAL_USAGE_STATUS="$http_code"

	if [[ "$http_code" != "200" ]]; then
		return 1
	fi

	if ! printf '%s\n' "$response_json" | jq -e 'type == "object"' >/dev/null 2>&1; then
		return 1
	fi

	_PROXY_PORTAL_USAGE_PAYLOAD="$response_json"
}

_proxy_json_string_field() {
	local payload="$1"
	local key="$2"

	if ! _has jq; then
		return 1
	fi

	printf '%s\n' "$payload" | jq -r --arg key "$key" '(.[$key] // empty) | strings' 2>/dev/null
}

_proxy_json_number_field() {
	local payload="$1"
	local key="$2"

	if ! _has jq; then
		return 1
	fi

	printf '%s\n' "$payload" | jq -r --arg key "$key" '(.[$key] // empty) | numbers' 2>/dev/null
}

_proxy_plain_text_field() {
	local payload="$1"
	local field="$2"

	printf '%s\n' "$payload" | awk -F ':' -v field="$field" '
		{
			key = $1
			gsub(/[[:space:]]/, "", key)
			if (key == field) {
				value = $0
				sub(/^[^:]*:/, "", value)
				gsub(/^[[:space:]]+/, "", value)
				gsub(/[[:space:]]+$/, "", value)
				if (value != "") {
					print value
				}
				exit
			}
		}'
}

_proxy_resolve_public_ip() {
	local timeout=${1:-$TIMEOUT}
	local tmp_file
	tmp_file="$(mktemp)"

	if ! command -v curl >/dev/null 2>&1; then
		rm -f "$tmp_file"
		return 1
	fi

	if ! _fetch_url "https://ipinfo.io/json" "$tmp_file" "$timeout"; then
		rm -f "$tmp_file"
		return 1
	fi

	local ipinfo
	ipinfo="$(<"$tmp_file")"
	rm -f "$tmp_file"

	if [ -z "$ipinfo" ]; then
		return 1
	fi

	local ip city
	ip=$(_proxy_json_string_field "$ipinfo" "ip" || true)
	city=$(_proxy_json_string_field "$ipinfo" "city" || true)

	if [ -n "$city" ] && [ -n "$ip" ]; then
		printf '%s\n' "${city}, ${ip}"
	elif [ -n "$ip" ]; then
		printf '%s\n' "$ip"
	elif [ -n "$city" ]; then
		printf '%s\n' "$city"
	else
		return 1
	fi

	return 0
}

_proxy_check_public_ip() {
	local timeout=${1:-$TIMEOUT}
	local public_ip error_msg

	if ! public_ip=$(_proxy_resolve_public_ip "$timeout"); then
		error_msg=$(_t 'Failed to detect public IP in {} seconds.')
		_warning "${error_msg//\{\}/$timeout}"
		return 1
	fi

	printf '%s\n' "$public_ip"
	return 0
}

_proxy_resolve_public_ip_cn() {
	local timeout=${1:-$TIMEOUT}
	local cip_info
	local tmp_info
	tmp_info="$(mktemp)"

	if ! command -v curl >/dev/null 2>&1; then
		rm -f "$tmp_info"
		return 1
	fi

	if ! _fetch_url "http://cip.cc" "$tmp_info" "$timeout"; then
		rm -f "$tmp_info"
		return 1
	fi

	cip_info="$(<"$tmp_info")"
	rm -f "$tmp_info"

	if [ -z "$cip_info" ]; then
		return 1
	fi

	local ip location
	ip=$(_proxy_plain_text_field "$cip_info" "IP" || true)
	location=$(_proxy_plain_text_field "$cip_info" "地址" || true)

	if [ -n "$location" ] && [ -n "$ip" ]; then
		printf '%s\n' "${location}, ${ip}"
	elif [ -n "$ip" ]; then
		printf '%s\n' "$ip"
	elif [ -n "$location" ]; then
		printf '%s\n' "$location"
	else
		return 1
	fi

	return 0
}

_proxy_check_public_ip_cn() {
	local timeout=${1:-$TIMEOUT}
	local public_ip error_msg

	if ! public_ip=$(_proxy_resolve_public_ip_cn "$timeout"); then
		error_msg=$(_t 'Failed to get IP from cip.cc in {} seconds.')
		_warning "${error_msg//\{\}/$timeout}"
		return 1
	fi

	printf '%s\n' "$public_ip"
	return 0
}

_proxy_resolve_private_ip() {
	local private_ip

	if ! private_ip=$(hostname -I 2>/dev/null | awk '{ print $1 }'); then
		return 1
	fi

	printf '%s\n' "${private_ip}"
	return 0
}

_proxy_check_private_ip() {
	local private_ip

	if ! private_ip=$(_proxy_resolve_private_ip); then
		_error "$(_t 'Failed to get private IP')"
		return 1
	fi

	printf '%s\n' "$private_ip"
	return 0
}

# Get proxy configuration based on platform (returns "host port" on stdout).
# `proxy on` passes the port it just placed; every other caller reads back what
# is on disk.
_get_proxy_config() {
	local runtime _host _port

	runtime=$(_proxy_runtime_kind)
	_port="${1:-$(_proxy_read_config_port)}"

	case "$runtime" in
	docker)
		_host="host.docker.internal"
		;;
	# The proxy listens on loopback only, so WSL2 reaches a Windows-side proxy
	# through mirrored networking, which shares the host's loopback.
	linux | macos | wsl2)
		_host="${DEFAULT_PROXY_HOST}"
		;;
	*)
		_error "$(_t 'This platform is not supported.')"
		return 1
		;;
	esac

	printf '%s %s\n' "$_host" "$_port"
	return 0
}

_proxy_describe_proxy_target() {
	local runtime host

	runtime=$(_proxy_runtime_kind)

	case "$runtime" in
	docker)
		host="host.docker.internal"
		;;
	linux | macos | wsl2)
		host="${DEFAULT_PROXY_HOST}"
		;;
	*)
		printf '%s\n' "$(_t 'unknown')"
		return 0
		;;
	esac

	if [[ -z "$host" ]]; then
		printf '%s\n' "$(_t 'unknown')"
		return 0
	fi

	printf '%s:%s\n' "$host" "$(_proxy_read_config_port)"
}

# Internal helper to set common proxy environment variables
_set_proxy_env_vars() {
	local proxy_host="$1"
	local proxy_port="$2"

	export http_proxy="http://${proxy_host}:${proxy_port}"
	export https_proxy="http://${proxy_host}:${proxy_port}"
	export ftp_proxy="ftp://${proxy_host}:${proxy_port}"
	export socks_proxy="socks5://${proxy_host}:${proxy_port}"
	export HTTP_PROXY="$http_proxy"
	export HTTPS_PROXY="$https_proxy"
	export FTP_PROXY="$ftp_proxy"
	export SOCKS_PROXY="$socks_proxy"
	export no_proxy="$DEFAULT_NO_PROXY"
	export NO_PROXY="${no_proxy}"
}

# Internal helper to unset common proxy environment variables
_unset_proxy_env_vars() {
	unset {http,https,ftp,socks,all,no}_proxy
	unset {HTTP,HTTPS,FTP,SOCKS,ALL,NO}_PROXY
}

# Only `proxy on` accepts a port option; `proxy port` uses its positional
# argument. Keep -p/--port gated behind a capability argument -- the same shape
# _proxy_parse_git_flags uses for its --local/-f split.
_parse_proxy_force_flag() {
	local allow_port="${1:-false}"
	shift

	_PROXY_FORCE_FLAG=false
	_PROXY_PORT_OVERRIDE=""

	while [[ $# -gt 0 ]]; do
		case "$1" in
		-f | --force)
			_PROXY_FORCE_FLAG=true
			;;
		-p | --port)
			if [[ "$allow_port" != true ]]; then
				_proxy_unknown_argument "$1"
				return 1
			fi
			shift
			if [[ $# -eq 0 ]]; then
				_error "$(_t 'Missing value for --port.')"
				return 1
			fi
			if ! _proxy_validate_port_value "$1"; then
				return 1
			fi
			_PROXY_PORT_OVERRIDE="$_PROXY_VALIDATED_PORT"
			;;
		*)
			_proxy_unknown_argument "$1"
			return 1
			;;
		esac
		shift
	done
}

_PROXY_VALIDATED_PORT=""
_proxy_validate_port_value() {
	local value="$1"
	local normalized

	_PROXY_VALIDATED_PORT=""
	if ! [[ "$value" =~ ^[0-9]+$ ]]; then
		_error "$(_t 'Port must be a decimal number between 1024 and 65535.')"
		return 1
	fi
	normalized="$value"
	while [[ "$normalized" == 0* && "$normalized" != 0 ]]; do
		normalized=${normalized#0}
	done
	if [[ ${#normalized} -gt 5 ]] ||
		! [[ "$normalized" -ge 1024 && "$normalized" -le 65535 ]]; then
		_error "$(_t 'Port must be a decimal number between 1024 and 65535.')"
		return 1
	fi
	_PROXY_VALIDATED_PORT="$normalized"
}

_ensure_proxy_service_active() {
	local force="${1:-false}"
	local protocol service_name msg

	if _proxy_is_docker || _proxy_is_wsl2; then
		return 0
	fi

	if ! _proxy_service_manager_available; then
		msg=$(_proxy_service_manager_unavailable_warning)
		_warning "$msg"
		if [[ "$force" == true ]]; then
			return 0
		fi
		return 1
	fi

	if protocol=$(_proxy_preferred_protocol); then
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_service_is_active "$service_name" || _proxy_system_service_is_active "$service_name"; then
			return 0
		fi

		msg=$(_t 'Proxy service {} is not active. Use -f/--force to set proxy anyway.')
		_warning "${msg//\{\}/$service_name}"
		if [[ "$force" == true ]]; then
			return 0
		fi
		return 1
	fi

	if _proxy_has_active_service; then
		return 0
	fi

	if protocol=$(_proxy_detect_protocol); then
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		msg=$(_t 'Proxy service {} is not active. Use -f/--force to set proxy anyway.')
		_warning "${msg//\{\}/$service_name}"
	else
		_warning "$(_t 'No active sing-box service detected. Use -f/--force to set proxy anyway.')"
	fi

	if [[ "$force" == true ]]; then
		return 0
	fi

	return 1
}

_proxy_prepare_proxy_config() {
	local force="${1:-false}"
	local port="${2:-}"
	local runtime proxy_config

	# This function's stdout is its return value, and `warning` prints to stdout,
	# so anything that may warn has to be redirected or it lands in the caller's
	# proxy host/port.
	if ! _ensure_proxy_service_active "$force" >&2; then
		return 1
	fi

	if ! proxy_config=$(_get_proxy_config "$port"); then
		return 1
	fi

	runtime=$(_proxy_runtime_kind)
	case "$runtime" in
	docker | wsl2)
		_warning "$(_t 'Make sure the VPN client is working on host.')" >&2
		;;
	esac

	printf '%s\n' "$proxy_config"
}

_proxy_list_user_protocols() {
	local protocol service_name

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_protocol_exists "$protocol" || _proxy_user_service_is_active "$service_name"; then
			printf '%s\n' "$protocol"
		fi
	done
}

_proxy_list_enable_protocols() {
	local protocol selected_protocol service_name
	local -a active_protocols=()
	local -a known_protocols=()

	if _proxy_is_supported_protocol "${VPN_PROTOCOL:-}"; then
		service_name=$(_proxy_service_name_for_protocol "$VPN_PROTOCOL")
		if _proxy_user_protocol_exists "$VPN_PROTOCOL" || _proxy_user_service_is_active "$service_name"; then
			printf '%s\n' "$VPN_PROTOCOL"
			return 0
		fi
		return 1
	fi

	selected_protocol=$(_proxy_selected_protocol || true)
	if [[ -n "$selected_protocol" ]]; then
		printf '%s\n' "$selected_protocol"
		return 0
	fi

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_service_is_active "$service_name"; then
			active_protocols+=("$protocol")
		fi
	done

	if [[ ${#active_protocols[@]} -gt 0 ]]; then
		printf '%s\n' "${active_protocols[@]}"
		return 0
	fi

	while IFS= read -r protocol; do
		if [[ -n "$protocol" ]]; then
			known_protocols+=("$protocol")
		fi
	done < <(_proxy_list_user_protocols)

	if [[ ${#known_protocols[@]} -eq 1 ]]; then
		printf '%s\n' "${known_protocols[@]}"
		return 0
	fi

	return 1
}

_proxy_list_disable_protocols() {
	local protocol service_name
	local -a active_protocols=()
	local -a known_protocols=()

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_service_is_active "$service_name"; then
			active_protocols+=("$protocol")
		fi
	done

	if [[ ${#active_protocols[@]} -gt 0 ]]; then
		printf '%s\n' "${active_protocols[@]}"
		return 0
	fi

	while IFS= read -r protocol; do
		if [[ -n "$protocol" ]]; then
			known_protocols+=("$protocol")
		fi
	done < <(_proxy_list_user_protocols)

	if [[ ${#known_protocols[@]} -gt 0 ]]; then
		printf '%s\n' "${known_protocols[@]}"
		return 0
	fi

	return 1
}

_proxy_run_systemctl_for_scope() {
	local scope="$1"
	shift

	case "$scope" in
	user)
		systemctl --user "$@"
		;;
	system)
		systemctl "$@"
		;;
	*)
		return 1
		;;
	esac
}

_proxy_disable_other_user_protocol_services() {
	local target_protocol="$1"
	local protocol service_name msg

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		if [[ "$protocol" == "$target_protocol" ]]; then
			continue
		fi

		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if ! _proxy_user_protocol_exists "$protocol" && ! _proxy_user_service_is_active "$service_name"; then
			continue
		fi

		msg=$(_t 'Failed to disable proxy service {}.')
		if ! _proxy_user_service_stop "$protocol"; then
			_error "${msg//\{\}/$service_name}"
			return 1
		fi
	done
}

_proxy_enable_services() {
	local force="${1:-false}"
	local allow_active_service_fallback="${2:-true}"
	local protocol selected_protocol service_name msg port
	local -a protocols=()
	local -a known_protocols=()

	if _proxy_is_docker || _proxy_is_wsl2; then
		return 0
	fi

	if ! _proxy_service_manager_available; then
		msg=$(_proxy_service_manager_unavailable_warning)
		_warning "$msg"
		if [[ "$force" == true ]]; then
			return 0
		fi
		return 1
	fi

	while IFS= read -r protocol; do
		if [[ -n "$protocol" ]]; then
			protocols+=("$protocol")
		fi
	done < <(_proxy_list_enable_protocols)

	if [[ ${#protocols[@]} -eq 0 ]]; then
		while IFS= read -r protocol; do
			if [[ -n "$protocol" ]]; then
				known_protocols+=("$protocol")
			fi
		done < <(_proxy_list_user_protocols)

		if [[ ${#known_protocols[@]} -gt 1 ]]; then
			_warning "$(_t "Multiple installed sing-box client protocols detected. Run \`proxy protocol <protocol>\` first.")"
			if [[ "$force" == true ]]; then
				return 0
			fi
			return 1
		fi

		if [[ "$allow_active_service_fallback" == true ]] && _proxy_has_active_service; then
			return 0
		fi

		_warning "$(_t 'No installed sing-box client protocol detected.')"
		if [[ "$force" == true ]]; then
			return 0
		fi
		return 1
	fi

	if [[ ${#protocols[@]} -eq 1 ]]; then
		for selected_protocol in "${protocols[@]}"; do
			break
		done
		_proxy_disable_other_user_protocol_services "$selected_protocol" || return 1
	fi

	port=$(_proxy_read_config_port)
	if ! _proxy_port_is_available "$port" && ! _proxy_client_is_serving "$port"; then
		if [[ "$_PROXY_PORT_CHECK_ERROR" == unavailable ]]; then
			msg=$(_t 'Could not verify that port {} is free for both TCP and UDP.')
		else
			msg=$(_t 'Port {} is already in use.')
		fi
		_error "${msg//\{\}/$port}"
		return 1
	fi

	for protocol in "${protocols[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		msg=$(_t 'Failed to enable proxy service {}.')
		if ! _proxy_user_service_start "$protocol"; then
			_error "${msg//\{\}/$service_name}"
			return 1
		fi
	done

	return 0
}

_proxy_set_service() {
	if ! _proxy_tun_guard_mixed 'proxy service on'; then
		return 1
	fi

	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi

	_proxy_enable_services false false
}

_proxy_unset_service() {
	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi

	_proxy_disable_services || return 1
	_proxy_tun_note_still_active
}

_proxy_switch_protocol() {
	local protocol="${1:-}"
	local service_name msg active_protocol

	if [[ $# -gt 0 ]]; then
		shift
	fi

	if [[ -z "$protocol" ]]; then
		active_protocol=$(_proxy_detect_active_protocol || true)
		_proxy_report_row 'Active protocol' "${active_protocol:-$(_t 'none')}"
		msg=$(_t 'Missing protocol. Supported protocols: {}')
		_error "${msg//\{\}/${SUPPORTED_PROTOCOLS[*]}}"
		return 1
	fi

	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi

	if ! _proxy_is_supported_protocol "$protocol"; then
		msg=$(_t 'Unsupported protocol {}. Supported protocols: {}')
		msg=${msg/\{\}/$protocol}
		_error "${msg//\{\}/${SUPPORTED_PROTOCOLS[*]}}"
		return 1
	fi

	# Under an active, transitioning, or half-recovered tunnel the mixed path
	# below would start a user unit beneath the TUN's route capture.
	if ! tun_is_safely_off; then
		_proxy_tun_switch_protocol "$protocol"
		return $?
	fi

	if ! _proxy_user_protocol_config_exists "$protocol"; then
		msg=$(_t 'Protocol {} is not installed for this user. Reinstall from the portal if you have access.')
		_error "${msg//\{\}/$protocol}"
		return 1
	fi

	service_name=$(_proxy_service_name_for_protocol "$protocol")
	if ! _proxy_user_service_exists "$service_name"; then
		msg=$(_t 'Protocol service {} is not installed for this user. Reinstall from the portal if you have access.')
		_error "${msg//\{\}/$service_name}"
		return 1
	fi

	if ! _has systemctl; then
		_error "$(_t 'systemctl is not available. Use -f/--force to set proxy anyway.')"
		return 1
	fi

	if ! _proxy_disable_other_user_protocol_services "$protocol"; then
		return 1
	fi

	if ! _proxy_run_systemctl_for_scope user enable --now "$service_name" >/dev/null 2>&1; then
		msg=$(_t 'Failed to switch proxy protocol to {}.')
		_error "${msg//\{\}/$protocol}"
		return 1
	fi

	if ! _proxy_user_service_is_active "$service_name"; then
		msg=$(_t 'Failed to switch proxy protocol to {}.')
		_error "${msg//\{\}/$protocol}"
		return 1
	fi

	if ! _proxy_save_selected_protocol "$protocol"; then
		_error "$(_t 'Failed to save selected proxy protocol.')"
		return 1
	fi

	msg=$(_t 'Proxy protocol switched to {}.')
	success "${msg//\{\}/$protocol}"
}

# Route switching is a single transaction across every installed mixed-mode
# protocol. These globals keep the rollback snapshot available to helpers while
# remaining compatible with Bash 3.2 (no associative arrays).
_PROXY_ROUTE_PROTOCOLS=()
_PROXY_ROUTE_ACTIVE_PROTOCOLS=()
_PROXY_ROUTE_SNAPSHOT_DIR=""
_PROXY_ROUTE_HAD_MARKER=false

_proxy_route_installed_protocols() {
	local protocol

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		if [[ -f "${SING_BOX_CONFIG_ROOT}/${protocol}/config.json" ]]; then
			printf '%s\n' "$protocol"
		fi
	done
}

_proxy_route_variant_path() {
	printf '%s/%s/config-%s.json\n' "$SING_BOX_CONFIG_ROOT" "$1" "$2"
}

_proxy_route_atomic_copy() {
	local source="$1"
	local destination="$2"
	local temporary="${destination}.route.$$"

	rm -f "$temporary"
	cp "$source" "$temporary" || {
		rm -f "$temporary"
		return 1
	}
	chmod 0600 "$temporary" || {
		rm -f "$temporary"
		return 1
	}
	mv -f "$temporary" "$destination" || {
		rm -f "$temporary"
		return 1
	}
}

# Run a command under a wall-clock limit. macOS ships no timeout(1), and this
# file is sourced by interactive shells, so the background job must not leave a
# job-control notice behind either.
_proxy_run_with_timeout() {
	local seconds="$1"
	shift
	local pid command_status waited=0
	local restore_bash_monitor=""

	if [ -n "${ZSH_VERSION:-}" ]; then
		setopt local_options nomonitor
	fi
	if [ -n "${BASH_VERSION:-}" ] && [[ -o monitor ]]; then
		restore_bash_monitor="on"
		set +m
	fi

	"$@" >/dev/null 2>&1 &
	pid=$!
	command_status=124
	while [ "$waited" -lt "$seconds" ]; do
		if ! kill -0 "$pid" 2>/dev/null; then
			wait "$pid"
			command_status=$?
			break
		fi
		sleep 1
		waited=$((waited + 1))
	done
	if [ "$command_status" -eq 124 ]; then
		kill "$pid" 2>/dev/null
		wait "$pid" 2>/dev/null
	fi

	if [ "$restore_bash_monitor" = "on" ]; then
		set -m
	fi
	return "$command_status"
}

# Fetch one URL through a config to prove its DNS and routing work.
#
# `sing-box check` only parses a config: a route whose resolver this machine
# refuses to query passes it, starts, and listens, while every destination it
# sends direct fails to resolve. `tools fetch` starts a config's outbounds, DNS
# and route without its inbounds, so a candidate can be exercised while the
# current route is still serving.
#
# The working directory is a throwaway, because the cache file is locked by
# whichever instance opened it and a probe sharing that path would block until
# the timeout. Bundled rule snapshots are read from the working directory, so
# they are linked back into it.
_proxy_route_probe() {
	local config="$1"
	local binary probe_dir probe_status

	binary=$(_proxy_client_binary_path)
	if [[ ! -x "$binary" || ! -f "$config" ]]; then
		return 1
	fi
	probe_dir=$(mktemp -d "${TMPDIR:-/tmp}/sing-box-probe.XXXXXX") || return 1
	if [[ -d "${SING_BOX_STATE_ROOT}/rules" ]]; then
		ln -s "${SING_BOX_STATE_ROOT}/rules" "${probe_dir}/rules" 2>/dev/null || true
	fi
	_proxy_run_with_timeout "$ROUTE_PROBE_TIMEOUT" \
		"$binary" tools fetch -D "$probe_dir" -c "$config" "$ROUTE_PROBE_URL"
	probe_status=$?
	rm -rf "$probe_dir"
	return "$probe_status"
}

# The Linux ai route resolves direct traffic through systemd-resolved's stub.
_proxy_resolved_stub_is_active() {
	plat_is_linux || return 1
	command -v systemctl >/dev/null 2>&1 || return 1
	[[ "$(systemctl is-active systemd-resolved 2>/dev/null)" == "active" ]]
}

_proxy_route_service_stop() {
	local protocol="$1"

	if plat_is_macos; then
		launchctl bootout "$(_proxy_launchd_target_for_protocol "$protocol")" >/dev/null 2>&1
		return
	fi
	_proxy_user_service_stop "$protocol"
}

_proxy_route_reset_transaction() {
	_PROXY_ROUTE_PROTOCOLS=()
	_PROXY_ROUTE_ACTIVE_PROTOCOLS=()
	_PROXY_ROUTE_SNAPSHOT_DIR=""
	_PROXY_ROUTE_HAD_MARKER=false
}

_proxy_route_rollback() {
	local protocol marker backup destination
	local rollback_ok=true

	# Stop any service that may have been started from the attempted configs so
	# the final state can exactly match the pre-transaction active set.
	for protocol in "${_PROXY_ROUTE_PROTOCOLS[@]}"; do
		if _proxy_user_service_is_active "$(_proxy_service_name_for_protocol "$protocol")"; then
			_proxy_route_service_stop "$protocol" || rollback_ok=false
		fi
	done

	for protocol in "${_PROXY_ROUTE_PROTOCOLS[@]}"; do
		backup="${_PROXY_ROUTE_SNAPSHOT_DIR}/${protocol}.json"
		destination="${SING_BOX_CONFIG_ROOT}/${protocol}/config.json"
		if [[ ! -f "$backup" ]] || ! _proxy_route_atomic_copy "$backup" "$destination"; then
			rollback_ok=false
		fi
	done

	marker=$(_proxy_selected_route_file)
	if [[ "$_PROXY_ROUTE_HAD_MARKER" == true ]]; then
		if ! _proxy_route_atomic_copy "${_PROXY_ROUTE_SNAPSHOT_DIR}/selected-route" "$marker"; then
			rollback_ok=false
		fi
	else
		rm -f "$marker" || rollback_ok=false
	fi

	for protocol in "${_PROXY_ROUTE_ACTIVE_PROTOCOLS[@]}"; do
		if ! _proxy_user_service_start "$protocol" ||
			! _proxy_user_service_wait_ready "$protocol"; then
			rollback_ok=false
		fi
	done

	rm -rf "$_PROXY_ROUTE_SNAPSHOT_DIR"
	_proxy_route_reset_transaction
	[[ "$rollback_ok" == true ]]
}

_proxy_check_route() {
	local route

	route=$(_proxy_read_selected_route || true)
	_proxy_report_row 'Selected route' "${route:-$(_t 'unknown')}"
	if ! tun_is_safely_off; then
		_proxy_report_row 'Active TUN route' "$(_t 'fixed/global')"
	fi
}

_proxy_switch_route() {
	local route="${1:-}"
	local protocol variant binary service_name msg marker
	local probe_protocol probe_config
	local -a protocols=()
	local -a active_protocols=()

	if [[ $# -gt 0 ]]; then
		shift
	fi
	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi
	if [[ -z "$route" ]]; then
		_proxy_check_route
		return 0
	fi
	if ! _proxy_is_supported_route "$route"; then
		msg=$(_t 'Unsupported route {}. Supported routes: {}')
		msg=${msg/\{\}/$route}
		_error "${msg//\{\}/${SUPPORTED_ROUTES[*]}}"
		return 1
	fi
	if ! _proxy_tun_guard_mixed "proxy route ${route}"; then
		return 1
	fi

	while IFS= read -r protocol; do
		[[ -n "$protocol" ]] && protocols+=("$protocol")
	done < <(_proxy_route_installed_protocols)
	if [[ ${#protocols[@]} -eq 0 ]]; then
		_error "$(_t 'No installed mixed proxy protocols were found.')"
		return 1
	fi

	# Refuse all incomplete or invalid target sets before stopping a service or
	# changing the marker.
	for protocol in "${protocols[@]}"; do
		variant=$(_proxy_route_variant_path "$protocol" "$route")
		if [[ ! -f "$variant" ]]; then
			msg=$(_t 'Route variant {} is missing. Reinstall the client.')
			_error "${msg//\{\}/$variant}"
			return 1
		fi
	done
	binary=$(_proxy_client_binary_path)
	if [[ ! -x "$binary" ]]; then
		_error "$(_t 'The installed sing-box binary is missing or is not executable.')"
		return 1
	fi
	for protocol in "${protocols[@]}"; do
		variant=$(_proxy_route_variant_path "$protocol" "$route")
		if ! "$binary" check -D "$SING_BOX_STATE_ROOT" -c "$variant" >/dev/null 2>&1; then
			msg=$(_t 'Route variant {} failed sing-box validation.')
			_error "${msg//\{\}/$variant}"
			return 1
		fi
	done

	# Validation above proves the configs parse. Resolve one name through the
	# candidate before stopping a service that works: a route can be perfectly
	# valid and still leave this machine unable to look anything up.
	probe_protocol=$(_proxy_selected_protocol || true)
	if [[ -z "$probe_protocol" ]]; then
		# Not "${protocols[0]}": this file is sourced into zsh, where arrays
		# start at 1.
		probe_protocol=$(_proxy_route_installed_protocols | head -n 1)
	fi
	if ! _proxy_route_probe "$(_proxy_route_variant_path "$probe_protocol" "$route")"; then
		probe_config="${SING_BOX_CONFIG_ROOT}/${probe_protocol}/config.json"
		if _proxy_route_probe "$probe_config"; then
			msg=$(_t 'Route {} cannot reach the network on this machine, so it was not installed.')
			_error "${msg//\{\}/$route}"
			if [[ "$route" == "ai" ]] && ! _proxy_resolved_stub_is_active; then
				_warning "$(_t 'The ai route resolves direct traffic through systemd-resolved, which is not running.')"
			fi
			hint "$(_t 'Routes that proxy unknown destinations still work: proxy route gfw')"
			return 1
		fi
		# The route in place fails the same fetch, so the machine is offline or
		# the binary is too old to probe. Refusing here would only strand it.
		msg=$(_t 'Could not verify route {}. The current route fails the same check, so this machine may be offline.')
		_warning "${msg//\{\}/$route}"
	fi

	_PROXY_ROUTE_SNAPSHOT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/sing-box-route.XXXXXX") || return 1
	_PROXY_ROUTE_PROTOCOLS=("${protocols[@]}")
	_PROXY_ROUTE_ACTIVE_PROTOCOLS=()
	_PROXY_ROUTE_HAD_MARKER=false
	for protocol in "${protocols[@]}"; do
		cp "${SING_BOX_CONFIG_ROOT}/${protocol}/config.json" \
			"${_PROXY_ROUTE_SNAPSHOT_DIR}/${protocol}.json" || {
			_proxy_route_rollback >/dev/null 2>&1 || true
			return 1
		}
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_service_is_active "$service_name"; then
			active_protocols+=("$protocol")
		fi
	done
	_PROXY_ROUTE_ACTIVE_PROTOCOLS=("${active_protocols[@]}")
	marker=$(_proxy_selected_route_file)
	if [[ -f "$marker" ]]; then
		cp "$marker" "${_PROXY_ROUTE_SNAPSHOT_DIR}/selected-route" || {
			_proxy_route_rollback >/dev/null 2>&1 || true
			return 1
		}
		_PROXY_ROUTE_HAD_MARKER=true
	fi

	for protocol in "${active_protocols[@]}"; do
		if ! _proxy_route_service_stop "$protocol"; then
			_error "$(_t 'Failed to stop active proxy services for route switching.')"
			if ! _proxy_route_rollback; then
				_error "$(_t 'Route rollback did not complete.')"
			fi
			return 1
		fi
	done

	for protocol in "${protocols[@]}"; do
		variant=$(_proxy_route_variant_path "$protocol" "$route")
		if ! _proxy_route_atomic_copy "$variant" \
			"${SING_BOX_CONFIG_ROOT}/${protocol}/config.json"; then
			msg=$(_t 'Failed to install route {}. Restoring the previous route.')
			_error "${msg//\{\}/$route}"
			if ! _proxy_route_rollback; then
				_error "$(_t 'Route rollback did not complete.')"
			fi
			return 1
		fi
	done
	if ! _proxy_save_selected_route "$route"; then
		msg=$(_t 'Failed to install route {}. Restoring the previous route.')
		_error "${msg//\{\}/$route}"
		if ! _proxy_route_rollback; then
			_error "$(_t 'Route rollback did not complete.')"
		fi
		return 1
	fi

	for protocol in "${active_protocols[@]}"; do
		if ! _proxy_user_service_start "$protocol" ||
			! _proxy_user_service_wait_ready "$protocol"; then
			_error "$(_t 'Failed to restart active proxy services. Restoring the previous route.')"
			if ! _proxy_route_rollback; then
				_error "$(_t 'Route rollback did not complete.')"
			fi
			return 1
		fi
	done

	rm -rf "$_PROXY_ROUTE_SNAPSHOT_DIR"
	_proxy_route_reset_transaction
	msg=$(_t 'Mixed-mode route switched to {}.')
	success "${msg//\{\}/$route}"
}

_proxy_disable_services() {
	local protocol service_name msg
	local -a protocols=()

	if _proxy_is_docker || _proxy_is_wsl2; then
		return 0
	fi

	if ! _proxy_service_manager_available; then
		return 0
	fi

	while IFS= read -r protocol; do
		if [[ -n "$protocol" ]]; then
			protocols+=("$protocol")
		fi
	done < <(_proxy_list_disable_protocols)

	if [[ ${#protocols[@]} -eq 0 ]]; then
		return 0
	fi

	for protocol in "${protocols[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		msg=$(_t 'Failed to disable proxy service {}.')
		if ! _proxy_user_service_stop "$protocol"; then
			_error "${msg//\{\}/$service_name}"
			return 1
		fi
	done

	return 0
}

# --- TUN mode ---------------------------------------------------------------
#
# `proxy tun on` provisions and starts a machine-wide tunnel: one fixed system
# service running as a dedicated locked account with CAP_NET_ADMIN and nothing
# else, from root-owned files outside $HOME. docs/TUN_MODE.md is the spec, and
# lib/tun.sh holds the parts client-install.sh and client-uninstall.sh share.
#
# The tunnel is Linux-only and refuses rather than half-supporting WSL2,
# containers, macOS, and remote shells.

# Transaction bookkeeping. Plain scalars rather than an associative array so
# this keeps running on the bash 3.2 that ships as macOS /bin/bash.
_PROXY_TUN_PROTOCOL=""
_PROXY_TUN_GENERATION=""
_PROXY_TUN_PERSIST=false
_PROXY_TUN_PHASE="none"
_PROXY_TUN_ACCOUNT_CREATED=false
_PROXY_TUN_PREVIOUS_GENERATION=""
_PROXY_TUN_PREVIOUS_ACTIVE=false
_PROXY_TUN_PREVIOUS_ENABLED=false
_PROXY_TUN_MIXED_SNAPSHOT=""
_PROXY_TUN_SHELL_PROXY=""
_PROXY_TUN_GIT_HTTP=""
_PROXY_TUN_GIT_HTTPS=""
_PROXY_TUN_DESKTOP_STATE="off"
_PROXY_TUN_DOCKER_STATE="off"
_PROXY_TUN_SAFEGUARD_ARMED=false
_PROXY_TUN_EGRESS_PROBE_URL="${SBM_TUN_EGRESS_PROBE_URL:-https://www.gstatic.com/generate_204}"

_proxy_tun_reset_transaction_state() {
	_PROXY_TUN_PROTOCOL=""
	_PROXY_TUN_GENERATION=""
	_PROXY_TUN_PERSIST=false
	_PROXY_TUN_PHASE="none"
	_PROXY_TUN_ACCOUNT_CREATED=false
	_PROXY_TUN_PREVIOUS_GENERATION=""
	_PROXY_TUN_PREVIOUS_ACTIVE=false
	_PROXY_TUN_PREVIOUS_ENABLED=false
	_PROXY_TUN_MIXED_SNAPSHOT=""
	_PROXY_TUN_SHELL_PROXY=""
	_PROXY_TUN_GIT_HTTP=""
	_PROXY_TUN_GIT_HTTPS=""
	_PROXY_TUN_DESKTOP_STATE="off"
	_PROXY_TUN_DOCKER_STATE="off"
	_PROXY_TUN_SAFEGUARD_ARMED=false
}

# Sources and identity ########################################################

_proxy_tun_proxy_endpoint() {
	printf 'http://%s:%s\n' "$DEFAULT_PROXY_HOST" "$(_proxy_read_config_port)"
}

# The self-managed TUN profile client-install.sh laid down next to config.json.
_proxy_tun_user_config_path() {
	local protocol="$1"
	local default_root

	if [[ -f "${SING_BOX_CONFIG_ROOT}/${protocol}/config-tun.json" ]]; then
		printf '%s/%s/config-tun.json\n' "$SING_BOX_CONFIG_ROOT" "$protocol"
		return 0
	fi

	default_root=$(_proxy_default_user_config_root)
	if [[ "$default_root" != "$SING_BOX_CONFIG_ROOT" ]] &&
		[[ -f "${default_root}/${protocol}/config-tun.json" ]]; then
		printf '%s/%s/config-tun.json\n' "$default_root" "$protocol"
		return 0
	fi

	return 1
}

_proxy_client_binary_path() {
	printf '%s/.local/bin/sing-box\n' "$HOME"
}

_proxy_client_cronet_path() {
	printf '%s/.local/bin/libcronet.so\n' "$HOME"
}

# Regular file, and not a symlink pointing somewhere the privileged half would
# otherwise follow.
_proxy_tun_is_plain_file() {
	local candidate_path="$1"

	[[ -f "$candidate_path" && ! -L "$candidate_path" ]]
}

_proxy_tun_remote_shell_detected() {
	[[ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}${SSH_TTY:-}" ]]
}

_proxy_tun_dev_tun_present() {
	[[ -c /dev/net/tun ]]
}

_proxy_tun_new_generation() {
	printf '%s-%s\n' "$(date -u '+%Y%m%dT%H%M%SZ')" "$$"
}

# The privileged toggle exists on native Linux only, so macOS is never offered
# a command it cannot run.
_proxy_tun_commands_available() {
	_proxy_is_linux
}

# Guards ######################################################################

# Applied by every command that would point applications at the mixed inbound.
# The predicate is "safely off", not "is-active": a half-finished transaction or
# a restart loop leaves routing in a state mixed mode must not start under.
# `--force` deliberately does not reach here.
_proxy_tun_guard_mixed() {
	local command_label="$1"
	local msg status_value

	if tun_is_safely_off; then
		return 0
	fi

	msg=$(_t 'Refusing {}: machine-wide TUN mode is not safely off.')
	_error "${msg//\{\}/$command_label}"

	status_value=$(tun_status)
	if tun_transaction_in_progress; then
		_proxy_report_row 'TUN' "$(_t 'a TUN transaction is in progress')"
	elif [[ -n "$status_value" ]]; then
		_proxy_report_row 'TUN' "$status_value"
	fi

	hint "$(_t "Run \`proxy tun off\` first, then run this command again.")"
	return 1
}

# `off` commands clear what they own and then say so, rather than refusing:
# leaving a user unable to clean up their own settings would be worse.
_proxy_tun_note_still_active() {
	if tun_is_safely_off; then
		return 0
	fi

	_warning "$(_t 'Machine-wide TUN mode is still active; these settings do not control it.')"
	hint "$(_t "Run \`proxy tun off\` to stop the tunnel.")"
}

_proxy_tun_owner_mismatch_error() {
	local owner_uid="$1"
	local msg

	msg=$(_t 'Machine-wide TUN mode is owned by another local user (uid {}).')
	_error "${msg//\{\}/$owner_uid}"
	hint "$(_t "That user runs \`proxy tun off\`; automatic takeover is not supported.")"
}

# Privileged primitives #######################################################

_proxy_tun_authenticate_sudo() {
	if [[ "$(id -u)" -eq 0 ]]; then
		return 0
	fi

	if ! _has sudo; then
		_error "$(_t 'sudo is required to change machine-wide TUN mode.')"
		return 1
	fi

	if sudo -n true 2>/dev/null; then
		return 0
	fi

	hint "$(_t 'Changing machine-wide TUN mode requires administrator privileges.')"
	if ! sudo -v; then
		_error "$(_t 'Administrator authentication failed.')"
		return 1
	fi
}

# stdin -> a root-owned file created with its final mode, so there is never a
# window where a credential-bearing config is readable by everyone.
_proxy_tun_write_root_file() {
	local owner="$1"
	local mode="$2"
	local dest="$3"

	tun_run_privileged install -o "${owner%%:*}" -g "${owner##*:}" -m "$mode" \
		/dev/null "$dest" >/dev/null 2>&1 || return 1
	tun_run_privileged tee "$dest" >/dev/null
}

# The unprivileged shell opens the source and pipes it in, so no privileged
# command ever dereferences a path the user controls.
_proxy_tun_stage_from_source() {
	local source_path="$1"
	local owner="$2"
	local mode="$3"
	local dest="$4"
	local staged

	staged=$(tun_staged_path "$dest")
	tun_run_privileged install -o "${owner%%:*}" -g "${owner##*:}" -m "$mode" \
		/dev/null "$staged" >/dev/null 2>&1 || return 1
	tun_run_privileged tee "$staged" <"$source_path" >/dev/null
}

_proxy_tun_discard_staged() {
	local dest

	for dest in "$@"; do
		tun_run_privileged rm -f "$(tun_staged_path "$dest")" >/dev/null 2>&1 || true
	done
}

# Journal, marker, status #####################################################

_proxy_tun_journal_write() {
	local content="$1"

	tun_run_privileged mkdir -p -m 0700 "$(tun_control_dir)" >/dev/null 2>&1 || return 1
	printf '%s' "$content" | _proxy_tun_write_root_file root:root 0600 "$(tun_journal_path)"
}

_proxy_tun_journal_value() {
	local key="$1"
	local line

	line=$(tun_run_privileged cat "$(tun_journal_path)" 2>/dev/null |
		grep "^${key}=" | head -n 1) || return 1
	if [[ -z "$line" ]]; then
		return 1
	fi

	printf '%s\n' "${line#*=}"
}

_proxy_tun_journal_present() {
	tun_run_privileged test -f "$(tun_journal_path)" >/dev/null 2>&1
}

# Everything the recovery path needs to put the machine back, written before the
# first mutation and re-written as the transaction advances.
_proxy_tun_journal_record() {
	_proxy_tun_journal_write "$(
		printf 'schema_version=%s\n' "$(tun_manifest_schema_version)"
		printf 'phase=%s\n' "$_PROXY_TUN_PHASE"
		printf 'owner_uid=%s\n' "$(id -u)"
		printf 'protocol=%s\n' "$_PROXY_TUN_PROTOCOL"
		printf 'generation=%s\n' "$_PROXY_TUN_GENERATION"
		printf 'persist=%s\n' "$_PROXY_TUN_PERSIST"
		printf 'account_created=%s\n' "$_PROXY_TUN_ACCOUNT_CREATED"
		printf 'previous_generation=%s\n' "${_PROXY_TUN_PREVIOUS_GENERATION:-none}"
		printf 'previous_tun_active=%s\n' "$_PROXY_TUN_PREVIOUS_ACTIVE"
		printf 'previous_tun_enabled=%s\n' "$_PROXY_TUN_PREVIOUS_ENABLED"
		printf 'mixed_services=%s\n' "$_PROXY_TUN_MIXED_SNAPSHOT"
		printf 'shell_proxy=%s\n' "${_PROXY_TUN_SHELL_PROXY:-none}"
		printf 'git_http=%s\n' "${_PROXY_TUN_GIT_HTTP:-none}"
		printf 'git_https=%s\n' "${_PROXY_TUN_GIT_HTTPS:-none}"
		printf 'desktop_state=%s\n' "$_PROXY_TUN_DESKTOP_STATE"
		printf 'docker_state=%s\n' "$_PROXY_TUN_DOCKER_STATE"
	)"
}

_proxy_tun_set_phase() {
	_PROXY_TUN_PHASE="$1"
	_proxy_tun_journal_record
}

# Non-secret, world-readable, and checked by every mixed-mode command so a
# delayed `systemctl --user start` cannot race a transaction.
_proxy_tun_mark_transaction() {
	printf 'in-progress\n' |
		_proxy_tun_write_root_file root:root 0644 "$(tun_transaction_marker_path)"
}

_proxy_tun_clear_transaction() {
	tun_run_privileged rm -f "$(tun_transaction_marker_path)" >/dev/null 2>&1
}

# `dirty` before every mutation, `safe-off` only after cleanup has been verified.
_proxy_tun_write_status() {
	printf '%s\n' "$1" | _proxy_tun_write_root_file root:root 0644 "$(tun_status_path)"
}

# Runtime account #############################################################

_proxy_tun_account_exists() {
	local account
	account=$(tun_runtime_account)

	if _has getent; then
		getent passwd "$account" >/dev/null 2>&1
		return
	fi

	id -u "$account" >/dev/null 2>&1
}

# A pre-existing account keeps the name only when it looks like the locked
# system account we would have created. Anything else means the name belongs to
# somebody, and running a privileged service as them is not ours to decide.
_proxy_tun_account_is_expected() {
	local account entry uid shell_path
	account=$(tun_runtime_account)

	if ! _has getent; then
		return 1
	fi

	entry=$(getent passwd "$account" 2>/dev/null) || return 1
	uid=$(printf '%s\n' "$entry" | cut -d: -f3)
	shell_path=$(printf '%s\n' "$entry" | cut -d: -f7)

	if [[ -z "$uid" ]] || [[ "$uid" -ge 1000 ]]; then
		return 1
	fi

	case "$shell_path" in
	*/nologin | */false)
		return 0
		;;
	esac

	return 1
}

_proxy_tun_create_account() {
	local account useradd_command passwd_command
	account=$(tun_runtime_account)

	if ! useradd_command=$(tun_resolve_admin_command useradd); then
		_error "$(_t 'useradd is required to create the TUN service account.')"
		return 1
	fi

	tun_run_privileged "$useradd_command" --system --no-create-home --user-group \
		--shell /usr/sbin/nologin "$account" >/dev/null 2>&1 || return 1

	if passwd_command=$(tun_resolve_admin_command passwd); then
		tun_run_privileged "$passwd_command" --lock "$account" >/dev/null 2>&1 || true
	fi
}

# Only ever removes an account this transaction created, and only once nothing
# references it any more.
_proxy_tun_remove_account() {
	local account userdel_command
	account=$(tun_runtime_account)

	if tun_unit_installed; then
		return 1
	fi

	if ! userdel_command=$(tun_resolve_admin_command userdel); then
		return 1
	fi

	tun_run_privileged rm -rf "$(tun_state_dir)" >/dev/null 2>&1 || true
	tun_run_privileged "$userdel_command" "$account" >/dev/null 2>&1
}

_proxy_tun_account_uid() {
	id -u "$(tun_runtime_account)" 2>/dev/null
}

_proxy_tun_account_gid() {
	id -g "$(tun_runtime_account)" 2>/dev/null
}

# Preflight ###################################################################

# Proves the capability boundary the unit will actually run under, rather than
# inferring it from /dev/net/tun being present. A disposable interface is
# created and removed under the same UID, capability set, and device policy.
_proxy_tun_capability_probe() {
	local probe_interface="sbm-tun-probe"
	local ip_command account probe_status

	if ! _has systemd-run; then
		_error "$(_t 'systemd-run is required to verify TUN capabilities.')"
		return 1
	fi

	if ! ip_command=$(tun_ip_command); then
		_error "$(_t 'iproute2 (ip) is required for TUN mode.')"
		return 1
	fi

	account=$(tun_runtime_account)
	tun_run_privileged systemd-run --wait --collect --quiet \
		--unit="sing-box-manager-tun-probe" \
		--property="User=${account}" \
		--property="Group=${account}" \
		--property="CapabilityBoundingSet=CAP_NET_ADMIN" \
		--property="AmbientCapabilities=CAP_NET_ADMIN" \
		--property="NoNewPrivileges=true" \
		--property="DevicePolicy=closed" \
		--property="DeviceAllow=/dev/net/tun rw" \
		"$ip_command" tuntap add dev "$probe_interface" mode tun >/dev/null 2>&1
	probe_status=$?

	tun_run_privileged "$ip_command" tuntap del dev "$probe_interface" mode tun \
		>/dev/null 2>&1 || true

	return $probe_status
}

# Cheap, unprivileged, and run before anything prompts for a password.
_proxy_tun_preflight_user() {
	local protocol="$1"
	local runtime config_path binary_path msg

	runtime=$(_proxy_runtime_kind)
	if [[ "$runtime" != "linux" ]]; then
		msg=$(_t 'Machine-wide TUN mode supports native Linux only, not {}.')
		_error "${msg//\{\}/$runtime}"
		return 1
	fi

	if ! tun_systemd_available; then
		_error "$(_t 'A running systemd system manager is required for TUN mode.')"
		return 1
	fi

	if ! _proxy_tun_dev_tun_present; then
		_error "$(_t '/dev/net/tun is missing; the kernel tun module is not available.')"
		return 1
	fi

	if _proxy_tun_remote_shell_detected; then
		_error "$(_t 'Refusing to change machine-wide routing from a remote shell.')"
		hint "$(_t "Run \`proxy tun on\` from a local session on that machine.")"
		return 1
	fi

	if [[ -z "$protocol" ]]; then
		_error "$(_t 'No installed sing-box client protocol detected.')"
		return 1
	fi

	if ! config_path=$(_proxy_tun_user_config_path "$protocol"); then
		msg=$(_t 'Protocol {} has no installed TUN profile. Reinstall from the portal if you have access.')
		_error "${msg//\{\}/$protocol}"
		return 1
	fi

	if ! _proxy_user_protocol_config_exists "$protocol"; then
		msg=$(_t 'Protocol {} is not installed for this user. Reinstall from the portal if you have access.')
		_error "${msg//\{\}/$protocol}"
		return 1
	fi

	binary_path=$(_proxy_client_binary_path)
	if ! _proxy_tun_is_plain_file "$binary_path"; then
		msg=$(_t 'The sing-box binary {} is missing or is not a regular file.')
		_error "${msg//\{\}/$binary_path}"
		return 1
	fi

	if ! _proxy_tun_is_plain_file "$config_path"; then
		msg=$(_t 'The TUN configuration {} is missing or is not a regular file.')
		_error "${msg//\{\}/$config_path}"
		return 1
	fi

	if ! tun_sha256_command >/dev/null; then
		_error "$(_t 'sha256sum or shasum is required to verify managed TUN files.')"
		return 1
	fi

	return 0
}

# Re-run under the lock, because everything above could have changed between the
# cheap check and the password prompt.
_proxy_tun_preflight_locked() {
	local owner_uid

	if _proxy_tun_account_exists && ! _proxy_tun_account_is_expected; then
		_error "$(_t 'An unexpected account already owns the sing-box-tun name.')"
		return 1
	fi

	if tun_manifest_exists; then
		owner_uid=$(tun_manifest_value owner_uid 2>/dev/null)
		if [[ -n "$owner_uid" && "$owner_uid" != "$(id -u)" ]]; then
			_proxy_tun_owner_mismatch_error "$owner_uid"
			return 1
		fi
	fi

	if tun_foreign_interface_conflict; then
		_error "$(_t 'Another interface already holds the managed TUN name.')"
		return 1
	fi

	if tun_address_conflict; then
		_error "$(_t 'The managed TUN address ranges conflict with an existing interface.')"
		return 1
	fi

	if _proxy_tun_foreign_sing_box_active; then
		_error "$(_t 'A sing-box service outside this client is active; stop it first.')"
		return 1
	fi

	return 0
}

# System-scope sing-box units are not ours to stop, so their presence is a
# refusal rather than something the transaction quietly manages.
_proxy_tun_foreign_sing_box_active() {
	local protocol

	if ! _has systemctl; then
		return 1
	fi

	if systemctl is-active --quiet "sing-box.service" 2>/dev/null; then
		return 0
	fi

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		if systemctl is-active --quiet "sing-box-${protocol}.service" 2>/dev/null; then
			return 0
		fi
	done

	return 1
}

# Snapshots ###################################################################

_proxy_tun_snapshot_services() {
	local protocol service_name active enabled entries=""

	if tun_unit_installed; then
		if tun_unit_is_active; then
			_PROXY_TUN_PREVIOUS_ACTIVE=true
		fi
		if tun_unit_is_enabled; then
			_PROXY_TUN_PREVIOUS_ENABLED=true
		fi
		_PROXY_TUN_PREVIOUS_GENERATION=$(tun_manifest_value generation 2>/dev/null || true)
	fi

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if ! _proxy_user_service_exists "$service_name"; then
			continue
		fi

		active=inactive
		if _proxy_user_service_is_active "$service_name"; then
			active=active
		fi

		enabled=disabled
		if systemctl --user is-enabled --quiet "$service_name" 2>/dev/null; then
			enabled=enabled
		fi

		entries="${entries}${entries:+ }${protocol}:${active}:${enabled}"
	done

	_PROXY_TUN_MIXED_SNAPSHOT="$entries"
}

# Only values that match this client's own endpoint are recorded as ours. A
# corporate or hand-configured proxy is reported and left exactly as it was.
_proxy_tun_snapshot_proxy_settings() {
	local endpoint value
	endpoint=$(_proxy_tun_proxy_endpoint)

	value=$(_proxy_first_proxy_value "${http_proxy:-}" "${HTTP_PROXY:-}" || true)
	if [[ "$value" == "$endpoint" ]]; then
		_PROXY_TUN_SHELL_PROXY="$value"
	elif [[ -n "$value" ]]; then
		_warning "$(_t 'A shell proxy this client does not own was left unchanged.')"
	fi

	if _has git; then
		value=$(git config --global --get http.proxy 2>/dev/null || true)
		if [[ "$value" == "$endpoint" ]]; then
			_PROXY_TUN_GIT_HTTP="$value"
		elif [[ -n "$value" ]]; then
			_warning "$(_t 'A global Git proxy this client does not own was left unchanged.')"
		fi

		value=$(git config --global --get https.proxy 2>/dev/null || true)
		if [[ "$value" == "$endpoint" ]]; then
			_PROXY_TUN_GIT_HTTPS="$value"
		fi
	fi

	_PROXY_TUN_DESKTOP_STATE=$(_proxy_tun_classify_desktop_proxy)
	if [[ "$_PROXY_TUN_DESKTOP_STATE" == "other" ]]; then
		_warning "$(_t 'A desktop proxy this client does not own was left unchanged.')"
	fi

	_PROXY_TUN_DOCKER_STATE=$(_proxy_tun_classify_docker_proxy)
	if [[ "$_PROXY_TUN_DOCKER_STATE" == "other" ]]; then
		_warning "$(_t 'A Docker proxy this client does not own was left unchanged.')"
	fi
}

_proxy_tun_classify_desktop_proxy() {
	local target

	if ! target=$(system_proxy_target_name 2>/dev/null); then
		printf 'off\n'
		return 0
	fi

	system_proxy_classify "$target" 2>/dev/null || printf 'off\n'
}

_proxy_tun_classify_docker_proxy() {
	local config_file endpoint value daemon_file

	endpoint=$(_proxy_tun_proxy_endpoint)
	config_file=$(_proxy_docker_config_file)
	daemon_file=$(_proxy_docker_daemon_proxy_file)

	if [[ -f "$config_file" ]] && _has jq; then
		value=$(jq -r '.proxies.default.httpProxy // empty' "$config_file" 2>/dev/null || true)
		if [[ -n "$value" && "$value" != "$endpoint" ]]; then
			printf 'other\n'
			return 0
		fi
		if [[ "$value" == "$endpoint" ]]; then
			printf 'ours\n'
			return 0
		fi
	fi

	if [[ -f "$daemon_file" ]]; then
		if grep -q "http_proxy=${endpoint}" "$daemon_file" 2>/dev/null; then
			printf 'ours\n'
		else
			printf 'other\n'
		fi
		return 0
	fi

	printf 'off\n'
}

# Quiesce #####################################################################

# Mixed and TUN must never overlap: the TUN's route capture would otherwise
# capture the mixed instance's own outbound and loop.
_proxy_tun_quiesce() {
	local entry protocol service_name msg

	if tun_unit_installed; then
		tun_run_privileged systemctl disable "$(tun_unit_name)" >/dev/null 2>&1 || true
		tun_run_privileged systemctl stop "$(tun_unit_name)" >/dev/null 2>&1 || true
		if ! _proxy_tun_wait_inactive; then
			_error "$(_t 'The previous TUN service did not stop.')"
			return 1
		fi
	fi

	while IFS= read -r entry; do
		if [[ -z "$entry" ]]; then
			continue
		fi
		protocol="${entry%%:*}"
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		systemctl --user disable --now "$service_name" >/dev/null 2>&1 || true
		if _proxy_user_service_is_active "$service_name"; then
			msg=$(_t 'Failed to stop mixed proxy service {}.')
			_error "${msg//\{\}/$service_name}"
			return 1
		fi
	done < <(_proxy_tun_mixed_snapshot_entries)

	if _proxy_tun_foreign_sing_box_active; then
		_error "$(_t 'A sing-box service outside this client is active; stop it first.')"
		return 1
	fi

	_proxy_tun_clear_owned_proxy_settings
	return 0
}

_proxy_tun_clear_owned_proxy_settings() {
	if [[ -n "$_PROXY_TUN_SHELL_PROXY" ]]; then
		_unset_proxy_env_vars
	fi

	if [[ -n "$_PROXY_TUN_GIT_HTTP" ]]; then
		git config --global --unset-all http.proxy >/dev/null 2>&1 || true
	fi
	if [[ -n "$_PROXY_TUN_GIT_HTTPS" ]]; then
		git config --global --unset-all https.proxy >/dev/null 2>&1 || true
	fi

	if [[ "$_PROXY_TUN_DESKTOP_STATE" == "ours" ]]; then
		_proxy_clear_desktop_proxy >/dev/null 2>&1 || true
	fi

	if [[ "$_PROXY_TUN_DOCKER_STATE" == "ours" ]]; then
		_proxy_unset_docker >/dev/null 2>&1 || true
	fi

	# One shell cannot rewrite variables another already inherited, and no
	# repository-local Git setting outside this tree can be enumerated.
	_warning "$(_t 'Shells and repositories configured earlier may still point at the stopped mixed inbound.')"
	hint "$(_t "Open a new shell, or run the matching \`proxy ... off\` command there.")"
}

_proxy_tun_restore_owned_proxy_settings() {
	local proxy_host proxy_port

	proxy_host="$DEFAULT_PROXY_HOST"
	proxy_port=$(_proxy_read_config_port)

	if [[ -n "$_PROXY_TUN_SHELL_PROXY" ]]; then
		_set_proxy_env_vars "$proxy_host" "$proxy_port"
	fi

	if [[ -n "$_PROXY_TUN_GIT_HTTP" ]]; then
		git config --global http.proxy "$_PROXY_TUN_GIT_HTTP" >/dev/null 2>&1 || true
	fi
	if [[ -n "$_PROXY_TUN_GIT_HTTPS" ]]; then
		git config --global https.proxy "$_PROXY_TUN_GIT_HTTPS" >/dev/null 2>&1 || true
	fi

	if [[ "$_PROXY_TUN_DESKTOP_STATE" == "ours" ]]; then
		_proxy_apply_desktop_proxy "$proxy_host" "$proxy_port" >/dev/null 2>&1 || true
	fi

	if [[ "$_PROXY_TUN_DOCKER_STATE" == "ours" ]]; then
		_proxy_apply_docker_client_proxy "$proxy_host" "$proxy_port" >/dev/null 2>&1 || true
		if plat_is_macos; then
			return 0
		fi
		if _has docker; then
			_proxy_apply_docker_daemon_proxy "$proxy_host" "$proxy_port" \
				>/dev/null 2>&1 || true
		fi
	fi
}

_proxy_tun_wait_inactive() {
	local attempt=0

	while [[ "$attempt" -lt 15 ]]; do
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

# Safeguard ###################################################################

# A root-owned transient timer, so a lost controlling process still returns the
# machine to direct networking. It cannot reconstruct user-scoped state; the
# normal rollback does that while this shell is still alive.
_proxy_tun_arm_safeguard() {
	local systemctl_command

	if ! _has systemd-run; then
		_error "$(_t 'systemd-run is required to arm the TUN safeguard.')"
		return 1
	fi

	systemctl_command=$(command -v systemctl 2>/dev/null)
	tun_run_privileged systemd-run --collect --quiet \
		--unit="$(tun_safeguard_unit_name)" \
		--on-active="${SBM_TUN_SAFEGUARD_SECONDS:-300}" \
		--description="sing-box-manager TUN safeguard" \
		"$systemctl_command" stop "$(tun_unit_name)" >/dev/null 2>&1 || return 1

	_PROXY_TUN_SAFEGUARD_ARMED=true
}

_proxy_tun_cancel_safeguard() {
	local safeguard

	if [[ "$_PROXY_TUN_SAFEGUARD_ARMED" != true ]]; then
		return 0
	fi

	safeguard=$(tun_safeguard_unit_name)
	tun_run_privileged systemctl stop "${safeguard}.timer" >/dev/null 2>&1 || true
	tun_run_privileged systemctl stop "${safeguard}.service" >/dev/null 2>&1 || true
	tun_run_privileged systemctl reset-failed "${safeguard}.timer" "${safeguard}.service" \
		>/dev/null 2>&1 || true

	# Confirm rather than assume: an armed timer that survives would stop a
	# tunnel the user believes is committed.
	if systemctl show "${safeguard}.timer" --property=LoadState --value 2>/dev/null |
		grep -q 'loaded'; then
		_warning "$(_t 'The TUN safeguard timer could not be cancelled.')"
		return 1
	fi

	_PROXY_TUN_SAFEGUARD_ARMED=false
	return 0
}

# Staging and publication #####################################################

_proxy_tun_managed_destinations() {
	printf '%s\n' \
		"$(tun_binary_path)" \
		"$(tun_cronet_path)" \
		"$(tun_config_path)" \
		"$(tun_unit_path)" \
		"$(tun_manifest_path)"
}

_proxy_tun_prepare_directories() {
	tun_run_privileged install -d -o root -g root -m 0755 "$(tun_libexec_dir)" \
		>/dev/null 2>&1 || return 1
	tun_run_privileged install -d -o root -g root -m 0755 "$(tun_unit_dir)" \
		>/dev/null 2>&1 || return 1
	tun_run_privileged install -d -o root -g root -m 0755 "${SBM_TUN_ROOT:-}/etc/sing-box-manager" \
		>/dev/null 2>&1 || return 1
	tun_run_privileged install -d -o root -g "$(tun_runtime_account)" -m 0750 \
		"$(tun_config_dir)" >/dev/null 2>&1 || return 1
	tun_run_privileged install -d -o root -g root -m 0755 "$(tun_rules_dir)" \
		>/dev/null 2>&1 || return 1
	tun_run_privileged install -d -o root -g root -m 0700 "$(tun_control_dir)" \
		>/dev/null 2>&1 || return 1
}

_proxy_tun_stage_payload() {
	local protocol="$1"
	local account binary_source cronet_source config_source staged_binary staged_config

	account=$(tun_runtime_account)
	binary_source=$(_proxy_client_binary_path)
	cronet_source=$(_proxy_client_cronet_path)
	config_source=$(_proxy_tun_user_config_path "$protocol") || return 1

	if ! _proxy_tun_stage_from_source "$binary_source" root:root 0755 "$(tun_binary_path)"; then
		_error "$(_t 'Failed to stage the TUN binary.')"
		return 1
	fi

	if _proxy_tun_is_plain_file "$cronet_source"; then
		if ! _proxy_tun_stage_from_source "$cronet_source" root:root 0644 "$(tun_cronet_path)"; then
			_error "$(_t 'Failed to stage the Naive runtime library.')"
			return 1
		fi
	fi

	if ! _proxy_tun_stage_from_source "$config_source" "root:${account}" 0640 "$(tun_config_path)"; then
		_error "$(_t 'Failed to stage the TUN configuration.')"
		return 1
	fi

	if ! tun_render_unit | _proxy_tun_write_root_file root:root 0644 \
		"$(tun_staged_path "$(tun_unit_path)")"; then
		_error "$(_t 'Failed to stage the TUN service unit.')"
		return 1
	fi

	staged_binary=$(tun_staged_path "$(tun_binary_path)")
	staged_config=$(tun_staged_path "$(tun_config_path)")
	if ! tun_run_privileged "$staged_binary" check -c "$staged_config" >/dev/null 2>&1; then
		_error "$(_t 'The staged TUN configuration failed sing-box validation.')"
		return 1
	fi

	_proxy_tun_stage_manifest "$protocol"
}

_proxy_tun_stage_manifest() {
	local protocol="$1"
	local staged_cronet cronet_hash

	staged_cronet=$(tun_staged_path "$(tun_cronet_path)")
	cronet_hash=absent
	if tun_run_privileged test -f "$staged_cronet" >/dev/null 2>&1; then
		cronet_hash=$(tun_privileged_sha256_of "$staged_cronet") || return 1
	fi

	# Provenance only: no credentials and no rendered config values. The config
	# hash is derived from secret-bearing content, so the file stays root-only.
	{
		printf 'schema_version=%s\n' "$(tun_manifest_schema_version)"
		printf 'owner_uid=%s\n' "$(id -u)"
		printf 'protocol=%s\n' "$protocol"
		printf 'generation=%s\n' "$_PROXY_TUN_GENERATION"
		printf 'binary_sha256=%s\n' "$(tun_privileged_sha256_of "$(tun_staged_path "$(tun_binary_path)")")"
		printf 'cronet_sha256=%s\n' "$cronet_hash"
		printf 'config_sha256=%s\n' "$(tun_privileged_sha256_of "$(tun_staged_path "$(tun_config_path)")")"
		printf 'unit_sha256=%s\n' "$(tun_privileged_sha256_of "$(tun_staged_path "$(tun_unit_path)")")"
		printf 'unit_version=%s\n' "$(tun_unit_version)"
		printf 'runtime_uid=%s\n' "$(_proxy_tun_account_uid)"
		printf 'runtime_gid=%s\n' "$(_proxy_tun_account_gid)"
		printf 'runtime_account_created=%s\n' "$_PROXY_TUN_ACCOUNT_CREATED"
	} | _proxy_tun_write_root_file root:root 0600 "$(tun_staged_path "$(tun_manifest_path)")"
}

# The old files stay reachable until verification succeeds, and the manifest is
# written last so its presence means the generation was published in full.
_proxy_tun_save_previous_generation() {
	local rollback_dir destination name

	rollback_dir=$(tun_rollback_dir)
	tun_run_privileged rm -rf "$rollback_dir" >/dev/null 2>&1 || true
	tun_run_privileged install -d -o root -g root -m 0700 "$rollback_dir" \
		>/dev/null 2>&1 || return 1

	while IFS= read -r destination; do
		name="${destination##*/}"
		if tun_run_privileged test -f "$destination" >/dev/null 2>&1; then
			tun_run_privileged cp -p "$destination" "${rollback_dir}/${name}" \
				>/dev/null 2>&1 || return 1
		fi
	done <<EOF
$(_proxy_tun_managed_destinations)
EOF
}

_proxy_tun_publish() {
	local destination staged

	while IFS= read -r destination; do
		staged=$(tun_staged_path "$destination")
		if ! tun_run_privileged test -f "$staged" >/dev/null 2>&1; then
			continue
		fi
		# The manifest is the commit marker, so it goes last.
		if [[ "$destination" == "$(tun_manifest_path)" ]]; then
			continue
		fi
		tun_run_privileged mv -f "$staged" "$destination" >/dev/null 2>&1 || return 1
	done <<EOF
$(_proxy_tun_managed_destinations)
EOF

	tun_run_privileged systemctl daemon-reload >/dev/null 2>&1 || return 1
	tun_run_privileged systemctl reset-failed "$(tun_unit_name)" >/dev/null 2>&1 || true

	staged=$(tun_staged_path "$(tun_manifest_path)")
	tun_run_privileged mv -f "$staged" "$(tun_manifest_path)" >/dev/null 2>&1 || return 1
}

_proxy_tun_restore_previous_generation() {
	local rollback_dir destination name staged restored=false

	rollback_dir=$(tun_rollback_dir)

	while IFS= read -r destination; do
		name="${destination##*/}"
		staged=$(tun_staged_path "$destination")
		tun_run_privileged rm -f "$staged" >/dev/null 2>&1 || true

		if tun_run_privileged test -f "${rollback_dir}/${name}" >/dev/null 2>&1; then
			tun_run_privileged cp -p "${rollback_dir}/${name}" "$staged" >/dev/null 2>&1 || return 1
			tun_run_privileged mv -f "$staged" "$destination" >/dev/null 2>&1 || return 1
			restored=true
		else
			tun_run_privileged rm -f "$destination" >/dev/null 2>&1 || true
		fi
	done <<EOF
$(_proxy_tun_managed_destinations)
EOF

	tun_run_privileged systemctl daemon-reload >/dev/null 2>&1 || true
	tun_run_privileged systemctl reset-failed "$(tun_unit_name)" >/dev/null 2>&1 || true

	if [[ "$restored" == true ]]; then
		return 0
	fi

	return 0
}

# Start and verify ############################################################

_proxy_tun_start() {
	tun_run_privileged systemctl start "$(tun_unit_name)" >/dev/null 2>&1
}

_proxy_tun_wait_stable() {
	local attempt=0 stable=0 pid restarts previous_pid="" previous_restarts=""

	while [[ "$attempt" -lt 20 ]]; do
		if [[ "$(tun_unit_active_state 2>/dev/null)" == "active" ]]; then
			pid=$(tun_unit_main_pid 2>/dev/null)
			restarts=$(tun_unit_restart_count 2>/dev/null)
			if [[ -n "$previous_pid" && "$pid" == "$previous_pid" &&
				"$restarts" == "$previous_restarts" && "$pid" != "0" ]]; then
				stable=$((stable + 1))
				if [[ "$stable" -ge 2 ]]; then
					return 0
				fi
			else
				stable=0
			fi
			previous_pid="$pid"
			previous_restarts="$restarts"
		else
			stable=0
			previous_pid=""
		fi

		attempt=$((attempt + 1))
		sleep 1
	done

	return 1
}

_proxy_tun_interface_tx_packets() {
	local counter
	counter="/sys/class/net/$(tun_interface_name)/statistics/tx_packets"

	if [[ ! -r "$counter" ]]; then
		return 1
	fi

	cat "$counter" 2>/dev/null
}

# Runs with every application proxy variable cleared, so a pass proves the
# tunnel carried the traffic rather than a leftover mixed inbound.
_proxy_tun_probe_egress() {
	local code

	if ! _has curl; then
		return 2
	fi

	code=$(env -u http_proxy -u https_proxy -u ftp_proxy -u all_proxy -u socks_proxy \
		-u HTTP_PROXY -u HTTPS_PROXY -u FTP_PROXY -u ALL_PROXY -u SOCKS_PROXY \
		curl --noproxy '*' --silent --max-time 10 --output /dev/null \
		--write-out '%{http_code}' "$_PROXY_TUN_EGRESS_PROBE_URL" 2>/dev/null) || return 1

	case "$code" in
	200 | 204)
		return 0
		;;
	esac

	return 1
}

_proxy_tun_probe_dns() {
	if ! _has getent; then
		return 2
	fi

	getent ahosts "${SBM_TUN_DNS_PROBE_HOST:-www.gstatic.com}" >/dev/null 2>&1
}

_proxy_tun_verify() {
	local tx_before tx_after probe_status msg

	if ! _proxy_tun_wait_stable; then
		_error "$(_t 'The TUN service did not reach a stable running state.')"
		return 1
	fi

	if ! tun_interface_exists; then
		msg=$(_t 'The expected TUN interface {} did not appear.')
		_error "${msg//\{\}/$(tun_interface_name)}"
		return 1
	fi

	if ! tun_managed_routes_present; then
		_error "$(_t 'No route was installed through the TUN interface.')"
		return 1
	fi

	tx_before=$(_proxy_tun_interface_tx_packets || true)

	_proxy_tun_probe_dns
	probe_status=$?
	if [[ "$probe_status" -eq 1 ]]; then
		_error "$(_t 'DNS resolution failed through the tunnel.')"
		return 1
	fi

	_proxy_tun_probe_egress
	probe_status=$?
	if [[ "$probe_status" -eq 1 ]]; then
		_error "$(_t 'Proxy-free egress failed through the tunnel.')"
		return 1
	fi
	if [[ "$probe_status" -eq 2 ]]; then
		_warning "$(_t 'curl is not installed, so egress could not be verified.')"
		return 0
	fi

	# The counters are what prove the probe traversed the tunnel rather than a
	# working direct route, so a missing counter is reported, not assumed good.
	tx_after=$(_proxy_tun_interface_tx_packets || true)
	if [[ -z "$tx_before" || -z "$tx_after" ]]; then
		_warning "$(_t 'The TUN interface packet counters are unavailable, so throughput was not verified.')"
		return 0
	fi

	if [[ "$tx_after" -le "$tx_before" ]]; then
		_error "$(_t 'The TUN interface counters did not move, so traffic bypassed the tunnel.')"
		return 1
	fi

	return 0
}

# Rollback ####################################################################

# Runs on every error or interruption after the first mutation, not only when
# the service fails to start. The order is mandatory: stop and prove the network
# is clean before putting any files back.
_proxy_tun_rollback() {
	local reason="$1"
	local rollback_failed=false

	_error "$reason"
	_warning "$(_t 'Rolling back the TUN transaction.')"

	tun_run_privileged systemctl disable "$(tun_unit_name)" >/dev/null 2>&1 || true
	tun_run_privileged systemctl stop "$(tun_unit_name)" >/dev/null 2>&1 || true

	if ! _proxy_tun_wait_inactive; then
		_error "$(_t 'The attempted TUN service did not stop during rollback.')"
		rollback_failed=true
	fi

	if tun_network_artifacts_present; then
		_error "$(_t 'TUN interface, route, or rule artifacts survived the rollback.')"
		rollback_failed=true
	fi

	if ! _proxy_tun_restore_previous_generation; then
		_error "$(_t 'Failed to restore the previous TUN generation.')"
		rollback_failed=true
	fi

	if [[ "$_PROXY_TUN_ACCOUNT_CREATED" == true && -z "$_PROXY_TUN_PREVIOUS_GENERATION" ]]; then
		_proxy_tun_remove_account || true
	fi

	if [[ "$_PROXY_TUN_PREVIOUS_ENABLED" == true ]]; then
		tun_run_privileged systemctl enable "$(tun_unit_name)" >/dev/null 2>&1 || true
	fi
	if [[ "$_PROXY_TUN_PREVIOUS_ACTIVE" == true ]]; then
		tun_run_privileged systemctl reset-failed "$(tun_unit_name)" >/dev/null 2>&1 || true
		tun_run_privileged systemctl start "$(tun_unit_name)" >/dev/null 2>&1 || true
	fi

	# Mixed mode may only come back when the previous TUN is not running again.
	if [[ "$_PROXY_TUN_PREVIOUS_ACTIVE" != true ]]; then
		_proxy_tun_restore_mixed_services
		_proxy_tun_restore_owned_proxy_settings
	fi

	if ! _proxy_tun_cancel_safeguard; then
		rollback_failed=true
	fi

	if [[ "$rollback_failed" == true ]]; then
		_proxy_tun_write_status dirty >/dev/null 2>&1 || true
	elif [[ "$_PROXY_TUN_PREVIOUS_ACTIVE" == true ]] && tun_unit_is_active; then
		_proxy_tun_write_status active >/dev/null 2>&1 || true
	else
		_proxy_tun_write_status safe-off >/dev/null 2>&1 || true
	fi

	tun_run_privileged rm -f "$(tun_journal_path)" >/dev/null 2>&1 || true
	tun_run_privileged rm -rf "$(tun_rollback_dir)" >/dev/null 2>&1 || true
	tun_run_privileged rmdir "$(tun_control_dir)" >/dev/null 2>&1 || true
	_proxy_tun_clear_transaction

	if [[ "$rollback_failed" == true ]]; then
		_error "$(_t "Rollback did not complete; run \`proxy check tun\` before using the network.")"
		return 1
	fi

	return 0
}

_proxy_tun_restore_mixed_services() {
	local entry protocol active enabled service_name

	while IFS= read -r entry; do
		if [[ -z "$entry" ]]; then
			continue
		fi
		protocol="${entry%%:*}"
		active="${entry#*:}"
		active="${active%%:*}"
		enabled="${entry##*:}"
		service_name=$(_proxy_service_name_for_protocol "$protocol")

		if [[ "$enabled" == "enabled" ]]; then
			systemctl --user enable "$service_name" >/dev/null 2>&1 || true
		fi
		if [[ "$active" == "active" ]]; then
			systemctl --user start "$service_name" >/dev/null 2>&1 || true
		fi
	done < <(_proxy_tun_mixed_snapshot_entries)
}

# The snapshot is one space-separated journal line; callers need it one entry
# per line so neither bash nor zsh has to word-split it.
_proxy_tun_mixed_snapshot_entries() {
	if [[ -z "$_PROXY_TUN_MIXED_SNAPSHOT" ]]; then
		return 0
	fi

	printf '%s\n' "$_PROXY_TUN_MIXED_SNAPSHOT" | tr ' ' '\n'
}

# Interrupted-transaction recovery ############################################

# A journal that outlived its process -- a killed shell, or a reboot -- means an
# unverified generation may be on disk. Put the previous one back before doing
# any new work.
_proxy_tun_recover_interrupted() {
	local owner_uid phase

	if ! _proxy_tun_journal_present; then
		return 0
	fi

	owner_uid=$(_proxy_tun_journal_value owner_uid 2>/dev/null)
	if [[ -n "$owner_uid" && "$owner_uid" != "$(id -u)" ]]; then
		_proxy_tun_owner_mismatch_error "$owner_uid"
		return 1
	fi

	phase=$(_proxy_tun_journal_value phase 2>/dev/null)
	_warning "$(_t 'Recovering an interrupted TUN transaction.')"

	_PROXY_TUN_PREVIOUS_GENERATION=$(_proxy_tun_journal_value previous_generation 2>/dev/null)
	if [[ "$_PROXY_TUN_PREVIOUS_GENERATION" == "none" ]]; then
		_PROXY_TUN_PREVIOUS_GENERATION=""
	fi
	_PROXY_TUN_PREVIOUS_ACTIVE=$(_proxy_tun_journal_value previous_tun_active 2>/dev/null)
	_PROXY_TUN_PREVIOUS_ENABLED=$(_proxy_tun_journal_value previous_tun_enabled 2>/dev/null)
	_PROXY_TUN_MIXED_SNAPSHOT=$(_proxy_tun_journal_value mixed_services 2>/dev/null)
	_PROXY_TUN_ACCOUNT_CREATED=$(_proxy_tun_journal_value account_created 2>/dev/null)
	_PROXY_TUN_SAFEGUARD_ARMED=true

	local msg
	msg=$(_t 'Interrupted at phase {}.')
	_info "${msg//\{\}/${phase:-unknown}}"

	_proxy_tun_rollback "$(_t 'A previous TUN transaction did not finish.')"
}

# Commands ####################################################################

_proxy_tun_transaction() {
	local protocol="$1"
	local persist="$2"

	_PROXY_TUN_PROTOCOL="$protocol"
	_PROXY_TUN_PERSIST="$persist"
	_PROXY_TUN_GENERATION=$(_proxy_tun_new_generation)

	if ! _proxy_tun_recover_interrupted; then
		return 1
	fi

	if ! _proxy_tun_preflight_locked; then
		return 1
	fi

	_proxy_tun_snapshot_services
	_proxy_tun_snapshot_proxy_settings

	# First durable record, written before the first mutation.
	_PROXY_TUN_PHASE=snapshot
	if ! _proxy_tun_journal_record || ! _proxy_tun_mark_transaction; then
		_error "$(_t 'Failed to record the TUN transaction journal.')"
		return 1
	fi
	_proxy_tun_write_status dirty >/dev/null 2>&1 || true

	# The account comes first: /etc/sing-box-manager/tun is group-owned by it,
	# so creating that directory before the group exists fails outright.
	if ! _proxy_tun_account_exists; then
		if ! _proxy_tun_create_account; then
			_proxy_tun_rollback "$(_t 'Failed to create the TUN service account.')"
			return 1
		fi
		_PROXY_TUN_ACCOUNT_CREATED=true
	fi
	_proxy_tun_set_phase account

	if ! _proxy_tun_prepare_directories; then
		_proxy_tun_rollback "$(_t 'Failed to create the managed TUN directories.')"
		return 1
	fi

	if ! _proxy_tun_capability_probe; then
		_proxy_tun_rollback "$(_t 'The kernel refused to create a TUN device under the service sandbox.')"
		return 1
	fi

	if ! _proxy_tun_stage_payload "$protocol"; then
		# shellcheck disable=SC2046
		_proxy_tun_discard_staged $(_proxy_tun_managed_destinations)
		_proxy_tun_rollback "$(_t 'Failed to stage and validate the TUN payload.')"
		return 1
	fi
	_proxy_tun_set_phase staged

	if ! _proxy_tun_arm_safeguard; then
		_proxy_tun_rollback "$(_t 'Failed to arm the TUN safeguard.')"
		return 1
	fi

	if ! _proxy_tun_quiesce; then
		_proxy_tun_rollback "$(_t 'Failed to stop the existing proxy services.')"
		return 1
	fi
	_proxy_tun_set_phase quiesced

	if ! _proxy_tun_save_previous_generation; then
		_proxy_tun_rollback "$(_t 'Failed to save the previous TUN generation.')"
		return 1
	fi

	if ! _proxy_tun_publish; then
		_proxy_tun_rollback "$(_t 'Failed to publish the staged TUN generation.')"
		return 1
	fi
	_proxy_tun_set_phase published

	if _proxy_tun_mixed_service_active; then
		_proxy_tun_rollback "$(_t 'A mixed proxy service started again before the tunnel; refusing to overlap.')"
		return 1
	fi

	if ! _proxy_tun_start; then
		_proxy_tun_rollback "$(_t 'Failed to start the TUN service.')"
		return 1
	fi
	_proxy_tun_set_phase started

	if ! _proxy_tun_verify; then
		_proxy_tun_rollback "$(_t 'The TUN service started but did not carry traffic.')"
		return 1
	fi
	_proxy_tun_set_phase verified

	# Persistence is granted only after verification, and an already persistent
	# installation keeps its state across a re-sync.
	if [[ "$persist" == true || "$_PROXY_TUN_PREVIOUS_ENABLED" == true ]]; then
		if ! tun_run_privileged systemctl enable "$(tun_unit_name)" >/dev/null 2>&1; then
			_warning "$(_t 'The tunnel is running but could not be enabled at boot.')"
		fi
	fi

	if ! _proxy_tun_cancel_safeguard; then
		_proxy_tun_write_status dirty >/dev/null 2>&1 || true
		_error "$(_t 'The tunnel is running but its safeguard could not be cancelled.')"
		return 1
	fi

	_proxy_tun_write_status active >/dev/null 2>&1 || true
	tun_run_privileged rm -f "$(tun_journal_path)" >/dev/null 2>&1 || true
	tun_run_privileged rm -rf "$(tun_rollback_dir)" >/dev/null 2>&1 || true
	_proxy_tun_clear_transaction

	return 0
}

_proxy_tun_mixed_service_active() {
	local protocol service_name

	for protocol in "${SUPPORTED_PROTOCOLS[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		if _proxy_user_service_is_active "$service_name"; then
			return 0
		fi
	done

	return 1
}

_proxy_tun_on() {
	local persist=false
	local protocol transaction_status msg

	while [[ $# -gt 0 ]]; do
		case "$1" in
		--persist)
			persist=true
			;;
		*)
			_proxy_unknown_argument "tun on $1"
			return 1
			;;
		esac
		shift
	done

	_proxy_tun_reset_transaction_state

	protocol=$(_proxy_preferred_protocol || true)
	if ! _proxy_tun_preflight_user "$protocol"; then
		return 1
	fi

	if ! _proxy_tun_authenticate_sudo; then
		return 1
	fi

	if ! tun_acquire_lock; then
		_error "$(_t 'Another TUN operation holds the transaction lock.')"
		return 1
	fi

	_proxy_tun_transaction "$protocol" "$persist"
	transaction_status=$?
	tun_release_lock

	if [[ "$transaction_status" -ne 0 ]]; then
		return "$transaction_status"
	fi

	msg=$(_t 'Machine-wide TUN mode is active with protocol {}.')
	success "${msg//\{\}/$protocol}"
	if [[ "$persist" == true ]]; then
		hint "$(_t 'The tunnel is enabled at boot.')"
	fi

	return 0
}

# A toggle, not an uninstall. The managed files stay so the next `proxy tun on`
# can re-sync by hash instead of re-provisioning from scratch.
_proxy_tun_off() {
	local owner_uid off_status=0

	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi

	if ! tun_unit_installed && ! tun_transaction_in_progress; then
		_warning "$(_t 'Machine-wide TUN mode is not provisioned.')"
		return 0
	fi

	if ! _proxy_tun_authenticate_sudo; then
		return 1
	fi

	if ! tun_acquire_lock; then
		_error "$(_t 'Another TUN operation holds the transaction lock.')"
		return 1
	fi

	if ! _proxy_tun_recover_interrupted; then
		tun_release_lock
		return 1
	fi

	if tun_manifest_exists; then
		owner_uid=$(tun_manifest_value owner_uid 2>/dev/null)
		if [[ -n "$owner_uid" && "$owner_uid" != "$(id -u)" ]]; then
			_proxy_tun_owner_mismatch_error "$owner_uid"
			tun_release_lock
			return 1
		fi
	fi

	_proxy_tun_write_status dirty >/dev/null 2>&1 || true
	tun_run_privileged systemctl disable "$(tun_unit_name)" >/dev/null 2>&1 || true
	tun_run_privileged systemctl stop "$(tun_unit_name)" >/dev/null 2>&1 || true

	if ! _proxy_tun_wait_inactive; then
		_error "$(_t 'The TUN service did not stop.')"
		off_status=1
	elif tun_network_artifacts_present; then
		_error "$(_t 'TUN interface, route, or rule artifacts are still present.')"
		off_status=1
	fi

	if [[ "$off_status" -eq 0 ]]; then
		_proxy_tun_write_status safe-off >/dev/null 2>&1 || true
	fi

	tun_release_lock

	if [[ "$off_status" -ne 0 ]]; then
		return "$off_status"
	fi

	success "$(_t 'Machine-wide TUN mode is off.')"
	_info "$(_t 'Mixed mode was not restored.')"
	hint "$(_t "Run \`proxy on\` to return to the local mixed inbound.")"
	return 0
}

# Protocol switching reuses the same staged transaction, so the previous
# generation comes back intact when the target fails to verify.
_proxy_tun_switch_protocol() {
	local protocol="$1"
	local owner_uid transaction_status msg

	_proxy_tun_reset_transaction_state

	if ! _proxy_tun_preflight_user "$protocol"; then
		return 1
	fi

	if ! _proxy_tun_authenticate_sudo; then
		return 1
	fi

	if ! tun_acquire_lock; then
		_error "$(_t 'Another TUN operation holds the transaction lock.')"
		return 1
	fi

	if tun_manifest_exists; then
		owner_uid=$(tun_manifest_value owner_uid 2>/dev/null)
		if [[ -n "$owner_uid" && "$owner_uid" != "$(id -u)" ]]; then
			_proxy_tun_owner_mismatch_error "$owner_uid"
			tun_release_lock
			return 1
		fi
	fi

	# Never turns a non-persistent tunnel persistent by accident.
	_proxy_tun_transaction "$protocol" false
	transaction_status=$?
	tun_release_lock

	if [[ "$transaction_status" -ne 0 ]]; then
		return "$transaction_status"
	fi

	if ! _proxy_save_selected_protocol "$protocol"; then
		_error "$(_t 'Failed to save selected proxy protocol.')"
		return 1
	fi

	msg=$(_t 'Proxy protocol switched to {}.')
	success "${msg//\{\}/$protocol}"
	return 0
}

# Diagnostics #################################################################

_proxy_check_tun() {
	local show_section="${1:-false}"
	local runtime protocol config_path status_value enabled_state manifest_state
	local owner_uid manifest_protocol manifest_generation auto_redirect

	if [[ "$show_section" == true ]]; then
		_proxy_report_section 'TUN'
	fi

	runtime=$(_proxy_runtime_kind)
	_proxy_report_row 'Runtime' "$(_t "$runtime")"
	_proxy_report_row 'Supported' "$(_proxy_bool_label "$([[ "$runtime" == "linux" ]] && printf 'true\n' || printf 'false\n')")"
	_proxy_report_row 'systemd' "$(_proxy_bool_label "$(tun_systemd_available && printf 'true\n' || printf 'false\n')")"
	_proxy_report_row '/dev/net/tun' "$(_proxy_bool_label "$(_proxy_tun_dev_tun_present && printf 'true\n' || printf 'false\n')")"

	if tun_foreign_interface_conflict || tun_address_conflict; then
		_proxy_report_row 'Interface' "$(_t 'conflict')"
	else
		_proxy_report_row 'Interface' "$(tun_interface_name)"
	fi

	_proxy_report_row 'Unit' "$(tun_unit_name)"
	if tun_unit_installed; then
		_proxy_report_row 'Installed' "$(_proxy_bool_label true)"
		_proxy_report_row 'Status' "$(_proxy_format_service_state "$(tun_unit_active_state)")"
		_proxy_report_row 'Substate' "$(tun_unit_sub_state)"
		enabled_state=$(tun_unit_enabled_state)
		case "$enabled_state" in
		enabled)
			_proxy_report_row 'Start at boot' "$(_proxy_bool_label true)"
			;;
		disabled)
			_proxy_report_row 'Start at boot' "$(_proxy_bool_label false)"
			;;
		*)
			# masked, static, and friends are worth showing verbatim.
			_proxy_report_row 'Start at boot' "${enabled_state:-$(_t 'unknown')}"
			;;
		esac
		_proxy_report_row 'MainPID' "$(tun_unit_main_pid)"
		_proxy_report_row 'Restarts' "$(tun_unit_restart_count)"
	else
		_proxy_report_row 'Installed' "$(_proxy_bool_label false)"
	fi

	status_value=$(tun_status)
	_proxy_report_row 'Attestation' "${status_value:-$(_t 'none')}"
	if tun_transaction_in_progress; then
		_proxy_report_row 'Transaction' "$(_t 'in progress')"
	else
		_proxy_report_row 'Transaction' "$(_t 'none')"
	fi
	_proxy_report_row 'Safely off' "$(_proxy_bool_label "$(tun_is_safely_off && printf 'true\n' || printf 'false\n')")"

	# Root-only fields are shown only when sudo is already unlocked. A report
	# must never prompt for a password just to print provenance.
	if tun_can_sudo_noninteractive; then
		owner_uid=$(tun_manifest_value_quiet owner_uid 2>/dev/null)
		manifest_protocol=$(tun_manifest_value_quiet protocol 2>/dev/null)
		manifest_generation=$(tun_manifest_value_quiet generation 2>/dev/null)
		if [[ -n "$owner_uid" ]]; then
			_proxy_report_row 'Owner uid' "$owner_uid"
			_proxy_report_row 'Active protocol' "${manifest_protocol:-$(_t 'unknown')}"
			_proxy_report_row 'Generation' "${manifest_generation:-$(_t 'unknown')}"
			manifest_state=$(_proxy_tun_manifest_hash_state)
			_proxy_report_row 'Managed files' "$manifest_state"
			if [[ "$owner_uid" == "$(id -u)" ]]; then
				_proxy_report_row 'Mutable by you' "$(_proxy_bool_label true)"
			else
				_proxy_report_row 'Mutable by you' "$(_proxy_bool_label false)"
			fi
		else
			_proxy_report_row 'Manifest' "$(_t 'none')"
		fi
	else
		_proxy_report_row 'Manifest' "$(_t 'not read; sudo would prompt')"
	fi

	# The manifest above is authoritative for what is running; this is what the
	# next activation would use.
	protocol=$(_proxy_preferred_protocol || true)
	if [[ -n "$protocol" ]]; then
		_proxy_report_row 'Selected protocol' "$protocol"
		if config_path=$(_proxy_tun_user_config_path "$protocol"); then
			_proxy_report_row 'Source config' "$(display_path "$config_path")"
		else
			_proxy_report_row 'Source config' "$(_t 'none')"
		fi
	fi
	_proxy_report_row 'Provisioned config' "$(tun_config_path)"

	auto_redirect=$(_proxy_tun_profile_auto_redirect)
	case "$auto_redirect" in
	true)
		_proxy_report_row 'Profile auto_redirect' "$(_proxy_format_toggle_state on)"
		# nftables becomes a hard requirement only in this case.
		_proxy_report_row 'nftables' \
			"$(_proxy_bool_label "$(_has nft && printf 'true\n' || printf 'false\n')")"
		;;
	false)
		_proxy_report_row 'Profile auto_redirect' "$(_proxy_format_toggle_state off)"
		;;
	*)
		_proxy_report_row 'Profile auto_redirect' "$(_t 'unknown')"
		;;
	esac

	# `proxy tun on` deliberately does not enable at boot, so a bare "no" above
	# looks like something went wrong. Say what it means and how to change it.
	if tun_unit_installed && tun_unit_is_active && ! tun_unit_is_enabled; then
		hint "$(_t "The tunnel is running but will not start after a reboot; run \`proxy tun on --persist\` to keep it.")"
	fi

	warning "$(_t 'Shell variables in other running processes and repository-local Git settings cannot be enumerated.')"
}

# `true`, `false`, or `unknown`, read from the profile that is staged rather
# than assumed from the shipped default.
_proxy_tun_profile_auto_redirect() {
	local protocol config_path value

	protocol=$(_proxy_preferred_protocol || true)
	if [[ -z "$protocol" ]] || ! config_path=$(_proxy_tun_user_config_path "$protocol"); then
		printf 'unknown\n'
		return 0
	fi

	if _has jq; then
		value=$(jq -r 'first(.inbounds[]? | select(.type == "tun") | .auto_redirect) // false' \
			"$config_path" 2>/dev/null)
	elif grep -q '"auto_redirect"[[:space:]]*:[[:space:]]*true' "$config_path" 2>/dev/null; then
		value=true
	else
		value=false
	fi

	case "$value" in
	true | false)
		printf '%s\n' "$value"
		;;
	*)
		printf 'unknown\n'
		;;
	esac
}

# Compares recorded hashes, not mtimes: a file replaced with identical content
# is unchanged, and one touched but not edited is not a reason to refuse.
_proxy_tun_manifest_hash_state() {
	local mismatched=0

	_proxy_tun_hash_matches binary_sha256 "$(tun_binary_path)" || mismatched=$((mismatched + 1))
	_proxy_tun_hash_matches config_sha256 "$(tun_config_path)" || mismatched=$((mismatched + 1))
	_proxy_tun_hash_matches unit_sha256 "$(tun_unit_path)" || mismatched=$((mismatched + 1))

	if [[ "$mismatched" -eq 0 ]]; then
		_t 'match the manifest'
	else
		_t 'do not match the manifest'
	fi
}

_proxy_tun_hash_matches() {
	local key="$1"
	local file_path="$2"
	local recorded actual

	recorded=$(tun_manifest_value_quiet "$key" 2>/dev/null) || return 1
	if [[ -z "$recorded" || "$recorded" == "absent" ]]; then
		return 0
	fi

	actual=$(tun_privileged_sha256_of "$file_path" 2>/dev/null) || return 1
	[[ "$recorded" == "$actual" ]]
}

_proxy_dispatch_tun() {
	local action="${1:-}"

	if [[ $# -gt 0 ]]; then
		shift
	fi

	case "$action" in
	on)
		_proxy_tun_on "$@"
		;;
	off)
		_proxy_tun_off "$@"
		;;
	*)
		_proxy_unknown_argument "tun${action:+ ${action}}"
		return 1
		;;
	esac
}

_proxy_apply_shell_proxy() {
	local proxy_host="$1"
	local proxy_port="$2"

	_debug "$(_t 'Set shell proxy environment variables.')"
	_set_proxy_env_vars "$proxy_host" "$proxy_port"
	return 0
}

_proxy_clear_shell_proxy() {
	_debug "$(_t 'Unset shell proxy environment variables.')"
	_unset_proxy_env_vars
	return 0
}

_proxy_parse_git_flags() {
	local allow_force="${1:-false}"
	shift

	_PROXY_FORCE_FLAG=false
	_PROXY_GIT_SCOPE="global"

	while [[ $# -gt 0 ]]; do
		case "$1" in
		--local)
			_PROXY_GIT_SCOPE="local"
			;;
		-f | --force)
			if [[ "$allow_force" != true ]]; then
				_proxy_unknown_argument "$1"
				return 1
			fi
			_PROXY_FORCE_FLAG=true
			;;
		*)
			_proxy_unknown_argument "$1"
			return 1
			;;
		esac
		shift
	done
}

_proxy_require_git_scope() {
	local scope="${1:-global}"

	if [[ "$scope" == "local" ]] && ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
		_error "$(_t 'Git local proxy settings require a Git repository.')"
		return 1
	fi

	return 0
}

_proxy_apply_git_proxy() {
	local scope="$1"
	local proxy_host="$2"
	local proxy_port="$3"

	if ! _has git; then
		_warning "$(_t 'Git is not installed or not available.')"
		return 0
	fi

	if ! _proxy_require_git_scope "$scope"; then
		return 1
	fi

	_debug "$(_t 'Set git network proxy.')"

	case "$scope" in
	local)
		git config --local http.proxy "http://${proxy_host}:${proxy_port}" || return 1
		git config --local https.proxy "http://${proxy_host}:${proxy_port}" || return 1
		;;
	*)
		git config --global http.proxy "http://${proxy_host}:${proxy_port}" || return 1
		git config --global https.proxy "http://${proxy_host}:${proxy_port}" || return 1
		;;
	esac

	return 0
}

_proxy_clear_git_proxy() {
	local scope="$1"

	if ! _has git; then
		_warning "$(_t 'Git is not installed or not available.')"
		return 0
	fi

	if ! _proxy_require_git_scope "$scope"; then
		return 1
	fi

	_debug "$(_t 'Unset git network proxy.')"

	case "$scope" in
	local)
		git config --local --unset-all http.proxy >/dev/null 2>&1 || true
		git config --local --unset-all https.proxy >/dev/null 2>&1 || true
		;;
	*)
		git config --global --unset-all http.proxy >/dev/null 2>&1 || true
		git config --global --unset-all https.proxy >/dev/null 2>&1 || true
		;;
	esac

	return 0
}

# Guard shared by every macOS system-proxy entry point.
_proxy_macos_resolve_network_service() {
	local network_service

	# Callers capture stdout, so warnings go to stderr or the user never sees them.
	if ! _has networksetup; then
		_warning "$(_t 'networksetup is not available. Skipping system proxy settings.')" >&2
		return 1
	fi

	if ! network_service=$(macos_proxy_network_service); then
		_warning "$(_t 'Could not determine the active network service. Skipping system proxy settings.')" >&2
		return 1
	fi

	printf '%s\n' "$network_service"
}

_proxy_macos_apply_system_proxy() {
	local network_service="$1"
	local proxy_host="$2"
	local proxy_port="$3"
	local bypass_domains

	macos_proxy_run_with_sudo networksetup -setwebproxy "$network_service" "$proxy_host" "$proxy_port" || return 1
	macos_proxy_run_with_sudo networksetup -setsecurewebproxy "$network_service" "$proxy_host" "$proxy_port" || return 1
	macos_proxy_run_with_sudo networksetup -setsocksfirewallproxy "$network_service" "$proxy_host" "$proxy_port" || return 1

	# networksetup takes the bypass list as separate arguments, so the split here
	# is deliberate.
	bypass_domains="${DEFAULT_NO_PROXY//,/ }"
	# shellcheck disable=SC2086
	macos_proxy_run_with_sudo networksetup -setproxybypassdomains "$network_service" $bypass_domains || return 1
	return 0
}

_proxy_apply_desktop_proxy() {
	local proxy_host="$1"
	local proxy_port="$2"
	local protocol formatted_no_proxy network_service

	if plat_is_macos; then
		if ! network_service=$(_proxy_macos_resolve_network_service); then
			return 0
		fi

		_debug "$(_t 'Set macOS system proxy settings.')"
		hint "$(_t 'Changing the macOS system proxy requires administrator privileges.')"
		if ! _proxy_macos_apply_system_proxy "$network_service" "$proxy_host" "$proxy_port"; then
			_error "$(_t 'Failed to change the macOS system proxy settings.')"
			return 1
		fi
		return 0
	fi

	if ! _proxy_is_gnome_desktop; then
		_warning "$(_t 'GNOME desktop environment not detected. Skipping desktop proxy settings.')"
		return 0
	fi

	if ! _has dconf; then
		_warning "$(_t 'dconf is not available. Skipping desktop proxy settings.')"
		return 0
	fi

	_debug "$(_t 'Set GNOME desktop proxy settings.')"
	dconf write /system/proxy/mode "'manual'" || return 1
	for protocol in http https ftp socks; do
		dconf write "/system/proxy/${protocol}/host" "'${proxy_host}'" || return 1
		dconf write "/system/proxy/${protocol}/port" "${proxy_port}" || return 1
	done

	formatted_no_proxy="${DEFAULT_NO_PROXY//,/','}"
	formatted_no_proxy="['${formatted_no_proxy}']"
	dconf write /system/proxy/ignore-hosts "${formatted_no_proxy}" || return 1
	return 0
}

_proxy_clear_desktop_proxy() {
	local network_service

	if plat_is_macos; then
		if ! network_service=$(_proxy_macos_resolve_network_service); then
			return 0
		fi

		_debug "$(_t 'Unset macOS system proxy settings.')"
		hint "$(_t 'Changing the macOS system proxy requires administrator privileges.')"
		if ! macos_proxy_clear "$network_service"; then
			_error "$(_t 'Failed to change the macOS system proxy settings.')"
			return 1
		fi
		return 0
	fi

	if ! _proxy_is_gnome_desktop; then
		_warning "$(_t 'GNOME desktop environment not detected. Skipping desktop proxy settings.')"
		return 0
	fi

	if ! _has dconf; then
		_warning "$(_t 'dconf is not available. Skipping desktop proxy settings.')"
		return 0
	fi

	_debug "$(_t 'Unset GNOME desktop proxy settings.')"
	dconf write /system/proxy/mode "'none'" || return 1
	return 0
}

_proxy_apply_docker_client_proxy() {
	local proxy_host="$1"
	local proxy_port="$2"
	local docker_config_dir docker_config_file tmp_file

	docker_config_dir=$(_proxy_docker_config_dir)
	docker_config_file=$(_proxy_docker_config_file)

	if ! _proxy_require_jq; then
		return 1
	fi

	_debug "$(_t 'Set Docker client proxy settings.')"

	if ! mkdir -p "$docker_config_dir"; then
		_error "$(_t 'Failed to update Docker client proxy settings.')"
		return 1
	fi

	if ! tmp_file=$(mktemp "${docker_config_dir}/config.json.tmp.XXXXXX"); then
		_error "$(_t 'Failed to update Docker client proxy settings.')"
		return 1
	fi

	if [[ -f "$docker_config_file" ]]; then
		if ! jq \
			--arg http_proxy "http://${proxy_host}:${proxy_port}" \
			--arg https_proxy "http://${proxy_host}:${proxy_port}" \
			--arg ftp_proxy "ftp://${proxy_host}:${proxy_port}" \
			--arg no_proxy "$DEFAULT_NO_PROXY" \
			'.proxies = (.proxies // {}) | .proxies.default = ((.proxies.default // {}) + {httpProxy: $http_proxy, httpsProxy: $https_proxy, ftpProxy: $ftp_proxy, noProxy: $no_proxy})' \
			"$docker_config_file" >"$tmp_file"; then
			rm -f "$tmp_file"
			_error "$(_t 'Docker client config is not valid JSON.')"
			return 1
		fi
	else
		if ! jq -n \
			--arg http_proxy "http://${proxy_host}:${proxy_port}" \
			--arg https_proxy "http://${proxy_host}:${proxy_port}" \
			--arg ftp_proxy "ftp://${proxy_host}:${proxy_port}" \
			--arg no_proxy "$DEFAULT_NO_PROXY" \
			'{proxies: {default: {httpProxy: $http_proxy, httpsProxy: $https_proxy, ftpProxy: $ftp_proxy, noProxy: $no_proxy}}}' >"$tmp_file"; then
			rm -f "$tmp_file"
			_error "$(_t 'Failed to update Docker client proxy settings.')"
			return 1
		fi
	fi

	if ! mv "$tmp_file" "$docker_config_file"; then
		rm -f "$tmp_file"
		_error "$(_t 'Failed to update Docker client proxy settings.')"
		return 1
	fi

	return 0
}

_proxy_clear_docker_client_proxy() {
	local docker_config_dir docker_config_file tmp_file

	docker_config_dir=$(_proxy_docker_config_dir)
	docker_config_file=$(_proxy_docker_config_file)

	if [[ ! -f "$docker_config_file" ]]; then
		return 0
	fi

	if ! _proxy_require_jq; then
		return 1
	fi

	_debug "$(_t 'Unset Docker client proxy settings.')"

	if ! tmp_file=$(mktemp "${docker_config_dir}/config.json.tmp.XXXXXX"); then
		_error "$(_t 'Failed to clear Docker client proxy settings.')"
		return 1
	fi

	if ! jq 'del(.proxies.default.httpProxy, .proxies.default.httpsProxy, .proxies.default.ftpProxy, .proxies.default.noProxy) | if (.proxies.default // {}) == {} then del(.proxies.default) else . end | if (.proxies // {}) == {} then del(.proxies) else . end' "$docker_config_file" >"$tmp_file"; then
		rm -f "$tmp_file"
		_error "$(_t 'Docker client config is not valid JSON.')"
		return 1
	fi

	if ! mv "$tmp_file" "$docker_config_file"; then
		rm -f "$tmp_file"
		_error "$(_t 'Failed to clear Docker client proxy settings.')"
		return 1
	fi

	return 0
}

_proxy_set_docker() {
	if ! _proxy_tun_guard_mixed 'proxy docker on'; then
		return 1
	fi

	local force
	local proxy_host proxy_port _proxy_config

	if ! _parse_proxy_force_flag false "$@"; then
		return 1
	fi
	force="$_PROXY_FORCE_FLAG"

	if ! _proxy_config=$(_proxy_prepare_proxy_config "$force"); then
		return 1
	fi
	read -r proxy_host proxy_port <<<"$_proxy_config"

	if ! command -v docker >/dev/null 2>&1; then
		_warning "$(_t 'Docker is not installed or not available.')"
		return 0
	fi

	if ! _proxy_apply_docker_client_proxy "$proxy_host" "$proxy_port"; then
		return 1
	fi

	# Docker Desktop runs the daemon inside its own VM, so there is no systemd
	# drop-in to write.
	if plat_is_macos; then
		_warning "$(_t 'Docker Desktop manages the daemon proxy in its own settings. Only the Docker client config was updated.')"
		return 0
	fi

	_proxy_apply_docker_daemon_proxy "$proxy_host" "$proxy_port"
}

# Split out from `proxy docker on` so the TUN rollback can put the drop-in back
# without going through that command's active-service check.
_proxy_apply_docker_daemon_proxy() {
	local proxy_host="$1"
	local proxy_port="$2"
	local docker_service_dir proxy_conf

	_debug "$(_t 'Set Docker daemon proxy settings.')"

	docker_service_dir=$(_proxy_docker_daemon_service_dir)
	if [[ ! -d "$docker_service_dir" ]]; then
		sudo mkdir -p "$docker_service_dir"
	fi

	proxy_conf=$(_proxy_docker_daemon_proxy_file)
	sudo tee "$proxy_conf" >/dev/null <<EOF
[Service]
Environment="http_proxy=http://${proxy_host}:${proxy_port}"
Environment="https_proxy=http://${proxy_host}:${proxy_port}"
Environment="no_proxy=${DEFAULT_NO_PROXY}"
Environment="HTTP_PROXY=http://${proxy_host}:${proxy_port}"
Environment="HTTPS_PROXY=http://${proxy_host}:${proxy_port}"
Environment="NO_PROXY=${DEFAULT_NO_PROXY}"
EOF

	sudo systemctl daemon-reload
	if sudo systemctl restart docker; then
		_debug "$(_t 'Docker daemon restarted successfully.')"
	else
		_warning "$(_t 'Failed to restart Docker daemon.')"
		return 1
	fi
}

_proxy_unset_docker() {
	local proxy_conf

	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi

	if ! command -v docker >/dev/null 2>&1; then
		return 0
	fi

	if ! _proxy_clear_docker_client_proxy; then
		return 1
	fi

	if plat_is_macos; then
		_warning "$(_t 'Docker Desktop manages the daemon proxy in its own settings. Only the Docker client config was updated.')"
		return 0
	fi

	_debug "$(_t 'Unset Docker daemon proxy settings.')"

	proxy_conf=$(_proxy_docker_daemon_proxy_file)
	if [[ -f "$proxy_conf" ]]; then
		sudo rm -f "$proxy_conf"

		sudo systemctl daemon-reload
		if sudo systemctl restart docker; then
			_debug "$(_t 'Docker daemon restarted successfully.')"
		else
			_warning "$(_t 'Failed to restart Docker daemon.')"
			return 1
		fi
	fi

	_proxy_tun_note_still_active
}

_proxy_set_shell() {
	if ! _proxy_tun_guard_mixed 'proxy shell|env on'; then
		return 1
	fi

	local force
	local proxy_host proxy_port _proxy_config

	if ! _parse_proxy_force_flag false "$@"; then
		return 1
	fi
	force="$_PROXY_FORCE_FLAG"

	if ! _proxy_config=$(_proxy_prepare_proxy_config "$force"); then
		return 1
	fi
	read -r proxy_host proxy_port <<<"$_proxy_config"

	_proxy_apply_shell_proxy "$proxy_host" "$proxy_port"
}

_proxy_unset_shell() {
	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi

	_proxy_clear_shell_proxy
	_proxy_tun_note_still_active
	return 0
}

_proxy_set_git() {
	if ! _proxy_tun_guard_mixed 'proxy git on'; then
		return 1
	fi

	local force
	local proxy_host proxy_port _proxy_config

	if ! _proxy_parse_git_flags true "$@"; then
		return 1
	fi
	force="$_PROXY_FORCE_FLAG"

	if ! _proxy_config=$(_proxy_prepare_proxy_config "$force"); then
		return 1
	fi
	read -r proxy_host proxy_port <<<"$_proxy_config"

	_proxy_apply_git_proxy "$_PROXY_GIT_SCOPE" "$proxy_host" "$proxy_port"
}

_proxy_unset_git() {
	if ! _proxy_parse_git_flags false "$@"; then
		return 1
	fi

	_proxy_clear_git_proxy "$_PROXY_GIT_SCOPE" || return 1
	_proxy_tun_note_still_active
}

_proxy_set_desktop() {
	if ! _proxy_tun_guard_mixed 'proxy desktop on'; then
		return 1
	fi

	local force
	local proxy_host proxy_port _proxy_config

	if ! _parse_proxy_force_flag false "$@"; then
		return 1
	fi
	force="$_PROXY_FORCE_FLAG"

	if ! plat_is_macos; then
		if ! _proxy_is_gnome_desktop; then
			_warning "$(_t 'GNOME desktop environment not detected. Skipping desktop proxy settings.')"
			return 0
		fi

		if ! _has dconf; then
			_warning "$(_t 'dconf is not available. Skipping desktop proxy settings.')"
			return 0
		fi
	fi

	if ! _proxy_config=$(_proxy_prepare_proxy_config "$force"); then
		return 1
	fi
	read -r proxy_host proxy_port <<<"$_proxy_config"

	_proxy_apply_desktop_proxy "$proxy_host" "$proxy_port"
}

_proxy_unset_desktop() {
	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi

	_proxy_clear_desktop_proxy || return 1
	_proxy_tun_note_still_active
}

_proxy_set_global() {
	if ! _proxy_tun_guard_mixed 'proxy on'; then
		return 1
	fi

	local force port wanted current_port
	local proxy_host proxy_port _proxy_config

	if ! _parse_proxy_force_flag true "$@"; then
		return 1
	fi
	force="$_PROXY_FORCE_FLAG"

	current_port=$(_proxy_read_config_port)

	if [[ -n "$_PROXY_PORT_OVERRIDE" ]]; then
		port="$_PROXY_PORT_OVERRIDE"
		# Pinning the port our own client already serves is a no-op, not a
		# collision with another program.
		if ! _proxy_client_is_serving "$port" && ! _proxy_port_is_available "$port"; then
			local msg
			if [[ "$_PROXY_PORT_CHECK_ERROR" == unavailable ]]; then
				msg=$(_t 'Could not verify that port {} is free for both TCP and UDP.')
				_error "${msg//\{\}/$port}"
				return 1
			fi
			msg=$(_t 'Port {} is already in use.')
			_error "${msg//\{\}/$port}"
			return 1
		fi
	else
		# The marker is the durable choice; the configs are installer-owned and
		# an upgrade resets them to the default. Fall back to what is on disk,
		# and only start hunting from the default when neither says anything.
		wanted=$(_proxy_read_selected_port)
		if [[ -z "$wanted" ]]; then
			wanted="$current_port"
			if ! _proxy_client_is_serving "$wanted"; then
				wanted="$DEFAULT_PROXY_PORT"
			fi
		fi

		port="$wanted"
		if ! _proxy_client_is_serving "$port" && ! _proxy_port_is_available "$port"; then
			if ! port=$(_proxy_find_free_port "$wanted"); then
				return 1
			fi
			local msg
			msg=$(_t 'Port {} is occupied; using port {} instead.')
			msg="${msg/\{\}/$wanted}"
			_info "${msg//\{\}/$port}"
		fi
	fi

	if ! _proxy_apply_selected_port "$port" true; then
		return 1
	fi

	if ! _proxy_enable_services "$force" true; then
		return 1
	fi

	if ! _proxy_config=$(_proxy_prepare_proxy_config "$force" "$port"); then
		return 1
	fi
	read -r proxy_host proxy_port <<<"$_proxy_config"

	_proxy_apply_shell_proxy "$proxy_host" "$proxy_port" || return 1
	_proxy_apply_git_proxy "global" "$proxy_host" "$proxy_port" || return 1
	_proxy_apply_desktop_proxy "$proxy_host" "$proxy_port" || return 1
	return 0
}

_proxy_unset_global() {
	if ! _proxy_expect_no_args "$@"; then
		return 1
	fi

	_proxy_clear_shell_proxy || return 1
	_proxy_clear_git_proxy "global" || return 1
	_proxy_clear_desktop_proxy || return 1
	_proxy_disable_services || return 1
	_proxy_tun_note_still_active
	return 0
}

_proxy_check_shell() {
	local show_section="${1:-false}"
	local proxy_env line key val

	_proxy_check_shell_summary "$show_section"
	echo
	_proxy_report_section 'Related Environment Variables'

	proxy_env=$(env | grep -i proxy || true)
	if [[ -z $proxy_env ]]; then
		printf '%s%s\n' "$INDENT" "$(_t 'The shell is not exporting any proxy-related environment variables.')"
		return 0
	fi

	echo "$proxy_env" | while read -r line; do
		if [ -n "$line" ]; then
			key="${line%%=*}"
			val="${line#*=}"
			printf '%s%-20s %s\n' "$INDENT" "$key" "$val"
		fi
	done
}

_proxy_check_env() {
	_proxy_check_shell "$@"
}

_proxy_check_system() {
	local show_section="${1:-false}"
	local runtime desktop support proxy_target
	local service_manager desktop_tool service_manager_available desktop_tool_available

	runtime=$(_proxy_runtime_kind)
	desktop=$(_proxy_desktop_kind)
	support=$(_proxy_support_level)
	proxy_target=$(_proxy_describe_proxy_target)
	service_manager=$(_proxy_service_manager)

	if plat_is_macos; then
		desktop_tool="networksetup"
	else
		desktop_tool="dconf"
	fi

	if _has "$service_manager"; then
		service_manager_available=$(_proxy_bool_label true)
	else
		service_manager_available=$(_proxy_bool_label false)
	fi

	if _has "$desktop_tool"; then
		desktop_tool_available=$(_proxy_bool_label true)
	else
		desktop_tool_available=$(_proxy_bool_label false)
	fi

	if [[ "$show_section" == true ]]; then
		_proxy_report_section 'System'
	fi
	_proxy_report_row 'Runtime' "$(_t "$runtime")"
	_proxy_report_row 'Desktop' "$(_t "$desktop")"
	_proxy_report_row 'Support' "$(_t "$support")"
	_proxy_report_row "$service_manager" "$service_manager_available"
	_proxy_report_row "$desktop_tool" "$desktop_tool_available"
	_proxy_report_row 'Proxy target' "$(_proxy_format_proxy_value "$proxy_target")"
}

_proxy_check_git() {
	local show_section="${1:-false}"
	local http_value https_value proxy_value status_value

	if [[ "$show_section" == true ]]; then
		_proxy_report_section 'Git'
	fi

	if ! _has git; then
		_warning "$(_t 'Git is not installed or not available.')"
		return 0
	fi

	printf '%s%s\n' "$INDENT" "$(_t 'Global')"
	http_value=$(git config --global --get http.proxy 2>/dev/null || true)
	https_value=$(git config --global --get https.proxy 2>/dev/null || true)
	proxy_value=$(_proxy_format_dual_value "$http_value" "$https_value" "http" "https" || true)
	if [[ -n "$proxy_value" ]]; then
		status_value=$(_proxy_format_toggle_state on)
	else
		status_value=$(_proxy_format_toggle_state off)
	fi
	_proxy_report_row 'Status' "$status_value" "$INDENT$INDENT"
	_proxy_report_row 'HTTP(S)' "$(_proxy_format_proxy_value "${proxy_value:-$(_t 'none')}")" "$INDENT$INDENT"

	if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
		return 0
	fi

	printf '%s%s\n' "$INDENT" "$(_t 'Local')"
	http_value=$(git config --local --get http.proxy 2>/dev/null || true)
	https_value=$(git config --local --get https.proxy 2>/dev/null || true)
	proxy_value=$(_proxy_format_dual_value "$http_value" "$https_value" "http" "https" || true)
	if [[ -n "$proxy_value" ]]; then
		status_value=$(_proxy_format_toggle_state on)
	else
		status_value=$(_proxy_format_toggle_state off)
	fi
	_proxy_report_row 'Status' "$status_value" "$INDENT$INDENT"
	_proxy_report_row 'HTTP(S)' "$(_proxy_format_proxy_value "${proxy_value:-$(_t 'none')}")" "$INDENT$INDENT"
}

_proxy_check_service() {
	local show_section="${1:-false}"
	local runtime protocol selected_protocol service_name service_state
	local -a active_protocols=()
	local -a known_protocols=()
	local -a protocols=()

	if [[ "$show_section" == true ]]; then
		_proxy_report_section 'Service'
	fi

	runtime=$(_proxy_runtime_kind)
	case "$runtime" in
	wsl2)
		warning "$(_t 'Unknown. For WSL2, the VPN client is probably running on the host machine. Please check manually.')"
		return 0
		;;
	docker)
		warning "$(_t 'Unknown. For a Docker container, the VPN client is probably running on the host machine. Please check manually.')"
		return 0
		;;
	esac

	# The machine-wide singleton is a different thing from the per-user mixed
	# units below it, so it gets its own row rather than being folded in.
	_proxy_check_service_tun_row

	while IFS= read -r protocol; do
		if [[ -n "$protocol" ]]; then
			active_protocols+=("$protocol")
		fi
	done < <(_proxy_list_active_protocols)

	while IFS= read -r protocol; do
		if [[ -n "$protocol" ]]; then
			known_protocols+=("$protocol")
		fi
	done < <(_proxy_list_known_protocols)

	selected_protocol=$(_proxy_selected_protocol || true)

	if [[ ${#active_protocols[@]} -gt 0 ]]; then
		protocols=("${active_protocols[@]}")
	else
		protocols=("${known_protocols[@]}")
	fi

	if ! _proxy_service_manager_available; then
		if [[ ${#protocols[@]} -gt 0 ]]; then
			_proxy_report_row 'Protocols' "${protocols[*]}"
		else
			_proxy_report_row 'Protocols' "$(_t 'none')"
		fi
		_proxy_report_row 'Status' "$(_t 'unknown')"
		if plat_is_macos; then
			warning "$(_t 'Cannot determine VPN status - launchctl not available')"
		else
			warning "$(_t 'Cannot determine VPN status - systemctl not available')"
		fi
		return 0
	fi

	if [[ ${#protocols[@]} -eq 0 ]]; then
		_proxy_report_row 'Protocols' "$(_t 'none')"
		if [[ -n "$selected_protocol" ]]; then
			_proxy_report_row 'Selected protocol' "$selected_protocol"
		fi
		_proxy_report_row 'Status' "$(_t 'none')"
		return 0
	fi

	if [[ ${#protocols[@]} -eq 1 ]]; then
		for protocol in "${protocols[@]}"; do
			break
		done
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		service_state=$(_proxy_service_state_for_protocol "$protocol")
		if [[ -z "$service_state" ]]; then
			service_state=$(_t 'unknown')
		else
			service_state=$(_proxy_format_service_state "$service_state")
		fi

		_proxy_report_row 'Protocol' "$protocol"
		_proxy_report_row 'Service' "$service_name"
		_proxy_report_row 'Status' "$service_state"
		return 0
	fi

	_proxy_report_row 'Protocols' "${protocols[*]}"
	if [[ -n "$selected_protocol" ]]; then
		_proxy_report_row 'Selected protocol' "$selected_protocol"
	fi
	printf '%s%s\n' "$INDENT" "$(_t 'Services')"

	for protocol in "${protocols[@]}"; do
		service_name=$(_proxy_service_name_for_protocol "$protocol")
		service_state=$(_proxy_service_state_for_protocol "$protocol")
		if [[ -z "$service_state" ]]; then
			service_state=$(_t 'unknown')
		else
			service_state=$(_proxy_format_service_state "$service_state")
		fi
		printf '%s%-30s %s\n' "$INDENT$INDENT" "$service_name" "$service_state"
	done
}

_proxy_check_service_tun_row() {
	if ! tun_unit_installed; then
		return 0
	fi

	_proxy_report_row 'TUN service' "$(tun_unit_name)"
	_proxy_report_row 'TUN status' \
		"$(_proxy_format_service_state "$(tun_unit_active_state)")"
	_proxy_report_row 'TUN enabled' "$(tun_unit_enabled_state)"
}

_proxy_check_shell_summary() {
	local show_section="${1:-false}"
	local proxy_env status_msg http_value socks_value no_proxy_value

	proxy_env=$(env | grep -i proxy || true)
	if [[ -n $proxy_env ]]; then
		status_msg=$(_proxy_format_toggle_state on)
	else
		status_msg=$(_proxy_format_toggle_state off)
	fi

	http_value=$(_proxy_first_proxy_value "${http_proxy:-}" "${HTTP_PROXY:-}" "${https_proxy:-}" "${HTTPS_PROXY:-}" || true)
	socks_value=$(_proxy_first_proxy_value "${socks_proxy:-}" "${SOCKS_PROXY:-}" || true)
	no_proxy_value=$(_proxy_first_proxy_value "${no_proxy:-}" "${NO_PROXY:-}" || true)

	if [[ "$show_section" == true ]]; then
		_proxy_report_section 'Shell'
	fi
	_proxy_report_row 'Status' "$status_msg"
	_proxy_report_row 'HTTP(S)' "$(_proxy_format_proxy_value "${http_value:-$(_t 'none')}")"
	_proxy_report_row 'SOCKS' "$(_proxy_format_proxy_value "${socks_value:-$(_t 'none')}")"
	_proxy_report_row 'No proxy' "${no_proxy_value:-$(_t 'none')}"
}

_proxy_macos_check_system_proxy() {
	local network_service bypass_domains status_value
	local http_pair https_pair socks_pair
	local http_target https_target http_value socks_value

	if ! network_service=$(_proxy_macos_resolve_network_service); then
		return 0
	fi

	http_pair=$(macos_proxy_read "$network_service" -getwebproxy || true)
	https_pair=$(macos_proxy_read "$network_service" -getsecurewebproxy || true)
	socks_pair=$(macos_proxy_read "$network_service" -getsocksfirewallproxy || true)

	# shellcheck disable=SC2086
	http_target=$(_proxy_format_host_port_value $http_pair || true)
	# shellcheck disable=SC2086
	https_target=$(_proxy_format_host_port_value $https_pair || true)
	# shellcheck disable=SC2086
	socks_value=$(_proxy_format_host_port_value $socks_pair || true)
	http_value=$(_proxy_format_dual_value "$http_target" "$https_target" "http" "https" || true)

	if [[ -n "$http_value" || -n "$socks_value" ]]; then
		status_value=$(_proxy_format_toggle_state on)
	else
		status_value=$(_proxy_format_toggle_state off)
	fi

	bypass_domains=$(networksetup -getproxybypassdomains "$network_service" 2>/dev/null |
		paste -sd ',' - || true)

	_proxy_report_row 'Status' "$status_value"
	_proxy_report_row 'Network service' "$network_service"
	_proxy_report_row 'HTTP(S)' "$(_proxy_format_proxy_value "${http_value:-$(_t 'none')}")"
	_proxy_report_row 'SOCKS' "$(_proxy_format_proxy_value "${socks_value:-$(_t 'none')}")"
	_proxy_report_row 'No proxy' "${bypass_domains:-$(_t 'none')}"
}

_proxy_check_desktop() {
	local show_section="${1:-false}"
	local mode status_value http_host http_port https_host https_port socks_host socks_port ignore_hosts
	local http_target https_target http_value socks_value

	if [[ "$show_section" == true ]]; then
		_proxy_report_section 'Desktop'
	fi

	if plat_is_macos; then
		_proxy_macos_check_system_proxy
		return 0
	fi

	if ! _proxy_is_gnome_desktop; then
		_warning "$(_t 'GNOME desktop environment not detected. Skipping desktop proxy settings.')"
		return 0
	fi

	if ! _has dconf; then
		_warning "$(_t 'dconf is not available. Skipping desktop proxy settings.')"
		return 0
	fi

	mode=$(gnome_proxy_read /system/proxy/mode)
	http_host=$(gnome_proxy_read /system/proxy/http/host)
	http_port=$(gnome_proxy_read /system/proxy/http/port)
	https_host=$(gnome_proxy_read /system/proxy/https/host)
	https_port=$(gnome_proxy_read /system/proxy/https/port)
	socks_host=$(gnome_proxy_read /system/proxy/socks/host)
	socks_port=$(gnome_proxy_read /system/proxy/socks/port)
	ignore_hosts=$(gnome_proxy_read /system/proxy/ignore-hosts)

	if [[ "$mode" == "manual" ]]; then
		status_value=$(_proxy_format_toggle_state on)
	else
		status_value=$(_proxy_format_toggle_state off)
	fi

	http_target=$(_proxy_format_host_port_value "$http_host" "$http_port" || true)
	https_target=$(_proxy_format_host_port_value "$https_host" "$https_port" || true)
	http_value=$(_proxy_format_dual_value "$http_target" "$https_target" "http" "https" || true)
	socks_value=$(_proxy_format_host_port_value "$socks_host" "$socks_port" || true)

	_proxy_report_row 'Status' "$status_value"
	_proxy_report_row 'Mode' "${mode:-$(_t 'unknown')}"
	_proxy_report_row 'HTTP(S)' "$(_proxy_format_proxy_value "${http_value:-$(_t 'none')}")"
	_proxy_report_row 'SOCKS' "$(_proxy_format_proxy_value "${socks_value:-$(_t 'none')}")"
	_proxy_report_row 'No proxy' "${ignore_hosts:-$(_t 'none')}"
}

_proxy_check_vps_quota() {
	local show_section="${1:-false}"
	local portal_base_url payload error_value hostname_value location quota_used quota_total quota_reset quota_value

	if [[ "$show_section" == true ]]; then
		_proxy_report_section 'VPS'
	fi

	if [[ -n "${SBM_PORTAL_BASE_URL:-}" ]]; then
		portal_base_url=$(_proxy_normalize_portal_base_url "$SBM_PORTAL_BASE_URL" || true)
		if [[ -z "$portal_base_url" ]]; then
			_warning "$(_t 'Portal base URL is invalid.')"
			return 1
		fi
	else
		portal_base_url=$(_proxy_resolve_portal_base_url || true)
		if [[ -z "$portal_base_url" ]]; then
			_warning "$(_t 'Portal base URL is not configured. Reinstall via the hosted installer or set SBM_PORTAL_BASE_URL.')"
			return 1
		fi
	fi

	if ! payload=$(_proxy_fetch_portal_vps_info "$portal_base_url"); then
		_warning "$(_t 'Failed to fetch VPS info from portal.')"
		return 1
	fi

	error_value=$(_proxy_json_string_field "$payload" "error" || true)
	hostname_value=$(_proxy_json_string_field "$payload" "hostname" || true)
	if [[ -n "$hostname_value" ]]; then
		_proxy_report_row 'Hostname' "$hostname_value"
	fi

	if [[ -n "$error_value" ]]; then
		_warning "$(_t 'VPS info unavailable')"
		return 1
	fi

	location=$(_proxy_json_string_field "$payload" "location" || true)
	quota_used=$(_proxy_json_number_field "$payload" "bandwidth_used_gb" || true)
	quota_total=$(_proxy_json_number_field "$payload" "bandwidth_total_gb" || true)
	quota_reset=$(_proxy_json_string_field "$payload" "bandwidth_reset_date" || true)

	if [[ -z "$hostname_value" ]]; then
		_proxy_report_row 'Hostname' "$(_t 'unknown')"
	fi

	if [[ -n "$quota_used" && -n "$quota_total" ]]; then
		quota_value="${quota_used} / ${quota_total} GB"
	elif [[ -n "$quota_used" ]]; then
		quota_value="${quota_used} GB"
	elif [[ -n "$quota_total" ]]; then
		quota_value="${quota_total} GB"
	else
		quota_value=$(_t 'unknown')
	fi

	_proxy_report_row 'Location' "${location:-$(_t 'unknown')}"
	_proxy_report_row 'Quota' "$quota_value"
	_proxy_report_row 'Reset date' "${quota_reset:-$(_t 'unknown')}"
}

_proxy_check_user_quota() {
	local show_section="${1:-false}"
	local portal_base_url payload error_value portal_token portal_username portal_password
	local cycle_start cycle_end upload_bytes download_bytes total_bytes updated_at cycle_value
	local relay_multiplier plan_total_bytes plan_usage_value

	if [[ "$show_section" == true ]]; then
		_proxy_report_section 'User'
	fi

	if [[ -n "${SBM_PORTAL_BASE_URL:-}" ]]; then
		portal_base_url=$(_proxy_normalize_portal_base_url "$SBM_PORTAL_BASE_URL" || true)
		if [[ -z "$portal_base_url" ]]; then
			_warning "$(_t 'Portal base URL is invalid.')"
			return 1
		fi
	else
		portal_base_url=$(_proxy_resolve_portal_base_url || true)
		if [[ -z "$portal_base_url" ]]; then
			_warning "$(_t 'Portal base URL is not configured. Reinstall via the hosted installer or set SBM_PORTAL_BASE_URL.')"
			return 1
		fi
	fi

	portal_token=$(_proxy_resolve_portal_token || true)
	portal_username="${SBM_USERNAME:-}"
	portal_password="${SBM_PASSWORD:-}"

	if [[ -z "$portal_token" && (-z "$portal_username" || -z "$portal_password") ]]; then
		_warning "$(_t 'Portal credentials are not configured. Set SBM_TOKEN or SBM_USERNAME/SBM_PASSWORD, or re-run the hosted installer.')"
		return 1
	fi

	if [[ -n "$portal_token" ]]; then
		if _proxy_fetch_portal_usage "$portal_base_url" "$portal_token" "" ""; then
			payload="$_PROXY_PORTAL_USAGE_PAYLOAD"
		fi
		case "$_PROXY_PORTAL_USAGE_STATUS" in
		200)
			;;
		401 | 403)
			if [[ -n "$portal_username" && -n "$portal_password" ]]; then
				if ! _proxy_fetch_portal_usage "$portal_base_url" "" "$portal_username" "$portal_password"; then
					if [[ "$_PROXY_PORTAL_USAGE_STATUS" == "401" || "$_PROXY_PORTAL_USAGE_STATUS" == "403" ]]; then
						_warning "$(_t 'Portal credentials are invalid.')"
					else
						_warning "$(_t 'Failed to fetch usage info from portal.')"
					fi
					return 1
				fi
				payload="$_PROXY_PORTAL_USAGE_PAYLOAD"
			else
				_warning "$(_t 'Saved portal token expired or invalid. Set SBM_USERNAME/SBM_PASSWORD or re-run the hosted installer.')"
				return 1
			fi
			;;
		*)
			_warning "$(_t 'Failed to fetch usage info from portal.')"
			return 1
			;;
		esac
	else
		if ! _proxy_fetch_portal_usage "$portal_base_url" "" "$portal_username" "$portal_password"; then
			if [[ "$_PROXY_PORTAL_USAGE_STATUS" == "401" || "$_PROXY_PORTAL_USAGE_STATUS" == "403" ]]; then
				_warning "$(_t 'Portal credentials are invalid.')"
			else
				_warning "$(_t 'Failed to fetch usage info from portal.')"
			fi
			return 1
		fi
		payload="$_PROXY_PORTAL_USAGE_PAYLOAD"
	fi

	error_value=$(_proxy_json_string_field "$payload" "error" || true)
	if [[ -n "$error_value" ]]; then
		_warning "$(_t 'Usage info unavailable')"
		return 1
	fi

	cycle_start=$(_proxy_json_string_field "$payload" "cycle_start" || true)
	cycle_end=$(_proxy_json_string_field "$payload" "cycle_end" || true)
	upload_bytes=$(_proxy_json_number_field "$payload" "upload_bytes" || true)
	download_bytes=$(_proxy_json_number_field "$payload" "download_bytes" || true)
	total_bytes=$(_proxy_json_number_field "$payload" "total_bytes" || true)
	relay_multiplier=$(_proxy_json_number_field "$payload" "relay_multiplier" || true)
	plan_total_bytes=$(_proxy_json_number_field "$payload" "plan_total_bytes" || true)
	updated_at=$(_proxy_json_string_field "$payload" "updated_at" || true)

	if [[ -n "$cycle_start" && -n "$cycle_end" ]]; then
		cycle_value="${cycle_start} ~ ${cycle_end}"
	else
		cycle_value=$(_t 'unknown')
	fi

	_proxy_report_row 'Cycle' "$cycle_value"
	_proxy_report_row 'Upload' "$(_proxy_format_byte_count "${upload_bytes:-0}")"
	_proxy_report_row 'Download' "$(_proxy_format_byte_count "${download_bytes:-0}")"
	_proxy_report_row 'Total' "$(_proxy_format_byte_count "${total_bytes:-0}")"

	# Omitted rather than guessed when the portal predates this field, so an
	# older server never makes the client print an unmultiplied plan share.
	plan_usage_value=$(_proxy_format_plan_usage \
		"${total_bytes:-0}" "$relay_multiplier" "$plan_total_bytes" || true)
	if [[ -n "$plan_usage_value" ]]; then
		_proxy_report_row 'Plan usage' "$plan_usage_value"
	fi

	_proxy_report_row 'Last updated' "${updated_at:-$(_t 'Not collected yet')}"
}

_proxy_check_quota() {
	local report_status=0

	_proxy_check_vps_quota true || report_status=1
	echo
	_proxy_check_user_quota true || report_status=1

	return "$report_status"
}

_proxy_check_network() {
	local show_section="${1:-false}"
	local public_ip public_ip_cn private_ip

	if [[ "$show_section" == true ]]; then
		_proxy_report_section 'Network'
	fi

	if public_ip=$(_proxy_resolve_public_ip "$TIMEOUT"); then
		_proxy_report_row 'Internet' "$public_ip"
	else
		_proxy_report_row 'Internet' "$(_t 'unknown')"
	fi

	if public_ip_cn=$(_proxy_resolve_public_ip_cn "$TIMEOUT"); then
		_proxy_report_row 'Internet (CN)' "$public_ip_cn"
	else
		_proxy_report_row 'Internet (CN)' "$(_t 'unknown')"
	fi

	if private_ip=$(_proxy_resolve_private_ip); then
		_proxy_report_row 'LAN' "$private_ip"
	else
		_proxy_report_row 'LAN' "$(_t 'unknown')"
	fi
}

_proxy_check_all() {
	# _proxy_check_system true
	# echo
	_proxy_check_service true
	echo
	_proxy_check_shell_summary true
	echo
	_proxy_check_network true
}

_proxy_help() {
	echo
	echo "  ${BOLD}${CYAN}${UNDERLINE}Sing-Box Manager (Client)${RESET}"
	echo
	echo "  ${BOLD}$(_t 'Toggling Commands')${RESET}"
	echo "${INDENT}${GREEN}${BOLD}proxy on [-f|--force] [-p|--port PORT]${RESET} $(_t 'Enable composite proxy config')"
	echo "${INDENT}${GREEN}${BOLD}proxy off${RESET}                             $(_t 'Remove composite proxy config')"
	echo "${INDENT}${GREEN}${BOLD}proxy protocol <protocol>${RESET}             $(_t 'Switch active proxy protocol')"
	echo "${INDENT}${GREEN}${BOLD}proxy route [china|gfw|ai|global]${RESET}          $(_t 'Switch mixed-mode routing strategy')"
	echo "${INDENT}${GREEN}${BOLD}proxy port [PORT]${RESET}                     $(_t 'Show or select mixed inbound port')"
	echo "${INDENT}${GREEN}${BOLD}proxy shell|env on [-f|--force]${RESET}       $(_t 'Enable shell proxy')"
	echo "${INDENT}${GREEN}${BOLD}proxy shell|env off${RESET}                   $(_t 'Remove shell proxy')"
	echo "${INDENT}${GREEN}${BOLD}proxy desktop on [-f|--force]${RESET}         $(_t 'Enable desktop proxy')"
	echo "${INDENT}${GREEN}${BOLD}proxy desktop off${RESET}                     $(_t 'Remove desktop proxy')"
	echo "${INDENT}${GREEN}${BOLD}proxy git on [-f|--force] [--local]${RESET}   $(_t 'Enable git proxy')"
	echo "${INDENT}${GREEN}${BOLD}proxy git off [--local]${RESET}               $(_t 'Remove git proxy')"
	echo "${INDENT}${GREEN}${BOLD}proxy docker on [-f|--force]${RESET}          $(_t 'Enable Docker proxy')"
	echo "${INDENT}${GREEN}${BOLD}proxy docker off${RESET}                      $(_t 'Remove Docker proxy')"
	echo "${INDENT}${GREEN}${BOLD}proxy service on${RESET}                      $(_t 'Enable user proxy systemd service')"
	echo "${INDENT}${GREEN}${BOLD}proxy service off${RESET}                     $(_t 'Disable user proxy systemd service')"
	echo "${INDENT}${GREEN}${BOLD}proxy upgrade${RESET}                         $(_t 'Upgrade the installed client')"
	echo "${INDENT}${GREEN}${BOLD}proxy uninstall [--yes]${RESET}               $(_t 'Remove the installed client')"
	if _proxy_tun_commands_available; then
		echo "${INDENT}${GREEN}${BOLD}proxy tun on [--persist]${RESET}              $(_t 'Enable machine-wide TUN mode')"
		echo "${INDENT}${GREEN}${BOLD}proxy tun off${RESET}                         $(_t 'Disable machine-wide TUN mode')"
	fi
	echo
	echo "${INDENT}${DIM}$(_t 'Use -f or --force to apply proxy settings even when no active sing-box service is detected.')${RESET}"
	echo
	echo "  ${BOLD}$(_t 'Checking Commands')${RESET}"
	echo "${INDENT}${GREEN}${BOLD}proxy version${RESET}                         $(_t 'Show bundled sing-box and manager versions')"
	echo "${INDENT}${GREEN}${BOLD}proxy check${RESET}                           $(_t 'Run common proxy checks')"
	echo "${INDENT}${GREEN}${BOLD}proxy check ip${RESET}                        $(_t 'Check public IP (ipinfo.io)')"
	echo "${INDENT}${GREEN}${BOLD}proxy check ip cn${RESET}                     $(_t 'Check public IP (cip.cc)')"
	echo "${INDENT}${GREEN}${BOLD}proxy check ip private${RESET}                $(_t 'Show private/LAN IP')"
	echo "${INDENT}${GREEN}${BOLD}proxy check service${RESET}                   $(_t 'Show detected protocol and service status')"
	echo "${INDENT}${GREEN}${BOLD}proxy check route${RESET}                     $(_t 'Show saved mixed-mode routing strategy')"
	if _proxy_tun_commands_available; then
		echo "${INDENT}${GREEN}${BOLD}proxy check tun${RESET}                       $(_t 'Show machine-wide TUN capability and state')"
	fi
	echo "${INDENT}${GREEN}${BOLD}proxy check shell|env${RESET}                 $(_t 'Show proxy environment variables for current shell')"
	echo "${INDENT}${GREEN}${BOLD}proxy check desktop${RESET}                   $(_t 'Show GNOME desktop proxy settings')"
	echo "${INDENT}${GREEN}${BOLD}proxy check git${RESET}                       $(_t 'Show git proxy settings')"
	echo "${INDENT}${GREEN}${BOLD}proxy check system${RESET}                    $(_t 'Show runtime and desktop support status')"
	echo "${INDENT}${GREEN}${BOLD}proxy check quota${RESET}                     $(_t 'Show VPS and user quota')"
	echo "${INDENT}${GREEN}${BOLD}proxy check update${RESET}                    $(_t 'Show installed and portal client versions')"
	echo
}

_proxy_dispatch_shell() {
	local command_name="${1:-shell}"
	shift

	local action="${1:-}"

	if [[ $# -gt 0 ]]; then
		shift
	fi

	case "$action" in
	on)
		_proxy_set_shell "$@"
		;;
	off)
		_proxy_unset_shell "$@"
		;;
	*)
		_proxy_unknown_argument "${command_name}${action:+ ${action}}"
		return 1
		;;
	esac
}

_proxy_dispatch_git() {
	local action="${1:-}"

	if [[ $# -gt 0 ]]; then
		shift
	fi

	case "$action" in
	on)
		_proxy_set_git "$@"
		;;
	off)
		_proxy_unset_git "$@"
		;;
	*)
		_proxy_unknown_argument "git${action:+ ${action}}"
		return 1
		;;
	esac
}

_proxy_dispatch_desktop() {
	local action="${1:-}"

	if [[ $# -gt 0 ]]; then
		shift
	fi

	case "$action" in
	on)
		_proxy_set_desktop "$@"
		;;
	off)
		_proxy_unset_desktop "$@"
		;;
	*)
		_proxy_unknown_argument "desktop${action:+ ${action}}"
		return 1
		;;
	esac
}

_proxy_dispatch_service() {
	local action="${1:-}"

	if [[ $# -gt 0 ]]; then
		shift
	fi

	case "$action" in
	on)
		_proxy_set_service "$@"
		;;
	off)
		_proxy_unset_service "$@"
		;;
	*)
		_proxy_unknown_argument "service${action:+ ${action}}"
		return 1
		;;
	esac
}

_proxy_dispatch_docker() {
	local action="${1:-}"

	if [[ $# -gt 0 ]]; then
		shift
	fi

	case "$action" in
	on)
		_proxy_set_docker "$@"
		;;
	off)
		_proxy_unset_docker "$@"
		;;
	*)
		_proxy_unknown_argument "docker${action:+ ${action}}"
		return 1
		;;
	esac
}

_proxy_dispatch_check_ip() {
	case "${1:-}" in
	cn)
		shift
		_proxy_check_public_ip_cn "$@"
		;;
	private)
		shift
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		_proxy_check_private_ip
		;;
	*)
		if ! _proxy_require_jq; then
			return 1
		fi
		_proxy_check_public_ip "$@"
		;;
	esac
}

_proxy_dispatch_check() {
	local action="${1:-}"

	if [[ $# -gt 0 ]]; then
		shift
	fi

	case "$action" in
	"")
		if ! _proxy_require_jq; then
			return 1
		fi
		_proxy_check_all
		;;
	system)
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		_proxy_check_system
		;;
	service)
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		_proxy_check_service
		;;
	route)
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		_proxy_check_route
		;;
	tun)
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		_proxy_check_tun
		;;
	git)
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		_proxy_check_git
		;;
	shell | env)
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		_proxy_check_shell
		;;
	desktop)
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		_proxy_check_desktop
		;;
	quota)
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		if ! _proxy_require_jq; then
			return 1
		fi
		_proxy_check_quota
		;;
	update)
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		_proxy_check_update
		;;
	ip)
		_proxy_dispatch_check_ip "$@"
		;;
	*)
		_proxy_unknown_argument "check ${action}"
		return 1
		;;
	esac
}

# Remove only endpoint values generated by this client. Keep foreign values,
# including other values of the same Git key and unrelated Docker JSON fields.
_proxy_uninstall_settings() {
	local port key value config_file tmp_file daemon_file expected
	local -a ports=("$(_proxy_read_config_port)" "$(_proxy_read_selected_port)")
	for port in "${ports[@]}"; do
		[[ -n "$port" ]] || continue
		if _has git; then
			for key in http.proxy https.proxy; do
				if git config --global --get-all "$key" | grep -Fxq "http://127.0.0.1:$port"; then
					git config --global --unset-all "$key" "^http://127[.]0[.]0[.]1:$port$" || return 1
				fi
			done
		fi

		config_file=$(_proxy_docker_config_file)
		if [[ -f "$config_file" ]]; then
			_proxy_require_jq || return 1
			tmp_file=$(mktemp "${config_file}.tmp.XXXXXX") || return 1
			if ! jq --arg http "http://127.0.0.1:$port" --arg ftp "ftp://127.0.0.1:$port" --arg bypass "$DEFAULT_NO_PROXY" '
				.proxies.default as $before |
				reduce ["httpProxy", "httpsProxy", "ftpProxy"][] as $key (.;
					if .proxies.default[$key] == $http or .proxies.default[$key] == $ftp
					then del(.proxies.default[$key]) else . end) |
				if .proxies.default != $before and
					([.proxies.default.httpProxy, .proxies.default.httpsProxy, .proxies.default.ftpProxy, .proxies.default.allProxy] | all(. == null)) and
					.proxies.default.noProxy == $bypass
				then del(.proxies.default.noProxy) else . end |
				if .proxies.default == {} then del(.proxies.default) else . end |
				if .proxies == {} then del(.proxies) else . end
			' "$config_file" >"$tmp_file"; then
				rm -f "$tmp_file"
				return 1
			fi
			mv "$tmp_file" "$config_file" || {
				rm -f "$tmp_file"
				return 1
			}
		fi

		# A daemon drop-in is shared machine configuration. Remove it only
		# when the entire file still matches what proxy docker on writes.
		daemon_file=$(_proxy_docker_daemon_proxy_file)
		if plat_is_linux && [[ -f "$daemon_file" ]]; then
			expected=$(printf '[Service]\nEnvironment="http_proxy=http://127.0.0.1:%s"\nEnvironment="https_proxy=http://127.0.0.1:%s"\nEnvironment="no_proxy=%s"\nEnvironment="HTTP_PROXY=http://127.0.0.1:%s"\nEnvironment="HTTPS_PROXY=http://127.0.0.1:%s"\nEnvironment="NO_PROXY=%s"' "$port" "$port" "$DEFAULT_NO_PROXY" "$port" "$port" "$DEFAULT_NO_PROXY")
			value=$(cat "$daemon_file") || return 1
			if [[ "$value" == "$expected" ]]; then
				sudo rm -f "$daemon_file" || return 1
				sudo systemctl daemon-reload || return 1
				sudo systemctl restart docker || return 1
			fi
		fi
	done
}

_proxy_uninstall_shell() {
	local port name value owned=false remaining=false
	for name in http_proxy https_proxy ftp_proxy socks_proxy all_proxy HTTP_PROXY HTTPS_PROXY FTP_PROXY SOCKS_PROXY ALL_PROXY; do
		# printenv misses shell variables that were not exported; indirect
		# expansion differs between bash and zsh, so use a fixed-name eval.
		eval 'value=${'"$name"':-}'
		for port in "$@"; do
			[[ -n "$port" ]] || continue
			case "$value" in
			http://127.0.0.1:"$port" | ftp://127.0.0.1:"$port" | socks5://127.0.0.1:"$port")
				unset "$name"
				owned=true
				value=""
				;;
			esac
		done
		[[ -z "$value" ]] || remaining=true
	done
	if [[ "$owned" == true && "$remaining" == false ]]; then
		[[ "${no_proxy:-}" != "$DEFAULT_NO_PROXY" ]] || unset no_proxy
		[[ "${NO_PROXY:-}" != "$DEFAULT_NO_PROXY" ]] || unset NO_PROXY
	fi
	return 0
}

_proxy_uninstall_client() {
	local uninstaller="${SING_BOX_DATA_ROOT}/client-uninstall.sh"
	local port selected_port
	if [[ ! -f "$uninstaller" ]]; then
		_error "$(_t 'Installed uninstaller not found. Reinstall the client or use client-uninstall.sh from the archive.')"
		return 1
	fi
	port=$(_proxy_read_config_port)
	selected_port=$(_proxy_read_selected_port)
	SBM_UNINSTALL_CLEAN_PROXY=1 bash "$uninstaller" "$@" || return $?
	# Help and cancellation also return success, but leave this file intact.
	[[ ! -f "$uninstaller" ]] || return 0
	_proxy_uninstall_shell "$port" "$selected_port"
	if [[ -n "${BASH_VERSION:-}" ]]; then
		complete -r proxy 2>/dev/null || true
		_proxy_remove_prompt_command_bash _proxy_register_completion_prompt_bash
	else
		compdef -d proxy 2>/dev/null || true
		_proxy_remove_precmd_hook_zsh _proxy_register_completion_prompt_zsh
	fi
	unset -f proxy
	return 0
}

# Register shell completion when setup.sh is sourced in interactive shells.
_proxy_complete_candidates() {
	local first="${1:-}"
	local second="${2:-}"

	case "$first" in
	"")
		printf '%s\n' on off protocol route port service shell env git desktop docker upgrade uninstall check version help -h --help
		if _proxy_tun_commands_available; then
			printf '%s\n' tun
		fi
		;;
	uninstall)
		case "$second" in
		--shell) printf '%s\n' bash zsh ;;
		*) printf '%s\n' -y --yes --shell --no-rc --preserve-tun -h --help ;;
		esac
		;;
	on)
		printf '%s\n' -f --force -p --port
		;;
	tun)
		if ! _proxy_tun_commands_available; then
			return 0
		fi
		case "$second" in
		"")
			printf '%s\n' on off
			;;
		on)
			printf '%s\n' --persist
			;;
		esac
		;;
	protocol)
		case "$second" in
		"")
			printf '%s\n' "${SUPPORTED_PROTOCOLS[@]}"
			;;
		esac
		;;
	route)
		case "$second" in
		"")
			printf '%s\n' "${SUPPORTED_ROUTES[@]}"
			;;
		esac
		;;
	service)
		case "$second" in
		"")
			printf '%s\n' on off
			;;
		esac
		;;
	shell | env | desktop | docker)
		case "$second" in
		"")
			printf '%s\n' on off
			;;
		on)
			printf '%s\n' -f --force
			;;
		esac
		;;
	git)
		case "$second" in
		"")
			printf '%s\n' on off
			;;
		on)
			printf '%s\n' --local -f --force
			;;
		off)
			printf '%s\n' --local
			;;
		esac
		;;
	check)
		case "$second" in
		"")
			printf '%s\n' system service route shell env git desktop quota update ip
			if _proxy_tun_commands_available; then
				printf '%s\n' tun
			fi
			;;
		ip)
			printf '%s\n' cn private
			;;
		esac
		;;
	esac
}

_proxy_completion_bash() {
	local current_word="${COMP_WORDS[COMP_CWORD]:-}"
	local suggestions
	local -a args=()

	if ((COMP_CWORD > 1)); then
		args=("${COMP_WORDS[@]:1:$((COMP_CWORD - 1))}")
	fi

	suggestions=$(_proxy_complete_candidates "${args[@]}")
	COMPREPLY=()

	if [[ -n "$suggestions" ]]; then
		# Read into COMPREPLY without mapfile, which bash 3.2 does not provide.
		local candidate
		while IFS= read -r candidate; do
			COMPREPLY+=("$candidate")
		done < <(compgen -W "$suggestions" -- "$current_word")
	fi
}

_proxy_completion_zsh() {
	local index
	local suggestion
	local -a args=()
	local -a suggestions=()

	for ((index = 2; index < CURRENT; index++)); do
		# shellcheck disable=SC2154
		args+=("${words[index]}")
	done

	while IFS= read -r suggestion; do
		suggestions+=("$suggestion")
	done < <(_proxy_complete_candidates "${args[@]}")

	if ((${#suggestions[@]} > 0)); then
		compadd -- "${suggestions[@]}"
	fi
}

_proxy_completion_is_registered_bash() {
	local completion_spec

	completion_spec=$(complete -p proxy 2>/dev/null || true)
	[[ "$completion_spec" == *"_proxy_completion_bash"* ]]
}

_proxy_completion_is_registered_zsh() {
	[[ "${_comps[proxy]:-}" == "_proxy_completion_zsh" ]]
}

_proxy_register_completion_now() {
	if [[ -n "${BASH_VERSION:-}" ]]; then
		if _proxy_completion_is_registered_bash; then
			return 0
		fi

		complete -F _proxy_completion_bash proxy
		return $?
	fi

	if [[ -n "${ZSH_VERSION:-}" ]]; then
		if _proxy_completion_is_registered_zsh; then
			return 0
		fi

		if ! command -v compdef >/dev/null 2>&1; then
			autoload -U +X compinit && compinit -i >/dev/null 2>&1 || return 1
		fi

		compdef _proxy_completion_zsh proxy 2>/dev/null || return 1
	fi

	return 0
}

_proxy_add_prompt_command_bash() {
	local hook_name="$1"
	local prompt_decl existing

	prompt_decl=$(declare -p PROMPT_COMMAND 2>/dev/null || true)
	if [[ "$prompt_decl" == declare\ -a* ]]; then
		for existing in "${PROMPT_COMMAND[@]}"; do
			if [[ "$existing" == "$hook_name" ]]; then
				return 0
			fi
		done

		PROMPT_COMMAND+=("$hook_name")
		return 0
	fi

	case ";${PROMPT_COMMAND:-};" in
	*";${hook_name};"*)
		return 0
		;;
	esac

	if [[ -n "${PROMPT_COMMAND:-}" ]]; then
		PROMPT_COMMAND="${PROMPT_COMMAND};${hook_name}"
	else
		PROMPT_COMMAND="${hook_name}"
	fi
}

_proxy_remove_prompt_command_bash() {
	local hook_name="$1"
	local prompt_decl existing prompt_command
	local -a updated_prompt_command=()

	prompt_decl=$(declare -p PROMPT_COMMAND 2>/dev/null || true)
	if [[ "$prompt_decl" == declare\ -a* ]]; then
		for existing in "${PROMPT_COMMAND[@]}"; do
			if [[ "$existing" != "$hook_name" ]]; then
				updated_prompt_command+=("$existing")
			fi
		done

		PROMPT_COMMAND=("${updated_prompt_command[@]}")
		return 0
	fi

	prompt_command="${PROMPT_COMMAND:-}"
	prompt_command=${prompt_command//";${hook_name}"/}
	prompt_command=${prompt_command//"${hook_name};"/}
	if [[ "$prompt_command" == "$hook_name" ]]; then
		prompt_command=""
	fi
	# shellcheck disable=SC2178
	PROMPT_COMMAND="$prompt_command"
}

_proxy_register_completion_prompt_bash() {
	if _proxy_register_completion_now; then
		_proxy_remove_prompt_command_bash "_proxy_register_completion_prompt_bash"
	fi
}

_proxy_add_precmd_hook_zsh() {
	local hook_name="$1"
	local existing

	for existing in "${precmd_functions[@]:-}"; do
		if [[ "$existing" == "$hook_name" ]]; then
			return 0
		fi
	done

	autoload -U add-zsh-hook 2>/dev/null || return 0
	add-zsh-hook precmd "$hook_name"
}

_proxy_remove_precmd_hook_zsh() {
	autoload -U add-zsh-hook 2>/dev/null || return 0
	add-zsh-hook -d precmd "$1" 2>/dev/null || true
}

_proxy_register_completion_prompt_zsh() {
	if _proxy_register_completion_now; then
		_proxy_remove_precmd_hook_zsh "_proxy_register_completion_prompt_zsh"
	fi
}

_proxy_register_completion() {
	case "$-" in
	*i*)
		;;
	*)
		return 0
		;;
	esac

	_proxy_register_completion_now || true

	if [[ -n "${BASH_VERSION:-}" ]]; then
		_proxy_add_prompt_command_bash "_proxy_register_completion_prompt_bash"
		return 0
	fi

	if [[ -n "${ZSH_VERSION:-}" ]]; then
		_proxy_add_precmd_hook_zsh "_proxy_register_completion_prompt_zsh"
	fi
}

# Public proxy command.
proxy() {
	local action="${1:-help}"

	if [[ $# -gt 0 ]]; then
		shift
	fi

	case "$action" in
	on)
		_proxy_set_global "$@"
		;;
	off)
		_proxy_unset_global "$@"
		;;
	protocol)
		_proxy_switch_protocol "$@"
		;;
	route)
		_proxy_switch_route "$@"
		;;
	port)
		_proxy_switch_port "$@"
		;;
	tun)
		_proxy_dispatch_tun "$@"
		;;
	service)
		_proxy_dispatch_service "$@"
		;;
	shell)
		_proxy_dispatch_shell shell "$@"
		;;
	env)
		_proxy_dispatch_shell env "$@"
		;;
	git)
		_proxy_dispatch_git "$@"
		;;
	desktop)
		_proxy_dispatch_desktop "$@"
		;;
	docker)
		_proxy_dispatch_docker "$@"
		;;
	upgrade)
		_proxy_upgrade_client "$@"
		;;
	uninstall)
		_proxy_uninstall_client "$@"
		;;
	check)
		_proxy_dispatch_check "$@"
		;;
	version)
		_proxy_version "$@"
		;;
	help | -h | --help)
		if ! _proxy_expect_no_args "$@"; then
			return 1
		fi
		_proxy_help
		;;
	*)
		_proxy_unknown_argument "$action"
		return 1
		;;
	esac
}

_proxy_register_completion
_proxy_maybe_notice_client_update
