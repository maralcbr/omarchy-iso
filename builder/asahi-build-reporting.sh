#!/bin/bash

# Post-stage reporting and retention live outside the image-producing stage
# runtime. This keeps diagnostic evidence policy changes from rekeying package,
# repository, base-image, configured-target, or finalized-boot checkpoints.

record_diagnostic_retention_skip() {
  local evidence_root=$1
  local destination=$evidence_root/retention.json
  local temporary
  [[ -d $evidence_root && ! -L $evidence_root ]] ||
    fail "diagnostic evidence root is missing or unsafe"
  [[ ! -e $destination && ! -L $destination ]] ||
    fail "diagnostic retention evidence already exists or is unsafe"
  temporary=$(mktemp "$evidence_root/.retention.XXXXXX")
  if ! jq -nS '{schema_version: 1,
      result: "diagnostic-additive-proof-no-eviction",
      evicted: [], reclaimed_bytes: 0}' >"$temporary"; then
    rm -f -- "$temporary"
    fail "diagnostic retention evidence could not be written"
  fi
  chmod 0644 "$temporary"
  mv -- "$temporary" "$destination"
}

record_gated_retention_skip() {
  local evidence_root=$1
  local destination=$evidence_root/retention.json
  local temporary
  [[ -d $evidence_root && ! -L $evidence_root ]] ||
    fail "retention evidence root is missing or unsafe"
  [[ ! -e $destination && ! -L $destination ]] ||
    fail "retention evidence already exists or is unsafe"
  temporary=$(mktemp "$evidence_root/.retention.XXXXXX")
  if ! jq -nS '{schema_version: 1,
      result: "retention-gated-pending-safe-pruner",
      evicted: [], reclaimed_bytes: 0}' >"$temporary"; then
    rm -f -- "$temporary"
    fail "retention evidence could not be written"
  fi
  chmod 0644 "$temporary"
  mv -- "$temporary" "$destination"
}

apply_checkpoint_retention() {
  local manifest
  local -a retention_arguments=()
  # Retention is gated off by default: a qualification build must not evict
  # from the shared checkpoint store while the pruner still deletes objects
  # that only an unprotected external manifest references. Setting
  # OMARCHY_APPLY_CHECKPOINT_RETENTION=1 restores the previous behaviour
  # unchanged; every other value, including unset, records a skip and never
  # reaches the pruner.
  if [[ ${OMARCHY_APPLY_CHECKPOINT_RETENTION:-} != "1" ]]; then
    record_gated_retention_skip "$run_evidence"
    return 0
  fi
  for manifest in "$run_evidence"/*.json; do
    [[ -f $manifest ]] || continue
    jq -e '.stage and .checkpoint_identity' "$manifest" >/dev/null 2>&1 || continue
    retention_arguments+=(--protect-run-manifest "$manifest")
  done
  python3 /builder/prune-asahi-checkpoints.py \
    --cache-root "$checkpoint_root" \
    --maximum-bytes "$(jq -er '.retention.maximum_allocated_bytes' "$build_lock")" \
    --maximum-checkpoints-per-stage \
      "$(jq -er '.retention.maximum_checkpoints_per_stage' "$build_lock")" \
    "${retention_arguments[@]}" \
    --output "$run_evidence/retention.json" >/dev/null
}
