#!/bin/sh
# Embedded sudo payload. The Go caller supplies a fixed Chrome directory and UID.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
operation=$1
policy_dir=$2
owner_uid=$3
case "$operation" in on | off) ;; *) exit 1 ;; esac
case "$owner_uid" in '' | *[!0-9]*) exit 1 ;; esac
policy_file="$policy_dir/sbc-geolocation-$owner_uid.json"
owner_file="$policy_dir/.sbc-geolocation-$owner_uid.owner"
owner_data="sbc-geolocation-v1:$owner_uid"
policy_data='{"DefaultGeolocationSetting":2}'
component=$policy_dir
while [ "$component" != / ]; do
	[ ! -L "$component" ] || exit 1
	component=$(dirname "$component")
done
[ ! -L "$policy_file" ] && [ ! -L "$owner_file" ] || exit 1
if [ -e "$owner_file" ]; then
	[ -f "$owner_file" ] || exit 1
	printf '%s\n' "$owner_data" | cmp -s - "$owner_file" || exit 1
fi
if [ "$operation" = on ]; then
	[ ! -e "$policy_file" ] || exit 1
	umask 022
	mkdir -p "$policy_dir"
	staging=$(mktemp "$policy_dir/.sbc-geolocation.XXXXXX")
	trap 'rm -f "$staging"' EXIT HUP INT TERM
	if [ ! -e "$owner_file" ]; then
		printf '%s\n' "$owner_data" >"$staging"
		chmod 644 "$staging"
		ln "$staging" "$owner_file"
		rm "$staging"
		staging=$(mktemp "$policy_dir/.sbc-geolocation.XXXXXX")
	fi
	printf '%s\n' "$policy_data" >"$staging"
	chmod 644 "$staging"
	# Atomic publication refuses a destination created by another writer.
	ln "$staging" "$policy_file"
else
	[ -f "$owner_file" ] || exit 0
	if [ -e "$policy_file" ]; then
		[ -f "$policy_file" ] || exit 1
		if printf '%s\n' "$policy_data" | cmp -s - "$policy_file"; then
			rm "$policy_file"
		fi
	fi
	rm "$owner_file"
fi
