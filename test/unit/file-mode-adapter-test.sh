#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
adapter=$ROOT/builder/file-mode-adapter.sh
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fail() {
  echo "file-mode-adapter-test: $*" >&2
  exit 1
}

[[ -f $adapter && ! -L $adapter ]] || fail "adapter is missing or unsafe"
# shellcheck source=/dev/null
source "$adapter"

printf 'mode probe\n' >"$work/input"
chmod 0444 "$work/input"
[[ $(file_mode "$work/input") == 444 ]] || fail "read-only mode differs"
chmod 0644 "$work/input"
[[ $(file_mode "$work/input") == 644 ]] || fail "writable mode differs"

ln -s "$work/input" "$work/link"
if file_mode "$work/link" >/dev/null 2>&1; then
  fail "symlink input was accepted"
fi
if file_mode "$work/missing" >/dev/null 2>&1; then
  fail "missing input was accepted"
fi
if file_mode relative-path >/dev/null 2>&1; then
  fail "relative input was accepted"
fi
if file_mode "$work/input" extra >/dev/null 2>&1; then
  fail "extra argument was accepted"
fi

echo "File-mode adapter tests passed"
