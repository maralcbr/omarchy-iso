#!/bin/bash

load_early_checkpoint_admission() {
  local early_checkpoint_admission=/builder/asahi-early-checkpoint-admission.sh
  if [[ ! -f $early_checkpoint_admission || -L $early_checkpoint_admission ]]; then
    echo "ERROR: early checkpoint admission adapter is missing or unsafe" >&2
    return 1
  fi
  # shellcheck disable=SC1090
  source "$early_checkpoint_admission"
}

reset_and_collect_offline_repository_packages() {
  rm -f "$offline_mirror_dir"/offline.db* \
    "$offline_mirror_dir"/offline.files*
  mapfile -t offline_packages < <(
    find "$offline_mirror_dir" -maxdepth 1 -type f -name '*.pkg.tar.*' \
      ! -name '*.sig' | sort
  )
  (( ${#offline_packages[@]} > 0 )) || {
    echo "ERROR: offline repository has no package payloads" >&2
    return 1
  }
}

create_generic_offline_repository_database() {
  reset_and_collect_offline_repository_packages
  repo-add "$offline_mirror_dir/offline.db.tar.gz" "${offline_packages[@]}"
  /builder/ensure-offline-repository-links.sh "$offline_mirror_dir"
}

create_offline_repository_database_identity() {
  local checkpoint_tool=$1
  local stage_root=$2
  local verified_package_cache_identity=$3
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
    echo "ERROR: offline-repository-database identity inputs are missing or unsafe" >&2
    return 1
  }
  source_identity=$(jq -er '.source_identity' "$source_manifest") || {
    echo "ERROR: offline-repository-database source identity is missing" >&2
    return 1
  }
  producer_binding_identity=$(jq -er '.producer_binding_identity' \
    "$source_manifest") || {
    echo "ERROR: offline-repository-database producer binding is missing" >&2
    return 1
  }
  [[ $source_identity =~ ^[0-9a-f]{64}$ ]] || {
    echo "ERROR: offline-repository-database source identity is invalid" >&2
    return 1
  }
  [[ $producer_binding_identity =~ ^[0-9a-f]{64}$ ]] || {
    echo "ERROR: offline-repository-database producer binding is invalid" >&2
    return 1
  }

  python3 "$checkpoint_tool" identity \
    --stage offline-repository-database \
    --mode "$asahi_build_mode" \
    --source-lock "$source_lock" \
    --source "omarchy_iso_stage=$source_identity" \
    --source "omarchy_iso_producer=$producer_binding_identity" \
    --input source-manifest="$source_manifest" \
    --input verified-package-cache="$verified_package_cache_identity" \
    --input repository-manifest="$repository_manifest" \
    --input runtime-manifest="$runtime_manifest" \
    >"$destination"
}

prepare_offline_repository_runtime_manifest() {
  local runtime_filename=offline-repository-database.runtime-inputs.json
  offline_repository_runtime_manifest=$asahi_run_evidence/$runtime_filename
  python3 /builder/asahi_stage_inputs.py runtime-manifest \
    --root "$asahi_run_evidence" \
    --spec /builder/asahi-stage-inputs.json \
    --stage offline-repository-database \
    --setting "OMARCHY_ARTIFACT_KIND=$OMARCHY_ARTIFACT_KIND" \
    --setting "OMARCHY_MEDIA_TARGET=$OMARCHY_MEDIA_TARGET" \
    --output "$offline_repository_runtime_manifest"
  [[ -f $offline_repository_runtime_manifest &&
    ! -L $offline_repository_runtime_manifest ]] || {
    echo "ERROR: offline repository runtime manifest is missing or unsafe" >&2
    return 1
  }
}

checkpoint_offline_repository_database() {
  load_early_checkpoint_admission
  prepare_offline_repository_runtime_manifest
  offline_database_stage_root=$asahi_stage_input_root/offline-repository-database
  [[ -f $offline_database_stage_root/source-lock.json &&
    -f $offline_database_stage_root/source-manifest.json ]] || {
    echo "ERROR: offline-repository-database identity inputs are missing" >&2
    exit 1
  }

  offline_database_identity=/tmp/offline-repository-database.identity.json
  create_offline_repository_database_identity \
    /builder/asahi_checkpoint.py \
    "$offline_database_stage_root" \
    "$package_cache_identity" \
    "$offline_repository_manifest" \
    "$offline_repository_runtime_manifest" \
    "$offline_database_identity"
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
      return 1
      ;;
  esac
  case "$checkpoint_policy" in
    read-write)
      if ! validate_current_early_checkpoint_receipt \
        offline-repository-database "$asahi_stage_input_root" \
        "$asahi_build_mode"; then
        printf '%s\n' "$invalidation_reason" \
          >"$asahi_run_evidence/offline-repository-database.invalidation"
        echo "ERROR: offline repository checkpoint requires current admission authority" >&2
        return 1
      fi
      ;;
    write-only)
      printf '%s\n' 'checkpoint-policy-write-only' \
        >"$asahi_run_evidence/offline-repository-database.invalidation"
      ;;
    *)
      echo "ERROR: unsupported checkpoint policy: $checkpoint_policy" >&2
      return 1
      ;;
  esac
  # Keep every byte-affecting repository input and cleanup operation inside
  # this stage boundary. A blocked read-write admission returns above before
  # deleting the currently prepared database.
  reset_and_collect_offline_repository_packages
  rekey_plan_root=${OMARCHY_ASAHI_REKEY_PLAN_ROOT:-}
  rekey_plan=$rekey_plan_root/offline-repository-database.json
  if [[ ${OMARCHY_CHECKPOINT_POLICY:-read-write} == read-write ]] &&
    ! python3 /builder/asahi_checkpoint.py verify \
      --cache-root "$asahi_checkpoint_root" \
      --identity "$offline_database_identity" >/dev/null 2>&1 &&
    [[ -n $rekey_plan_root && -f $rekey_plan && ! -L $rekey_plan ]]; then
    if ! python3 /builder/apply-asahi-checkpoint-rekey.py \
      --cache-root "$asahi_checkpoint_root" \
      --target-identity "$offline_database_identity" \
      --plan "$rekey_plan" \
      --legacy-build-lock "$rekey_plan_root/asahi-build-lock.json" \
      --package-source-lock "$package_cache_stage_root/source-lock.json" \
      >"$asahi_run_evidence/offline-repository-database.rekey.json" \
      2>"$asahi_run_evidence/offline-repository-database.rekey.error"; then
      echo "ERROR: authorized offline repository checkpoint rekey failed closed" >&2
      exit 1
    fi
  fi
  # One restore attempt decides reuse. The verification pass that used to gate
  # it is not repeated here because restore repeats it: restore_checkpoint runs
  # the same manifest identity, immutability, inline-tree and per-object
  # storage, type, mode and size checks, authenticates every object's bytes
  # while streaming them, and then reads the materialized output back -- a
  # check the discarded pass never made. A failed attempt removes whatever it
  # had materialized, so the rebuild starts from the same clean state as
  # before, and a checkpoint that exists but no longer matches still fails the
  # build closed when store re-verifies it below.
  if [[ ${OMARCHY_CHECKPOINT_POLICY:-read-write} != read-write ]] ||
    ! python3 /builder/asahi_checkpoint.py restore \
      --cache-root "$asahi_checkpoint_root" \
      --identity "$offline_database_identity" \
      --run-manifest "$asahi_run_evidence/offline-repository-database.json" \
      --destination repository-db="$offline_mirror_dir/offline.db.tar.gz" \
      --destination repository-files="$offline_mirror_dir/offline.files.tar.gz" \
      >/dev/null 2>&1; then
    printf '%s\n' 'checkpoint-missing-stale-unsafe-or-mismatched' \
      >"$asahi_run_evidence/offline-repository-database.invalidation"
    database_started=$SECONDS
    repo-add "$offline_mirror_dir/offline.db.tar.gz" "${offline_packages[@]}"
    [[ -f $offline_mirror_dir/offline.db.tar.gz &&
      -f $offline_mirror_dir/offline.files.tar.gz ]] || {
      echo "ERROR: repo-add omitted an offline repository database" >&2
      exit 1
    }
    # repo-add stamps every tar member with the wall clock, so identical
    # package sets rebuild with different bytes and the checkpoint store
    # refuses the collision. Rewrite both tarballs deterministically.
    : "${SOURCE_DATE_EPOCH:?deterministic repository database requires SOURCE_DATE_EPOCH}"
    python3 /builder/normalize-repository-database.py \
      "$offline_mirror_dir/offline.db.tar.gz" \
      "$offline_mirror_dir/offline.files.tar.gz"
    python3 /builder/asahi_checkpoint.py store \
      --cache-root "$asahi_checkpoint_root" \
      --identity "$offline_database_identity" \
      --elapsed-seconds "$((SECONDS - database_started))" \
      --run-manifest "$asahi_run_evidence/offline-repository-database.json" \
      --output repository-db="$offline_mirror_dir/offline.db.tar.gz" \
      --output repository-files="$offline_mirror_dir/offline.files.tar.gz" \
      >/dev/null
  fi
  /builder/ensure-offline-repository-links.sh "$offline_mirror_dir"
  export OMARCHY_OFFLINE_REPOSITORY_IDENTITY=$offline_database_identity
}

produce_offline_repository_database() {
  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon &&
    $OMARCHY_ARTIFACT_KIND == asahi-os-package ]]; then
    checkpoint_offline_repository_database
  else
    create_generic_offline_repository_database
  fi
}
