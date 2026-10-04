#!/usr/bin/env bash

# Shared OS and architecture detection for the client scripts.

plat_os() {
	case "$(uname -s 2>/dev/null || echo unknown)" in
	Linux) echo linux ;;
	Darwin) echo darwin ;;
	*) echo unknown ;;
	esac
}

plat_arch() {
	case "$(uname -m 2>/dev/null || echo unknown)" in
	x86_64 | amd64) echo amd64 ;;
	arm64 | aarch64) echo arm64 ;;
	*) echo unknown ;;
	esac
}

plat_id() {
	printf '%s-%s\n' "$(plat_os)" "$(plat_arch)"
}

plat_is_linux() {
	[ "$(plat_os)" = linux ]
}

plat_is_macos() {
	[ "$(plat_os)" = darwin ]
}
