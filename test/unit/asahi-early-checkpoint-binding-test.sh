#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

sed -e "s#/builder/#$ROOT/builder/#g" \
  "$ROOT/builder/asahi-early-checkpoint-admission.sh" \
  >"$work/early-checkpoint-admission.sh"
chmod 0444 "$work/early-checkpoint-admission.sh"

# Exercise the real identity functions while remapping their fixed container
# paths into this isolated host fixture. Production paths remain fail closed.
source <(sed \
  -e "s#/builder/asahi-early-checkpoint-admission.sh#$work/early-checkpoint-admission.sh#g" \
  -e "s#/builder/#$ROOT/builder/#g" \
  "$ROOT/builder/checkpoint-verified-package-cache.sh")
source <(sed \
  -e "s#/builder/asahi-early-checkpoint-admission.sh#$work/early-checkpoint-admission.sh#g" \
  -e "s#/builder/#$ROOT/builder/#g" \
  "$ROOT/builder/checkpoint-offline-repository-database.sh")

python3 "$ROOT/builder/asahi_stage_inputs.py" generate \
  --repo-root "$ROOT" \
  --spec "$ROOT/builder/asahi-stage-inputs.json" \
  --build-lock "$ROOT/builder/asahi-build-lock.json" \
  --mode qualification \
  --output-root "$work/stage-inputs"

# The public selector is byte-affecting for this checkpoint: it determines
# whether the stage emits a checkpoint-authoritative package repository or a
# generic validation-ISO repository. Bind both selector values so the generated
# producer identity cannot silently reinterpret this branch.
#
# Superseded assertion (until 2026-08-29): this additionally required
# builder/asahi-stage-inputs.json to appear in the stage's own source_paths --
# whole-file spec binding, under which any edit to any stage's declaration
# invalidated every stage. The schema-2 work replaced that with per-declaration
# digests: the generated source manifest carries declaration_sha256 over the
# stage's own producer declaration. Re-pinned positively against the generated
# manifest, which is a stronger check than the old membership test because it
# verifies the digest actually published for this stage rather than the
# presence of a path in a list.
#
# Coverage is pinned at exactly the seven producer keys. It was six until
# 2026-08-30, when Phase C1 adopted queue item 8(b) and folded `dispatches` in,
# so widening a stage's suppression list now invalidates checkpoints produced
# under the narrower declaration. `admission_paths` is still consumed but
# unbound -- queue items 8(a)/(c), still open.
jq -e '
  .stages["offline-repository-database"].runtime_settings == [
    "OMARCHY_ARTIFACT_KIND",
    "OMARCHY_MEDIA_TARGET"
  ]
' "$ROOT/builder/asahi-stage-inputs.json" >/dev/null

database_source_manifest=$work/stage-inputs/offline-repository-database/source-manifest.json
expected_declaration_sha256=$(python3 - "$ROOT/builder/asahi-stage-inputs.json" <<'PY'
import hashlib
import json
import sys

specification = json.loads(open(sys.argv[1]).read())
declaration = specification["stages"]["offline-repository-database"]
producer_declaration = {
    key: declaration.get(key, [])
    for key in (
        "depends_on",
        "dispatches",
        "entrypoints",
        "source_paths",
        "lock_paths",
        "runtime_inputs",
        "runtime_settings",
    )
}
canonical = json.dumps(
    producer_declaration, sort_keys=True, separators=(",", ":"), ensure_ascii=True
).encode("utf-8")
print(hashlib.sha256(canonical).hexdigest())
PY
)
jq -e --arg declaration_sha256 "$expected_declaration_sha256" '
  .declaration_sha256 == $declaration_sha256 and
  (.declaration | keys) == [
    "depends_on",
    "dispatches",
    "entrypoints",
    "lock_paths",
    "runtime_inputs",
    "runtime_settings",
    "source_paths"
  ] and
  .declaration.runtime_settings == [
    "OMARCHY_ARTIFACT_KIND",
    "OMARCHY_MEDIA_TARGET"
  ] and
  .declaration.depends_on == ["verified-package-cache"]
' "$database_source_manifest" >/dev/null

