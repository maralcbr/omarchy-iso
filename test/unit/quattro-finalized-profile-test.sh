#!/bin/bash
set -euo pipefail
ROOT=$(cd "${BASH_SOURCE[0]%/*}/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
source "$ROOT/builder/asahi-stages/finalized-runtime-inputs.sh"
export build_cache_dir="$work/cache" OMARCHY_ARCH=aarch64
export OMARCHY_CANDIDATE_ROOT="$work/candidate" OMARCHY_DEPENDENCY_ROOT="$work/dependency"
mkdir -p "$OMARCHY_CANDIDATE_ROOT" "$OMARCHY_DEPENDENCY_ROOT"
printf '{}\n' >"$OMARCHY_CANDIDATE_ROOT/signing.json"
printf '{}\n' >"$OMARCHY_DEPENDENCY_ROOT/manifest.json"
printf 'signature fixture\n' >"$OMARCHY_DEPENDENCY_ROOT/manifest.json.sig"
profile="$build_cache_dir/airootfs/usr/share/omarchy-iso/apple-boot-profile.json"
# A cache restore has the immutable host snapshot, not necessarily the
# previous disposable builder's /tmp authentication scratch directory.
printf '{"schema":4}\n' >"$OMARCHY_CANDIDATE_ROOT/manifest.json"
prepare_finalized_runtime_inputs
jq -e '. == {schema:1, boot_profile:"limine", candidate_schema:4}' "$profile" >/dev/null
printf '{"schema":3}\n' >"$OMARCHY_CANDIDATE_ROOT/manifest.json"
prepare_finalized_runtime_inputs
[[ ! -e $profile ]]
printf '{"schema":5}\n' >"$OMARCHY_CANDIDATE_ROOT/manifest.json"
if prepare_finalized_runtime_inputs; then exit 1; fi
rm "$OMARCHY_CANDIDATE_ROOT/manifest.json"
if prepare_finalized_runtime_inputs 2>"$work/error"; then exit 1; fi
printf 'PASS: finalized profile comes only from the authenticated candidate snapshot\n'
