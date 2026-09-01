#!/usr/bin/env bash

set -euo pipefail

fail() {
  echo "ensure-asahi-toolchain-image: $*" >&2
  exit 1
}

script_directory=$({ cd "${BASH_SOURCE[0]%/*}" && pwd -P; })
repository_root=$({ cd "$script_directory/.." && pwd -P; })
# shellcheck source=sha256-adapter.sh
source "$script_directory/sha256-adapter.sh"
lock=${OMARCHY_ASAHI_TOOLCHAIN_LOCK:-$script_directory/asahi-build-lock.json}
source_manifest=${OMARCHY_ASAHI_TOOLCHAIN_SOURCE_MANIFEST:-}
containerfile=$script_directory/asahi-toolchain.Containerfile
checkpoint_root=${OMARCHY_ASAHI_CHECKPOINT_ROOT:-${XDG_CACHE_HOME:-$HOME/.cache}/omarchy/asahi-checkpoints}
builder_toolchain_root=$checkpoint_root/builder-toolchain
run_manifest=${OMARCHY_ASAHI_TOOLCHAIN_RUN_MANIFEST:-}
build_mode=${OMARCHY_BUILD_MODE:-qualification}
print_declared_inputs=false
qualification_checkpoint_failure='qualification requires a previously verified toolchain checkpoint; run a diagnostic build to bootstrap and content-lock the toolchain first'

case ${1:-} in
  "") ;;
  --print-declared-inputs) print_declared_inputs=true ;;
  *) fail "unsupported argument: $1" ;;
esac

case "$build_mode" in
  qualification|diagnostic) ;;
  *) fail "unsupported build mode: $build_mode" ;;
esac

