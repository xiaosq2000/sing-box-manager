#!/bin/sh
# Embedded sudo payload. Arguments come only from the Go allowlist.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
operation=$1
policy_dir=$2
owner_uid=$3
policy_data=$4
case "$operation" in on | off | legacy-off) ;; *) exit 1 ;; esac
case "$owner_uid" in '' | *[!0-9]*) exit 1 ;; esac
policy_file="$policy_dir/sbc-webrtc-$owner_uid.json"
owner_file="$policy_dir/.sbc-webrtc-$owner_uid.owner"
owner_data="sbc-webrtc-v1:$owner_uid"
# Refuse symbolic links in all destination components, including a dangling one.
component=$policy_dir
while [ "$component" != / ]; do
	[ ! -L "$component" ] || exit 1
	component=$(dirname "$component")
done
if [ "$operation" = legacy-off ]; then
	policy_file="$policy_dir/webrtc.json"
	[ ! -L "$policy_file" ] && [ -f "$policy_file" ] || exit 1
	# Recheck the exact legacy payload under sudo; never remove a changed file.
	printf '%s' "$policy_data" | cmp -s - "$policy_file" || exit 1
	rm "$policy_file"
	exit 0
fi
[ ! -L "$policy_file" ] && [ ! -L "$owner_file" ] || exit 1
if [ -e "$owner_file" ]; then
	[ -f "$owner_file" ] && [ "$(cat "$owner_file")" = "$owner_data" ] || exit 1
fi
if [ "$operation" = on ]; then
	[ ! -e "$policy_file" ] || exit 1
	mkdir -p "$policy_dir"
	if [ ! -e "$owner_file" ]; then
		(
			umask 022
			set -C
			printf '%s\n' "$owner_data" >"$owner_file"
		)
	fi
	staging=$(mktemp "$policy_dir/.sbc-webrtc.XXXXXX")
	trap 'rm -f "$staging"' EXIT HUP INT TERM
	printf '%s' "$policy_data" >"$staging"
	chmod 644 "$staging"
	# link is atomic and refuses a destination created by another writer.
	ln "$staging" "$policy_file"
else
	[ -f "$owner_file" ] || exit 0
	if [ -e "$policy_file" ]; then
		[ -f "$policy_file" ] || exit 1
		if printf '%s' "$policy_data" | cmp -s - "$policy_file"; then
			rm "$policy_file"
		fi
	fi
	rm "$owner_file"
fi
