#!/bin/bash

# Replace the inherited custom repository, retaining the independently pinned
# Asahi platform. This function is called only for explicit candidate inputs.
prepare_quattro_dependency_packages() {
  [[ -n ${OMARCHY_CANDIDATE_ROOT:-} && $OMARCHY_MEDIA_TARGET == "aarch64/apple-silicon" &&
    $OMARCHY_BUILD_MODE == "diagnostic" && ${ASAHI_KERNEL_PACKAGE:-linux-asahi} == "linux-asahi" ]] || return 1
  local verified=/tmp/omarchy-dependencies-verified primary filename candidate_schema
  candidate_schema=$(jq -er '.schema' /tmp/omarchy-quattro-verified/manifest.json) || return 1
  local -a overlay_arguments=(--platform-overlay /builder/quattro-hyprland-repair.json)
  if [[ $candidate_schema == 4 ]]; then
    overlay_arguments+=(--platform-overlay /builder/quattro-limine.json)
  fi
  # Every composition decision precedes importing any candidate trust. The
  # full platform stays authenticated; only the declared U-Boot replacement
  # is omitted from the selected repository for schema 4.
  python3 /builder/quattro-dependencies.py --input "$OMARCHY_DEPENDENCY_ROOT" \
    --output "$verified" --manifest-sha256 "$OMARCHY_DEPENDENCY_SHA256" \
    --candidate-schema "$candidate_schema" \
    --candidate-manifest /tmp/omarchy-quattro-verified/manifest.json \
    --platform-manifest "$OMARCHY_APPLE_PLATFORM_SNAPSHOT" \
    "${overlay_arguments[@]}" --selected-platform /tmp/quattro-selected-platform || return 1
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
  mapfile -t apple_package_names </tmp/quattro-selected-platform
  for filename in "${apple_keyring_names[@]}" "${apple_package_names[@]}"; do
    archives+=("$offline_mirror_dir/$filename")
  done
  bash /builder/fetch-quattro-hyprland.sh "$offline_mirror_dir"
  archives+=("$offline_mirror_dir/$(jq -er '.filename' /builder/quattro-hyprland-repair.json)")
  if [[ $candidate_schema == 4 ]]; then
    bash /builder/fetch-quattro-limine.sh "$offline_mirror_dir" || return 1
    archives+=("$offline_mirror_dir/$(jq -er '.filename' /builder/quattro-limine.json)")
  fi
  # repo-add updates existing databases: remove inherited entries first.
  rm -f "$offline_mirror_dir"/arm-snapshots.{db,files}* "$offline_mirror_dir"/ARM-{REPOSITORY,RUNTIME,RUNTIME-CHANNEL,PACKAGES}
  repo-add "$offline_mirror_dir/arm-snapshots.db.tar.gz" "${archives[@]}"
  pacman --config "$PACMAN_ONLINE_CONFIG" --noconfirm -Sy omarchy-keyring
  pacman-key --populate omarchy
  local evidence=/out/build-evidence/$OMARCHY_BUILD_RUN_ID
  mkdir -p "$evidence"
  cp /builder/quattro-hyprland-repair.json "$evidence/verified-package-cache.hyprland-repair.json"
  if [[ $candidate_schema == 4 ]]; then
    cp /builder/quattro-limine.json "$evidence/verified-package-cache.limine.json"
  fi
  cp /tmp/quattro-selected-platform "$evidence/verified-package-cache.selected-platform.txt"
  for filename in manifest.json manifest.json.sig origin.db origin.db.sig; do
    cp "$verified/$filename" "$evidence/verified-package-cache.dependencies-$filename"
  done
}
