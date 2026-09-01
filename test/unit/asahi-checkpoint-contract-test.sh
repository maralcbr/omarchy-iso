#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
lock=$ROOT/builder/asahi-build-lock.json
package_builder=$ROOT/builder/build-asahi-os-package.sh
checkpoint_admission=$ROOT/builder/asahi-checkpoint-admission.sh
build_reporting=$ROOT/builder/asahi-build-reporting.sh
sha256_adapter=$ROOT/builder/sha256-adapter.sh
stage_sources=("$ROOT"/builder/asahi-stages/*.sh)
wrapper=$ROOT/bin/omarchy-iso-make
toolchain_builder=$ROOT/builder/ensure-asahi-toolchain-image.sh
repository_link_helper=$ROOT/builder/ensure-offline-repository-links.sh
repository_checkpoint=$ROOT/builder/checkpoint-offline-repository-database.sh
verified_package_stage=$ROOT/builder/asahi-stages/verified-package-cache.sh
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

if grep -Fq 'grep -oP' "$wrapper"; then
  echo "host wrapper must not require GNU grep on macOS" >&2
  exit 1
fi

[[ -f $sha256_adapter && ! -L $sha256_adapter ]]
grep -Fq 'source "$BUILD_ROOT/builder/sha256-adapter.sh"' "$wrapper"
grep -Fq 'source "$script_directory/sha256-adapter.sh"' "$toolchain_builder"
if grep -Fq '/usr/bin/shasum' "$wrapper" "$toolchain_builder"; then
  echo "host/toolchain entrypoints must use the portable SHA-256 adapter" >&2
  exit 1
fi
echo "ok - host and pinned Linux entrypoints share a fail-closed SHA-256 adapter"

[[ -f $lock ]]
jq -e '
  .schema_version == 1 and
  .builder.base_image == "menci/archlinuxarm@sha256:1245992a2b371b5aeeede7dae44937ab29dc446e9e77abe263b99b02e5c1813d" and
  .builder.maximum_workers == 10 and
  .node.version == "26.8.1" and
  .node.filename == "node-v26.8.1-linux-arm64.tar.gz" and
  .node.size_bytes == 62035643 and
  .node.sha256 == "d5f973ce975e4bd03e6c2038260f7e9201615aa8e1ee293c72f8dcc2a6d9fddb" and
  .compression.workers == 1 and
  .compression.deterministic_multi_worker_proven == false and
  .retention.maximum_allocated_bytes == 274877906944 and
  .retention.maximum_checkpoints_per_stage == 3 and
  .modes.qualification.catalog_eligible == true and
  .modes.qualification.release_compression == true and
  .modes.diagnostic.catalog_eligible == false and
  .modes.diagnostic.release_compression == false and
  .stages == [
    "builder-toolchain",
    "verified-package-cache",
    "offline-repository-database",
    "base-images",
    "configured-target",
    "finalized-boot",
    "sealed-release-package",
    "installer-metadata"
  ]
' "$lock" >/dev/null
echo "ok - source lock separates diagnostic and qualification stage contracts"

[[ -f $ROOT/builder/asahi-toolchain.Containerfile ]]
[[ -x $ROOT/builder/ensure-asahi-toolchain-image.sh ]]
grep -Fq 'OMARCHY_ASAHI_TOOLCHAIN_PREPARED=1' \
  "$ROOT/builder/ensure-asahi-toolchain-image.sh"
grep -Fq 'docker image inspect' "$ROOT/builder/ensure-asahi-toolchain-image.sh"
grep -Fq 'read_immutable_manifest_snapshot' \
  "$ROOT/builder/ensure-asahi-toolchain-image.sh"
grep -Fq 'checkpoint object directory is writable' \
  "$ROOT/builder/ensure-asahi-toolchain-image.sh"
grep -Fq 'pacman -Q' "$ROOT/builder/asahi-toolchain.Containerfile"
echo "ok - exact ARM64 toolchain image has a verified reusable boundary"

mkdir -p "$work/fake-bin"
cat >"$work/fake-bin/docker" <<'SH'
#!/bin/sh
touch "$OMARCHY_FAKE_DOCKER_CALLED"
exit 99
SH
chmod +x "$work/fake-bin/docker"
python3 "$ROOT/builder/asahi_stage_inputs.py" generate \
  --repo-root "$ROOT" \
  --spec "$ROOT/builder/asahi-stage-inputs.json" \
  --build-lock "$lock" \
  --mode qualification \
  --output-root "$work/stage-inputs"
if PATH="$work/fake-bin:$PATH" \
  OMARCHY_BUILD_MODE=qualification \
  OMARCHY_ASAHI_CHECKPOINT_ROOT="$work/qualification-cache" \
  OMARCHY_FAKE_DOCKER_CALLED="$work/docker-called" \
  OMARCHY_ASAHI_TOOLCHAIN_LOCK="$work/stage-inputs/builder-toolchain/source-lock.json" \
  OMARCHY_ASAHI_TOOLCHAIN_SOURCE_MANIFEST="$work/stage-inputs/builder-toolchain/source-manifest.json" \
  "$toolchain_builder" >"$work/out" 2>"$work/error"; then
  echo "qualification unexpectedly bootstrapped a moving toolchain" >&2
  exit 1
fi
grep -Fq 'qualification requires a previously verified toolchain checkpoint' \
  "$work/error"
[[ ! -e $work/docker-called ]]
echo "ok - qualification cannot bootstrap a moving toolchain repository"

grep -Fq 'asahi-build-lock.json' "$verified_package_stage"
grep -Fq -- '--legacy-build-lock "$rekey_plan_root/asahi-build-lock.json"' \
  "$ROOT/builder/checkpoint-offline-repository-database.sh"
grep -Fq '.node.filename' "$verified_package_stage"
grep -Fq '.node.size_bytes' "$verified_package_stage"
grep -Fq '.node.sha256' "$verified_package_stage"
grep -Fq 'if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then' \
  "$verified_package_stage"
grep -Fq 'pinned-node-cache.py snapshot' "$verified_package_stage"
grep -Fq 'private pinned Node view is missing or stale' "$verified_package_stage"
if grep -Fq '.node.filename' "$ROOT/builder/build-iso.sh"; then
  echo "authoritative Node payload logic leaked back into build-iso.sh" >&2
  exit 1
fi
echo "ok - authoritative Apple Node archive is exact and independently verified"

grep -Fq -- '--mode' "$wrapper"
grep -Fq 'OMARCHY_BUILD_MODE' "$wrapper"
grep -Fq 'diagnostic artifacts are never catalog eligible' "$wrapper"
grep -Fq '/var/cache/omarchy/asahi-checkpoints' "$wrapper"
grep -Fq 'OMARCHY_ASAHI_STAGE_INPUT_ROOT' "$wrapper"
grep -Fq 'HOST_ASAHI_UEFI_PAYLOAD' "$wrapper"
grep -Fq 'TOOLCHAIN_IDENTITY_MANIFEST' "$wrapper"
grep -Fq 'OMARCHY_ASAHI_TOOLCHAIN_IDENTITY_MANIFEST' \
  "$ROOT/builder/checkpoint-verified-package-cache.sh"
grep -Fq 'asahi_stage_inputs.py" generate' "$wrapper"
grep -Fq 'OMARCHY_BUILD_MODE="$OMARCHY_BUILD_MODE"' "$wrapper"
grep -Fq -- '--spec "$BUILD_ROOT/builder/asahi-stage-inputs.json"' "$wrapper"
grep -Fq -- '--build-lock "$BUILD_ROOT/builder/asahi-build-lock.json"' "$wrapper"
echo "ok - host wrapper carries mode and persistent checkpoint boundaries"

[[ -x $repository_link_helper ]]
grep -Fq '/builder/ensure-offline-repository-links.sh "$offline_mirror_dir"' \
  "$repository_checkpoint"
echo "ok - offline repository links use the fail-closed idempotent helper"

for stage in \
  verified-package-cache offline-repository-database base-images \
  configured-target finalized-boot sealed-release-package installer-metadata; do
  grep -Fq "$stage" "$package_builder" "$ROOT/builder/build-iso.sh" \
    "$ROOT/builder/checkpoint-verified-package-cache.sh" \
    "$repository_checkpoint" "${stage_sources[@]}"
done
grep -Fq 'python3 "$runner"' "${stage_sources[@]}"
grep -Fq '/builder/run-asahi-configured-stage.py "$configured_source_root"' \
  "${stage_sources[@]}"
grep -Fq '/builder/run-asahi-finalized-stage.py "$finalized_source_root"' \
  "${stage_sources[@]}"
grep -Fq -- '--root "$configured_source_root"' "$package_builder"
grep -Fq -- '--root "$finalized_source_root"' "$package_builder"
grep -Fq 'export OMARCHY_ISO_MEDIA_ROOT="$stage_source_root"' \
  "$ROOT/builder/asahi-stages/image-runtime.sh"
if grep -Fq '"OMARCHY_ISO_MEDIA_ROOT"' \
  "$ROOT/builder/asahi-stage-inputs.json"; then
  echo "physical runtime paths must not participate in producer identities" >&2
  exit 1
fi
grep -Fq 'diagnostic builds never emit a release ZIP' "$package_builder"
grep -Fxq 'source /builder/asahi-checkpoint-admission.sh' "$package_builder"
grep -Fxq 'initialize_asahi_checkpoint_admission' "$package_builder"
grep -Fq 'checkpoint admission adapter is missing or unsafe' "$package_builder"
grep -Fq 'cache-hit policy is missing or unsafe' "$checkpoint_admission"
grep -Fq 'identity-admission-stopped-before-restore-or-build' \
  "$checkpoint_admission"
grep -Fq 'checkpoint identity admission is diagnostic-only' \
  "$checkpoint_admission"
grep -Fq 'OMARCHY_BUILD_MODE' "$package_builder"
grep -Fq -- '--input source-manifest="$source_manifest"' \
  "$checkpoint_admission"
grep -Fq -- '--input configured-runtime="$configured_runtime_manifest"' \
  "${stage_sources[@]}"
grep -Fq -- '--input configured-product="$configured_product_manifest"' \
  "${stage_sources[@]}"
grep -Fq 'OMARCHY_ASAHI_CONFIGURED_CONTRACT_PROOF' "${stage_sources[@]}"
grep -Fq -- '--input configured-contract-proof="$configured_contract_proof"' \
  "${stage_sources[@]}"
if grep -Fq 'apply-asahi-checkpoint-rekey.py' "$checkpoint_admission"; then
  echo "package checkpoint admission still reaches compatibility rekey" >&2
  exit 1
fi
if grep -Eq '"[$]checkpoint_tool"[[:space:]]+(verify|restore)' \
  "$checkpoint_admission"; then
  echo "package checkpoint admission still reaches checkpoint content" >&2
  exit 1
fi
grep -Fq 'qualification-restore-requires-current-authoritative-admission-receipt' \
  "$checkpoint_admission"
grep -Fq 'diagnostic-restore-requires-current-admission-policy-receipt' \
  "$checkpoint_admission"
grep -Fq -- '--input node-runtime="$node_tarball"' \
  "${stage_sources[@]}"
grep -Fq -- '--input finalized-runtime="$finalized_runtime_manifest"' \
  "${stage_sources[@]}"
grep -Fq -- '--input finalized-product="$finalized_product_manifest"' \
  "${stage_sources[@]}"
grep -Fq -- '--stage configured-target' "$package_builder"
grep -Fq -- '--stage finalized-boot' "$package_builder"
grep -Fq 'source /builder/asahi-build-reporting.sh' "$package_builder"
grep -Fq 'record_diagnostic_retention_skip "$run_evidence"' "$package_builder"
grep -Fq 'diagnostic-additive-proof-no-eviction' "$build_reporting"
echo "ok - package stages are independently checkpointed and diagnostic-safe"

for function_name in \
  create_stage_identity admit_stage_identity restore_stage store_stage; do
  if grep -Eq "^${function_name}[(][)]" "$package_builder"; then
    echo "checkpoint function remained in producer: $function_name" >&2
    exit 1
  fi
  grep -Eq "^${function_name}[(][)]" "$checkpoint_admission"
done
grep -Fq "'.source_identity'" "$checkpoint_admission"
grep -Fq "'.producer_binding_identity'" "$checkpoint_admission"
grep -Fq "'.stages[\$stage].admission_policy_identity'" \
  "$checkpoint_admission"
grep -Fq -- '--source "omarchy_iso_stage=$source_identity"' \
  "$checkpoint_admission"
grep -Fq -- '--source "omarchy_iso_producer=$producer_binding_identity"' \
  "$checkpoint_admission"
# Until 2026-08-30 this pinned common_producer_inputs to exactly
# ["builder/asahi_stage_inputs.py"], which encoded the whole control plane
# being admission-side. Owner decision 8(a) resolved as option A: a
# control-plane file whose edits can change produced bytes joins producer
# identity. The dispatcher and the package dispatch script moved, so the
# equality pin grows to the new exact list -- and, because producer and
# admission must stay disjoint per stage, they are also asserted absent from
# every stage's admission_paths. The verifier and reuse-policy adapters are
# unchanged and still pinned admission-only for the five package stages: their
# edits re-admit, they do not rebuild.
jq -e '
  .common_producer_inputs == ["builder/asahi_stage_inputs.py",
    "builder/build-asahi-os-package.sh",
    "builder/asahi-package-dispatch.sh"] and
  (.common_admission_inputs | index("builder/asahi_stage_inputs.py") | not) and
  ([$spec.stages | keys[]] | all(
      . as $stage |
      ($spec.stages[$stage].admission_paths |
        index("builder/build-asahi-os-package.sh") | not) and
      ($spec.stages[$stage].admission_paths |
        index("builder/asahi-package-dispatch.sh") | not)
    )) and
  (["base-images", "configured-target", "finalized-boot",
    "sealed-release-package", "installer-metadata"] | all(
      . as $stage |
      ($spec.stages[$stage].source_paths |
        index("builder/asahi-checkpoint-admission.sh") | not) and
      ($spec.stages[$stage].admission_paths |
        index("builder/asahi-checkpoint-admission.sh") != null) and
      ($spec.stages[$stage].admission_paths |
        index("builder/asahi-cache-hit-policy.sh") != null)
    ))
' --argjson spec "$(<"$ROOT/builder/asahi-stage-inputs.json")" \
  "$ROOT/builder/asahi-stage-inputs.json" >/dev/null
echo "ok - byte-shaping control plane is producer-bound, verifiers stay admission"

if grep -Eq '(pigz|zstd[[:space:]].*-T|--threads)' "$package_builder"; then
  echo "unproven parallel release compression is forbidden" >&2
  exit 1
fi
echo "ok - qualification compression remains single-worker and deterministic"

echo "Asahi checkpoint contract tests passed"
