#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT
mkdir -p "$test_root/bin" "$test_root/tmp"

cat >"$test_root/bin/python3" <<'PYTHON'
#!/bin/bash
set -euo pipefail

script=${1##*/}
shift
case "$script" in
  asahi_stage_inputs.py)
    command=${1:-}
    shift
    [[ $command == runtime-manifest ]]
    printf '%s\n' runtime-manifest >>"$ASAHI_EARLY_AUTHORITY_TRACE"
    output=
    while (( $# > 0 )); do
      case "$1" in
        --output)
          output=$2
          shift 2
          ;;
        *) shift ;;
      esac
    done
    [[ -n $output ]]
    printf '%s\n' \
      '{"schema_version":1,"stage":"offline-repository-database"}' \
      >"$output"
    ;;
  capture-asahi-offline-repository.py)
    printf '%s\n' capture >>"$ASAHI_EARLY_AUTHORITY_TRACE"
    output=
    while (( $# > 0 )); do
      case "$1" in
        --output)
          output=$2
          shift 2
          ;;
        *) shift ;;
      esac
    done
    [[ -n $output ]]
    printf '%s\n' '{"schema_version":1,"packages":[]}' >"$output"
    ;;
  asahi_checkpoint.py)
    command=${1:-}
    printf '%s\n' "$command" >>"$ASAHI_EARLY_AUTHORITY_TRACE"
    case "$command" in
      identity|store) printf '%s\n' '{}' ;;
      verify) exit 1 ;;
      restore)
        # Refusal is the default because a cold cache is the ordinary case.
        # A case that wants reuse sets the exit to 0, and then the stand-in
        # must also materialize the destinations a real restore would.
        restore_exit=${ASAHI_EARLY_AUTHORITY_RESTORE_EXIT:-91}
        if (( restore_exit == 0 )); then
          shift
          while (( $# > 0 )); do
            case "$1" in
              --destination)
                printf '%s\n' restored >"${2#*=}"
                shift 2
                ;;
              *) shift ;;
            esac
          done
        fi
        exit "$restore_exit"
        ;;
      *) exit 92 ;;
    esac
    ;;
  apply-asahi-checkpoint-rekey.py)
    printf '%s\n' rekey >>"$ASAHI_EARLY_AUTHORITY_TRACE"
    ;;
  normalize-repository-database.py)
    # Deterministic-tarball rewrite of the freshly built database; the
    # stand-in leaves the fixture files as they are.
    printf '%s\n' normalize >>"$ASAHI_EARLY_AUTHORITY_TRACE"
    ;;
  *)
    echo "unexpected Python entry point: $script" >&2
    exit 93
    ;;
esac
PYTHON
chmod +x "$test_root/bin/python3"

cat >"$test_root/bin/repo-add" <<'REPO_ADD'
#!/bin/bash
set -euo pipefail
database=${1:?missing repository database}
files=${database/offline.db.tar.gz/offline.files.tar.gz}
printf '%s\n' repo-add >>"$ASAHI_EARLY_AUTHORITY_TRACE"
printf '%s\n' database >"$database"
printf '%s\n' files >"$files"
REPO_ADD
chmod +x "$test_root/bin/repo-add"

sed \
  -e "s#/builder/#$ROOT/builder/#g" \
  -e "s#/omarchy-asahi-stage-admission-receipt.json#$test_root/current-receipt.json#g" \
  "$ROOT/builder/asahi-early-checkpoint-admission.sh" \
  >"$test_root/early-admission.sh"
chmod 0444 "$test_root/early-admission.sh"

# Exercise the real early-stage control flow while remapping only fixed
# container paths into this isolated host fixture.
source <(sed \
  -e "s#/builder/asahi-early-checkpoint-admission.sh#$test_root/early-admission.sh#g" \
  -e "s#/builder/#$ROOT/builder/#g" \
  -e "s#/out/build-evidence#$test_root/build-evidence#g" \
  -e "s#/tmp/verified-package-cache.identity.json#$test_root/tmp/verified-package-cache.identity.json#g" \
  "$ROOT/builder/checkpoint-verified-package-cache.sh")
