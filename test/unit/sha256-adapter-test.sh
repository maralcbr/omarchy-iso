#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
adapter=$ROOT/builder/sha256-adapter.sh
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fail() {
  echo "sha256-adapter-test: $*" >&2
  exit 1
}

[[ -f $adapter && ! -L $adapter ]] || fail "adapter is missing or unsafe"
# shellcheck source=/dev/null
source "$adapter"

printf 'abc' >"$work/input"
expected=ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad

[[ $(sha256_file "$work/input") == "$expected" ]] ||
  fail "file digest differs"
[[ $(printf 'abc' | sha256_stdin) == "$expected" ]] ||
  fail "stdin digest differs"

ln -s "$work/input" "$work/link"
if sha256_file "$work/link" >/dev/null 2>&1; then
  fail "symlink input was accepted"
fi
if sha256_file "$work/missing" >/dev/null 2>&1; then
  fail "missing input was accepted"
fi
if sha256_file "$work/input" extra >/dev/null 2>&1; then
  fail "extra file argument was accepted"
fi
if sha256_stdin extra </dev/null >/dev/null 2>&1; then
  fail "stdin helper accepted an argument"
fi

echo "SHA-256 adapter tests passed"
