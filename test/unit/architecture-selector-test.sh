#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(realpath "$(mktemp -d)")
apple_package_filename=$(jq -er '.package_filename' \
  "$ROOT/builder/products/omarchy-mx-mac.json")
# Every artifact this test produces lands in an isolated release root; the
# real release directory must never appear in a test's write or cleanup set —
# the product filename is real, so a shared root deletes real releases.
export OMARCHY_BUILD_RELEASE_ROOT=$work/release
mkdir -p "$OMARCHY_BUILD_RELEASE_ROOT"
cleanup() {
  chmod -R u+w "$work" 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT
apple_build_image="menci/archlinuxarm@sha256:1245992a2b371b5aeeede7dae44937ab29dc446e9e77abe263b99b02e5c1813d"
toolchain_image="sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
# shellcheck source=/dev/null
source "$ROOT/builder/sha256-adapter.sh"

mkdir -p "$work/bin" "$work/home"

cat >"$work/bin/docker" <<'STUB'
#!/bin/bash
set -euo pipefail

if [[ ${1:-} == build ]]; then
  exit 0
fi
if [[ ${1:-} == image && ${2:-} == inspect ]]; then
  if [[ $* == *source-lock-sha256* ]]; then
    printf '%s\n' "$TEST_TOOLCHAIN_LOCK_SHA256"
  elif [[ $* == *'{{.Size}}'* ]]; then
    printf '%s\n' "$TEST_TOOLCHAIN_IMAGE_SIZE"
  else
    printf '%s\n' "$TEST_TOOLCHAIN_IMAGE"
  fi
  exit 0
fi
if [[ ${1:-} == run ]]; then
  if [[ $* == *'cat /usr/share/omarchy-asahi-toolchain/packages.txt'* ]]; then
    cat "$TEST_TOOLCHAIN_PACKAGES"
    exit 0
  fi
  if [[ $* == *'cat /usr/share/omarchy-asahi-toolchain/source-lock.sha256'* ]]; then
    printf '%s\n' "$TEST_TOOLCHAIN_LOCK_SHA256"
    exit 0
  fi
  if [[ $* == *'sha256sum /usr/share/omarchy-asahi-toolchain/packages.txt'* ]]; then
    printf '%s  %s\n' "$TEST_TOOLCHAIN_PACKAGES_SHA256" \
      /usr/share/omarchy-asahi-toolchain/packages.txt
    exit 0
  fi
  if [[ $* == *'cut -d'*packages.sha256* ]]; then
    printf '%s\n' "$TEST_TOOLCHAIN_PACKAGES_SHA256"
    exit 0
  fi
  if [[ $* == *'/var/lib/pacman/sync'* ]]; then
    printf '%064d  /var/lib/pacman/sync/core.db\n' 0
    exit 0
  fi
fi

printf '%s\n' "$@" >"$TEST_DOCKER_ARGS"

arch=""
media_target=""
artifact_kind=""
build_mode=""
checkpoint_policy=""
run_id=""
out=""
builder_source=""
stage_inputs_source=""
receipt_source=""
previous=""
for argument in "$@"; do
  if [[ $previous == "-e" && $argument == OMARCHY_ARCH=* ]]; then
    arch="${argument#*=}"
  elif [[ $previous == "-e" && $argument == OMARCHY_MEDIA_TARGET=* ]]; then
    media_target="${argument#*=}"
  elif [[ $previous == "-e" && $argument == OMARCHY_ARTIFACT_KIND=* ]]; then
    artifact_kind="${argument#*=}"
  elif [[ $previous == "-e" && $argument == OMARCHY_BUILD_MODE=* ]]; then
    build_mode="${argument#*=}"
  elif [[ $previous == "-e" && $argument == OMARCHY_CHECKPOINT_POLICY=* ]]; then
    checkpoint_policy="${argument#*=}"
  elif [[ $previous == "-e" && $argument == OMARCHY_BUILD_RUN_ID=* ]]; then
    run_id="${argument#*=}"
  elif [[ $previous == "-v" && $argument == *:/out/ ]]; then
    out="${argument%:/out/}"
  elif [[ $previous == "-v" && $argument == *:/builder:ro ]]; then
    builder_source="${argument%:/builder:ro}"
  elif [[ $previous == "-v" && $argument == *:/omarchy-asahi-stage-inputs:ro ]]; then
    stage_inputs_source="${argument%:/omarchy-asahi-stage-inputs:ro}"
  elif [[ $previous == "-v" && $argument == *:/omarchy-asahi-stage-admission-receipt.json:ro ]]; then
    receipt_source="${argument%:/omarchy-asahi-stage-admission-receipt.json:ro}"
  fi
  previous="$argument"
done

[[ -n $arch && -n $media_target && -n $artifact_kind && -n $out ]]
if [[ $artifact_kind == "asahi-os-package" ]]; then
  [[ -n $builder_source && -n $stage_inputs_source && -n $receipt_source && -n $run_id ]]
  bytecode_path=$(find "$builder_source" \
    \( -type d -name __pycache__ -o -type f \
      \( -name '*.pyc' -o -name '*.pyo' \) \) -print -quit)
  [[ -z $bytecode_path ]] || {
    echo "immutable builder snapshot retained bytecode: $bytecode_path" >&2
    exit 94
  }
  adapted_admission=$(mktemp)
  trap 'rm -f "$adapted_admission"' EXIT
  sed \
    -e "s#/builder/#$builder_source/#g" \
    -e "s#/omarchy-asahi-stage-admission-receipt.json#$receipt_source#g" \
    "$builder_source/asahi-early-checkpoint-admission.sh" \
    >"$adapted_admission"
  # shellcheck disable=SC1090
  source "$adapted_admission"
  if [[ $build_mode == diagnostic ]]; then
    validate_current_early_checkpoint_receipt verified-package-cache \
      "$stage_inputs_source" diagnostic
    validate_current_early_checkpoint_receipt offline-repository-database \
      "$stage_inputs_source" diagnostic
  else
    if validate_current_early_checkpoint_receipt verified-package-cache \
      "$stage_inputs_source" qualification 2>/dev/null; then
      echo "qualification accepted a metadata-only host receipt" >&2
      exit 95
    fi
  fi
  [[ -n $checkpoint_policy ]]
  if [[ $build_mode == qualification ]]; then
    package_filename=$(jq -er '.package_filename' \
      "$builder_source/products/omarchy-mx-mac.json")
    touch "$out/$package_filename"
    printf '{}\n' >"$out/$package_filename.asahi-package-evidence.json"
    printf '{}\n' >"$out/$package_filename.installer-data.json"
    mkdir -p "$out/build-evidence/$run_id"
    printf '{"result":"passed","run_id":"%s","package_filename":"%s"}\n' \
      "$run_id" "$package_filename" \
      >"$out/build-evidence/$run_id/release-publication.json"
  fi
  exit 0
fi
touch "$out/omarchy-test-$arch.iso"
if [[ $media_target == "aarch64/apple-silicon" ]]; then
  printf 'static evidence\n' >"$out/omarchy-test-$arch.iso.apple-media-evidence.json"
  printf 'build environment\n' >"$out/omarchy-test-$arch.iso.apple-build-environment.txt"
fi
STUB
cat >"$work/bin/git" <<'STUB'
#!/bin/bash
set -euo pipefail
if [[ ${1:-} == submodule && ${2:-} == update ]]; then
  exit 0
fi
if [[ $* == "rev-parse HEAD" ]]; then
  printf '%s\n' "$TEST_ISO_SOURCE_COMMIT"
  exit 0
fi
if [[ $* == "-C archiso rev-parse HEAD" ]]; then
  printf '%s\n' "$TEST_ARCHISO_SOURCE_COMMIT"
  exit 0
fi
exec "$TEST_REAL_GIT" "$@"
STUB
chmod +x "$work/bin/docker" "$work/bin/git"

jq -r '.builder.toolchain_packages[] + " 1"' \
  "$ROOT/builder/asahi-build-lock.json" >"$work/toolchain-packages.txt"
export TEST_TOOLCHAIN_IMAGE="$toolchain_image"
export TEST_TOOLCHAIN_IMAGE_SIZE=1925543142
export TEST_TOOLCHAIN_PACKAGES="$work/toolchain-packages.txt"
TEST_REAL_GIT=$(command -v git)
TEST_ISO_SOURCE_COMMIT=$(git rev-parse HEAD)
TEST_ARCHISO_SOURCE_COMMIT=$(git -C archiso rev-parse HEAD)
export TEST_REAL_GIT TEST_ISO_SOURCE_COMMIT TEST_ARCHISO_SOURCE_COMMIT
TEST_TOOLCHAIN_PACKAGES_SHA256=$(sha256_file "$TEST_TOOLCHAIN_PACKAGES")
export TEST_TOOLCHAIN_PACKAGES_SHA256
python3 "$ROOT/builder/asahi_stage_inputs.py" generate \
  --repo-root "$ROOT" \
  --spec "$ROOT/builder/asahi-stage-inputs.json" \
  --build-lock "$ROOT/builder/asahi-build-lock.json" \
  --mode qualification \
  --output-root "$work/prepared-stage-inputs"
prepared_toolchain=$work/prepared-stage-inputs/builder-toolchain
TEST_TOOLCHAIN_LOCK_SHA256=$(sha256_file "$prepared_toolchain/source-lock.json")
export TEST_TOOLCHAIN_LOCK_SHA256
toolchain_source_identity=$(jq -er '.source_identity' \
  "$prepared_toolchain/source-manifest.json")
toolchain_producer_binding_identity=$(jq -er '.producer_binding_identity' \
  "$prepared_toolchain/source-manifest.json")
toolchain_source_manifest_sha256=$(sha256_file \
  "$prepared_toolchain/source-manifest.json")
toolchain_containerfile_sha256=$(sha256_file \
  "$ROOT/builder/asahi-toolchain.Containerfile")
toolchain_script_sha256=$(sha256_file \
  "$ROOT/builder/ensure-asahi-toolchain-image.sh")
toolchain_base_image=$(jq -er '.inputs.builder.base_image' \
  "$prepared_toolchain/source-lock.json")
toolchain_declared_inputs=$(jq -cnS \
  --arg base_image "$toolchain_base_image" \
  --arg source_lock_sha256 "$TEST_TOOLCHAIN_LOCK_SHA256" \
  --arg containerfile_sha256 "$toolchain_containerfile_sha256" \
  --arg script_sha256 "$toolchain_script_sha256" \
  --arg source_identity "$toolchain_source_identity" \
  --arg producer_binding_identity "$toolchain_producer_binding_identity" \
  --arg source_manifest_sha256 "$toolchain_source_manifest_sha256" \
  --argjson toolchain_packages \
    "$(jq -c '.inputs.builder.toolchain_packages' "$prepared_toolchain/source-lock.json")" \
  '{base_image: $base_image, source_lock_sha256: $source_lock_sha256,
    containerfile_sha256: $containerfile_sha256,
    script_sha256: $script_sha256,
    source: {omarchy_iso_stage: $source_identity,
      omarchy_iso_producer: $producer_binding_identity,
      manifest_sha256: $source_manifest_sha256},
    toolchain_packages: $toolchain_packages}')
toolchain_declared_digest=$(printf '%s' "$toolchain_declared_inputs" | sha256_stdin)
toolchain_inventory=$(jq -Rn '[inputs | select(length > 0)]' \
  <"$TEST_TOOLCHAIN_PACKAGES")
toolchain_sync_digest=$(printf '%064d  /var/lib/pacman/sync/core.db' 0)
toolchain_actual_inputs=$(jq -cnS \
  --argjson declared "$toolchain_declared_inputs" \
  --arg inventory_sha256 "$TEST_TOOLCHAIN_PACKAGES_SHA256" \
  --argjson package_inventory "$toolchain_inventory" \
  --arg sync_digest "$toolchain_sync_digest" \
  '$declared + {package_inventory_sha256: $inventory_sha256,
    package_inventory: $package_inventory,
    synchronized_database_digests: [$sync_digest]}')
toolchain_checkpoint_identity=$(printf '%s' "$toolchain_actual_inputs" | \
  sha256_stdin)
toolchain_checkpoint=$work/home/.cache/omarchy/asahi-checkpoints/builder-toolchain/$toolchain_checkpoint_identity
mkdir -p "$toolchain_checkpoint"
jq -nS \
  --argjson declared_inputs "$toolchain_declared_inputs" \
  --arg declared_input_digest "$toolchain_declared_digest" \
  --argjson actual_inputs "$toolchain_actual_inputs" \
  --arg checkpoint_identity "$toolchain_checkpoint_identity" \
  --arg image_id "$TEST_TOOLCHAIN_IMAGE" \
  --argjson image_size "$TEST_TOOLCHAIN_IMAGE_SIZE" \
  --arg inventory_sha256 "$TEST_TOOLCHAIN_PACKAGES_SHA256" \
  --argjson package_inventory "$toolchain_inventory" \
  --arg sync_digest "$toolchain_sync_digest" \
  '{schema_version: 2, stage: "builder-toolchain", mode: "shared",
    declared_inputs: $declared_inputs,
    declared_input_digest: $declared_input_digest,
    actual_inputs: $actual_inputs,
    checkpoint_identity: $checkpoint_identity,
    output: {image_id: $image_id, size_bytes: $image_size,
      package_inventory_sha256: $inventory_sha256},
    validation: {result: "passed"}, completed_at: "2026-08-29T00:00:00Z",
    elapsed_seconds: 0, cache_hit: false, immutable: true,
    environment: "OMARCHY_ASAHI_TOOLCHAIN_PREPARED=1"}' \
  >"$toolchain_checkpoint/manifest.json"