source <(sed \
  -e "s#/builder/asahi-early-checkpoint-admission.sh#$test_root/early-admission.sh#g" \
  -e "s#/builder/#$ROOT/builder/#g" \
  -e "s#/tmp/offline-repository-database.identity.json#$test_root/tmp/offline-repository-database.identity.json#g" \
  "$ROOT/builder/checkpoint-offline-repository-database.sh")

export PATH="$test_root/bin:$PATH"
export ASAHI_EARLY_AUTHORITY_TRACE=$test_root/trace
# The rebuild path requires a pinned epoch for deterministic tarballs.
export SOURCE_DATE_EPOCH=1787839908
# shellcheck source=../../builder/epochrealtime-stage-timing.sh
source "$ROOT/builder/epochrealtime-stage-timing.sh"

write_case_inputs() {
  local case_root=$1
  local mode=$2
  local authorization_scope=$3
  local source_identity=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  local producer_identity=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  local admission_identity=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
  local package_root=$case_root/stage-inputs/verified-package-cache
  local database_root=$case_root/stage-inputs/offline-repository-database
  local package_lock_sha database_lock_sha package_manifest_sha database_manifest_sha
  local package_policy_sha database_policy_sha

  mkdir -p "$case_root/mirror" "$package_root" "$database_root"
  for stage in verified-package-cache offline-repository-database; do
    stage_root=$case_root/stage-inputs/$stage
    printf '%s\n' \
      "{\"schema_version\":2,\"stage\":\"$stage\",\"mode\":\"$mode\"}" \
      >"$stage_root/source-lock.json"
    printf '%s\n' \
      "{\"schema_version\":2,\"stage\":\"$stage\",\"source_identity\":\"$source_identity\",\"producer_binding_identity\":\"$producer_identity\",\"producer_binding_mode\":\"$mode\"}" \
      >"$stage_root/source-manifest.json"
    printf '%s\n' \
      "{\"schema_version\":2,\"stage\":\"$stage\",\"mode\":\"$mode\",\"admission_policy_identity\":\"$admission_identity\"}" \
      >"$stage_root/admission-policy.json"
  done

  package_lock_sha=$(sha256_file "$package_root/source-lock.json")
  database_lock_sha=$(sha256_file "$database_root/source-lock.json")
  package_manifest_sha=$(sha256_file "$package_root/source-manifest.json")
  database_manifest_sha=$(sha256_file "$database_root/source-manifest.json")
  package_policy_sha=$(sha256_file "$package_root/admission-policy.json")
  database_policy_sha=$(sha256_file "$database_root/admission-policy.json")

  jq -nS \
    --arg mode "$mode" \
    --arg source "$source_identity" \
    --arg producer "$producer_identity" \
    --arg package_lock "$package_lock_sha" \
    --arg database_lock "$database_lock_sha" '
      {schema_version: 1, mode: $mode, stages: {
        "verified-package-cache": {
          producer_binding_identity: $producer,
          producer_binding_mode: $mode,
          source_identity: $source,
          source_lock_sha256: $package_lock
        },
        "offline-repository-database": {
          producer_binding_identity: $producer,
          producer_binding_mode: $mode,
          source_identity: $source,
          source_lock_sha256: $database_lock
        }
      }}' >"$case_root/stage-inputs/index.json"
  jq -nS \
    --arg mode "$mode" \
    --arg admission "$admission_identity" '
      {schema_version: 1, mode: $mode, stages: {
        "verified-package-cache": {
          admission_policy_identity: $admission,
          admission_policy_mode: $mode
        },
        "offline-repository-database": {
          admission_policy_identity: $admission,
          admission_policy_mode: $mode
        }
      }}' >"$case_root/stage-inputs/admission-index.json"

  chmod 0644 "$test_root/current-receipt.json" 2>/dev/null || true
  jq -nS \
    --arg mode "$mode" \
    --arg scope "$authorization_scope" \
    --arg index_sha "$(sha256_file "$case_root/stage-inputs/index.json")" \
    --arg admission_index_sha \
      "$(sha256_file "$case_root/stage-inputs/admission-index.json")" \
    --arg source "$source_identity" \
    --arg producer "$producer_identity" \
    --arg admission "$admission_identity" \
    --arg package_lock "$package_lock_sha" \
    --arg database_lock "$database_lock_sha" \
    --arg package_manifest "$package_manifest_sha" \
    --arg database_manifest "$database_manifest_sha" \
    --arg package_policy "$package_policy_sha" \
    --arg database_policy "$database_policy_sha" '
      {schema_version: 1,
       verification_kind: "asahi-current-source-admission-receipt",
       authorization_scope: $scope,
       mode: $mode,
       source_snapshot: {kind: "immutable-host-snapshot",
         paths: [".git", "archiso", "bin", "builder", "configs"]},
       stage_input_index_sha256: $index_sha,
       admission_index_sha256: $admission_index_sha,
       stages: {
         "verified-package-cache": {
           admission_policy_identity: $admission,
           admission_policy_mode: $mode,
           admission_policy_sha256: $package_policy,
           producer_binding_identity: $producer,
           producer_binding_mode: $mode,
           source_identity: $source,
           source_lock_sha256: $package_lock,
           source_manifest_sha256: $package_manifest
         },
         "offline-repository-database": {
           admission_policy_identity: $admission,
           admission_policy_mode: $mode,
           admission_policy_sha256: $database_policy,
           producer_binding_identity: $producer,
           producer_binding_mode: $mode,
           source_identity: $source,
           source_lock_sha256: $database_lock,
           source_manifest_sha256: $database_manifest
         }
       }}' >"$test_root/current-receipt.json"
  chmod 0444 "$test_root/current-receipt.json"

  printf '%s\n' '{}' >"$case_root/toolchain-identity.json"
  printf '%s\n' \
    '{"schema_version":1,"stage":"verified-package-cache"}' \
    >"$case_root/package-runtime-manifest.json"
  printf '%s\n' package.pkg.tar.zst >"$case_root/requested-packages"
  : >"$case_root/mirror/package.pkg.tar.zst"
}

