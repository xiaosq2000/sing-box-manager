#!/usr/bin/env bash
# post-edit-hook.sh — Auto-format and lint after AI edits.
# Called by Claude Code hooks and OpenCode plugins.
#
# Usage: post-edit-hook.sh <file-path>
# Exit codes: 0 = success, 2 = blocking error (tells AI to fix the issue)
#
# Outputs JSON on stdout when there is an error, so Claude Code can parse it.

set -euo pipefail

FILE="${1:?Usage: post-edit-hook.sh <file-path>}"
EXT="${FILE##*.}"

# Change to the project root (where pixi.toml lives).
cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || dirname "$(readlink -f "$0")/../..")}"

run_task() {
	local task="$1"
	local output
	if ! output=$(pixi run "$task" 2>&1); then
		echo "$output" >&2
		printf '{"decision":"block","reason":"pixi run %s failed:\\n%s"}\n' "$task" "$output"
		exit 2
	fi
}

case "$EXT" in
py)
	run_task format-python
	run_task lint-python
	;;
sh)
	run_task format-shell
	run_task lint-shell
	;;
toml)
	run_task format-toml
	;;
json | yaml | yml | md | js | jsx | ts | tsx | css | scss | less | html | graphql | vue)
	run_task format-prettier
	;;
*)
	# No formatter for this file type; silently succeed.
	;;
esac
