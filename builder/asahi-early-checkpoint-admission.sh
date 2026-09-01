#!/bin/bash

# Diagnostic-only admission for the two early Apple checkpoints. The receipt is
# generated from an immutable host snapshot and mounted read-only at a fixed
# path. It never authorizes qualification reuse.

early_checkpoint_receipt=/omarchy-asahi-stage-admission-receipt.json

for adapter in /builder/sha256-adapter.sh /builder/file-mode-adapter.sh; do
  if [[ ! -f $adapter || -L $adapter ]]; then
    echo "ERROR: early checkpoint admission dependency is missing or unsafe: $adapter" >&2
    return 1
  fi
  # shellcheck disable=SC1090
  source "$adapter"
done

validate_current_early_checkpoint_receipt() {
  local stage=$1
  local stage_input_root=$2
  local build_mode=$3
  local index=$stage_input_root/index.json
  local admission_index=$stage_input_root/admission-index.json
  local stage_root=$stage_input_root/$stage
  local source_lock=$stage_root/source-lock.json
  local source_manifest=$stage_root/source-manifest.json
  local admission_policy=$stage_root/admission-policy.json
  local receipt_mode
  local index_sha256 admission_index_sha256 source_lock_sha256
  local source_manifest_sha256 admission_policy_sha256
  local source_identity producer_binding_identity producer_binding_mode
  local admission_policy_identity admission_policy_mode

  case "$build_mode" in
    diagnostic) ;;
    qualification)
      echo "ERROR: host receipt is not authoritative for qualification reuse" >&2
      return 1
      ;;
    *)
      echo "ERROR: unsupported early checkpoint mode: $build_mode" >&2
      return 1
      ;;
  esac

  for input in \
    "$early_checkpoint_receipt" "$index" "$admission_index" \
    "$source_lock" "$source_manifest" "$admission_policy"; do
    [[ -f $input && ! -L $input ]] || {
      echo "ERROR: current-source admission input is missing or unsafe: $input" >&2
      return 1
    }
  done
  receipt_mode=$(file_mode "$early_checkpoint_receipt") || return 1
  (( (8#$receipt_mode & 0222) == 0 )) || {
    echo "ERROR: current-source admission receipt is writable" >&2
    return 1
  }

  index_sha256=$(sha256_file "$index") || return 1
  admission_index_sha256=$(sha256_file "$admission_index") || return 1
  source_lock_sha256=$(sha256_file "$source_lock") || return 1
  source_manifest_sha256=$(sha256_file "$source_manifest") || return 1
  admission_policy_sha256=$(sha256_file "$admission_policy") || return 1
  source_identity=$(jq -er '.source_identity' "$source_manifest") || return 1
  producer_binding_identity=$(jq -er '.producer_binding_identity' \
    "$source_manifest") || return 1
  producer_binding_mode=$(jq -er '.producer_binding_mode' \
    "$source_manifest") || return 1
  admission_policy_identity=$(jq -er '.admission_policy_identity' \
    "$admission_policy") || return 1
  admission_policy_mode=$(jq -er '.mode' "$admission_policy") || return 1

  [[ $source_identity =~ ^[0-9a-f]{64}$ &&
    $producer_binding_identity =~ ^[0-9a-f]{64}$ &&
    $admission_policy_identity =~ ^[0-9a-f]{64}$ ]] || {
    echo "ERROR: current-source admission identity is malformed: $stage" >&2
    return 1
  }
  jq -e \
    --arg stage "$stage" \
    --arg mode "$build_mode" \
    --arg source_identity "$source_identity" \
    --arg producer_binding_identity "$producer_binding_identity" \
    --arg producer_binding_mode "$producer_binding_mode" \
    --arg source_lock_sha256 "$source_lock_sha256" \
    --arg admission_policy_identity "$admission_policy_identity" \
    --arg admission_policy_mode "$admission_policy_mode" '
      .schema_version == 1 and .mode == $mode and
      .stages[$stage] == {
        producer_binding_identity: $producer_binding_identity,
        producer_binding_mode: $producer_binding_mode,
        source_identity: $source_identity,
        source_lock_sha256: $source_lock_sha256
      }
    ' "$index" >/dev/null || {
    echo "ERROR: stage producer index is stale or mismatched: $stage" >&2
    return 1
  }
  jq -e \
    --arg stage "$stage" \
    --arg mode "$build_mode" \
    --arg admission_policy_identity "$admission_policy_identity" \
    --arg admission_policy_mode "$admission_policy_mode" '
      .schema_version == 1 and .mode == $mode and
      .stages[$stage] == {
        admission_policy_identity: $admission_policy_identity,
        admission_policy_mode: $admission_policy_mode
      }
    ' "$admission_index" >/dev/null || {
    echo "ERROR: stage admission index is stale or mismatched: $stage" >&2
    return 1
  }

  jq -e \
    --arg stage "$stage" \
    --arg mode "$build_mode" \
    --arg index_sha256 "$index_sha256" \
    --arg admission_index_sha256 "$admission_index_sha256" \
    --arg source_identity "$source_identity" \
    --arg producer_binding_identity "$producer_binding_identity" \
    --arg producer_binding_mode "$producer_binding_mode" \
    --arg source_lock_sha256 "$source_lock_sha256" \
    --arg source_manifest_sha256 "$source_manifest_sha256" \
    --arg admission_policy_identity "$admission_policy_identity" \
    --arg admission_policy_mode "$admission_policy_mode" \
    --arg admission_policy_sha256 "$admission_policy_sha256" '
      keys == ["admission_index_sha256", "authorization_scope", "mode",
        "schema_version", "source_snapshot", "stage_input_index_sha256",
        "stages", "verification_kind"] and
      .schema_version == 1 and
      .verification_kind == "asahi-current-source-admission-receipt" and
      .authorization_scope == "diagnostic-checkpoint-reuse" and
      .mode == $mode and
      .source_snapshot == {
        kind: "immutable-host-snapshot",
        paths: [".git", "archiso", "bin", "builder", "configs"]
      } and
      .stage_input_index_sha256 == $index_sha256 and
      .admission_index_sha256 == $admission_index_sha256 and
      .stages[$stage] == {
        admission_policy_identity: $admission_policy_identity,
        admission_policy_mode: $admission_policy_mode,
        admission_policy_sha256: $admission_policy_sha256,
        producer_binding_identity: $producer_binding_identity,
        producer_binding_mode: $producer_binding_mode,
        source_identity: $source_identity,
        source_lock_sha256: $source_lock_sha256,
        source_manifest_sha256: $source_manifest_sha256
      }
    ' "$early_checkpoint_receipt" >/dev/null || {
    echo "ERROR: current-source admission receipt is stale or mismatched: $stage" >&2
    return 1
  }
}