chmod 0444 "$toolchain_checkpoint/manifest.json"
chmod 0555 "$toolchain_checkpoint"

# Keep this wrapper test fake-only. The pinned payload's real hash/size are
# covered by test_pinned_node_cache.py; here we only need a private mount path
# to prove host control flow and Docker argument selection.
TEST_REAL_PYTHON=$(command -v python3)
export TEST_REAL_PYTHON
python3() {
  if [[ ${1:-} == */pinned-node-cache.py ]]; then
    shift
    local operation=${1:-}
    shift || true
    if [[ $operation == snapshot ]]; then
      local destination_root="" filename=""
      while (( $# )); do
        case $1 in
        --destination-root) destination_root=$2; shift 2 ;;
        --filename) filename=$2; shift 2 ;;
        *) shift ;;
        esac
      done
      [[ -d $destination_root && -n $filename ]]
      touch "$destination_root/$filename"
      chmod 0444 "$destination_root/$filename"
    fi
    return 0
  fi
  "$TEST_REAL_PYTHON" "$@"
}
export -f python3

run_make() {
  local label="$1"
  shift
  export TEST_DOCKER_ARGS="$work/docker-$label.args"
  OMARCHY_BUILD_RUN_ID="architecture-test-$label" \
    HOME="$work/home" PATH="$work/bin:$PATH" \
    "$BASH" "$ROOT/bin/omarchy-iso-make" "$@" --keep-pkg-cache --no-cache --no-boot-offer
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
rm -f "$OMARCHY_BUILD_RELEASE_ROOT/omarchy-test-x86_64-quattro.iso"

run_make x86_64 --arch x86_64
assert_arg "$work/docker-x86_64.args" "OMARCHY_ARCH=x86_64"
assert_arg "$work/docker-x86_64.args" "OMARCHY_MEDIA_TARGET=x86_64/pc"
assert_arg "$work/docker-x86_64.args" "archlinux/archlinux:latest"
if grep -qxF -- "--platform" "$work/docker-x86_64.args"; then
  echo "x86_64 unexpectedly selected a Docker platform" >&2
  exit 1
fi
rm -f "$OMARCHY_BUILD_RELEASE_ROOT/omarchy-test-x86_64-quattro.iso"

run_make aarch64 --arch=aarch64
assert_arg "$work/docker-aarch64.args" "OMARCHY_ARCH=aarch64"
assert_arg "$work/docker-aarch64.args" "OMARCHY_MEDIA_TARGET=aarch64/generic"
assert_arg "$work/docker-aarch64.args" "--platform"
assert_arg "$work/docker-aarch64.args" "linux/arm64"
assert_arg "$work/docker-aarch64.args" "menci/archlinuxarm:latest"

run_make apple-validation --target aarch64/apple-silicon --apple-media-validation-build
assert_arg "$work/docker-apple-validation.args" "OMARCHY_ARCH=aarch64"
assert_arg "$work/docker-apple-validation.args" "OMARCHY_MEDIA_TARGET=aarch64/apple-silicon"
assert_arg "$work/docker-apple-validation.args" "OMARCHY_ARTIFACT_KIND=iso"
assert_arg "$work/docker-apple-validation.args" "OMARCHY_APPLE_MEDIA_BUILD_PROBE=1"
assert_arg "$work/docker-apple-validation.args" "OMARCHY_BUILD_IMAGE=$apple_build_image"
assert_arg "$work/docker-apple-validation.args" "$apple_build_image"
assert_arg "$work/docker-apple-validation.args" "OMARCHY_ISO_SOURCE_COMMIT=$(git rev-parse HEAD)"
assert_arg "$work/docker-apple-validation.args" "OMARCHY_ARCHISO_SOURCE_COMMIT=$(git -C archiso rev-parse HEAD)"
[[ -f $OMARCHY_BUILD_RELEASE_ROOT/omarchy-test-aarch64-apple-silicon-quattro.iso ]]
[[ -f $OMARCHY_BUILD_RELEASE_ROOT/omarchy-test-aarch64-apple-silicon-quattro.iso.apple-media-evidence.json ]]
[[ -f $OMARCHY_BUILD_RELEASE_ROOT/omarchy-test-aarch64-apple-silicon-quattro.iso.apple-build-environment.txt ]]

SOURCE_DATE_EPOCH=1787832096 run_make apple-package --target aarch64/apple-silicon \
  --artifact asahi-os-package
assert_arg "$work/docker-apple-package.args" "OMARCHY_ARCH=aarch64"
assert_arg "$work/docker-apple-package.args" "OMARCHY_MEDIA_TARGET=aarch64/apple-silicon"
assert_arg "$work/docker-apple-package.args" "OMARCHY_ARTIFACT_KIND=asahi-os-package"
assert_arg "$work/docker-apple-package.args" "OMARCHY_ASAHI_PRODUCT=/builder/products/omarchy-mx-mac.json"
assert_arg "$work/docker-apple-package.args" "OMARCHY_ASAHI_TOOLCHAIN_PREPARED=1"
assert_arg "$work/docker-apple-package.args" "OMARCHY_BUILD_MODE=qualification"
assert_arg "$work/docker-apple-package.args" "OMARCHY_CHECKPOINT_POLICY=write-only"
assert_arg "$work/docker-apple-package.args" "OMARCHY_ASAHI_REQUIRE_CACHE_HIT_THROUGH="
assert_arg "$work/docker-apple-package.args" "$toolchain_image"
grep -Eq '/omarchy-asahi-stage-admission-receipt.json:ro$' \
  "$work/docker-apple-package.args"
[[ -f $OMARCHY_BUILD_RELEASE_ROOT/$apple_package_filename ]]
[[ -f $OMARCHY_BUILD_RELEASE_ROOT/$apple_package_filename.asahi-package-evidence.json ]]
[[ -f $OMARCHY_BUILD_RELEASE_ROOT/$apple_package_filename.installer-data.json ]]

run_make apple-diagnostic --target aarch64/apple-silicon \
  --artifact asahi-os-package --mode diagnostic
assert_arg "$work/docker-apple-diagnostic.args" "OMARCHY_BUILD_MODE=diagnostic"
assert_arg "$work/docker-apple-diagnostic.args" "OMARCHY_CHECKPOINT_POLICY=read-write"

set +e
qualification_reuse_output=$(HOME="$work/home" PATH="$work/bin:$PATH" \
  "$BASH" "$ROOT/bin/omarchy-iso-make" \
  --target aarch64/apple-silicon --artifact asahi-os-package \
  --checkpoint-policy read-write --keep-pkg-cache --no-boot-offer 2>&1)
qualification_reuse_status=$?
set -e
(( qualification_reuse_status != 0 ))
[[ $qualification_reuse_output == *"requires signed admission authority"* ]]

set +e
required_hit_output=$(HOME="$work/home" PATH="$work/bin:$PATH" \
  "$BASH" "$ROOT/bin/omarchy-iso-make" \
  --target aarch64/apple-silicon --artifact asahi-os-package \
  --require-cache-hit-through configured-target \
  --keep-pkg-cache --no-boot-offer 2>&1)
required_hit_status=$?
set -e
(( required_hit_status != 0 ))
[[ $required_hit_output == \
  *"Write-only qualification cannot satisfy a required cache hit"* ]]

# A valid timestamp-based pyc can contain different executable bytes while
# matching the intended source file's timestamp and size. Prove that such a
# cache is neither imported by receipt generation nor mounted into the builder.
bytecode_root=$work/bytecode-root
mkdir -p "$bytecode_root"
for source_directory in archiso bin builder configs; do
  cp -a "$ROOT/$source_directory" "$bytecode_root/$source_directory"
done
test_git_common=$(git -C "$ROOT" rev-parse --path-format=absolute \
  --git-common-dir)
test_git_worktree=$(git -C "$ROOT" rev-parse --path-format=absolute --git-dir)
cp -a "$test_git_common" "$bytecode_root/.git"
cp -a "$test_git_worktree/HEAD" "$bytecode_root/.git/HEAD"
cp -a "$test_git_worktree/index" "$bytecode_root/.git/index"
malicious_pyc=$(python3 - "$bytecode_root/builder/asahi_stage_inputs.py" <<'PYTHON'
import os
from pathlib import Path
import py_compile
import sys

source = Path(sys.argv[1])
original = source.read_bytes()
metadata = source.stat()
prefix = b'raise RuntimeError("malicious cached admission planner executed")\n#'
if len(prefix) >= len(original):
    raise SystemExit("planner source is unexpectedly too small")
malicious = prefix + (b"x" * (len(original) - len(prefix)))
source.write_bytes(malicious)
os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
pyc = Path(py_compile.compile(str(source), doraise=True))
source.write_bytes(original)
os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
print(pyc)
PYTHON
)
[[ -f $malicious_pyc ]]
set +e
malicious_import_output=$(PYTHONPATH="$bytecode_root/builder" \
  PYTHONDONTWRITEBYTECODE=1 python3 -c 'import asahi_stage_inputs' 2>&1)
malicious_import_status=$?
set -e
(( malicious_import_status != 0 ))
[[ $malicious_import_output == *"malicious cached admission planner executed"* ]]
export TEST_DOCKER_ARGS="$work/docker-bytecode.args"
SOURCE_DATE_EPOCH=1787832096 OMARCHY_BUILD_RUN_ID=architecture-test-bytecode \
  HOME="$work/home" PATH="$work/bin:$PATH" \
  "$BASH" "$bytecode_root/bin/omarchy-iso-make" \
  --target aarch64/apple-silicon --artifact asahi-os-package \
  --keep-pkg-cache --no-cache --no-boot-offer

SOURCE_DATE_EPOCH=1787832096 run_make apple-reproducible \
  --target aarch64/apple-silicon --apple-media-validation-build
assert_arg "$work/docker-apple-reproducible.args" "SOURCE_DATE_EPOCH=1787832096"
assert_arg "$work/docker-apple-reproducible.args" "$apple_build_image"

set +e
invalid_epoch_output=$(SOURCE_DATE_EPOCH=not-a-timestamp \
  HOME="$work/home" PATH="$work/bin:$PATH" \
  "$BASH" "$ROOT/bin/omarchy-iso-make" --keep-pkg-cache --no-boot-offer 2>&1)
invalid_epoch_status=$?
set -e

(( invalid_epoch_status != 0 ))
[[ $invalid_epoch_output == *"SOURCE_DATE_EPOCH must be a non-negative integer"* ]]

set +e
invalid_output=$("$BASH" "$ROOT/bin/omarchy-iso-make" --arch sparc 2>&1)
invalid_status=$?
set -e

(( invalid_status != 0 ))
[[ $invalid_output == *"Unsupported architecture: sparc"* ]]

set +e
unsupported_output=$(HOME="$work/home" PATH="$work/bin:$PATH" \
  "$BASH" "$ROOT/bin/omarchy-iso-make" --arch aarch64 --edge --keep-pkg-cache --no-boot-offer 2>&1)
unsupported_status=$?
set -e

(( unsupported_status != 0 ))
[[ $unsupported_output == *"requires the pinned quattro/stable package snapshots"* ]]

set +e
apple_output=$(HOME="$work/home" PATH="$work/bin:$PATH" \
  "$BASH" "$ROOT/bin/omarchy-iso-make" --target aarch64/apple-silicon \
  --keep-pkg-cache --no-boot-offer 2>&1)
apple_status=$?
set -e

(( apple_status != 0 ))
[[ $apple_output == *"defined but not buildable yet"* ]]
[[ $apple_output == *"Refusing to substitute the generic aarch64 media target"* ]]

set +e
probe_output=$(HOME="$work/home" PATH="$work/bin:$PATH" \
  "$BASH" "$ROOT/bin/omarchy-iso-make" --apple-media-validation-build \
  --keep-pkg-cache --no-boot-offer 2>&1)
probe_status=$?
set -e

(( probe_status != 0 ))
[[ $probe_output == *"requires --target aarch64/apple-silicon"* ]]

set +e
conflict_output=$("$BASH" "$ROOT/bin/omarchy-iso-make" --arch x86_64 \
  --target aarch64/apple-silicon 2>&1)
conflict_status=$?
set -e

(( conflict_status != 0 ))
[[ $conflict_output == *"conflicts with media target"* ]]

echo "Architecture selector tests passed"
