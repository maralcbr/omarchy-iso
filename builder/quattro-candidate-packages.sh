#!/bin/bash

# Only used in the disposable Apple image builder. No installed keyring or
# online repository configuration is changed by this adapter.
admit_private_limine_qualification() {
  if [[ ${OMARCHY_ASAHI_PRODUCT:-} != /builder/products/omarchy-mx-mac-limine-private.json ||
    ${OMARCHY_BUILD_MODE:-} != "diagnostic" || -z ${OMARCHY_DEPENDENCY_ROOT:-} ]]; then
    echo "Schema-4 Limine inputs verified, but image assembly is not enabled for this product. Use the explicitly pinned private diagnostic product." >&2
    return 1
  fi
  python3 /builder/private-limine-qualification.py \
    "$OMARCHY_ASAHI_PRODUCT" "$1/manifest.json" \
    "$OMARCHY_DEPENDENCY_ROOT/manifest.json" /builder/quattro-trust/policy.json
}

preflight_quattro_candidate_packages() {
  [[ -n ${OMARCHY_CANDIDATE_ROOT:-} ]] || return 0
  [[ $OMARCHY_BUILD_MODE == "diagnostic" &&
    $OMARCHY_MEDIA_TARGET == "aarch64/apple-silicon" &&
    $OMARCHY_ARTIFACT_KIND == "asahi-os-package" ]] || return 1
  local verified=/tmp/omarchy-quattro-verified
  python3 /builder/quattro-candidate.py \
    --input "$OMARCHY_CANDIDATE_ROOT" --output "$verified" \
    --receipt-sha256 "$OMARCHY_CANDIDATE_RECEIPT_SHA256" \
    --source-revision "$OMARCHY_CANDIDATE_SOURCE" || return 1
  OMARCHY_CANDIDATE_SCHEMA=$(jq -er '.schema' "$verified/manifest.json") || return 1
  if [[ $OMARCHY_CANDIDATE_SCHEMA == 4 ]]; then
    admit_private_limine_qualification "$verified" || return 1
  elif [[ ${OMARCHY_ASAHI_PRODUCT:-} == /builder/products/omarchy-mx-mac-limine-private.json ]]; then
    echo "Private Limine qualification requires candidate schema 4." >&2
    return 1
  fi
}

prepare_quattro_candidate_packages() {
  [[ -n ${OMARCHY_CANDIDATE_ROOT:-} ]] || return 0
  local verified=/tmp/omarchy-quattro-verified
  # initialize_verified_package_cache_stage authenticated these inputs before
  # any builder trust or dependency preparation. Never accept an unverified root.
  [[ -f $verified/manifest.json ]] || return 1
  if [[ $(jq -er '.schema' "$verified/manifest.json") == 4 ]]; then
    admit_private_limine_qualification "$verified" || return 1
  fi
  local primary filename
  primary=$(jq -er '.primary_fingerprint' /builder/quattro-trust/policy.json)
  pacman-key --add /builder/quattro-trust/public.gpg
  pacman-key --lsign-key "$primary"
  local -a archives=()
  mapfile -t candidate_package_files < <(jq -er '.packages[].filename' "$verified/manifest.json")
  for filename in "${candidate_package_files[@]}"; do
    cp "$verified/$filename" "$verified/$filename.sig" "$offline_mirror_dir/"
    archives+=("$offline_mirror_dir/$filename")
  done
  repo-add "$offline_mirror_dir/quattro-candidates.db.tar.gz" "${archives[@]}"
  # This is a private build config, with candidates ahead of the old runtime
  # snapshot. Every package still requires a verified signature.
  awk '/^\[arm-snapshots\]$/ {
    print "[quattro-candidates]"
    print "SigLevel = Required DatabaseOptional"
    print "Server = file:///var/cache/airootfs/var/cache/omarchy/mirror/offline"
    print ""
  } {print}' "$PACMAN_ONLINE_CONFIG" >/tmp/pacman-quattro-candidates.conf
  PACMAN_ONLINE_CONFIG=/tmp/pacman-quattro-candidates.conf
  local evidence=/out/build-evidence/$OMARCHY_BUILD_RUN_ID
  mkdir -p "$evidence"
  # The full image driver admits flat, phase-owned early evidence only.
  for filename in signing.json signing.json.sig manifest.json manifest.json.sig; do
    cp "$verified/$filename" "$evidence/verified-package-cache.candidate-$filename"
  done
}

verify_quattro_candidate_selection() {
  [[ -n ${OMARCHY_CANDIDATE_ROOT:-} ]] || return 0
  local filename
  if [[ ${OMARCHY_CANDIDATE_SCHEMA:-3} == 4 ]] &&
    grep -Eq '^omarchy-(apple-boot|first-boot)-' "$requested_package_files"; then
    echo "Legacy boot package selected alongside Limine candidate" >&2
    return 1
  fi
  for filename in "${candidate_package_files[@]}"; do
    grep -Fxq "$filename" "$requested_package_files" || {
      echo "Candidate package was not selected: $filename" >&2
      return 1
    }
  done
  if grep -Eq '^omarchy-(dev|settings-dev)-' "$requested_package_files"; then
    echo "Old desktop package selected alongside the quattro candidate" >&2
    return 1
  fi
}
