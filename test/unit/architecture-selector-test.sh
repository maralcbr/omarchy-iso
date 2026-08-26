#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"; rm -f "$ROOT"/release/omarchy-test-{x86_64,aarch64}-quattro.iso "$ROOT"/release/omarchy-test-aarch64-apple-silicon-quattro.iso; rmdir "$ROOT/release" 2>/dev/null || true' EXIT

mkdir -p "$work/bin" "$work/home"

cat >"$work/bin/docker" <<'STUB'
#!/bin/bash
set -euo pipefail

printf '%s\n' "$@" >"$TEST_DOCKER_ARGS"

arch=""
media_target=""
out=""
previous=""
for argument in "$@"; do
  if [[ $previous == "-e" && $argument == OMARCHY_ARCH=* ]]; then
    arch="${argument#*=}"
  elif [[ $previous == "-e" && $argument == OMARCHY_MEDIA_TARGET=* ]]; then
    media_target="${argument#*=}"
  elif [[ $previous == "-v" && $argument == *:/out/ ]]; then
    out="${argument%:/out/}"
  fi
  previous="$argument"
done

[[ -n $arch && -n $media_target && -n $out ]]
touch "$out/omarchy-test-$arch.iso"
if [[ $media_target == "aarch64/apple-silicon" ]]; then
  printf 'static evidence\n' >"$out/omarchy-test-$arch.iso.apple-media-evidence.json"
fi
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
assert_arg "$work/docker-default.args" "OMARCHY_MEDIA_TARGET=x86_64/pc"
assert_arg "$work/docker-default.args" "archlinux/archlinux:latest"
rm -f "$ROOT/release/omarchy-test-x86_64-quattro.iso"

run_make x86_64 --arch x86_64
assert_arg "$work/docker-x86_64.args" "OMARCHY_ARCH=x86_64"
assert_arg "$work/docker-x86_64.args" "OMARCHY_MEDIA_TARGET=x86_64/pc"
assert_arg "$work/docker-x86_64.args" "archlinux/archlinux:latest"
if grep -qxF -- "--platform" "$work/docker-x86_64.args"; then
  echo "x86_64 unexpectedly selected a Docker platform" >&2
  exit 1
fi
rm -f "$ROOT/release/omarchy-test-x86_64-quattro.iso"

run_make aarch64 --arch=aarch64
assert_arg "$work/docker-aarch64.args" "OMARCHY_ARCH=aarch64"
assert_arg "$work/docker-aarch64.args" "OMARCHY_MEDIA_TARGET=aarch64/generic"
assert_arg "$work/docker-aarch64.args" "--platform"
assert_arg "$work/docker-aarch64.args" "linux/arm64"
assert_arg "$work/docker-aarch64.args" "menci/archlinuxarm:latest"

run_make apple-validation --target aarch64/apple-silicon --apple-media-validation-build
assert_arg "$work/docker-apple-validation.args" "OMARCHY_ARCH=aarch64"
assert_arg "$work/docker-apple-validation.args" "OMARCHY_MEDIA_TARGET=aarch64/apple-silicon"
assert_arg "$work/docker-apple-validation.args" "OMARCHY_APPLE_MEDIA_BUILD_PROBE=1"
[[ -f $ROOT/release/omarchy-test-aarch64-apple-silicon-quattro.iso ]]
[[ -f $ROOT/release/omarchy-test-aarch64-apple-silicon-quattro.iso.apple-media-evidence.json ]]

set +e
invalid_output=$("$ROOT/bin/omarchy-iso-make" --arch sparc 2>&1)
invalid_status=$?
set -e

(( invalid_status != 0 ))
[[ $invalid_output == *"Unsupported architecture: sparc"* ]]

set +e
unsupported_output=$(HOME="$work/home" PATH="$work/bin:$PATH" \
  "$ROOT/bin/omarchy-iso-make" --arch aarch64 --edge --keep-pkg-cache --no-boot-offer 2>&1)
unsupported_status=$?
set -e

(( unsupported_status != 0 ))
[[ $unsupported_output == *"requires the pinned quattro/stable package snapshots"* ]]

set +e
apple_output=$(HOME="$work/home" PATH="$work/bin:$PATH" \
  "$ROOT/bin/omarchy-iso-make" --target aarch64/apple-silicon \
  --keep-pkg-cache --no-boot-offer 2>&1)
apple_status=$?
set -e

(( apple_status != 0 ))
[[ $apple_output == *"defined but not buildable yet"* ]]
[[ $apple_output == *"Refusing to substitute the generic aarch64 media target"* ]]

set +e
probe_output=$(HOME="$work/home" PATH="$work/bin:$PATH" \
  "$ROOT/bin/omarchy-iso-make" --apple-media-validation-build \
  --keep-pkg-cache --no-boot-offer 2>&1)
probe_status=$?
set -e

(( probe_status != 0 ))
[[ $probe_output == *"requires --target aarch64/apple-silicon"* ]]

set +e
conflict_output=$("$ROOT/bin/omarchy-iso-make" --arch x86_64 \
  --target aarch64/apple-silicon 2>&1)
conflict_status=$?
set -e

(( conflict_status != 0 ))
[[ $conflict_output == *"conflicts with media target"* ]]

echo "Architecture selector tests passed"
