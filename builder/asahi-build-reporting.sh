#!/bin/bash

# Post-stage reporting and retention live outside the image-producing stage
# runtime. This keeps diagnostic evidence policy changes from rekeying package,
# repository, base-image, configured-target, or finalized-boot checkpoints.

record_retention_skip() {
  local evidence_root=$1 result=$2
  local destination=$evidence_root/retention.json
  local temporary
  [[ -d $evidence_root && ! -L $evidence_root ]] ||
    fail "retention evidence root is missing or unsafe"
  [[ ! -e $destination && ! -L $destination ]] ||
    fail "retention evidence already exists or is unsafe"
  temporary=$(mktemp "$evidence_root/.retention.XXXXXX")
  if ! jq -nS --arg result "$result" '{schema_version: 1,
      result: $result, evicted: [], reclaimed_bytes: 0}' >"$temporary"; then
    rm -f -- "$temporary"
    fail "retention evidence could not be written"
  fi
  chmod 0644 "$temporary"
  mv -- "$temporary" "$destination"
}

# Retention runs after every build, diagnostic or qualification, so the store
# is bounded as builds go by rather than growing until someone deletes it. It
# keeps the newest checkpoints per stage (build lock: retention.
# maximum_checkpoints_per_stage) within a byte budget (retention.
# maximum_allocated_bytes, or OMARCHY_CHECKPOINT_RETENTION_MAX_BYTES when set
# lower for a small host), and never deletes an object that this run's own
# manifests or a surviving checkpoint still reference. Setting
# OMARCHY_APPLY_CHECKPOINT_RETENTION=0 skips it and records the skip.
apply_checkpoint_retention() {
  local manifest maximum_bytes lock_maximum_bytes
  local pruner=${OMARCHY_CHECKPOINT_PRUNER:-/builder/prune-asahi-checkpoints.py}
  local -a retention_arguments=()
  [[ -d $run_evidence && ! -L $run_evidence ]] ||
    fail "retention evidence root is missing or unsafe"
  [[ ! -e $run_evidence/retention.json && ! -L $run_evidence/retention.json ]] ||
    fail "retention evidence already exists or is unsafe"
  if [[ ${OMARCHY_APPLY_CHECKPOINT_RETENTION:-1} == "0" ]]; then
    record_retention_skip "$run_evidence" retention-disabled-by-operator
    return 0
  fi
  lock_maximum_bytes=$(jq -er '.retention.maximum_allocated_bytes' "$build_lock") ||
    fail "build lock has no retention byte budget"
  maximum_bytes=$lock_maximum_bytes
  if [[ -n ${OMARCHY_CHECKPOINT_RETENTION_MAX_BYTES:-} ]]; then
    [[ $OMARCHY_CHECKPOINT_RETENTION_MAX_BYTES =~ ^[1-9][0-9]*$ ]] ||
      fail "OMARCHY_CHECKPOINT_RETENTION_MAX_BYTES must be a positive integer"
    if (( OMARCHY_CHECKPOINT_RETENTION_MAX_BYTES < lock_maximum_bytes )); then
      maximum_bytes=$OMARCHY_CHECKPOINT_RETENTION_MAX_BYTES
    fi
  fi
  for manifest in "$run_evidence"/*.json; do
    [[ -f $manifest ]] || continue
    jq -e '.stage and .checkpoint_identity' "$manifest" >/dev/null 2>&1 || continue
    retention_arguments+=(--protect-run-manifest "$manifest")
  done
  python3 "$pruner" \
    --cache-root "$checkpoint_root" \
    --maximum-bytes "$maximum_bytes" \
    --maximum-checkpoints-per-stage \
      "$(jq -er '.retention.maximum_checkpoints_per_stage' "$build_lock")" \
    "${retention_arguments[@]}" \
    --output "$run_evidence/retention.json" >/dev/null
}
