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

# Globals the function reads from its sourcing environment.
build_lock=$work/build-lock.json
jq -nS '{retention: {maximum_allocated_bytes: 1073741824,
  maximum_checkpoints_per_stage: 2}}' >"$build_lock"

stage=verified-package-cache
identity() { printf "$1%.0s" {1..64}; }

# A fixture store: three checkpoints in one stage, each naming one object,
# plus one object referenced only by this run's manifest.
make_store() {
  local root=$1 digest index
  checkpoint_root=$root
  mkdir -p "$root/objects/sha256" "$root/checkpoints/$stage"
  for index in 1 2 3 4; do
    digest=$(printf 'payload-%s' "$index" | shasum -a 256 | cut -c1-64)
    mkdir -p "$root/objects/sha256/${digest:0:2}"
    printf 'payload-%s' "$index" >"$root/objects/sha256/${digest:0:2}/$digest"
    object[$index]=$digest
  done
  for index in 1 2 3; do
    mkdir -p "$root/checkpoints/$stage/$(identity "$index")"
    jq -nS --arg stage "$stage" --arg id "$(identity "$index")" \
      --arg at "2026-08-29T0$((4 - index)):00:00Z" --arg obj "${object[$index]}" \
      '{schema_version: 1, stage: $stage, checkpoint_identity: $id,
        completed_at: $at,
        outputs: [{name: "out", storage: {kind: "sha256-object", sha256: $obj}}]}' \
      >"$root/checkpoints/$stage/$(identity "$index")/manifest.json"
  done
}
declare -A object

# Default path: retention runs the pruner, protects this run's manifest and
# the object only that manifest names, and records the result.
unset OMARCHY_APPLY_CHECKPOINT_RETENTION
export OMARCHY_CHECKPOINT_PRUNER=$ROOT/builder/prune-asahi-checkpoints.py
run_evidence=$work/default
mkdir "$run_evidence"
make_store "$work/default-store"
jq -nS --arg stage "$stage" --arg id "$(identity 1)" --arg obj "${object[4]}" \
  '{stage: $stage, checkpoint_identity: $id,
    outputs: [{name: "external", storage: {kind: "sha256-object", sha256: $obj}}]}' \
  >"$run_evidence/$stage.json"
apply_checkpoint_retention
jq -e --arg stage "$stage" --arg evicted "$(identity 3)" --arg obj "${object[4]}" '
  .result == "passed" and
  ([.evicted[] | select(.kind == "checkpoint") | .identity] == [$evicted]) and
  (.protected_objects == [$obj])
' "$run_evidence/retention.json" >/dev/null ||
  fail "default retention did not evict the oldest checkpoint and protect the run manifest"
[[ ! -e $checkpoint_root/checkpoints/$stage/$(identity 3) ]] ||
  fail "default retention kept the over-limit checkpoint"
[[ -e $checkpoint_root/checkpoints/$stage/$(identity 1) &&
  -e $checkpoint_root/checkpoints/$stage/$(identity 2) ]] ||
  fail "default retention evicted a checkpoint within the limit"
[[ -e $checkpoint_root/objects/sha256/${object[4]:0:2}/${object[4]} ]] ||
  fail "default retention deleted an object only this run's manifest references"
[[ ! -e $checkpoint_root/objects/sha256/${object[3]:0:2}/${object[3]} ]] ||
  fail "default retention kept the evicted checkpoint's object"

# A smaller operator byte budget applies; a larger one never loosens the lock.
run_evidence=$work/budget
mkdir "$run_evidence"
make_store "$work/budget-store"
OMARCHY_CHECKPOINT_RETENTION_MAX_BYTES=99999999999 apply_checkpoint_retention
jq -e '.maximum_bytes == 1073741824' "$run_evidence/retention.json" >/dev/null ||
  fail "a larger operator budget loosened the lock's budget"
run_evidence=$work/budget-small
mkdir "$run_evidence"
make_store "$work/budget-small-store"
OMARCHY_CHECKPOINT_RETENTION_MAX_BYTES=4096 apply_checkpoint_retention
jq -e '.maximum_bytes == 4096' "$run_evidence/retention.json" >/dev/null ||
  fail "a smaller operator budget was ignored"
run_evidence=$work/budget-bad
mkdir "$run_evidence"
if (OMARCHY_CHECKPOINT_RETENTION_MAX_BYTES=lots apply_checkpoint_retention) 2>"$work/bad.error"; then
  fail "a malformed operator budget was accepted"
fi
grep -Fq 'positive integer' "$work/bad.error"

# Operator opt-out: exactly "0" skips pruning and records the skip; the
# store is untouched.
run_evidence=$work/optout
mkdir "$run_evidence"
make_store "$work/optout-store"
OMARCHY_APPLY_CHECKPOINT_RETENTION=0 apply_checkpoint_retention
jq -e '
  (keys == ["evicted", "reclaimed_bytes", "result", "schema_version"]) and
  .result == "retention-disabled-by-operator" and
  .evicted == [] and .reclaimed_bytes == 0
' "$run_evidence/retention.json" >/dev/null || fail "opt-out did not record a skip"
[[ -e $checkpoint_root/checkpoints/$stage/$(identity 3) ]] ||
  fail "opt-out touched the checkpoint root"

# Fail closed when retention evidence already exists. Run in a subshell
# because fail() exits.
existing_root=$work/existing
mkdir "$existing_root"
printf '%s\n' '{"result":"pre-existing"}' >"$existing_root/retention.json"
run_evidence=$existing_root
if (apply_checkpoint_retention) 2>"$work/existing.error"; then
  fail "retention overwrote pre-existing evidence"
fi
grep -Fq 'retention evidence already exists or is unsafe' "$work/existing.error"
jq -e '.result == "pre-existing"' "$existing_root/retention.json" >/dev/null

# Without the override the pruner is the container path, which does not exist
# on this host: the attempt must fail loudly and write no evidence.
unset OMARCHY_CHECKPOINT_PRUNER
[[ ! -e /builder/prune-asahi-checkpoints.py ]] ||
  fail "precondition: /builder/prune-asahi-checkpoints.py exists on this host"
run_evidence=$work/missing
mkdir "$run_evidence"
make_store "$work/missing-store"
set +e
apply_checkpoint_retention >"$work/missing.out" 2>"$work/missing.error"
missing_status=$?
set -e
(( missing_status != 0 )) || fail "retention succeeded without a pruner"
grep -Fq '/builder/prune-asahi-checkpoints.py' "$work/missing.error" ||
  fail "retention never attempted to invoke the pruner"
[[ ! -e $run_evidence/retention.json ]] ||
  fail "retention wrote evidence although the pruner never ran"

echo "Asahi build reporting tests passed"
