#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
# Superseded assertion (until 2026-08-29): the lease mode was read with BSD
# `stat -f '%Lp'`. The runner prepends /opt/homebrew/opt/coreutils/libexec/gnubin
# to PATH, so GNU stat answered instead, where -f means "file system
# information" -- the check compared a filesystem dump against 600 and this test
# never passed under the runner. Use the repo's own portable adapter, which
# pins /usr/bin/stat absolutely, branches on uname, and validates the result is
# an octal mode; it also fails closed on a missing, non-regular, symlinked, or
# relative path, so the replacement is strictly stronger.
source "$ROOT/builder/file-mode-adapter.sh"
work=$(mktemp -d "$ROOT/.asahi-lifecycle-wrapper-test.XXXXXX")
cleanup() {
  chmod -R u+w "$work" 2>/dev/null || true
  rm -rf -- "$work"
}
trap cleanup EXIT
mkdir -m 0700 "$work/home"

set +e
output=$( \
  OMARCHY_ASAHI_LIFECYCLE_LEASE_HELD=1 \
  XDG_CACHE_HOME="$work/home/cache" \
  SOURCE_DATE_EPOCH=0 \
  /opt/homebrew/bin/bash "$ROOT/bin/omarchy-iso-make" \
    --target aarch64/apple-silicon \
    --artifact asahi-os-package \
    --local-source "$work/missing-omarchy" "$work/missing-pkgs" \
    --keep-pkg-cache --no-cache --no-boot-offer 2>&1
)
status=$?
set -e

(( status != 0 ))
[[ $output == *"Omarchy checkout not found"* ]]
lease=$work/home/cache/omarchy/.omarchy-lifecycle.lease
[[ -f $lease && ! -L $lease ]]
[[ $(file_mode "$lease") == 600 ]]

echo "Asahi lifecycle public-wrapper tests passed."
