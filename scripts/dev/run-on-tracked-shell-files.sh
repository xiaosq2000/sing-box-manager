#!/usr/bin/env bash

set -euo pipefail

if (($# == 0)); then
	printf 'Usage: %s <command> [args...]\n' "${0##*/}" >&2
	exit 1
fi

cd "$(git rev-parse --show-toplevel)"

# Keep shell tooling scoped to repository-owned scripts.
mapfile -d '' -t files < <(git ls-files -z -- '*.sh')
existing_files=()

for file in "${files[@]}"; do
	if [[ -f "$file" ]]; then
		existing_files+=("$file")
	fi
done

if ((${#existing_files[@]} == 0)); then
	exit 0
fi

"$@" "${existing_files[@]}"
