#!/usr/bin/env bash
# Terminal User Interface Utilities
# Shared functions for colors, logging, translations, and UI elements

# Terminal colors and effects
_setup_colors() {
    if [[ -t 2 ]] && [[ -z "${NO_COLOR-}" ]] && [[ "${TERM-}" != "dumb" ]]; then
        BOLD="$(tput bold 2>/dev/null || echo '')"
        DIM="$(tput dim 2>/dev/null || echo '')"
        ITALIC="$(tput sitm 2>/dev/null || echo '')"
        UNDERLINE="$(tput smul 2>/dev/null || echo '')"
        BLINK="$(tput blink 2>/dev/null || echo '')"
        REVERSE="$(tput rev 2>/dev/null || echo '')"
        GREY="$(tput setaf 0 2>/dev/null || echo '')"
        RED="$(tput setaf 1 2>/dev/null || echo '')"
        GREEN="$(tput setaf 2 2>/dev/null || echo '')"
        YELLOW="$(tput setaf 3 2>/dev/null || echo '')"
        BLUE="$(tput setaf 4 2>/dev/null || echo '')"
        MAGENTA="$(tput setaf 5 2>/dev/null || echo '')"
        CYAN="$(tput setaf 6 2>/dev/null || echo '')"
        WHITE="$(tput setaf 7 2>/dev/null || echo '')"
        RESET="$(tput sgr0 2>/dev/null || echo '')"
        # Unicode symbols with fallback
        if [[ "${LANG:-}" =~ UTF-8$ ]]; then
            CHECK_MARK="✓"
            CROSS_MARK="✗"
            ARROW="→"
            BULLET="•"
            ELLIPSIS="…"
            WARNING_SIGN="⚠"
            INFO_SIGN="ℹ"
            QUESTION_MARK="?"
            LOCK="🔒"
        else
            CHECK_MARK="[OK]"
            CROSS_MARK="[X]"
            ARROW="->"
            BULLET="*"
            ELLIPSIS="..."
            WARNING_SIGN="[!]"
            INFO_SIGN="[i]"
            QUESTION_MARK="[?]"
            LOCK="[LOCK]"
        fi
    else
        BOLD=""
        DIM=""
        ITALIC="" UNDERLINE="" BLINK="" REVERSE=""
        GREY="" RED="" GREEN="" YELLOW="" BLUE=""
        MAGENTA=""
        CYAN=""
        WHITE=""
        RESET=""
        CHECK_MARK="[OK]" CROSS_MARK="[X]" ARROW="->" BULLET="*"
        ELLIPSIS="..."
        WARNING_SIGN="[!]" INFO_SIGN="[i]"
        QUESTION_MARK="[?]"
        LOCK="[LOCK]"
    fi
}

