#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
MAKE="$ROOT/bin/omarchy-iso-make"
PROFILE="$ROOT/configs/profiledef.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fail() {
  printf 'not ok - %s\n' "$1" >&2
  exit 1
}

pass() {
  printf 'ok - %s\n' "$1"
}

output=$(bash -c 'declare -A file_permissions; source "$1"; printf "%s" "$arch"' _ "$PROFILE")
[[ $output == "x86_64" ]] || fail "profile defaults to x86_64"
pass "profile defaults to x86_64"

output=$(OMARCHY_ARCH=aarch64 bash -c 'declare -A file_permissions; source "$1"; printf "%s" "$arch"' _ "$PROFILE")
[[ $output == "aarch64" ]] || fail "profile reads the explicit architecture"
pass "profile reads the explicit architecture"

if "$MAKE" --arch >"$work/out" 2>"$work/err"; then
  fail "missing --arch value is rejected"
fi
grep -Fq -- '--arch requires an architecture' "$work/err" ||
  fail "missing --arch value explains the error"
pass "missing --arch value is rejected"

if "$MAKE" --arch aarch64 >"$work/out" 2>"$work/err"; then
  fail "unsupported architecture is rejected"
fi
grep -Fq -- 'unsupported architecture: aarch64' "$work/err" ||
  fail "unsupported architecture explains the error"
pass "unsupported architecture is rejected before the build"

grep -Fq -- '-e "OMARCHY_ARCH=$OMARCHY_ARCH"' "$MAKE" ||
  fail "selected architecture is passed to the build container"
grep -Fq -- '*-"$OMARCHY_ARCH".iso' "$MAKE" ||
  fail "artifact selection is architecture-specific"
pass "the selected architecture reaches the profile and artifact selection"
