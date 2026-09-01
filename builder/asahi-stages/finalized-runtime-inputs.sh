#!/bin/bash

# Materialize only the ARM repository inputs consumed during boot finalization.
# These files are independently hashed by the finalized-boot runtime manifest.

prepare_finalized_runtime_inputs() {
  local runtime_root="$build_cache_dir/airootfs/usr/share/omarchy-iso"
  local installed_pacman_source=/configs/airootfs/usr/share/omarchy-iso/pacman-online-installed-arm.conf

  [[ $OMARCHY_ARCH == aarch64 ]] || return 0
  mkdir -p "$runtime_root"
  install -m 0644 "$offline_mirror_dir/ARM-REPOSITORY" \
    "$runtime_root/arm-repository"
  install -m 0644 "$offline_mirror_dir/ARM-RUNTIME" \
    "$runtime_root/arm-runtime"
  install -m 0644 /builder/omarchy-arm-repository.asc \
    "$runtime_root/omarchy-arm-repository.asc"

  # The tracked installed configuration is the single source of truth for the
  # repositories an installed system keeps, and the stage projection carries
  # it into the media root verbatim. Deriving it from the build-time
  # configuration silently dropped [omarchy] and [asahi-alarm], which broke
  # omarchy-update-asahi-bundle on the first installed system. Validate the
  # tracked file fail-closed here: the signed repositories must be present
  # and no build-only section or local path may survive.
  grep -Fxq 'Architecture = aarch64' "$installed_pacman_source"
  grep -Fxq '[core]' "$installed_pacman_source"
  grep -Fxq '[omarchy]' "$installed_pacman_source"
  grep -Fxq '[asahi-alarm]' "$installed_pacman_source"
  grep -Eq '^Server = https://' "$installed_pacman_source"
  if grep -Eq '^\[arm-snapshots\]$|^Server = file://' \
    "$installed_pacman_source"; then
    echo "ERROR: installed ARM pacman configuration retains build-only paths" >&2
    return 1
  fi
}