# Internationalization
typeset -A TRANSLATIONS
TRANSLATIONS=(
    ["Installation Script"]="安装脚本"
    ["Deployment Script"]="部署脚本"
    ["ERROR"]="错误"
    ["WARNING"]="警告"
    ["INFO"]="信息"
    ["DEBUG"]="调试"
    ["SUCCESS"]="成功"
    ["FAILED"]="失败"
    ["Installing"]="正在安装"
    ["Configuring"]="正在配置"
    ["Detecting"]="正在检测"
    ["Deploying"]="正在部署"
    ["Connecting"]="正在连接"
    ["Uploading"]="正在上传"
    ["Extracting"]="正在解压"
    ["Restarting"]="正在重启"
    ["This script will install the VPN client service"]="此脚本将安装 VPN 客户端服务"
    ["This script will deploy the VPN server service"]="此脚本将部署 VPN 服务器服务"
    ["Administrator privileges required"]="需要管理员权限"
    ["Please enter your password"]="请输入您的密码"
    ["Installation cancelled"]="安装已取消"
    ["Deployment cancelled"]="部署已取消"
    ["Detecting shell environment"]="检测 Shell 环境"
    ["Detected shell"]="检测到的 Shell"
    ["Shell RC file"]="Shell 配置文件"
    ["Shell RC configuration disabled"]="Shell 配置文件修改已禁用"
    ["Backing up RC file"]="备份配置文件"
    ["RC file already configured"]="配置文件已经配置过"
    ["Adding proxy functions to RC file"]="添加代理功能到配置文件"
    ["No shell RC file found"]="未找到 Shell 配置文件"
    ["Creating shell RC file"]="创建 Shell 配置文件"
    ["Stopping existing services"]="停止现有服务"
    ["Installing service files"]="安装服务文件"
    ["Service file not found"]="服务文件未找到"
    ["Binary file not found"]="二进制文件未找到"
    ["Configuration file not found"]="配置文件未找到"
    ["Creating directories"]="创建目录"
    ["Copying files"]="复制文件"
    ["Reloading systemd"]="重新加载 systemd"
    ["Enabling service"]="启用服务"
    ["Installation complete"]="安装完成"
    ["Deployment complete"]="部署完成"
    ["Service is now running"]="服务正在运行"
    ["To apply shell changes, run"]="要应用 Shell 更改，请运行"
    ["Or open a new terminal"]="或打开一个新的终端"
    ["Usage"]="用法"
    ["Options"]="选项"
    ["Display help messages"]="显示帮助信息"
    ["Debug logging"]="调试日志"
    ["Specify which protocol to install"]="指定要安装的协议"
    ["Skip shell RC configuration"]="跳过 Shell 配置文件修改"
    ["Unknown argument"]="未知参数"
    ["is not supported yet"]="尚不支持"
    ["Supported protocols"]="支持的协议"
    ["Contact your admin"]="请联系您的管理员"
    ["Checking prerequisites"]="检查先决条件"
    ["systemd is required but not found"]="需要 systemd 但未找到"
    ["This system does not use systemd"]="此系统不使用 systemd"
    ["This script should not be run as root"]="此脚本不应以 root 身份运行"
    ["Please run without sudo"]="请不要使用 sudo 运行"
    ["Shell RC configuration complete"]="Shell 配置完成"
    ["not found"]="未找到"
    ["Unsupported protocol"]="不支持的协议"
    ["SCP failed"]="SCP 传输失败"
    ["Extraction failed"]="解压失败"
    ["Install with systemd failed"]="systemd 安装失败"
    ["Service restart failed"]="服务重启失败"
    ["Uploading to server"]="上传到服务器"
    ["Extracting on server"]="在服务器上解压"
    ["Installing on server"]="在服务器上安装"
    ["Restarting service"]="重启服务"
)

# Detect system locale
_detect_language() {
    if [[ -n "${FORCE_LANG}" ]]; then
        case "$FORCE_LANG" in
            zh_CN* | zh_SG*) echo "zh_CN" ;;
            *) echo "en_US" ;;
        esac
    else
        local lang=${LANG:-en_US.UTF-8}
        case "$lang" in
            zh_CN* | zh_SG*) echo "zh_CN" ;;
            *) echo "en_US" ;;
        esac
    fi
}

# Translation function
_t() {
    local text="$1"
    local language
    language=$(_detect_language)

    if [[ "$language" == "zh_CN" ]]; then
        echo "${TRANSLATIONS[$text]:-$text}"
    else
        echo "$text"
    fi
}

# Logging functions with internationalization
INDENT='    '
error() {
    printf '%s\n' "${BOLD}${RED}${CROSS_MARK} $(_t 'ERROR'):${RESET} $*" >&2
}
warning() {
    printf '%s\n' "${BOLD}${YELLOW}${WARNING_SIGN} $(_t 'WARNING'):${RESET} $*"
}
info() {
    printf '%s\n' "${BOLD}${CYAN}${INFO_SIGN} $(_t 'INFO'):${RESET} $*"
}
debug() {
    if [[ ${VERBOSE:-false} == true ]]; then
        printf '%s\n' "${DIM}${GREY}$(_t 'DEBUG'):${RESET} $*"
    fi
}
success() {
    printf '%s\n' "${BOLD}${GREEN}${CHECK_MARK} $(_t 'SUCCESS'):${RESET} $*"
}
progress() {
    printf '%s\n' "${BOLD}${BLUE}${ARROW} $(_t "$1"):${RESET} ${2:-}"
}

# Spinner for long operations
spin() {
    local pid=$1
    local delay=0.1
    local spinstr='|/-\'
    printf " "
    while ps -p "$pid" > /dev/null 2>&1; do
        local temp=${spinstr#?}
        printf " [%c]  " "$spinstr"
        local spinstr=$temp${spinstr%"$temp"}
        sleep $delay
        printf "\b\b\b\b\b\b"
    done
    printf "    \b\b\b\b"
}

# Initialize colors (call this when sourcing the library)
_setup_colors
