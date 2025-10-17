#!/usr/bin/env bash

# Simple UI library for consistent CLI output across scripts
# Automatically detects interactive vs headless environments

# Detect if we're in an interactive environment
if [ -t 1 ] && [ -z "${DOCKER_CONTAINER:-}" ]; then
    INTERACTIVE=true
else
    INTERACTIVE=false
fi


if [ "$INTERACTIVE" = true ]; then
    # Style and color setup (prefer tput; fall back to empty). Colors disabled when not interactive.
    BOLD="$(tput bold 2>/dev/null || printf '')"
    DIM="$(tput dim 2>/dev/null || printf '')"
    GREY="$(tput setaf 0 2>/dev/null || printf '')"
    UNDERLINE="$(tput smul 2>/dev/null || printf '')"
    RED="$(tput setaf 1 2>/dev/null || printf '')"
    GREEN="$(tput setaf 2 2>/dev/null || printf '')"
    YELLOW="$(tput setaf 3 2>/dev/null || printf '')"
    BLUE="$(tput setaf 4 2>/dev/null || printf '')"
    MAGENTA="$(tput setaf 5 2>/dev/null || printf '')"
    RESET="$(tput sgr0 2>/dev/null || printf '')"
else
    BOLD=""
    DIM=""
    GREY=""
    UNDERLINE=""
    RED=""
    GREEN=""
    YELLOW=""
    BLUE=""
    MAGENTA=""
    RESET=""
fi

# Detect Nerd Font support
_has_nerd_font() {
    # Check if terminal supports UTF-8
    if [[ "${LANG-}" != *"UTF-8"* ]] && [[ "${LC_ALL-}" != *"UTF-8"* ]]; then
        return 1
    fi

    # Check for known terminals/fonts that support Nerd Fonts
    if [[ -n "${KITTY_WINDOW_ID-}" ]] || \
        [[ -n "${ALACRITTY_SOCKET-}" ]] || \
        [[ -n "${WEZTERM_EXECUTABLE-}" ]] || \
        [[ "${TERM_PROGRAM-}" == "iTerm.app" ]] || \
        [[ "${TERM_PROGRAM-}" == "WezTerm" ]] || \
        [[ "${TERM-}" == *"kitty"* ]] || \
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

# Core message helpers (prefer snippet style)
error() { printf '%s\n' "${BOLD}${RED}${UNDERLINE}${ICON_ERROR}error:${RESET} $*" >&2; }
warning() { printf '%s\n' "${BOLD}${YELLOW}${UNDERLINE}${ICON_WARNING}warning:${RESET} $*"; }
info() { printf '%s\n' "${BOLD}${MAGENTA}${UNDERLINE}${ICON_INFO}info:${RESET} $*"; }
completed() { printf '%s\n' "${BOLD}${GREEN}${UNDERLINE}${ICON_SUCCESS}success:${RESET} $*"; }
success() { completed "$@"; }
hint() { printf '%s\n' "${BOLD}${BLUE}${UNDERLINE}${ICON_HINT}hint:${RESET} $*"; }
debug() {
    local debug_enabled=false
    local value
    for value in "${DEBUG-}" "${debug-}" "${VERBOSE-}" "${verbose-}"; do
        case "$value" in
            [Tt][Rr][Uu][Ee]|1|[Oo][Nn]|[Yy][Ee][Ss])
                debug_enabled=true
                break
                ;;
        esac
    done
    if [ "$debug_enabled" = "true" ]; then
        printf '%s\n' "${BOLD}${DIM}${UNDERLINE}${ICON_DEBUG}debug:${RESET} $*"
    fi
}

# Simple spinner for long-running operations
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
