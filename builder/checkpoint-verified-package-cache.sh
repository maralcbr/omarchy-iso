#!/bin/bash

early_checkpoint_admission=/builder/asahi-early-checkpoint-admission.sh
if [[ ! -f $early_checkpoint_admission || -L $early_checkpoint_admission ]]; then
  echo "ERROR: early checkpoint admission adapter is missing or unsafe" >&2
  return 1
fi
# shellcheck disable=SC1090
source "$early_checkpoint_admission"

create_verified_package_cache_identity() {
  local checkpoint_tool=$1
  local stage_root=$2
  local toolchain_identity=$3
  local repository_manifest=$4
  local runtime_manifest=$5
  local destination=$6
  local source_lock=$stage_root/source-lock.json
  local source_manifest=$stage_root/source-manifest.json
  local source_identity
  local producer_binding_identity

  [[ -f $source_lock && ! -L $source_lock &&
    -f $source_manifest && ! -L $source_manifest &&
    -f $runtime_manifest && ! -L $runtime_manifest ]] || {
    echo "ERROR: verified-package-cache identity inputs are missing or unsafe" >&2
    return 1
  }
  source_identity=$(jq -er '.source_identity' "$source_manifest") || {
    echo "ERROR: verified-package-cache source identity is missing" >&2
    return 1
  }
  producer_binding_identity=$(jq -er '.producer_binding_identity' \
    "$source_manifest") || {
    echo "ERROR: verified-package-cache producer binding is missing" >&2
    return 1
  }
  [[ $source_identity =~ ^[0-9a-f]{64}$ ]] || {
    echo "ERROR: verified-package-cache source identity is invalid" >&2
    return 1
  }
  [[ $producer_binding_identity =~ ^[0-9a-f]{64}$ ]] || {
    echo "ERROR: verified-package-cache producer binding is invalid" >&2
    return 1
  }

  python3 "$checkpoint_tool" identity \
    --stage verified-package-cache \
    --mode "$asahi_build_mode" \
    --source-lock "$source_lock" \
    --source "omarchy_iso_stage=$source_identity" \
    --source "omarchy_iso_producer=$producer_binding_identity" \
    --input source-manifest="$source_manifest" \
    --input builder-toolchain="$toolchain_identity" \
    --input repository-manifest="$repository_manifest" \
    --input runtime-manifest="$runtime_manifest" \
    >"$destination"
}

checkpoint_verified_package_cache() {
  asahi_run_id=${OMARCHY_BUILD_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}
  asahi_run_evidence=/out/build-evidence/$asahi_run_id
  asahi_checkpoint_root=${OMARCHY_ASAHI_CHECKPOINT_ROOT:-/var/cache/omarchy/asahi-checkpoints}
  asahi_build_mode=${OMARCHY_BUILD_MODE:-qualification}
  mkdir -p "$asahi_run_evidence" "$asahi_checkpoint_root"
  offline_repository_manifest=$offline_mirror_dir/offline-repository-inputs.json
  package_cache_stage_root=$asahi_stage_input_root/verified-package-cache
  [[ -f $package_cache_stage_root/source-lock.json &&
    -f $package_cache_stage_root/source-manifest.json &&
    -f $verified_package_runtime_manifest &&
    ! -L $verified_package_runtime_manifest ]] || {
    echo "ERROR: verified-package-cache identity inputs are missing" >&2
    exit 1
  }

  python3 /builder/capture-asahi-offline-repository.py \
    --mirror "$offline_mirror_dir" \
    --requested-list "$requested_package_files" \
    --snapshot-lock package-source-lock="$package_cache_stage_root/source-lock.json" \
    --snapshot-lock arm-snapshot=/builder/arm-package-snapshots.conf \
    --snapshot-lock apple-platform=/builder/apple-platform-snapshot.json \
    --snapshot-lock arm-repository="$offline_mirror_dir/ARM-REPOSITORY" \
    --snapshot-lock arm-runtime="$offline_mirror_dir/ARM-RUNTIME" \
    --snapshot-lock arm-packages="$offline_mirror_dir/ARM-PACKAGES" \
    --snapshot-lock apple-packages="$offline_mirror_dir/APPLE-PACKAGES" \
    --snapshot-lock apple-keyring="$offline_mirror_dir/APPLE-KEYRING" \
    --output "$offline_repository_manifest"
  export OMARCHY_OFFLINE_REPOSITORY_MANIFEST=$offline_repository_manifest

  package_cache_identity=/tmp/verified-package-cache.identity.json
  create_verified_package_cache_identity \
    /builder/asahi_checkpoint.py \
    "$package_cache_stage_root" \
    "$OMARCHY_ASAHI_TOOLCHAIN_IDENTITY_MANIFEST" \
    "$offline_repository_manifest" \
    "$verified_package_runtime_manifest" \
    "$package_cache_identity"
  local checkpoint_policy=${OMARCHY_CHECKPOINT_POLICY:-read-write}
  local invalidation_reason
  case "$asahi_build_mode" in
    qualification)
      invalidation_reason=qualification-restore-requires-current-authoritative-admission-receipt
      ;;
    diagnostic)
      invalidation_reason=diagnostic-restore-requires-current-admission-policy-receipt
      ;;
    *)
      echo "ERROR: unsupported early checkpoint mode: $asahi_build_mode" >&2
      exit 1
      ;;
  esac
  case "$checkpoint_policy" in
    read-write)
      if ! validate_current_early_checkpoint_receipt \
        verified-package-cache "$asahi_stage_input_root" "$asahi_build_mode"; then
        printf '%s\n' "$invalidation_reason" \
          >"$asahi_run_evidence/verified-package-cache.invalidation"
        echo "ERROR: verified package checkpoint requires current admission authority" >&2
        return 1
      fi
      if ! python3 /builder/asahi_checkpoint.py verify \
        --cache-root "$asahi_checkpoint_root" \
        --identity "$package_cache_identity" >/dev/null 2>&1; then
        printf '%s\n' 'checkpoint-missing-stale-unsafe-or-mismatched' \
          >"$asahi_run_evidence/verified-package-cache.invalidation"
      fi
      ;;
    write-only)
      printf '%s\n' 'checkpoint-policy-write-only' \
        >"$asahi_run_evidence/verified-package-cache.invalidation"
      ;;
    *)
      echo "ERROR: unsupported checkpoint policy: $checkpoint_policy" >&2
      exit 1
      ;;
  esac
  verified_package_stage_elapsed_seconds=$(elapsed_epochrealtime_timer \
    "$verified_package_stage_started") || return 1
  python3 /builder/asahi_checkpoint.py store \
    --cache-root "$asahi_checkpoint_root" \
    --identity "$package_cache_identity" \
    --elapsed-seconds "$verified_package_stage_elapsed_seconds" \
    --run-manifest "$asahi_run_evidence/verified-package-cache.json" \
    --output repository-manifest="$offline_repository_manifest" >/dev/null
}