set_runtime() {
  local case_root=$1
  local mode=$2
  asahi_stage_input_root=$case_root/stage-inputs
  offline_mirror_dir=$case_root/mirror
  requested_package_files=$case_root/requested-packages
  verified_package_runtime_manifest=$case_root/package-runtime-manifest.json
  export OMARCHY_ASAHI_TOOLCHAIN_IDENTITY_MANIFEST=$case_root/toolchain-identity.json
  export OMARCHY_BUILD_MODE=$mode
  export OMARCHY_CHECKPOINT_POLICY=read-write
  export OMARCHY_BUILD_RUN_ID=authority-$mode
  export OMARCHY_ASAHI_CHECKPOINT_ROOT=$case_root/checkpoints
  export OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
  export OMARCHY_ARTIFACT_KIND=asahi-os-package
  start_epochrealtime_timer verified_package_stage_started
}

diagnostic_root=$test_root/diagnostic
write_case_inputs "$diagnostic_root" diagnostic diagnostic-checkpoint-reuse
set_runtime "$diagnostic_root" diagnostic
: >"$ASAHI_EARLY_AUTHORITY_TRACE"
checkpoint_verified_package_cache
produce_offline_repository_database
[[ $(grep -c '^store$' "$ASAHI_EARLY_AUTHORITY_TRACE") -eq 2 ]]
[[ $(grep -c '^repo-add$' "$ASAHI_EARLY_AUTHORITY_TRACE") -eq 1 ]]
# Until 2026-08-30 the repository stage verified twice on the read-write path
# and a cache miss therefore never reached restore, which this pinned as the
# absence of a restore. The redundant pre-restore verify is gone: one restore
# attempt now decides reuse, and its refusal is what falls through to the
# rebuild. The replacement pins exact counts rather than absence, so it also
# catches a second restore or a resurrected second verify. The surviving
# verify is the rekey gate, which must not rekey without an authorized plan.
[[ $(grep -c '^restore$' "$ASAHI_EARLY_AUTHORITY_TRACE") -eq 1 ]]
[[ $(grep -c '^verify$' "$ASAHI_EARLY_AUTHORITY_TRACE") -eq 2 ]]
if grep -Eq '^rekey$' "$ASAHI_EARLY_AUTHORITY_TRACE"; then
  echo "diagnostic fake unexpectedly rekeyed after a cache miss" >&2
  exit 1