printf '%s\n' '{"schema_version":2,"stage":"builder-toolchain"}' \
  >"$work/builder-toolchain.json"
printf '%s\n' '{"schema_version":1,"packages":[]}' \
  >"$work/repository-manifest.json"
printf '%s\n' '{"schema_version":1,"stage":"verified-package-cache"}' \
  >"$work/package-runtime-manifest.json"
printf '%s\n' '{"schema_version":1,"stage":"offline-repository-database"}' \
  >"$work/offline-repository-runtime-manifest.json"
asahi_build_mode=qualification

toolchain_stage_root=$work/stage-inputs/builder-toolchain
toolchain_declared_inputs=$work/builder-toolchain.declared-inputs.json
OMARCHY_BUILD_MODE=qualification \
OMARCHY_ASAHI_CHECKPOINT_ROOT="$work/metadata-only-cache-must-not-exist" \
OMARCHY_ASAHI_TOOLCHAIN_LOCK="$toolchain_stage_root/source-lock.json" \
OMARCHY_ASAHI_TOOLCHAIN_SOURCE_MANIFEST="$toolchain_stage_root/source-manifest.json" \
  "$ROOT/builder/ensure-asahi-toolchain-image.sh" --print-declared-inputs \
  >"$toolchain_declared_inputs"
toolchain_source_identity=$(jq -er '.source_identity' \
  "$toolchain_stage_root/source-manifest.json")
toolchain_producer_binding_identity=$(jq -er '.producer_binding_identity' \
  "$toolchain_stage_root/source-manifest.json")
toolchain_source_manifest_sha256=$(python3 -c \
  'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
  "$toolchain_stage_root/source-manifest.json")
jq -e \
  --arg source_identity "$toolchain_source_identity" \
  --arg producer_binding_identity "$toolchain_producer_binding_identity" \
  --arg manifest_sha256 "$toolchain_source_manifest_sha256" '
    .source == {
      manifest_sha256: $manifest_sha256,
      omarchy_iso_producer: $producer_binding_identity,
      omarchy_iso_stage: $source_identity
    }
  ' "$toolchain_declared_inputs" >/dev/null
[[ ! -e $work/metadata-only-cache-must-not-exist ]]

toolchain_missing_binding=$work/toolchain-missing-producer-binding.json
jq 'del(.producer_binding_identity)' \
  "$toolchain_stage_root/source-manifest.json" \
  >"$toolchain_missing_binding"
if OMARCHY_BUILD_MODE=qualification \
  OMARCHY_ASAHI_CHECKPOINT_ROOT="$work/rejected-cache-must-not-exist" \
  OMARCHY_ASAHI_TOOLCHAIN_LOCK="$toolchain_stage_root/source-lock.json" \
  OMARCHY_ASAHI_TOOLCHAIN_SOURCE_MANIFEST="$toolchain_missing_binding" \
    "$ROOT/builder/ensure-asahi-toolchain-image.sh" --print-declared-inputs \
    >"$work/rejected-toolchain.out" 2>"$work/rejected-toolchain.error"; then
  echo "toolchain source without a producer binding unexpectedly passed" >&2
  exit 1
fi
[[ ! -e $work/rejected-cache-must-not-exist ]]

