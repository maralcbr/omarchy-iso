#!/bin/bash

# Checkpoint identity, restore, rekey, and store control plane for the Asahi
# package producer. This adapter is an admission-policy input, never a producer
# identity input. It intentionally does not issue authoritative admission
# receipts; the diagnostic stop below records identity inspection only.

checkpoint_tool=/builder/asahi_checkpoint.py
checkpoint_root=${OMARCHY_ASAHI_CHECKPOINT_ROOT:-/var/cache/omarchy/asahi-checkpoints}
checkpoint_policy=${OMARCHY_CHECKPOINT_POLICY:-read-write}
checkpoint_admission_stage=${OMARCHY_CHECKPOINT_ADMISSION_STAGE:-}
cache_hit_requirement=${OMARCHY_ASAHI_REQUIRE_CACHE_HIT_THROUGH:-}
stage_manifests=()

cache_hit_policy_tool=/builder/asahi-cache-hit-policy.sh
if [[ ! -f $cache_hit_policy_tool || -L $cache_hit_policy_tool ]]; then
  echo "build-asahi-os-package: cache-hit policy is missing or unsafe" >&2
  return 1
fi
source "$cache_hit_policy_tool"

initialize_asahi_checkpoint_admission() {
  [[ $checkpoint_policy == read-write || $checkpoint_policy == write-only ]] ||
    fail "unsupported checkpoint policy: $checkpoint_policy"
  if [[ -n $checkpoint_admission_stage ]]; then
    [[ $build_mode == diagnostic ]] ||
      fail "checkpoint identity admission is diagnostic-only"
    case "$checkpoint_admission_stage" in
      base-images|configured-target|finalized-boot) ;;
      *)
        fail "unsupported checkpoint identity admission stage: $checkpoint_admission_stage"
        ;;
    esac
  fi
  asahi_validate_cache_hit_requirement "$cache_hit_requirement" ||
    fail "unsupported cache-hit requirement: $cache_hit_requirement"
  [[ -x $checkpoint_tool && ! -L $checkpoint_tool ]] ||
    fail "checkpoint tool is missing or unsafe"
  if [[ -e $checkpoint_root && ( ! -d $checkpoint_root || -L $checkpoint_root ) ]]; then
    fail "checkpoint root is unsafe: $checkpoint_root"
  fi
  [[ -f $stage_input_root/admission-index.json &&
    ! -L $stage_input_root/admission-index.json ]] ||
    fail "stage admission index is missing or unsafe"
  mkdir -p "$checkpoint_root"
}

stage_run_manifest() {
  printf '%s/%s.json\n' "$run_evidence" "$1"
}

create_stage_identity() {
  local stage=$1 destination=$2
  local stage_root=$stage_input_root/$stage
  local source_lock=$stage_root/source-lock.json
  local source_manifest=$stage_root/source-manifest.json
  local admission_policy=$stage_root/admission-policy.json
  local admission_policy_identity
  local indexed_admission_policy_identity
  local source_identity
  local producer_binding_identity
  shift 2
  [[ -f $source_lock && ! -L $source_lock &&
    -f $source_manifest && ! -L $source_manifest &&
    -f $admission_policy && ! -L $admission_policy ]] ||
    fail "stage-specific identity or admission inputs are missing or unsafe for $stage"
  [[ $(jq -er '.stage' "$source_lock") == "$stage" &&
    $(jq -er '.stage' "$source_manifest") == "$stage" &&
    $(jq -er '.stage' "$admission_policy") == "$stage" ]] ||
    fail "stage-specific identity or admission inputs belong to another stage: $stage"
  admission_policy_identity=$(jq -er '.admission_policy_identity' "$admission_policy")
  indexed_admission_policy_identity=$(
    jq -er --arg stage "$stage" \
      '.stages[$stage].admission_policy_identity' \
      "$stage_input_root/admission-index.json"
  )
  [[ $admission_policy_identity =~ ^[0-9a-f]{64}$ &&
    $indexed_admission_policy_identity == "$admission_policy_identity" ]] ||
    fail "stage admission policy identity is invalid or mismatched: $stage"
  source_identity=$(jq -er '.source_identity' "$source_manifest")
  producer_binding_identity=$(jq -er '.producer_binding_identity' "$source_manifest")
  [[ $source_identity =~ ^[0-9a-f]{64}$ ]] ||
    fail "stage source identity is invalid: $stage"
  [[ $producer_binding_identity =~ ^[0-9a-f]{64}$ ]] ||
    fail "stage producer binding identity is invalid: $stage"
  local -a source_arguments=(
    --source "omarchy_iso_stage=$source_identity"
    --source "omarchy_iso_producer=$producer_binding_identity"
  )
  case "$stage" in
    finalized-boot|sealed-release-package|installer-metadata)
      source_arguments+=(--source "source_date_epoch=${SOURCE_DATE_EPOCH:-unset}")
      ;;
  esac
  python3 "$checkpoint_tool" identity \
    --stage "$stage" \
    --mode "$build_mode" \
    --source-lock "$source_lock" \
    "${source_arguments[@]}" \
    --input source-manifest="$source_manifest" \
    "$@" >"$destination"
}

admit_stage_identity() {
  local stage=$1 identity=$2
  [[ $checkpoint_admission_stage == "$stage" ]] || return 0
  cp -- "$identity" "$run_evidence/$stage.admission.identity.json"
  printf '%s\n' "identity-admission-stopped-before-restore-or-build" \
    >"$run_evidence/$stage.admission.result"
  echo "Admitted diagnostic checkpoint identity: $stage" >&2
  exit 0
}

restore_stage() {
  local stage=$1 identity=$2 invalidation_reason failure_message
  shift 2
  [[ $checkpoint_policy == read-write ]] || return 1
  case "$build_mode" in
    qualification)
      invalidation_reason=qualification-restore-requires-current-authoritative-admission-receipt
      failure_message="required qualification checkpoint lacks current authoritative admission"
      ;;
    diagnostic)
      invalidation_reason=diagnostic-restore-requires-current-admission-policy-receipt
      failure_message="required diagnostic checkpoint lacks current admission policy"
      ;;
    *)
      fail "unsupported checkpoint restore mode: $build_mode"
      ;;
  esac

  # This adapter does not issue or verify external admission receipts. Keep
  # metadata-only identity inspection available, but never reach checkpoint
  # content, compatibility rekey, or restore until that authority seam exists.
  printf '%s\n' "$invalidation_reason" >"$run_evidence/$stage.invalidation"
  if asahi_cache_hit_required "$cache_hit_requirement" "$stage"; then
    fail "$failure_message: $stage"
  fi
  : "$identity" "$@"
  return 1
}

store_stage() {
  local stage=$1 identity=$2 elapsed=$3
  shift 3
  python3 "$checkpoint_tool" store \
    --cache-root "$checkpoint_root" \
    --identity "$identity" \
    --elapsed-seconds "$elapsed" \
    --run-manifest "$(stage_run_manifest "$stage")" \
    "$@" >"$work/$stage.store.json"
  stage_manifests+=("$(stage_run_manifest "$stage")")
}