fi
# The refused restore must still record the exact invalidation evidence the
# rebuild path has always written.
grep -Fxq 'checkpoint-missing-stale-unsafe-or-mismatched' \
  "$test_root/build-evidence/authority-diagnostic/offline-repository-database.invalidation"

# The other half of the collapse: a restore that succeeds ends the stage. No
# rebuild, no repository store, no invalidation evidence. The single attempt
# carries a decision that used to need a separate verification pass first.
reuse_root=$test_root/reuse
write_case_inputs "$reuse_root" diagnostic diagnostic-checkpoint-reuse
set_runtime "$reuse_root" diagnostic
export OMARCHY_BUILD_RUN_ID=authority-reuse
export ASAHI_EARLY_AUTHORITY_RESTORE_EXIT=0
: >"$ASAHI_EARLY_AUTHORITY_TRACE"
checkpoint_verified_package_cache
produce_offline_repository_database
unset ASAHI_EARLY_AUTHORITY_RESTORE_EXIT
[[ $(grep -c '^restore$' "$ASAHI_EARLY_AUTHORITY_TRACE") -eq 1 ]]
[[ $(grep -c '^store$' "$ASAHI_EARLY_AUTHORITY_TRACE") -eq 1 ]]
if grep -Eq '^(repo-add|rekey)$' "$ASAHI_EARLY_AUTHORITY_TRACE"; then
  echo "reused repository checkpoint unexpectedly rebuilt or rekeyed" >&2
  exit 1
fi
if [[ -e $test_root/build-evidence/authority-reuse/offline-repository-database.invalidation ]]; then
  echo "reused repository checkpoint recorded an invalidation" >&2
  exit 1
fi
[[ $(readlink "$reuse_root/mirror/offline.db") == offline.db.tar.gz ]]
[[ $(readlink "$reuse_root/mirror/offline.files") == offline.files.tar.gz ]]

qualification_root=$test_root/qualification
write_case_inputs "$qualification_root" qualification qualification-metadata-only
set_runtime "$qualification_root" qualification
: >"$ASAHI_EARLY_AUTHORITY_TRACE"
if checkpoint_verified_package_cache 2>"$qualification_root/package.error"; then
  echo "qualification reused an unsigned host receipt" >&2
  exit 1
fi
if checkpoint_offline_repository_database 2>"$qualification_root/database.error"; then
  echo "qualification repository reused an unsigned host receipt" >&2
  exit 1
fi
if grep -Eq '^(verify|restore|rekey|store|repo-add)$' \
  "$ASAHI_EARLY_AUTHORITY_TRACE"; then
  echo "qualification touched checkpoint content without signed authority" >&2
  exit 1
fi

write_case_inputs "$diagnostic_root" diagnostic diagnostic-checkpoint-reuse
if validate_current_early_checkpoint_receipt verified-package-cache \
  "$diagnostic_root/stage-inputs" diagnostic \
  2>"$test_root/valid.error"; then
  :
else
  echo "valid diagnostic receipt was rejected" >&2
  exit 1
fi

mv "$test_root/current-receipt.json" "$test_root/missing-receipt.saved"
if validate_current_early_checkpoint_receipt verified-package-cache \
  "$diagnostic_root/stage-inputs" diagnostic \
  2>"$test_root/missing.error"; then
  echo "missing receipt unexpectedly passed" >&2
  exit 1
fi
grep -Fq 'missing or unsafe' "$test_root/missing.error"
mv "$test_root/missing-receipt.saved" "$test_root/current-receipt.json"

chmod 0644 "$test_root/current-receipt.json"
if validate_current_early_checkpoint_receipt verified-package-cache \
  "$diagnostic_root/stage-inputs" diagnostic \
  2>"$test_root/writable.error"; then
  echo "writable receipt unexpectedly passed" >&2
  exit 1
fi
grep -Fq 'receipt is writable' "$test_root/writable.error"

jq '.authorization_scope = "tampered"' "$test_root/current-receipt.json" \
  >"$test_root/tampered-receipt.json"
mv "$test_root/tampered-receipt.json" "$test_root/current-receipt.json"
chmod 0444 "$test_root/current-receipt.json"
if validate_current_early_checkpoint_receipt verified-package-cache \
  "$diagnostic_root/stage-inputs" diagnostic \
  2>"$test_root/tampered.error"; then
  echo "tampered receipt unexpectedly passed" >&2
  exit 1