assert_generated_binding() {
  local stage=$1
  local source_manifest=$2
  local identity=$3
  local source_identity
  local producer_binding_identity
  local source_manifest_sha256

  source_identity=$(jq -er '.source_identity' "$source_manifest")
  producer_binding_identity=$(jq -er '.producer_binding_identity' \
    "$source_manifest")
  source_manifest_sha256=$(python3 -c \
    'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "$source_manifest")
  jq -e \
    --arg stage "$stage" \
    --arg source_identity "$source_identity" \
    --arg producer_binding_identity "$producer_binding_identity" \
    --arg source_manifest_sha256 "$source_manifest_sha256" '
      .stage == $stage and .mode == "qualification" and
      .source_commits == {
        omarchy_iso_producer: $producer_binding_identity,
        omarchy_iso_stage: $source_identity
      } and
      ([.inputs[] | select(
        .name == "source-manifest" and
        .sha256 == $source_manifest_sha256
      )] | length) == 1
    ' "$identity" >/dev/null
}

package_stage_root=$work/stage-inputs/verified-package-cache
package_identity=$work/verified-package-cache.identity.json
create_verified_package_cache_identity \
  "$ROOT/builder/asahi_checkpoint.py" \
  "$package_stage_root" \
  "$work/builder-toolchain.json" \
  "$work/repository-manifest.json" \
  "$work/package-runtime-manifest.json" \
  "$package_identity"
assert_generated_binding \
  verified-package-cache \
  "$package_stage_root/source-manifest.json" \
  "$package_identity"

database_stage_root=$work/stage-inputs/offline-repository-database
database_identity=$work/offline-repository-database.identity.json
create_offline_repository_database_identity \
  "$ROOT/builder/asahi_checkpoint.py" \
  "$database_stage_root" \
  "$package_identity" \
  "$work/repository-manifest.json" \
  "$work/offline-repository-runtime-manifest.json" \
  "$database_identity"
assert_generated_binding \
  offline-repository-database \
  "$database_stage_root/source-manifest.json" \
  "$database_identity"
offline_runtime_sha256=$(python3 -c \
  'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
  "$work/offline-repository-runtime-manifest.json")
jq -e --arg runtime_sha256 "$offline_runtime_sha256" '
  ([.inputs[] | select(
    .name == "runtime-manifest" and .sha256 == $runtime_sha256
  )] | length) == 1
' "$database_identity" >/dev/null

missing_binding_root=$work/missing-producer-binding
mkdir "$missing_binding_root"
cp "$package_stage_root/source-lock.json" "$missing_binding_root/source-lock.json"
jq 'del(.producer_binding_identity)' "$package_stage_root/source-manifest.json" \
  >"$missing_binding_root/source-manifest.json"
if create_verified_package_cache_identity \
  "$ROOT/builder/asahi_checkpoint.py" \
  "$missing_binding_root" \
  "$work/builder-toolchain.json" \
  "$work/repository-manifest.json" \
  "$work/package-runtime-manifest.json" \
  "$work/missing-producer-binding.identity.json" \
  2>"$work/missing-producer-binding.error"; then
  echo "missing producer binding unexpectedly produced an identity" >&2
  exit 1
fi
grep -Fq 'producer binding is missing' "$work/missing-producer-binding.error"

invalid_binding_root=$work/invalid-producer-binding
mkdir "$invalid_binding_root"
cp "$package_stage_root/source-lock.json" "$invalid_binding_root/source-lock.json"
jq '.producer_binding_identity = "invalid"' \
  "$package_stage_root/source-manifest.json" \
  >"$invalid_binding_root/source-manifest.json"
if create_verified_package_cache_identity \
  "$ROOT/builder/asahi_checkpoint.py" \
  "$invalid_binding_root" \
  "$work/builder-toolchain.json" \
  "$work/repository-manifest.json" \
  "$work/package-runtime-manifest.json" \
  "$work/invalid-producer-binding.identity.json" \
  2>"$work/invalid-producer-binding.error"; then
  echo "malformed producer binding unexpectedly produced an identity" >&2
  exit 1
fi
grep -Fq 'producer binding is invalid' "$work/invalid-producer-binding.error"

invalid_source_root=$work/invalid-source-identity
mkdir "$invalid_source_root"
cp "$database_stage_root/source-lock.json" "$invalid_source_root/source-lock.json"
jq '.source_identity = "invalid"' \
  "$database_stage_root/source-manifest.json" \
  >"$invalid_source_root/source-manifest.json"
if create_offline_repository_database_identity \
  "$ROOT/builder/asahi_checkpoint.py" \
  "$invalid_source_root" \
  "$package_identity" \
  "$work/repository-manifest.json" \
  "$work/offline-repository-runtime-manifest.json" \
  "$work/invalid-source-identity.identity.json" \
  2>"$work/invalid-source-identity.error"; then
  echo "malformed source identity unexpectedly produced an identity" >&2
  exit 1
fi
grep -Fq 'source identity is invalid' "$work/invalid-source-identity.error"

echo "ok - generated toolchain and early-stage manifests bind provenance and current producer inputs"
