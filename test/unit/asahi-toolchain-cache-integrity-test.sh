#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
cleanup() {
  find "$work" -type d -exec chmod u+w {} + 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

stage_inputs=$work/stage-inputs
lock=$stage_inputs/builder-toolchain/source-lock.json
source_manifest=$stage_inputs/builder-toolchain/source-manifest.json
fake_docker_log=$work/docker.log
image_id=sha256:$(printf '4%.0s' {1..64})
image_size=12345
inventory='bash 5.2.037-1'
inventory_sha=$(printf '%s\n' "$inventory" |
  python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
sync_digest=$(printf '5%.0s' {1..64})
sync_databases="$sync_digest  /var/lib/pacman/sync/core.db"

python3 "$ROOT/builder/asahi_stage_inputs.py" generate \
  --repo-root "$ROOT" \
  --spec "$ROOT/builder/asahi-stage-inputs.json" \
  --build-lock "$ROOT/builder/asahi-build-lock.json" \
  --mode qualification \
  --output-root "$stage_inputs"

declared_inputs=$(OMARCHY_BUILD_MODE=qualification \
  OMARCHY_ASAHI_TOOLCHAIN_LOCK="$lock" \
  OMARCHY_ASAHI_TOOLCHAIN_SOURCE_MANIFEST="$source_manifest" \
  "$ROOT/builder/ensure-asahi-toolchain-image.sh" --print-declared-inputs)
declared_input_digest=$(printf '%s' "$declared_inputs" |
  python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
actual_inputs=$(jq -cnS \
  --argjson declared "$declared_inputs" \
  --arg package_inventory_sha256 "$inventory_sha" \
  --arg package_inventory "$inventory" \
  --arg sync_databases "$sync_databases" '
    $declared + {
      package_inventory_sha256: $package_inventory_sha256,
      package_inventory: ($package_inventory | split("\n") | map(select(length > 0))),
      synchronized_database_digests: ($sync_databases | split("\n") | map(select(length > 0)))
    }
  ')
checkpoint_identity=$(printf '%s' "$actual_inputs" |
  python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())')
lock_sha=$(python3 -c \
  'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
  "$lock")

mkdir "$work/fake-bin"
cat >"$work/fake-bin/docker" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >>"$OMARCHY_FAKE_DOCKER_LOG"
if [ -n "${OMARCHY_FAKE_REPLACE_MANIFEST:-}" ] &&
  [ ! -e "$OMARCHY_FAKE_REPLACE_MANIFEST.done" ]; then
  chmod u+w "$OMARCHY_FAKE_REPLACE_MANIFEST"
  printf '%s\n' '{"forged":true}' >"$OMARCHY_FAKE_REPLACE_MANIFEST"
  touch "$OMARCHY_FAKE_REPLACE_MANIFEST.done"
fi
if [ "$1 $2" = "image inspect" ]; then
  case "$4" in
    '{{.Id}}') printf '%s\n' "$OMARCHY_FAKE_IMAGE_ID" ;;
    '{{.Size}}') printf '%s\n' "$OMARCHY_FAKE_IMAGE_SIZE" ;;
    *) printf '%s\n' "$OMARCHY_FAKE_LOCK_SHA" ;;
  esac
  exit 0
fi
case "$*" in
  *source-lock.sha256*) printf '%s\n' "$OMARCHY_FAKE_LOCK_SHA" ;;
  *packages.txt*sha256sum*|*sha256sum*packages.txt*)
    printf '%s  %s\n' "$OMARCHY_FAKE_INVENTORY_SHA" \
      /usr/share/omarchy-asahi-toolchain/packages.txt
    ;;
  *packages.txt*) printf '%s\n' "$OMARCHY_FAKE_INVENTORY" ;;
  *'/var/lib/pacman/sync'*) printf '%s\n' "$OMARCHY_FAKE_SYNC_DATABASES" ;;
  *) exit 98 ;;
esac
SH
chmod +x "$work/fake-bin/docker"

# completed_at, elapsed_seconds, and environment were added 2026-08-30. The real
# producer has always written them; this fixture omitted them, which the inline
# jq gate tolerated because it only checked the fields it named. Metadata
# validation now runs through builder/asahi_toolchain_metadata.py, which closes
# over the manifest key set, so the fixture has to match what is really written.
write_manifest() {
  local cache_root=$1
  local directory_identity=$2
  local recorded_digest=$3
  local recorded_identity=$4
  local manifest=$cache_root/builder-toolchain/$directory_identity/manifest.json

  mkdir -p "${manifest%/*}"
  jq -nS \
    --argjson declared_inputs "$declared_inputs" \
    --arg declared_input_digest "$recorded_digest" \
    --argjson actual_inputs "$actual_inputs" \
    --arg checkpoint_identity "$recorded_identity" \
    --arg image_id "$image_id" \
    --argjson image_size "$image_size" \
    --arg package_inventory_sha256 "$inventory_sha" '
      {
        schema_version: 2,
        stage: "builder-toolchain",
        mode: "shared",
        declared_inputs: $declared_inputs,
        declared_input_digest: $declared_input_digest,
        actual_inputs: $actual_inputs,
        checkpoint_identity: $checkpoint_identity,
        output: {
          image_id: $image_id,
          size_bytes: $image_size,
          package_inventory_sha256: $package_inventory_sha256
        },
        validation: {result: "passed"},
        completed_at: "2026-08-29T00:00:00Z",
        elapsed_seconds: 1,
        cache_hit: false,
        immutable: true,
        environment: "OMARCHY_ASAHI_TOOLCHAIN_PREPARED=1"
      }
    ' >"$manifest"
  chmod 0444 "$manifest"
  chmod 0555 "${manifest%/*}"
}

