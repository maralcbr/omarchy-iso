#!/bin/bash
#
# Parity characterization, producer surface (a). Added 2026-08-29 (plan Phase B).
#
# Drives the shared schema-2 fixture family from test/unit/asahi_schema2_fixtures.py
# through verify_cached_manifest in builder/ensure-asahi-toolchain-image.sh, with
# a fake docker on PATH so the image-inspect and image-run probes are
# controllable. No real docker is ever invoked and no image is ever built.
#
# Since 2026-08-30 the producer's metadata decisions come from
# builder/asahi_toolchain_metadata.py; only the docker-state probes remain in
# bash. The planner and projection surfaces for the same fixtures live in
# test/unit/test_asahi_schema2_manifest_parity.py, whose module docstring carries
# the full three-surface table. Measured here:
#
#   valid-baseline                 accept
#   unknown-extra-field            reject
#   manifest-cache-hit             reject
#   tampered-compatibility-reason  reject
#   tampered-compatibility-lock    reject
#   docker-image-absent            reject   (the only surface that can see this)

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
cleanup() {
  find "$work" -type d -exec chmod u+w {} + 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

fixtures=$ROOT/test/unit/asahi_schema2_fixtures.py
stage_inputs=$work/stage-inputs
lock=$stage_inputs/builder-toolchain/source-lock.json
source_manifest=$stage_inputs/builder-toolchain/source-manifest.json
legacy_lock=$ROOT/builder/asahi-build-lock.json
fake_docker_log=$work/docker.log
: >"$fake_docker_log"

image_id=sha256:$(printf '4%.0s' {1..64})
image_size=12345
inventory='bash 5.2.037-1'
sha256_stdin() {
  python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}
sha256_file() {
  python3 -c \
    'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$1"
}
inventory_sha=$(printf '%s\n' "$inventory" | sha256_stdin)
sync_digest=$(printf '5%.0s' {1..64})
sync_databases="$sync_digest  /var/lib/pacman/sync/core.db"

python3 "$ROOT/builder/asahi_stage_inputs.py" generate \
  --repo-root "$ROOT" \
  --spec "$ROOT/builder/asahi-stage-inputs.json" \
  --build-lock "$legacy_lock" \
  --mode qualification \
  --output-root "$stage_inputs"

declared_inputs=$(OMARCHY_BUILD_MODE=qualification \
  OMARCHY_ASAHI_TOOLCHAIN_LOCK="$lock" \
  OMARCHY_ASAHI_TOOLCHAIN_SOURCE_MANIFEST="$source_manifest" \
  "$ROOT/builder/ensure-asahi-toolchain-image.sh" --print-declared-inputs)
declared_input_digest=$(printf '%s' "$declared_inputs" | sha256_stdin)
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
checkpoint_identity=$(printf '%s' "$actual_inputs" | sha256_stdin)
lock_sha=$(sha256_file "$lock")
legacy_lock_sha=$(sha256_file "$legacy_lock")

# The baseline carries a compatibility block, so the whole fixture family --
# including both compatibility mutations -- reaches every surface. A real
# rekeyed manifest binds source_lock_sha256 to the legacy lock file on disk and
# target_lock_sha256 to the stage source lock, so the baseline does the same.
# With compatibility present the producer expects the image to embed the legacy
# lock digest, which is what the fake docker reports.
base_manifest=$work/base-manifest.json
jq -nS \
  --argjson declared_inputs "$declared_inputs" \
  --arg declared_input_digest "$declared_input_digest" \
  --argjson actual_inputs "$actual_inputs" \
  --arg checkpoint_identity "$checkpoint_identity" \
  --arg image_id "$image_id" \
  --argjson image_size "$image_size" \
  --arg package_inventory_sha256 "$inventory_sha" \
  --arg source_lock "$legacy_lock_sha" \
  --arg target_lock "$lock_sha" '
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
      environment: "OMARCHY_ASAHI_TOOLCHAIN_PREPARED=1",
      compatibility: {
        schema_version: 1,
        reason: "stage-input-granularity-v1",
        source_checkpoint_identity: "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        source_lock_sha256: $source_lock,
        target_lock_sha256: $target_lock
      }
    }
  ' >"$base_manifest"