fi
grep -Fq 'receipt is stale or mismatched' "$test_root/tampered.error"

write_case_inputs "$diagnostic_root" diagnostic diagnostic-checkpoint-reuse
jq '.source_identity = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"' \
  "$diagnostic_root/stage-inputs/verified-package-cache/source-manifest.json" \
  >"$test_root/stale-source-manifest.json"
mv "$test_root/stale-source-manifest.json" \
  "$diagnostic_root/stage-inputs/verified-package-cache/source-manifest.json"
if validate_current_early_checkpoint_receipt verified-package-cache \
  "$diagnostic_root/stage-inputs" diagnostic \
  2>"$test_root/stale.error"; then
  echo "stale stage bundle unexpectedly passed" >&2
  exit 1
fi
grep -Eq 'producer index is stale|receipt is stale' "$test_root/stale.error"

# The same focused producer owns the generic path, but generic repo creation
# must not load Apple admission policy or inspect checkpoint content.
generic_root=$test_root/generic
mkdir -p "$generic_root/mirror"
: >"$generic_root/mirror/generic.pkg.tar.zst"
printf '%s\n' old >"$generic_root/mirror/offline.db.tar.gz"
printf '%s\n' old >"$generic_root/mirror/offline.files.tar.gz"
offline_mirror_dir=$generic_root/mirror
export OMARCHY_MEDIA_TARGET=x86_64/pc
: >"$ASAHI_EARLY_AUTHORITY_TRACE"
produce_offline_repository_database
[[ $(grep -c '^repo-add$' "$ASAHI_EARLY_AUTHORITY_TRACE") -eq 1 ]]
if grep -Eq '^(runtime-manifest|identity|verify|restore|rekey|store)$' \
  "$ASAHI_EARLY_AUTHORITY_TRACE"; then
  echo "generic repository producer unexpectedly touched Apple checkpoints" >&2
  exit 1
fi
[[ $(readlink "$generic_root/mirror/offline.db") == offline.db.tar.gz ]]
[[ $(readlink "$generic_root/mirror/offline.files") == offline.files.tar.gz ]]

# Apple validation ISOs use the same generic repo-add producer. They do not
# have a verified package-cache identity or stage receipt, so any checkpoint
# inspection here would make the public validation path unbuildable.
validation_root=$test_root/apple-validation-iso
mkdir -p "$validation_root/mirror"
: >"$validation_root/mirror/validation.pkg.tar.zst"
offline_mirror_dir=$validation_root/mirror
asahi_stage_input_root=$validation_root/missing-stage-inputs
package_cache_identity=$validation_root/missing-package-cache-identity.json
offline_repository_manifest=$validation_root/missing-repository-manifest.json
export OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
export OMARCHY_ARTIFACT_KIND=iso
: >"$ASAHI_EARLY_AUTHORITY_TRACE"
produce_offline_repository_database
[[ $(grep -c '^repo-add$' "$ASAHI_EARLY_AUTHORITY_TRACE") -eq 1 ]]
if grep -Eq '^(runtime-manifest|identity|verify|restore|rekey|store)$' \
  "$ASAHI_EARLY_AUTHORITY_TRACE"; then
  echo "Apple validation ISO unexpectedly touched checkpoint content" >&2
  exit 1
fi
[[ -f $validation_root/mirror/offline.db.tar.gz ]]
[[ -f $validation_root/mirror/offline.files.tar.gz ]]
[[ $(readlink "$validation_root/mirror/offline.db") == offline.db.tar.gz ]]
[[ $(readlink "$validation_root/mirror/offline.files") == offline.files.tar.gz ]]

grep -Fq 'source /builder/checkpoint-offline-repository-database.sh' \
  "$ROOT/builder/build-iso.sh"
grep -Fq 'produce_offline_repository_database' "$ROOT/builder/build-iso.sh"
if grep -Fq 'repo-add "$offline_mirror_dir/offline.db.tar.gz"' \
  "$ROOT/builder/build-iso.sh"; then
  echo "build controller retained an offline repository producer" >&2
  exit 1
fi

echo "ok - package checkpoints are admitted and validation ISO stays generic"
