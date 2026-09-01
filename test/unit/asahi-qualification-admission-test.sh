#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT
mkdir -p "$test_root/bin" "$test_root/work" "$test_root/evidence"

cat >"$test_root/bin/python3" <<'PYTHON'
#!/bin/bash
set -euo pipefail
command=${2:-}
printf '%s\n' "$command" >>"$ASAHI_ADMISSION_TEST_TRACE"
case "$command" in
  verify|restore)
    printf '%s\n' '{}'
    ;;
  *)
    echo "unexpected checkpoint command: $command" >&2
    exit 2
    ;;
esac
PYTHON
chmod +x "$test_root/bin/python3"

export ASAHI_ADMISSION_TEST_TRACE=$test_root/checkpoint-commands
export PATH=$test_root/bin:$PATH
export OMARCHY_CHECKPOINT_POLICY=read-write
printf '%s\n' '{"untrusted":"not-an-authoritative-receipt"}' \
  >"$test_root/untrusted-receipt.json"
export OMARCHY_ASAHI_AUTHORITATIVE_ADMISSION_RECEIPT=$test_root/untrusted-receipt.json
build_mode=qualification
stage_input_root=$test_root/stage-inputs
work=$test_root/work
run_evidence=$test_root/evidence

fail() {
  echo "test admission failure: $*" >&2
  return 1
}

# Source the real adapter while mapping its container-owned /builder paths to
# this checkout. The restore control flow itself is not copied or mocked.
source <(sed "s#/builder/#$ROOT/builder/#g" \
  "$ROOT/builder/asahi-checkpoint-admission.sh")

identity=$test_root/configured-target.identity.json
printf '%s\n' '{}' >"$identity"
if restore_stage configured-target "$identity" \
  --destination root-image="$test_root/root.img"; then
  echo "qualification restored a checkpoint without authoritative admission" >&2
  exit 1
fi

if [[ -f $ASAHI_ADMISSION_TEST_TRACE ]] &&
  grep -Fxq restore "$ASAHI_ADMISSION_TEST_TRACE"; then
  echo "qualification reached checkpoint restore without authoritative admission" >&2
  exit 1
fi
grep -Fxq \
  'qualification-restore-requires-current-authoritative-admission-receipt' \
  "$run_evidence/configured-target.invalidation"

: >"$ASAHI_ADMISSION_TEST_TRACE"
stale_stage_input_root=$test_root/stale-stage-inputs
mkdir -p "$stale_stage_input_root/configured-target"
printf '%s\n' '{"producer_binding_identity":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_identity":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}' \
  >"$stale_stage_input_root/configured-target/source-manifest.json"
stage_input_root=$stale_stage_input_root
build_mode=diagnostic
if restore_stage configured-target "$identity" \
  --destination root-image="$test_root/diagnostic-root.img"; then
  echo "diagnostic restored from self-reported stale stage inputs without current-policy authority" >&2
  exit 1
fi
if grep -Eq '^(verify|restore)$' "$ASAHI_ADMISSION_TEST_TRACE"; then
  echo "diagnostic reached checkpoint content before current-policy authority" >&2
  exit 1
fi
grep -Fxq \
  'diagnostic-restore-requires-current-admission-policy-receipt' \
  "$run_evidence/configured-target.invalidation"

checkpoint_admission_stage=configured-target
(
  admit_stage_identity configured-target "$identity"
)
cmp -s "$identity" "$run_evidence/configured-target.admission.identity.json"
grep -Fxq 'identity-admission-stopped-before-restore-or-build' \
  "$run_evidence/configured-target.admission.result"

echo "ok - restore requires external authority while diagnostic metadata inspection remains usable"
