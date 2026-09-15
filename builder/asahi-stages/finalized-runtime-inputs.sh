#!/bin/bash

# Materialize only the ARM repository inputs consumed during boot finalization.
# These files are independently hashed by the finalized-boot runtime manifest.

prepare_finalized_runtime_inputs() {
  local runtime_root="$build_cache_dir/airootfs/usr/share/omarchy-iso"
  local installed_pacman_source=/configs/airootfs/usr/share/omarchy-iso/pacman-online-installed-arm.conf
  if [[ ${ASAHI_KERNEL_PACKAGE:-linux-asahi} == linux-aurora ]]; then
    installed_pacman_source=/configs/airootfs/usr/share/omarchy-iso/pacman-online-installed-arm-aurora.conf
  fi

  [[ $OMARCHY_ARCH == aarch64 ]] || return 0
  mkdir -p "$runtime_root"
  install -m 0644 "$offline_mirror_dir/ARM-REPOSITORY" \
    "$runtime_root/arm-repository"
  install -m 0644 "$offline_mirror_dir/ARM-RUNTIME" \
    "$runtime_root/arm-runtime"
  install -m 0644 "$offline_mirror_dir/ARM-RUNTIME-CHANNEL" \
    "$runtime_root/arm-runtime-channel"
  install -m 0644 /builder/omarchy-arm-repository.asc \
    "$runtime_root/omarchy-arm-repository.asc"

  validate_installed_arm_pacman_config "$installed_pacman_source" \
    "${ASAHI_KERNEL_PACKAGE:-linux-asahi}" || return 1
}

# The tracked installed configuration is the single source of truth for the
# repositories an installed system keeps, and the stage projection carries
# it into the media root verbatim. Deriving it from the build-time
# configuration silently dropped [omarchy] and [asahi-alarm], which broke
# omarchy-update-asahi-bundle on the first installed system. Validate the
# tracked file fail-closed here: the signed repositories must be present
# and no build-only section or local path may survive.
validate_installed_arm_pacman_config() {
  local config=$1 kernel=$2
  if ! grep -Fxq 'Architecture = aarch64' "$config" ||
    ! grep -Fxq '[core]' "$config" ||
    ! grep -Fxq '[omarchy]' "$config" ||
    ! grep -Fxq '[asahi-alarm]' "$config" ||
    ! grep -Eq '^Server = https://' "$config"; then
    echo "ERROR: installed ARM pacman configuration lacks a signed repository: $config" >&2
    return 1
  fi
  if grep -Eq '^\[arm-snapshots\]$|^Server = file://' "$config"; then
    echo "ERROR: installed ARM pacman configuration retains build-only paths" >&2
    return 1
  fi

  [[ $kernel == linux-aurora ]] || return 0
  # An Aurora install receives its kernel and m1n1 only from [omarchy-aurora],
  # and only while that repository sits ahead of [omarchy].
  local aurora_line omarchy_line
  aurora_line=$(grep -nFx -m1 '[omarchy-aurora]' "$config" | cut -d: -f1) || true
  omarchy_line=$(grep -nFx -m1 '[omarchy]' "$config" | cut -d: -f1) || true
  if [[ -z $aurora_line ]] || (( aurora_line > omarchy_line )) ||
    ! awk '
      /^\[/ { section = $0 }
      section == "[omarchy-aurora]" && /^Server = https:\/\// { found = 1 }
      END { exit !found }
    ' "$config"; then
    echo "ERROR: Aurora pacman configuration must list [omarchy-aurora] with an https server before [omarchy]: $config" >&2
    return 1
  fi
}