read_selected_json_snapshot() {
  local path=$1

  python3 - "$path" <<'PY'
import hashlib
import json
import os
import stat
import sys


class UnsafeInput(Exception):
    pass


def reject_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise UnsafeInput(f"duplicate JSON key: {key}")
        value[key] = item
    return value


path = os.path.abspath(sys.argv[1])
descriptor = None
try:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o222
        or before.st_size <= 0
        or before.st_size > 4 * 1024 * 1024
    ):
        raise UnsafeInput("selected stage input is mutable or unsafe")
    chunks = []
    remaining = before.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            raise UnsafeInput("selected stage input was truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise UnsafeInput("selected stage input grew while reading")
    after = os.fstat(descriptor)
    path_after = os.stat(path, follow_symlinks=False)
    if (
        (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or (before.st_dev, before.st_ino) != (path_after.st_dev, path_after.st_ino)
        or stat.S_IMODE(after.st_mode) & 0o222
    ):
        raise UnsafeInput("selected stage input changed while being read")
    content = b"".join(chunks)
    document = json.loads(content, object_pairs_hook=reject_duplicates)
    print(
        json.dumps(
            {
                "document": document,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
except (OSError, UnicodeDecodeError, json.JSONDecodeError, UnsafeInput):
    raise SystemExit(1)
finally:
    if descriptor is not None:
        os.close(descriptor)
PY
}

lock_snapshot=$(read_selected_json_snapshot "$lock") ||
  fail "source lock is missing, mutable, or unsafe"
source_manifest_snapshot=$(read_selected_json_snapshot "$source_manifest") ||
  fail "stage-specific source manifest is missing, mutable, or unsafe"
lock_json=$(jq -ce '.document' <<<"$lock_snapshot")
source_manifest_json=$(jq -ce '.document' <<<"$source_manifest_snapshot")
[[ -f $containerfile && ! -L $containerfile ]] || fail "Containerfile is missing or unsafe"

[[ $(jq -er '.stage' <<<"$lock_json") == builder-toolchain ]] ||
  fail "source lock belongs to a different stage"
base_image=$(jq -er '.inputs.builder.base_image' <<<"$lock_json")
maximum_workers=$(jq -er '.inputs.builder.maximum_workers' <<<"$lock_json")
(( maximum_workers == 10 )) || fail "source lock must retain the ten-worker ceiling"
mapfile -t toolchain_packages < <(jq -er \
  '.inputs.builder.toolchain_packages[]' <<<"$lock_json")
(( ${#toolchain_packages[@]} > 0 )) || fail "source lock contains no toolchain packages"
toolchain_package_arguments="${toolchain_packages[*]}"

current_binding=$(PYTHONDONTWRITEBYTECODE=1 python3 - \
  "$repository_root" "$build_mode" <<'PY'
import importlib.util
import json
from pathlib import Path
import sys

repository = Path(sys.argv[1])
mode = sys.argv[2]
module_path = repository / "builder" / "asahi_stage_inputs.py"
specification = importlib.util.spec_from_file_location(
    "current_asahi_stage_inputs", module_path
)
if specification is None or specification.loader is None:
    raise SystemExit(1)
module = importlib.util.module_from_spec(specification)
specification.loader.exec_module(module)
stage_specification = module.load_specification(
    repository / "builder" / "asahi-stage-inputs.json"
)
module.validate_specification(repository, stage_specification)
build_lock = json.loads(
    (repository / "builder" / "asahi-build-lock.json").read_text()
)
stage = "builder-toolchain"
declaration = stage_specification["stages"][stage]
effective_mode = module._effective_stage_mode(stage, mode)
producer_paths = module._producer_paths(stage_specification, stage)
producer_declaration = module._producer_declaration(declaration)
source_lock = module.build_lock_projection(
    build_lock, stage, declaration, effective_mode
)
producer_identity = module._digest(
    {
        "schema_version": module.SCHEMA_VERSION,
        "stage": stage,
        "mode": effective_mode,
        "declaration_sha256": module._digest(producer_declaration),
        "source_lock": source_lock,
        "sources": module._source_records(repository, producer_paths, {}),
        "dependencies": {},
    }
)
source_manifest = module.build_stage_source_manifest(
    repository, stage, producer_paths, declaration
)
source_manifest["producer_binding_identity"] = producer_identity
source_manifest["producer_binding_mode"] = effective_mode
binding = {
    "effective_mode": effective_mode,
    "producer_binding_identity": producer_identity,
    "source_identity": source_manifest["source_identity"],
    "source_manifest": module._generated_json_file_record(
        "source-manifest.json", source_manifest, include_executable_mode=True
    ),
    "source_lock": module._generated_json_file_record(
        "source-lock.json", source_lock
    ),
}
print(json.dumps(binding, sort_keys=True, separators=(",", ":")))
PY
) || fail "current builder toolchain declaration could not be recomputed"

lock_sha256=$(jq -er '.sha256' <<<"$lock_snapshot")
containerfile_sha256=$(sha256_file "$containerfile")
script_sha256=$(sha256_file "${BASH_SOURCE[0]}")
source_identity=$(jq -er '.source_identity' <<<"$source_manifest_json")
[[ $source_identity =~ ^[0-9a-f]{64}$ ]] || fail "source identity is unsafe"
producer_binding_identity=$(jq -er '.producer_binding_identity' \
  <<<"$source_manifest_json")
[[ $producer_binding_identity =~ ^[0-9a-f]{64}$ ]] ||
  fail "producer binding identity is unsafe"
source_manifest_sha256=$(jq -er '.sha256' <<<"$source_manifest_snapshot")
jq -e \
  --arg lock_sha256 "$lock_sha256" \
  --argjson lock_size "$(jq -er '.size_bytes' <<<"$lock_snapshot")" \
  --arg source_manifest_sha256 "$source_manifest_sha256" \
  --argjson source_manifest_size "$(jq -er '.size_bytes' \
    <<<"$source_manifest_snapshot")" \
  --arg source_identity "$source_identity" \
  --arg producer_binding_identity "$producer_binding_identity" '
    .effective_mode == "shared" and
    .source_lock.sha256 == $lock_sha256 and
    .source_lock.size_bytes == $lock_size and
    .source_manifest.sha256 == $source_manifest_sha256 and
    .source_manifest.size_bytes == $source_manifest_size and
    .source_identity == $source_identity and
    .producer_binding_identity == $producer_binding_identity
  ' <<<"$current_binding" >/dev/null ||
  fail "selected toolchain source bundle does not match current repository declarations"
declared_inputs=$(jq -cnS \
  --arg base_image "$base_image" \
  --arg source_lock_sha256 "$lock_sha256" \
  --arg containerfile_sha256 "$containerfile_sha256" \
  --arg script_sha256 "$script_sha256" \
  --arg source_identity "$source_identity" \
  --arg producer_binding_identity "$producer_binding_identity" \
  --arg source_manifest_sha256 "$source_manifest_sha256" \
  --argjson toolchain_packages "$(jq -c \
    '.inputs.builder.toolchain_packages' <<<"$lock_json")" \
  '{base_image: $base_image, source_lock_sha256: $source_lock_sha256,
    containerfile_sha256: $containerfile_sha256,
    script_sha256: $script_sha256,
    source: {omarchy_iso_stage: $source_identity,
      omarchy_iso_producer: $producer_binding_identity,
      manifest_sha256: $source_manifest_sha256},
    toolchain_packages: $toolchain_packages}')
declared_input_digest=$(printf '%s' "$declared_inputs" | sha256_stdin)

if [[ $print_declared_inputs == true ]]; then
  printf '%s\n' "$declared_inputs"
  exit 0
fi

if [[ -e $checkpoint_root && ( ! -d $checkpoint_root || -L $checkpoint_root ) ]]; then
  fail "checkpoint root is not a real directory: $checkpoint_root"
fi
if [[ -e $builder_toolchain_root &&
  ( ! -d $builder_toolchain_root || -L $builder_toolchain_root ) ]]; then
  fail "builder checkpoint root is not a real directory: $builder_toolchain_root"
fi
if [[ $build_mode == qualification ]]; then
  [[ -d $checkpoint_root && ! -L $checkpoint_root &&
    -d $builder_toolchain_root && ! -L $builder_toolchain_root ]] ||
    fail "$qualification_checkpoint_failure"
else
  mkdir -p "$checkpoint_root"
  mkdir -p "$builder_toolchain_root"
fi

read_immutable_manifest_snapshot() {
  local manifest=$1

  python3 - "$checkpoint_root" "$manifest" <<'PY'
import json
import os
import re
import stat
import sys


class UnsafeManifest(Exception):
    pass


def reject_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise UnsafeManifest(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def require_owned_directory(descriptor, *, immutable):
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise UnsafeManifest("checkpoint directory is unsafe")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022:
        raise UnsafeManifest("checkpoint ancestor is group/world writable")
    if immutable and mode & 0o222:
        raise UnsafeManifest("checkpoint object directory is writable")
    return metadata


root = os.path.abspath(sys.argv[1])
manifest = os.path.abspath(sys.argv[2])
checkpoint_identity = os.path.basename(os.path.dirname(manifest))
if re.fullmatch(r"[0-9a-f]{64}", checkpoint_identity) is None:
    raise SystemExit(1)
expected = os.path.join(
    root, "builder-toolchain", checkpoint_identity, "manifest.json"
)
if os.path.normpath(manifest) != os.path.normpath(expected):
    raise SystemExit(1)

directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
no_follow = getattr(os, "O_NOFOLLOW", 0)
close_on_exec = getattr(os, "O_CLOEXEC", 0)
descriptors = []
try:
    root_fd = os.open(root, directory_flags | no_follow | close_on_exec)
    descriptors.append(root_fd)
    require_owned_directory(root_fd, immutable=False)
    builder_fd = os.open(
        "builder-toolchain",
        directory_flags | no_follow | close_on_exec,
        dir_fd=root_fd,
    )
    descriptors.append(builder_fd)
    require_owned_directory(builder_fd, immutable=False)
    checkpoint_fd = os.open(
        checkpoint_identity,
        directory_flags | no_follow | close_on_exec,
        dir_fd=builder_fd,
    )
    descriptors.append(checkpoint_fd)
    checkpoint_before = require_owned_directory(checkpoint_fd, immutable=True)
    manifest_fd = os.open(
        "manifest.json", os.O_RDONLY | no_follow | close_on_exec, dir_fd=checkpoint_fd
    )
    descriptors.append(manifest_fd)
    manifest_before = os.fstat(manifest_fd)
    if (
        not stat.S_ISREG(manifest_before.st_mode)
        or manifest_before.st_uid != os.geteuid()
        or manifest_before.st_nlink != 1
        or stat.S_IMODE(manifest_before.st_mode) & 0o222
        or manifest_before.st_size <= 0
        or manifest_before.st_size > 4 * 1024 * 1024
    ):
        raise UnsafeManifest("checkpoint manifest is mutable or unsafe")
    chunks = []
    remaining = manifest_before.st_size
    while remaining:
        chunk = os.read(manifest_fd, min(remaining, 64 * 1024))
        if not chunk:
            raise UnsafeManifest("checkpoint manifest was truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(manifest_fd, 1):
        raise UnsafeManifest("checkpoint manifest grew while reading")
    manifest_after = os.fstat(manifest_fd)
    checkpoint_after = os.fstat(checkpoint_fd)
    path_after = os.stat("manifest.json", dir_fd=checkpoint_fd, follow_symlinks=False)
    if (
        (manifest_before.st_dev, manifest_before.st_ino, manifest_before.st_size)
        != (manifest_after.st_dev, manifest_after.st_ino, manifest_after.st_size)
        or (manifest_before.st_dev, manifest_before.st_ino)
        != (path_after.st_dev, path_after.st_ino)
        or stat.S_IMODE(manifest_after.st_mode) & 0o222
        or (checkpoint_before.st_dev, checkpoint_before.st_ino)
        != (checkpoint_after.st_dev, checkpoint_after.st_ino)
        or stat.S_IMODE(checkpoint_after.st_mode) & 0o222
    ):
        raise UnsafeManifest("checkpoint changed while being read")
    document = json.loads(b"".join(chunks), object_pairs_hook=reject_duplicates)
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
except (OSError, UnicodeDecodeError, json.JSONDecodeError, UnsafeManifest):
    raise SystemExit(1)
finally:
    for descriptor in reversed(descriptors):
        os.close(descriptor)
PY
}

verify_cached_manifest() {
  local manifest=$1
  local image_id image_size actual_image_size inventory_sha
  local manifest_inventory_sha actual_inventory_sha label_lock
  local manifest_mode manifest_sync_databases actual_sync_databases
  local embedded_lock compatibility legacy_lock_path legacy_lock_sha
  local canonical_declared_inputs canonical_actual_inputs
  local recomputed_declared_digest recomputed_checkpoint_identity
  local manifest_checkpoint_identity manifest_directory_identity manifest_json

  manifest_json=$(read_immutable_manifest_snapshot "$manifest") || return 1
  canonical_declared_inputs=$(jq -ceS '.declared_inputs' <<<"$manifest_json") ||
    return 1
  canonical_actual_inputs=$(jq -ceS '.actual_inputs' <<<"$manifest_json") ||
    return 1
  recomputed_declared_digest=$(printf '%s' "$canonical_declared_inputs" |
    sha256_stdin) || return 1
  recomputed_checkpoint_identity=$(printf '%s' "$canonical_actual_inputs" |
    sha256_stdin) || return 1
  manifest_checkpoint_identity=$(jq -er '.checkpoint_identity' \
    <<<"$manifest_json") || return 1
  manifest_directory_identity=${manifest%/*}
  manifest_directory_identity=${manifest_directory_identity##*/}
  [[ $(jq -er '.declared_input_digest' <<<"$manifest_json") == \
    "$recomputed_declared_digest" ]] || return 1
  [[ $manifest_checkpoint_identity == \
    "$recomputed_checkpoint_identity" ]] || return 1
  [[ $manifest_directory_identity == \
    "$manifest_checkpoint_identity" ]] || return 1
  # Metadata validation is delegated to the canonical implementation so the
  # producer, the planner, and the projection gate cannot drift apart. The
  # snapshot is piped in rather than re-read from disk, preserving the
  # time-of-check guarantee. Everything below this call is docker state, which
  # only the producer can see and which stays here.
  python3 "$script_directory/asahi_toolchain_metadata.py" \
    validate-checkpoint-manifest --manifest - \
    --expected-declared-input-digest "$declared_input_digest" \
    --expected-source-lock-sha256 "$lock_sha256" \
    --expected-source-identity "$source_identity" \
    --expected-producer-binding-identity "$producer_binding_identity" \
    --legacy-lock "$script_directory/asahi-build-lock.json" \
    <<<"$manifest_json" >/dev/null 2>&1 || return 1
  image_id=$(jq -er '.output.image_id' <<<"$manifest_json") || return 1
  image_size=$(jq -er '.output.size_bytes' <<<"$manifest_json") || return 1
  inventory_sha=$(jq -er '.output.package_inventory_sha256' \
    <<<"$manifest_json") || return 1
  [[ $image_id =~ ^sha256:[0-9a-f]{64}$ && $image_size =~ ^[1-9][0-9]*$ &&
    $inventory_sha =~ ^[0-9a-f]{64}$ ]] || return 1
  [[ $(docker image inspect --format '{{.Id}}' "$image_id" 2>/dev/null) == "$image_id" ]] || return 1
  actual_image_size=$(docker image inspect --format '{{.Size}}' "$image_id" 2>/dev/null) || return 1
  [[ $actual_image_size == "$image_size" ]] || return 1
  embedded_lock=$lock_sha256
  compatibility=$(jq -c '.compatibility // null' <<<"$manifest_json") ||
    return 1
  if [[ $compatibility != null ]]; then
    legacy_lock_path=$script_directory/asahi-build-lock.json
    [[ -f $legacy_lock_path && ! -L $legacy_lock_path ]] || return 1
    legacy_lock_sha=$(sha256_file "$legacy_lock_path")
    # The compatibility block itself, including binding source_lock_sha256 to
    # this file's digest, was validated by the canonical call above. What
    # remains here is the lock-content comparison and choosing which lock digest
    # the image is expected to embed.
    jq -e --argjson projected "$lock_json" \
      '.builder == $projected.inputs.builder' "$legacy_lock_path" >/dev/null ||
      return 1
    embedded_lock=$legacy_lock_sha
  fi
  label_lock=$(docker image inspect \
    --format '{{index .Config.Labels "org.omarchy.mx.asahi.source-lock-sha256"}}' \
    "$image_id" 2>/dev/null) || return 1
  [[ $label_lock == "$embedded_lock" ]] || return 1
  image_lock=$(docker run --platform linux/arm64 --rm "$image_id" \
    cat /usr/share/omarchy-asahi-toolchain/source-lock.sha256 2>/dev/null) || return 1
  [[ $image_lock == "$embedded_lock" ]] || return 1
  actual_inventory_sha=$(docker run --platform linux/arm64 --rm "$image_id" \
    sha256sum /usr/share/omarchy-asahi-toolchain/packages.txt 2>/dev/null)
  actual_inventory_sha=${actual_inventory_sha%% *}
  [[ $actual_inventory_sha == "$inventory_sha" ]] || return 1
  manifest_inventory_sha=$(jq -j \
    '.actual_inputs.package_inventory | join("\n") + "\n"' \
    <<<"$manifest_json" |
    sha256_stdin)
  [[ $manifest_inventory_sha == "$inventory_sha" ]] || return 1
  manifest_sync_databases=$(jq -jr \
    '.actual_inputs.synchronized_database_digests | join("\n")' \
    <<<"$manifest_json") ||
    return 1
  actual_sync_databases=$(docker run --platform linux/arm64 --rm "$image_id" \
    /bin/bash -c \
    'find /var/lib/pacman/sync -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum' \
    2>/dev/null) || return 1
  [[ $actual_sync_databases == "$manifest_sync_databases" ]] || return 1
  jq -cnS \
    --arg image_id "$image_id" \
    --argjson manifest "$manifest_json" \
    '{image_id: $image_id, manifest: $manifest}'
}

started=$SECONDS
cached_records=()
while IFS= read -r manifest; do
  if verified_record=$(verify_cached_manifest "$manifest"); then
    cached_records+=("$verified_record")
  fi
done < <(find "$builder_toolchain_root" \
  -type f -name manifest.json -print | sort)

if (( ${#cached_records[@]} > 0 )); then
  (( ${#cached_records[@]} == 1 )) || fail "ambiguous verified toolchain checkpoints"
  cached_record=${cached_records[0]}
  cached_image=$(jq -er '.image_id' <<<"$cached_record")
  cached_manifest_json=$(jq -ce '.manifest' <<<"$cached_record")
  cached_checkpoint_identity=$(jq -er '.checkpoint_identity' \
    <<<"$cached_manifest_json")
  [[ $cached_checkpoint_identity =~ ^[0-9a-f]{64}$ ]] ||
    fail "cached toolchain checkpoint identity is unsafe"
  if [[ -n $run_manifest ]]; then
    mkdir -p "${run_manifest%/*}"
    jq -nS \
      --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --arg checkpoint_identity "$cached_checkpoint_identity" \
      --arg input_digest "$declared_input_digest" \
      --argjson output "$(jq -c '.output' <<<"$cached_manifest_json")" \
      --argjson compatibility "$(jq -c '.compatibility // null' \
        <<<"$cached_manifest_json")" \
      --argjson elapsed_seconds "$((SECONDS - started))" \
      '{schema_version: 2, stage: "builder-toolchain", mode: "shared",
        checkpoint_identity: $checkpoint_identity, input_digest: $input_digest,
        validation: {result: "passed"}, completed_at: $completed_at,
        elapsed_seconds: $elapsed_seconds, cache_hit: true, output: $output} +
        (if $compatibility == null then {} else {compatibility: $compatibility} end)' \
      >"$run_manifest"
  fi
  printf '%s\n' "$cached_image"
  exit 0
fi

if [[ $build_mode == qualification ]]; then
  fail "$qualification_checkpoint_failure"
fi

rekey_plan_root=${OMARCHY_ASAHI_REKEY_PLAN_ROOT:-}
rekey_plan=$rekey_plan_root/builder-toolchain.json
if [[ -n $rekey_plan_root && -f $rekey_plan && ! -L $rekey_plan ]]; then
  legacy_lock=$script_directory/asahi-build-lock.json
  [[ -f $legacy_lock && ! -L $legacy_lock ]] ||
    fail "legacy builder source lock required by rekey plan is unavailable"
  legacy_lock_sha=$(sha256_file "$legacy_lock")
  jq -e --argjson projected "$lock_json" \
    '.builder == $projected.inputs.builder' "$legacy_lock" >/dev/null ||
    fail "builder lock projection differs from the verified legacy builder inputs"
  jq -e \
    --arg target_source_identity "$source_identity" \
    --arg target_lock_sha256 "$lock_sha256" \
    --arg legacy_lock_sha256 "$legacy_lock_sha" \
    --arg containerfile_sha256 "$containerfile_sha256" \
    --arg base_image "$base_image" \
    --argjson toolchain_packages "$(jq -c \
      '.inputs.builder.toolchain_packages' <<<"$lock_json")" \
    'keys == ["base_image", "containerfile_sha256", "expected_output",
      "legacy_lock_sha256", "reason", "schema_version",
      "source_checkpoint_identity", "target_checkpoint_identity",
      "target_lock_sha256",
      "target_source_identity", "toolchain_packages"] and
      .schema_version == 1 and .reason == "stage-input-granularity-v1" and
      .target_source_identity == $target_source_identity and
      .target_lock_sha256 == $target_lock_sha256 and
      .legacy_lock_sha256 == $legacy_lock_sha256 and
      .containerfile_sha256 == $containerfile_sha256 and
      .base_image == $base_image and .toolchain_packages == $toolchain_packages and
      (.source_checkpoint_identity | test("^[0-9a-f]{64}$")) and
      (.target_checkpoint_identity | test("^[0-9a-f]{64}$")) and
      (.expected_output.image_id | test("^sha256:[0-9a-f]{64}$")) and
      (.expected_output.package_inventory_sha256 | test("^[0-9a-f]{64}$")) and
      (.expected_output.size_bytes | type == "number" and . > 0)' \
    "$rekey_plan" >/dev/null || fail "builder checkpoint rekey plan is stale or invalid"
  legacy_identity=$(jq -er '.source_checkpoint_identity' "$rekey_plan")
  legacy_manifest=$builder_toolchain_root/$legacy_identity/manifest.json
  legacy_manifest_json=$(read_immutable_manifest_snapshot "$legacy_manifest") ||
    fail "planned legacy builder checkpoint is missing, mutable, or unsafe"
  jq -e \
    --arg checkpoint_identity "$legacy_identity" \
    --arg legacy_lock_sha256 "$legacy_lock_sha" \
    --arg containerfile_sha256 "$containerfile_sha256" \
    --arg base_image "$base_image" \
    --argjson toolchain_packages "$(jq -c \
      '.inputs.builder.toolchain_packages' <<<"$lock_json")" \
    --slurpfile plan "$rekey_plan" \
    '.schema_version == 2 and .stage == "builder-toolchain" and
      .checkpoint_identity == $checkpoint_identity and
      .declared_inputs.source_lock_sha256 == $legacy_lock_sha256 and
      .declared_inputs.containerfile_sha256 == $containerfile_sha256 and
      .declared_inputs.base_image == $base_image and
      .declared_inputs.toolchain_packages == $toolchain_packages and
      .output == $plan[0].expected_output and
      .validation.result == "passed" and .cache_hit == false and
      .immutable == true' <<<"$legacy_manifest_json" >/dev/null ||
    fail "planned legacy builder checkpoint does not match exact inputs and outputs"
  legacy_image=$(jq -er '.output.image_id' <<<"$legacy_manifest_json")
  legacy_size=$(jq -er '.output.size_bytes' <<<"$legacy_manifest_json")
  legacy_inventory_sha=$(jq -er '.output.package_inventory_sha256' \
    <<<"$legacy_manifest_json")
  [[ $(docker image inspect --format '{{.Id}}' "$legacy_image" 2>/dev/null) == \
    "$legacy_image" ]] || fail "planned legacy builder image is unavailable"
  [[ $(docker image inspect --format '{{.Size}}' "$legacy_image" 2>/dev/null) == \
    "$legacy_size" ]] || fail "planned legacy builder image size differs"
  legacy_inventory=$(docker run --platform linux/arm64 --rm "$legacy_image" \
    cat /usr/share/omarchy-asahi-toolchain/packages.txt)
  actual_legacy_inventory_sha=$(printf '%s\n' "$legacy_inventory" | sha256_stdin)
  [[ $actual_legacy_inventory_sha == "$legacy_inventory_sha" ]] ||
    fail "planned legacy builder inventory differs"
  legacy_sync_databases=$(docker run --platform linux/arm64 --rm "$legacy_image" \
    /bin/bash -c \
    'find /var/lib/pacman/sync -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum')
  [[ $legacy_sync_databases == \
    "$(jq -jr '.actual_inputs.synchronized_database_digests | join("\n")' \
      <<<"$legacy_manifest_json")" ]] ||
    fail "planned legacy builder repository inventory differs"

  migrated_actual_inputs=$(jq -cnS \
    --argjson declared "$declared_inputs" \
    --arg package_inventory_sha256 "$legacy_inventory_sha" \
    --arg package_inventory "$legacy_inventory" \
    --arg sync_databases "$legacy_sync_databases" \
    '$declared + {package_inventory_sha256: $package_inventory_sha256,
      package_inventory: ($package_inventory | split("\n") |
        map(select(length > 0))),
      synchronized_database_digests: ($sync_databases | split("\n") |
        map(select(length > 0)))}')
  migrated_identity=$(printf '%s' "$migrated_actual_inputs" | sha256_stdin)
  [[ $(jq -er '.target_checkpoint_identity' "$rekey_plan") == \
    "$migrated_identity" ]] || fail "builder checkpoint rekey target identity is stale"
  migrated_directory=$builder_toolchain_root/$migrated_identity
  [[ ! -e $migrated_directory && ! -L $migrated_directory ]] ||
    fail "refusing to replace an existing migrated builder checkpoint"
  migrated_temporary=$(mktemp -d "$builder_toolchain_root/.${migrated_identity}.XXXXXX")
  jq -nS \
    --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --argjson declared_inputs "$declared_inputs" \
    --arg declared_input_digest "$declared_input_digest" \
    --argjson actual_inputs "$migrated_actual_inputs" \
    --arg checkpoint_identity "$migrated_identity" \
    --arg image_id "$legacy_image" \
    --argjson image_size "$legacy_size" \
    --arg package_inventory_sha256 "$legacy_inventory_sha" \
    --arg source_checkpoint_identity "$legacy_identity" \
    --arg source_lock_sha256 "$legacy_lock_sha" \
    --arg target_lock_sha256 "$lock_sha256" \
    '{schema_version: 2, stage: "builder-toolchain", mode: "shared",
      declared_inputs: $declared_inputs,
      declared_input_digest: $declared_input_digest,
      actual_inputs: $actual_inputs, checkpoint_identity: $checkpoint_identity,
      output: {image_id: $image_id, size_bytes: $image_size,
        package_inventory_sha256: $package_inventory_sha256},
      validation: {result: "passed"}, completed_at: $completed_at,
      elapsed_seconds: 0, cache_hit: false, immutable: true,
      environment: "OMARCHY_ASAHI_TOOLCHAIN_PREPARED=1",
      compatibility: {schema_version: 1,
        reason: "stage-input-granularity-v1",
        source_checkpoint_identity: $source_checkpoint_identity,
        source_lock_sha256: $source_lock_sha256,
        target_lock_sha256: $target_lock_sha256}}' \
    >"$migrated_temporary/manifest.json"
  chmod 0444 "$migrated_temporary/manifest.json"
  chmod 0555 "$migrated_temporary"
  mv "$migrated_temporary" "$migrated_directory"
  migrated_manifest=$migrated_directory/manifest.json
  migrated_verified_record=$(verify_cached_manifest "$migrated_manifest") ||
    fail "migrated builder checkpoint did not pass independent verification"
  migrated_manifest_json=$(jq -ce '.manifest' <<<"$migrated_verified_record")
  if [[ -n $run_manifest ]]; then
    mkdir -p "${run_manifest%/*}"
    jq -nS \
      --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --arg checkpoint_identity "$migrated_identity" \
      --arg input_digest "$declared_input_digest" \
      --argjson output "$(jq -c '.output' <<<"$migrated_manifest_json")" \
      --argjson compatibility "$(jq -c '.compatibility' \
        <<<"$migrated_manifest_json")" \
      --argjson elapsed_seconds "$((SECONDS - started))" \
      '{schema_version: 2, stage: "builder-toolchain", mode: "shared",
        checkpoint_identity: $checkpoint_identity, input_digest: $input_digest,
        validation: {result: "passed"}, completed_at: $completed_at,
        elapsed_seconds: $elapsed_seconds, cache_hit: false, output: $output,
        rekeyed: true, compatibility: $compatibility}' >"$run_manifest"
  fi
  printf '%s\n' "$legacy_image"
  exit 0
fi

temporary_tag="omarchy/asahi-toolchain:${declared_input_digest:0:16}-$$"
docker build --platform linux/arm64 \
  --file "$containerfile" \
  --build-arg "BASE_IMAGE=$base_image" \
  --build-arg "SOURCE_LOCK_SHA256=$lock_sha256" \
  --build-arg "TOOLCHAIN_PACKAGES=$toolchain_package_arguments" \
  --tag "$temporary_tag" \
  "$repository_root" >&2

image_id=$(docker image inspect --format '{{.Id}}' "$temporary_tag")
[[ $image_id =~ ^sha256:[0-9a-f]{64}$ ]] || fail "Docker returned an unsafe image identity"
image_size=$(docker image inspect --format '{{.Size}}' "$image_id")
[[ $image_size =~ ^[1-9][0-9]*$ ]] || fail "Docker returned an unsafe image size"
inventory=$(docker run --platform linux/arm64 --rm "$image_id" \
  cat /usr/share/omarchy-asahi-toolchain/packages.txt)
inventory_sha256=$(printf '%s\n' "$inventory" | sha256_stdin)
recorded_inventory_sha256=$(docker run --platform linux/arm64 --rm "$image_id" \
  cut -d' ' -f1 /usr/share/omarchy-asahi-toolchain/packages.sha256)
[[ $inventory_sha256 == "$recorded_inventory_sha256" ]] || fail "toolchain inventory digest mismatch"
for package in "${toolchain_packages[@]}"; do
  grep -Eq "^${package//+/\\+} " <<<"$inventory" ||
    fail "toolchain image omitted required package: $package"
done

sync_databases=$(docker run --platform linux/arm64 --rm "$image_id" /bin/bash -c \
  'find /var/lib/pacman/sync -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum')
actual_inputs=$(jq -cnS \
  --argjson declared "$declared_inputs" \
  --arg package_inventory_sha256 "$inventory_sha256" \
  --arg package_inventory "$inventory" \
  --arg sync_databases "$sync_databases" \
  '$declared + {package_inventory_sha256: $package_inventory_sha256,
    package_inventory: ($package_inventory | split("\n") |
      map(select(length > 0))),
    synchronized_database_digests: ($sync_databases | split("\n") | map(select(length > 0)))}')
checkpoint_identity=$(printf '%s' "$actual_inputs" | sha256_stdin)
checkpoint_directory="$builder_toolchain_root/$checkpoint_identity"
[[ ! -e $checkpoint_directory && ! -L $checkpoint_directory ]] ||
  fail "refusing to replace an existing toolchain checkpoint"
temporary_directory=$(mktemp -d "$builder_toolchain_root/.${checkpoint_identity}.XXXXXX")
manifest=$temporary_directory/manifest.json
jq -nS \
  --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson declared_inputs "$declared_inputs" \
  --arg declared_input_digest "$declared_input_digest" \
  --argjson actual_inputs "$actual_inputs" \
  --arg checkpoint_identity "$checkpoint_identity" \
  --arg image_id "$image_id" \
  --argjson image_size "$image_size" \
  --arg package_inventory_sha256 "$inventory_sha256" \
  --argjson elapsed_seconds "$((SECONDS - started))" \
  '{schema_version: 2, stage: "builder-toolchain", mode: "shared",
    declared_inputs: $declared_inputs,
    declared_input_digest: $declared_input_digest,
    actual_inputs: $actual_inputs, checkpoint_identity: $checkpoint_identity,
    output: {image_id: $image_id,
      size_bytes: $image_size,
      package_inventory_sha256: $package_inventory_sha256},
    validation: {result: "passed"}, completed_at: $completed_at,
    elapsed_seconds: $elapsed_seconds, cache_hit: false, immutable: true,
    environment: "OMARCHY_ASAHI_TOOLCHAIN_PREPARED=1"}' >"$manifest"
chmod 0444 "$manifest"
chmod 0555 "$temporary_directory"
mv "$temporary_directory" "$checkpoint_directory"

if [[ -n $run_manifest ]]; then
  mkdir -p "${run_manifest%/*}"
  jq \
    '{schema_version, stage, mode, checkpoint_identity,
      input_digest: .declared_input_digest, validation, completed_at,
      elapsed_seconds, cache_hit, output}' "$checkpoint_directory/manifest.json" \
    >"$run_manifest"
fi

printf '%s\n' "$image_id"
