#!/usr/bin/env bash

# Simple UI library for consistent CLI output across scripts
# Automatically detects interactive vs headless environments

# Detect if we're in an interactive environment
if [ -t 1 ] && [ -z "${DOCKER_CONTAINER:-}" ]; then
	INTERACTIVE=true
else
	INTERACTIVE=false
fi

# Color and style setup (supports NO_COLOR convention and dumb terminals)
if [ "$INTERACTIVE" = true ] && [ -z "${NO_COLOR-}" ] && [ "${TERM-}" != "dumb" ]; then
	BOLD="$(tput bold 2>/dev/null || printf '')"
	DIM="$(tput dim 2>/dev/null || printf '')"
	ITALIC="$(tput sitm 2>/dev/null || printf '')"
	UNDERLINE="$(tput smul 2>/dev/null || printf '')"
	BLINK="$(tput blink 2>/dev/null || printf '')"
	REVERSE="$(tput rev 2>/dev/null || printf '')"
	GREY="$(tput setaf 0 2>/dev/null || printf '')"
	RED="$(tput setaf 1 2>/dev/null || printf '')"
	GREEN="$(tput setaf 2 2>/dev/null || printf '')"
	YELLOW="$(tput setaf 3 2>/dev/null || printf '')"
	BLUE="$(tput setaf 4 2>/dev/null || printf '')"
	MAGENTA="$(tput setaf 5 2>/dev/null || printf '')"
	CYAN="$(tput setaf 6 2>/dev/null || printf '')"
	WHITE="$(tput setaf 7 2>/dev/null || printf '')"
	RESET="$(tput sgr0 2>/dev/null || printf '')"

	# Unicode symbols with fallback
	if [[ "${LANG:-}" =~ UTF-8$ ]] || [[ "${LC_ALL:-}" =~ UTF-8$ ]]; then
		CHECK_MARK="✓"
		CROSS_MARK="✗"
		ARROW="→"
		BULLET="•"
		ELLIPSIS="…"
		WARNING_SIGN="!"
		INFO_SIGN="ℹ"
		QUESTION_MARK="?"
		LOCK="🔒"
	else
		CHECK_MARK="[OK]"
		CROSS_MARK="[X]"
		ARROW="->"
		BULLET="*"
		ELLIPSIS="..."
		WARNING_SIGN="!"
		INFO_SIGN="i"
		QUESTION_MARK="?"
		LOCK="[!]"
	fi
else
	# shellcheck disable=SC2034
	BOLD="" DIM="" ITALIC="" UNDERLINE="" BLINK="" REVERSE=""
	# shellcheck disable=SC2034
	GREY="" RED="" GREEN="" YELLOW="" BLUE="" MAGENTA="" CYAN="" WHITE=""
	RESET=""
	# shellcheck disable=SC2034
	CHECK_MARK="[OK]" CROSS_MARK="[X]" ARROW="->" BULLET="*"
	# shellcheck disable=SC2034
	ELLIPSIS="..." WARNING_SIGN="[!]" INFO_SIGN="[i]"
	# shellcheck disable=SC2034
	QUESTION_MARK="[?]" LOCK="[LOCK]"
fi

# shellcheck disable=SC2034
INDENT='  '

# Detect Nerd Font support
_has_nerd_font() {
	# Check if terminal supports UTF-8
	if [[ "${LANG-}" != *"UTF-8"* ]] && [[ "${LC_ALL-}" != *"UTF-8"* ]]; then
		return 1
	fi

	# Check for known terminals/fonts that support Nerd Fonts
	if [[ -n "${KITTY_WINDOW_ID-}" ]] ||
		[[ -n "${ALACRITTY_SOCKET-}" ]] ||
		[[ -n "${WEZTERM_EXECUTABLE-}" ]] ||
		[[ "${TERM_PROGRAM-}" == "iTerm.app" ]] ||
		[[ "${TERM_PROGRAM-}" == "WezTerm" ]] ||
		[[ "${TERM-}" == *"kitty"* ]] ||
		[[ "${TERM-}" == *"alacritty"* ]]; then
		return 0
	fi

	# Check if a Nerd Font is explicitly set
	if [[ -n "${NERD_FONT-}" ]] || [[ "${USE_NERD_FONT-}" == "true" ]]; then
		return 0
	fi

	return 1
}

# Set icons based on Nerd Font support
if _has_nerd_font; then
	ICON_ERROR="󰅚 "
	ICON_WARNING="󰀪 "
	ICON_INFO="󰋽 "
	ICON_DEBUG="󰃤 "
	ICON_SUCCESS="󰄬 "
	ICON_HINT="󰛿 "
	ICON_STEP="󰛿 "
else
	ICON_ERROR=""
	ICON_WARNING=""
	ICON_INFO=""
	ICON_DEBUG=""
	ICON_SUCCESS=""
	ICON_HINT=""
	ICON_STEP=""
fi

# Internationalization machinery (scripts override _translate_zh after sourcing).
# A case-based hook rather than an associative array keeps every consumer
# compatible with bash 3.2, the stock /bin/bash on macOS.
_translate_zh() {
	return 1
}

