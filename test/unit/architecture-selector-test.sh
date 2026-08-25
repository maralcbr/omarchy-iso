#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"; rm -f "$ROOT"/release/omarchy-test-{x86_64,aarch64}-quattro.iso; rmdir "$ROOT/release" 2>/dev/null || true' EXIT

mkdir -p "$work/bin" "$work/home"

cat >"$work/bin/docker" <<'STUB'
#!/bin/bash
set -euo pipefail

printf '%s\n' "$@" >"$TEST_DOCKER_ARGS"

arch=""
out=""
previous=""
for argument in "$@"; do
  if [[ $previous == "-e" && $argument == OMARCHY_ARCH=* ]]; then
    arch="${argument#*=}"
  elif [[ $previous == "-v" && $argument == *:/out/ ]]; then
    out="${argument%:/out/}"
  fi
  previous="$argument"
done

[[ -n $arch && -n $out ]]
touch "$out/omarchy-test-$arch.iso"
STUB
chmod +x "$work/bin/docker"

run_make() {
  local label="$1"
  shift
  export TEST_DOCKER_ARGS="$work/docker-$label.args"
  HOME="$work/home" PATH="$work/bin:$PATH" \
    "$ROOT/bin/omarchy-iso-make" "$@" --keep-pkg-cache --no-cache --no-boot-offer
}

assert_arg() {
  local file="$1"
  local expected="$2"
  grep -qxF -- "$expected" "$file" || {
    printf 'missing Docker argument %q in %s\n' "$expected" "$file" >&2
    exit 1
  }
}

run_make default
assert_arg "$work/docker-default.args" "OMARCHY_ARCH=x86_64"
assert_arg "$work/docker-default.args" "archlinux/archlinux:latest"
rm -f "$ROOT/release/omarchy-test-x86_64-quattro.iso"

run_make x86_64 --arch x86_64
assert_arg "$work/docker-x86_64.args" "OMARCHY_ARCH=x86_64"
assert_arg "$work/docker-x86_64.args" "archlinux/archlinux:latest"
if grep -qxF -- "--platform" "$work/docker-x86_64.args"; then
  echo "x86_64 unexpectedly selected a Docker platform" >&2
  exit 1
fi
rm -f "$ROOT/release/omarchy-test-x86_64-quattro.iso"

run_make aarch64 --arch=aarch64
assert_arg "$work/docker-aarch64.args" "OMARCHY_ARCH=aarch64"
assert_arg "$work/docker-aarch64.args" "--platform"
assert_arg "$work/docker-aarch64.args" "linux/arm64"
assert_arg "$work/docker-aarch64.args" "menci/archlinuxarm:latest"

set +e
invalid_output=$("$ROOT/bin/omarchy-iso-make" --arch sparc 2>&1)
invalid_status=$?
set -e

(( invalid_status != 0 ))
[[ $invalid_output == *"Unsupported architecture: sparc"* ]]

echo "Architecture selector tests passed"
