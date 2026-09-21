#!/bin/bash

# Replace the inherited custom repository, retaining the independently pinned
# Asahi platform. This function is called only for explicit candidate inputs.
prepare_quattro_dependency_packages() {
  [[ -n ${OMARCHY_CANDIDATE_ROOT:-} && $OMARCHY_MEDIA_TARGET == "aarch64/apple-silicon" &&
    $OMARCHY_BUILD_MODE == "diagnostic" && ${ASAHI_KERNEL_PACKAGE:-linux-asahi} == "linux-asahi" ]] || return 1
  local verified=/tmp/omarchy-dependencies-verified primary filename
  python3 /builder/quattro-dependencies.py --input "$OMARCHY_DEPENDENCY_ROOT" \
    --output "$verified" --manifest-sha256 "$OMARCHY_DEPENDENCY_SHA256" || return 1
  python3 - "$verified/manifest.json" "$OMARCHY_CANDIDATE_ROOT/manifest.json" <<'PYVERIFY' || return 1
import json, sys
sets = [{p['name'] for p in json.load(open(path))['packages']} for path in sys.argv[1:]]
if sets[0] & sets[1]:
    raise SystemExit('Candidate and dependency packages overlap')
PYVERIFY
  primary=$(jq -er '.primary_fingerprint' /builder/quattro-trust/policy.json)
  pacman-key --add /builder/quattro-trust/public.gpg
  pacman-key --lsign-key "$primary"
  local -a archives=()
  mapfile -t dependency_package_files < <(jq -er '.packages[].filename' "$verified/manifest.json")
  for filename in "${dependency_package_files[@]}"; do
    cp "$verified/$filename" "$verified/$filename.sig" "$offline_mirror_dir/"
    archives+=("$offline_mirror_dir/$filename")
  done
  bash /builder/fetch-apple-platform-snapshot.sh "$offline_mirror_dir"
  mapfile -t apple_keyring_names <"$offline_mirror_dir/APPLE-KEYRING"
  (( ${#apple_keyring_names[@]} == 1 ))
  bash /builder/install-apple-platform-keyring.sh "$OMARCHY_APPLE_PLATFORM_SNAPSHOT" \
    "$offline_mirror_dir/${apple_keyring_names[0]}"
  mapfile -t apple_package_names <"$offline_mirror_dir/APPLE-PACKAGES"
  for filename in "${apple_keyring_names[@]}" "${apple_package_names[@]}"; do
    archives+=("$offline_mirror_dir/$filename")
  done
  # repo-add updates existing databases: remove inherited entries first.
  rm -f "$offline_mirror_dir"/arm-snapshots.{db,files}* "$offline_mirror_dir"/ARM-{REPOSITORY,RUNTIME,RUNTIME-CHANNEL,PACKAGES}
  repo-add "$offline_mirror_dir/arm-snapshots.db.tar.gz" "${archives[@]}"
  pacman --config "$PACMAN_ONLINE_CONFIG" --noconfirm -Sy omarchy-keyring
  pacman-key --populate omarchy
  local evidence=/out/build-evidence/$OMARCHY_BUILD_RUN_ID
  mkdir -p "$evidence"
  for filename in manifest.json manifest.json.sig origin.db origin.db.sig; do
    cp "$verified/$filename" "$evidence/verified-package-cache.dependencies-$filename"
  done
}