mkdir "$work/fake-bin"
cat >"$work/fake-bin/docker" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >>"$OMARCHY_FAKE_DOCKER_LOG"
if [ "$1 $2" = "image inspect" ]; then
  if [ -n "${OMARCHY_FAKE_IMAGE_MISSING:-}" ]; then
    printf 'Error: No such image\n' >&2
    exit 1
  fi
  case "$4" in
    '{{.Id}}') printf '%s\n' "$OMARCHY_FAKE_IMAGE_ID" ;;
    '{{.Size}}') printf '%s\n' "$OMARCHY_FAKE_IMAGE_SIZE" ;;
    *) printf '%s\n' "$OMARCHY_FAKE_LOCK_SHA" ;;
  esac
  exit 0
fi
if [ -n "${OMARCHY_FAKE_IMAGE_MISSING:-}" ]; then
  printf 'Error: No such image\n' >&2
  exit 1
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

# Build one cache root per fixture, holding that fixture's manifest.
install_fixture() {
  local name=$1
  local cache_root=$work/$name/cache
  local checkpoint=$cache_root/builder-toolchain/$checkpoint_identity

  mkdir -p "$checkpoint"
  python3 "$fixtures" "$name" <"$base_manifest" |
    jq -S . >"$checkpoint/manifest.json"
  chmod 0444 "$checkpoint/manifest.json"
  chmod 0555 "$checkpoint"
  printf '%s\n' "$cache_root"
}

run_producer() {
  local name=$1
  local cache_root=$2
  local image_missing=""

  if [[ $name == docker-image-absent ]]; then
    image_missing=1
  fi
  PATH="$work/fake-bin:$PATH" \
  OMARCHY_BUILD_MODE=qualification \
  OMARCHY_ASAHI_CHECKPOINT_ROOT="$cache_root" \
  OMARCHY_ASAHI_TOOLCHAIN_LOCK="$lock" \
  OMARCHY_ASAHI_TOOLCHAIN_SOURCE_MANIFEST="$source_manifest" \
  OMARCHY_FAKE_DOCKER_LOG="$fake_docker_log" \
  OMARCHY_FAKE_IMAGE_ID="$image_id" \
  OMARCHY_FAKE_IMAGE_SIZE="$image_size" \
  OMARCHY_FAKE_LOCK_SHA="$legacy_lock_sha" \
  OMARCHY_FAKE_INVENTORY="$inventory" \
  OMARCHY_FAKE_INVENTORY_SHA="$inventory_sha" \
  OMARCHY_FAKE_SYNC_DATABASES="$sync_databases" \
  OMARCHY_FAKE_IMAGE_MISSING="$image_missing" \
    "$ROOT/builder/ensure-asahi-toolchain-image.sh" \
    >"$work/$name.out" 2>"$work/$name.error"
}

assert_accepted() {
  local name=$1
  local cache_root
  cache_root=$(install_fixture "$name")

  if ! run_producer "$name" "$cache_root"; then
    echo "producer unexpectedly rejected fixture: $name" >&2
    cat "$work/$name.error" >&2
    exit 1
  fi
  grep -Fxq "$image_id" "$work/$name.out"
}

assert_rejected() {
  local name=$1
  local cache_root
  cache_root=$(install_fixture "$name")

  if run_producer "$name" "$cache_root"; then
    echo "producer unexpectedly accepted fixture: $name" >&2
    exit 1
  fi
  # Qualification never falls back to building, so a refused cached manifest
  # surfaces as the qualification gate rather than a docker build.
  grep -Fq \
    'qualification requires a previously verified toolchain checkpoint' \
    "$work/$name.error"
}

assert_accepted valid-baseline

# Aligned 2026-08-30. The producer's inline jq gate asserted only the fields it
# named and did not close over the manifest key set, so an undeclared key used
# to ride through here while the planner rejected the same document. Metadata
# validation now runs through the canonical module, which closes over the key
# set on every surface.
assert_rejected unknown-extra-field

assert_rejected manifest-cache-hit
assert_rejected tampered-compatibility-reason

# compatibility.source_lock_sha256 is bound to the digest of
# builder/asahi-build-lock.json on disk. The planner used to check only that the
# value was a well-formed sha256 and accepted this fixture; it now binds the
# file too.
assert_rejected tampered-compatibility-lock

# The manifest is byte-identical to the baseline; only the container runtime
# differs. This is the sole surface that can observe it.
docker_calls_before=$(wc -l <"$fake_docker_log")
assert_rejected docker-image-absent
(( $(wc -l <"$fake_docker_log") > docker_calls_before ))

echo "ok - schema-2 fixture family characterized against the producer surface"