run_toolchain() {
  local cache_root=$1
  local output=$2
  local error=$3
  local selected_source_manifest=${4:-$source_manifest}

  PATH="$work/fake-bin:$PATH" \
  OMARCHY_BUILD_MODE=qualification \
  OMARCHY_ASAHI_CHECKPOINT_ROOT="$cache_root" \
  OMARCHY_ASAHI_TOOLCHAIN_LOCK="$lock" \
  OMARCHY_ASAHI_TOOLCHAIN_SOURCE_MANIFEST="$selected_source_manifest" \
  OMARCHY_FAKE_DOCKER_LOG="$fake_docker_log" \
  OMARCHY_FAKE_IMAGE_ID="$image_id" \
  OMARCHY_FAKE_IMAGE_SIZE="$image_size" \
  OMARCHY_FAKE_LOCK_SHA="$lock_sha" \
  OMARCHY_FAKE_INVENTORY="$inventory" \
  OMARCHY_FAKE_INVENTORY_SHA="$inventory_sha" \
  OMARCHY_FAKE_SYNC_DATABASES="$sync_databases" \
  OMARCHY_FAKE_REPLACE_MANIFEST="${OMARCHY_FAKE_REPLACE_MANIFEST:-}" \
    "$ROOT/builder/ensure-asahi-toolchain-image.sh" >"$output" 2>"$error"
}

valid_root=$work/valid
write_manifest \
  "$valid_root" "$checkpoint_identity" \
  "$declared_input_digest" "$checkpoint_identity"
run_toolchain "$valid_root" "$work/valid.out" "$work/valid.error"
grep -Fxq "$image_id" "$work/valid.out"
docker_calls_after_control=$(wc -l <"$fake_docker_log")
(( docker_calls_after_control > 0 ))

stale_source_manifest=$work/stale-source-manifest.json
jq --arg stale_binding "$(printf 'b%.0s' {1..64})" \
  '.producer_binding_identity = $stale_binding' \
  "$source_manifest" >"$stale_source_manifest"
chmod 0444 "$stale_source_manifest"
if run_toolchain \
  "$valid_root" "$work/stale-source.out" "$work/stale-source.error" \
  "$stale_source_manifest"; then
  echo "stale caller-selected source bundle unexpectedly passed" >&2
  exit 1
fi
grep -Fq \
  'selected toolchain source bundle does not match current repository declarations' \
  "$work/stale-source.error"
[[ $(wc -l <"$fake_docker_log") == "$docker_calls_after_control" ]]

assert_rejected_before_docker() {
  local case_name=$1
  local cache_root=$2
  local output=$work/$case_name.out
  local error=$work/$case_name.error

  if run_toolchain "$cache_root" "$output" "$error"; then
    echo "$case_name toolchain manifest unexpectedly passed" >&2
    exit 1
  fi
  grep -Fq \
    'qualification requires a previously verified toolchain checkpoint' \
    "$error"
  [[ $(wc -l <"$fake_docker_log") == "$docker_calls_after_control" ]]
}

tampered_digest_root=$work/tampered-digest
write_manifest \
  "$tampered_digest_root" "$checkpoint_identity" \
  "$(printf '8%.0s' {1..64})" "$checkpoint_identity"
assert_rejected_before_docker tampered-digest "$tampered_digest_root"

tampered_identity=$(printf '9%.0s' {1..64})
tampered_identity_root=$work/tampered-identity
write_manifest \
  "$tampered_identity_root" "$tampered_identity" \
  "$declared_input_digest" "$tampered_identity"
assert_rejected_before_docker tampered-identity "$tampered_identity_root"

wrong_directory_identity=$(printf 'a%.0s' {1..64})
wrong_basename_root=$work/wrong-basename
write_manifest \
  "$wrong_basename_root" "$wrong_directory_identity" \
  "$declared_input_digest" "$checkpoint_identity"
assert_rejected_before_docker wrong-basename "$wrong_basename_root"

writable_directory_root=$work/writable-directory
write_manifest \
  "$writable_directory_root" "$checkpoint_identity" \
  "$declared_input_digest" "$checkpoint_identity"
chmod 0755 "$writable_directory_root/builder-toolchain/$checkpoint_identity"
assert_rejected_before_docker writable-directory "$writable_directory_root"

unsafe_ancestor_root=$work/unsafe-ancestor
write_manifest \
  "$unsafe_ancestor_root" "$checkpoint_identity" \
  "$declared_input_digest" "$checkpoint_identity"
chmod 0777 "$unsafe_ancestor_root/builder-toolchain"
assert_rejected_before_docker unsafe-ancestor "$unsafe_ancestor_root"

replacement_root=$work/replacement
write_manifest \
  "$replacement_root" "$checkpoint_identity" \
  "$declared_input_digest" "$checkpoint_identity"
replacement_manifest=$replacement_root/builder-toolchain/$checkpoint_identity/manifest.json
OMARCHY_FAKE_REPLACE_MANIFEST=$replacement_manifest \
  run_toolchain "$replacement_root" "$work/replacement.out" "$work/replacement.error"
grep -Fxq "$image_id" "$work/replacement.out"
jq -e '.forged == true' "$replacement_manifest" >/dev/null

echo "ok - toolchain cache verifies identities and uses one immutable descriptor-bound snapshot"
