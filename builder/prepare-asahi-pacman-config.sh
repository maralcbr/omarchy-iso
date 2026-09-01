#!/bin/bash

set -euo pipefail

fail() {
  echo "prepare-asahi-pacman-config: $*" >&2
  exit 1
}

[[ $# == 2 ]] || fail "usage: SOURCE DESTINATION"
source_path=$(realpath -- "$1")
destination_parent=$(realpath -- "$(dirname -- "$2")")
destination_path=$destination_parent/${2##*/}

[[ -s $source_path ]] || fail "source config is missing or empty"
[[ $source_path != "$destination_path" ]] || fail "source and destination must differ"
[[ ! -L $destination_path ]] || fail "destination must not be a symlink"

temporary=$(mktemp "${destination_path}.tmp.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
awk '{ print } /^\[options\]$/ { print "DisableSandbox" }' \
  "$source_path" >"$temporary"
grep -Fxq '[offline]' "$temporary" || fail "derived config has no offline repository"
grep -Eq '^Server[[:space:]]*=[[:space:]]*file://' "$temporary" ||
  fail "derived config has no file repository server"
chmod 0644 "$temporary"
mv -f -- "$temporary" "$destination_path"
trap - EXIT
