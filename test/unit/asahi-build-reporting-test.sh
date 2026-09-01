#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The reporting helpers call fail() from their sourcing environment.
fail() {
  echo "fail: $*" >&2
  exit 1
}

source "$ROOT/builder/asahi-build-reporting.sh"
record_diagnostic_retention_skip "$work"

jq -e '
  .schema_version == 1 and
  .result == "diagnostic-additive-proof-no-eviction" and
  .evicted == [] and
  .reclaimed_bytes == 0
' "$work/retention.json" >/dev/null

# The retention gate (2026-08-30). apply_checkpoint_retention must not reach
# prune-asahi-checkpoints.py unless the opt-in is set to exactly "1".
#
# This host has no /builder tree -- that is a container path -- so the pruner
# script is genuinely absent. The absence is load bearing twice over: on the
# default path it proves the pruner was never needed, and on the opt-in path it
# is what makes the attempted invocation observable. Assert the precondition so
# the proof cannot quietly weaken.
[[ ! -e /builder/prune-asahi-checkpoints.py ]] ||
  fail "precondition: /builder/prune-asahi-checkpoints.py exists on this host"

# Globals the function reads from its sourcing environment.
checkpoint_root=$work/checkpoint-root-must-not-be-touched
build_lock=$work/build-lock.json
jq -nS '{retention: {maximum_allocated_bytes: 1048576,
  maximum_checkpoints_per_stage: 2}}' >"$build_lock"

assert_gated() {
  local label=$1
  local gate_root=$work/gate-$label

  mkdir -p "$gate_root"
  run_evidence=$gate_root
  apply_checkpoint_retention
  jq -e '
    (keys == ["evicted", "reclaimed_bytes", "result", "schema_version"]) and
    .schema_version == 1 and
    .result == "retention-gated-pending-safe-pruner" and
    .evicted == [] and
    .reclaimed_bytes == 0
  ' "$gate_root/retention.json" >/dev/null
  [[ ! -e $checkpoint_root ]] ||
    fail "$label: retention touched the checkpoint root"
}

# Default path: the opt-in is unset.
unset OMARCHY_APPLY_CHECKPOINT_RETENTION
assert_gated unset

# Only the exact string "1" opens the gate; every near miss must still gate.
index=0
for value in true 0 "" 01 "1 " " 1" yes 11 TRUE; do
  index=$((index + 1))
  export OMARCHY_APPLY_CHECKPOINT_RETENTION=$value
  assert_gated "value-$index"
done
unset OMARCHY_APPLY_CHECKPOINT_RETENTION

# Fail closed when retention evidence already exists, matching the diagnostic
# path. Run in a subshell because fail() exits.
existing_root=$work/existing
mkdir "$existing_root"
printf '%s\n' '{"result":"pre-existing"}' >"$existing_root/retention.json"
run_evidence=$existing_root
if (apply_checkpoint_retention) 2>"$work/existing.error"; then
  fail "retention overwrote pre-existing evidence"
fi
grep -Fq 'retention evidence already exists or is unsafe' "$work/existing.error"
jq -e '.result == "pre-existing"' "$existing_root/retention.json" >/dev/null

# Opt-in path: the gate opens and the pruner is actually invoked. It fails
# because the container path does not exist here, which is what distinguishes
# "attempted and could not exec" from "never attempted": python3 reports the
# exact script path it could not open, and no gated evidence file is written.
optin_root=$work/optin
mkdir "$optin_root"
run_evidence=$optin_root
jq -nS --arg identity "$(printf 'a%.0s' {1..64})" \
  '{stage: "builder-toolchain", checkpoint_identity: $identity}' \
  >"$optin_root/builder-toolchain.json"
export OMARCHY_APPLY_CHECKPOINT_RETENTION=1
set +e
apply_checkpoint_retention >"$work/optin.out" 2>"$work/optin.error"
optin_status=$?
set -e
unset OMARCHY_APPLY_CHECKPOINT_RETENTION

(( optin_status != 0 )) ||
  fail "opt-in retention unexpectedly succeeded without a pruner"
grep -Fq '/builder/prune-asahi-checkpoints.py' "$work/optin.error" ||
  fail "opt-in retention never attempted to invoke the pruner"
[[ ! -e $optin_root/retention.json ]] ||
  fail "opt-in retention wrote gated evidence instead of opening the gate"
[[ ! -e $checkpoint_root ]] ||
  fail "opt-in retention touched the checkpoint root"

echo "ok - diagnostic reporting records a non-destructive retention boundary"
echo "ok - qualification retention is gated off by default and opens only on the exact opt-in"