# Detect system locale, allowing override with FORCE_LANG
_detect_language() {
	if [[ -n "${FORCE_LANG:-}" ]]; then
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

# Translation function with fallback
_t() {
	local text="$1"
	local translated

	if [[ "$(_detect_language)" == "zh_CN" ]] && translated=$(_translate_zh "$text"); then
		printf '%s\n' "$translated"
		return 0
	fi

	printf '%s\n' "$text"
}

# Shorten a path for display, preferring the target user's home when available.
display_path() {
	local target_path="$1"
	local home_dir="${2:-${TARGET_HOME:-${HOME:-}}}"

	if [[ -z "$target_path" ]]; then
		printf '\n'
		return 0
	fi

	if [[ -n "$home_dir" ]] && [[ "$target_path" == "$home_dir" ]]; then
		printf '~\n'
		return 0
	fi

	if [[ -n "$home_dir" ]] && [[ "$target_path" == "${home_dir}/"* ]]; then
		printf '%s/%s\n' '~' "${target_path#"${home_dir}"/}"
		return 0
	fi

	echo "${ITALIC}$target_path${RESET}"
}

# Core message helpers
error() { printf '%s\n' "${INDENT}${BOLD}${RED}${UNDERLINE}${ICON_ERROR}error:${RESET} $*" >&2; }
warning() { printf '%s\n' "${INDENT}${BOLD}${YELLOW}${UNDERLINE}${ICON_WARNING}warning:${RESET} $*"; }
info() { printf '%s\n' "${INDENT}${BOLD}${MAGENTA}${UNDERLINE}${ICON_INFO}info:${RESET} $*"; }
completed() { printf '%s\n' "${INDENT}${BOLD}${GREEN}${UNDERLINE}${ICON_SUCCESS}success:${RESET} $*"; }
success() { completed "$@"; }
hint() { printf '%s\n' "${INDENT}${BOLD}${BLUE}${UNDERLINE}${ICON_HINT}hint:${RESET} $*"; }
debug() {
	local debug_enabled=false
	local value
	for value in "${DEBUG-}" "${debug-}" "${VERBOSE-}" "${verbose-}"; do
		case "$value" in
		[Tt][Rr][Uu][Ee] | 1 | [Oo][Nn] | [Yy][Ee][Ss])
			debug_enabled=true
			break
			;;
		esac
	done
	if [ "$debug_enabled" = "true" ]; then
		printf '%s\n' "${INDENT}${BOLD}${DIM}${UNDERLINE}${ICON_DEBUG}debug:${RESET} $*"
	fi
}

# Installer-style step helpers
step_ok() {
	printf "$INDENT$GREEN$CHECK_MARK$RESET %s\n" "$*"
}

step_fail() {
	printf "$INDENT$RED$CROSS_MARK$RESET %s\n" "$*"
}

step_warn() {
	printf "$INDENT$YELLOW$WARNING_SIGN$RESET %s\n" "$*"
}

# Progress indicator
progress() {
	printf '%s\n' "${BOLD}${BLUE}${ARROW} $(_t "$1"):${RESET} ${2:-}"
}

_UI_SPINNER_PID=""

_ui_spinner_clear() {
	if [ "$INTERACTIVE" = true ]; then
		printf '\r\033[2K'
	fi
}

# Managed spinner with an inline status message
spinner_start() {
	local msg="$1"
	local frames="|/-\\"
	local frame_count=4

	spinner_stop

	if [[ "${LANG:-}${LC_ALL:-}" == *"UTF-8"* ]]; then
		frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
		frame_count=10
	fi

	if [ "$INTERACTIVE" != true ]; then
		printf '  %s %s\n' "$ELLIPSIS" "$msg"
		return 0
	fi

	(
		trap 'exit 0' TERM INT
		local i=0
		while true; do
			printf '\r\033[2K  %s%s%s %s' "$YELLOW" "${frames:i:1}" "$RESET" "$msg"
			i=$(((i + 1) % frame_count))
			sleep 0.08
		done
	) &
	_UI_SPINNER_PID=$!
}

spinner_stop() {
	if [[ -n "${_UI_SPINNER_PID:-}" ]]; then
		kill "$_UI_SPINNER_PID" 2>/dev/null || true
		wait "$_UI_SPINNER_PID" 2>/dev/null || true
		_UI_SPINNER_PID=""
		_ui_spinner_clear
	fi
}

# Simple spinner for waiting on a background PID
spinner() {
	if [ "$INTERACTIVE" = true ]; then
		local pid=$1
		local delay=0.1
		# shellcheck disable=SC1003
		local spinstr='|/-\'
		while kill -0 "$pid" 2>/dev/null; do
			local temp=${spinstr#?}
			printf " [%c]  " "$spinstr"
			spinstr=$temp${spinstr%"$temp"}
			sleep $delay
			printf "\b\b\b\b\b\b"
		done
		printf "    \b\b\b\b"
	else
		# In headless mode, just wait
		wait "$1" 2>/dev/null || true
	fi
}

# Display a step/action being performed
step() {
	if [ "$INTERACTIVE" = true ]; then
		printf '\n%s\n' "${BLUE}${ICON_STEP} ${BOLD}$*${RESET}"
	else
		printf 'step: %s\n' "$*"
	fi
}

# Display a header (for main scripts)
header() {
	if [ "$INTERACTIVE" = true ]; then
		printf '\n%s\n\n' "${BOLD}${BLUE}==> $*${RESET}"
	else
		printf '==> %s\n' "$*"
	fi
}

# Display a footer (for main scripts)
footer() {
	if [ "$INTERACTIVE" = true ]; then
		printf '\n%s\n\n' "${BOLD}${GREEN}<== $*${RESET}"
	else
		printf '<== %s\n' "$*"
	fi
}
