#!/bin/bash

# Shared byte-producing image operations for configured-target and
# finalized-boot. The package controller sources this only after base-images
# has completed, so changes here cannot invalidate the verified base image.

unmount_target_tree() {
  local target=$1
  local attempt

  mountpoint -q "$target" || return 0
  umount -R -- "$target" && return 0

  echo "build-asahi-os-package: target remained busy; inspecting scoped holders" >&2
  findmnt -R -o TARGET,SOURCE,FSTYPE,OPTIONS --target "$target" >&2 || true
  fuser -v -m -M "$target" >&2 || true

  # arch-chroot subprocesses such as gpg-agent may outlive their parent while
  # retaining the target as their process root. This Docker build has its own
  # PID namespace, and -M refuses to signal anything unless the exact temporary
  # image target is still a mountpoint.
  fuser -k -TERM -m -M "$target" >/dev/null 2>&1 || true
  for attempt in 1 2 3 4 5; do
    sleep 1
    umount -R -- "$target" && return 0
  done

  fuser -k -KILL -m -M "$target" >/dev/null 2>&1 || true
  umount -R -- "$target"
}

detach_images() {
  local index
  if [[ -n ${target:-} ]] && mountpoint -q "$target"; then
    unmount_target_tree "$target"
  fi
  mounts=()
  for ((index=${#loops[@]} - 1; index >= 0; index--)); do
    losetup -d "${loops[index]}"
  done
  loops=()
}

# The loop-backed images come out of a build session dense: most of their
# free space has been written as literal zeros, so a 34 GB root image that
# holds a few GB of files is stored, hashed, and read back as ~31 GB. Punch
# those zero runs back into holes before the checkpoint store copies the
# image. Content and sha256 are unchanged; only allocation changes.
dig_image_holes() {
  local image
  for image in "$@"; do
    [[ -f $image && ! -L $image ]] || fail "cannot dig holes in $image"
    fallocate --dig-holes -- "$image"
  done
}

attach_images() {
  local image_directory=$1
  target=$work/target
  mkdir -p "$target"
  root_loop=$(losetup --find --show "$image_directory/root.img")
  boot_loop=$(losetup --find --show "$image_directory/boot.img")
  esp_loop=$(losetup --find --show "$image_directory/esp-build.img")
  loops=("$root_loop" "$boot_loop" "$esp_loop")
  mount -o subvol=@,compress=zstd "$root_loop" "$target"
  mounts=("$target")
  mkdir -p "$target/home" "$target/var/log" "$target/var/cache/pacman/pkg"
  mount -o subvol=@home,compress=zstd "$root_loop" "$target/home"
  mounts+=("$target/home")
  mount -o subvol=@log,compress=zstd "$root_loop" "$target/var/log"
  mounts+=("$target/var/log")
  mount -o subvol=@pkg,compress=zstd "$root_loop" "$target/var/cache/pacman/pkg"
  mounts+=("$target/var/cache/pacman/pkg")
  mkdir -p "$target/boot"
  mount "$boot_loop" "$target/boot"
  mounts+=("$target/boot")
  mkdir -p "$target/boot/efi"
  mount "$esp_loop" "$target/boot/efi"
  mounts+=("$target/boot/efi")
}

write_install_config() {
  local config=$1
  jq -n \
    --arg target "$target" \
    --arg root_device "$root_loop" \
    --arg boot_device "$boot_loop" \
    --arg esp_device "$esp_loop" \
    --arg kernel "$kernel_package" \
    --argjson build_jobs "$build_jobs" \
    '{"app_config": null, "archinstall-language": "English", "auth_config": {},
      "audio_config": {"audio": "pipewire"},
      "bootloader_config": {"bootloader": "Grub", "uki": false, "removable": true},
      "custom_commands": [],
      "disk_config": {"config_type": "pre_mounted_config", "mountpoint": $target},
      "hostname": "omarchy", "kernels": [$kernel],
      "locale_config": {"kb_layout": "us", "sys_enc": "UTF-8", "sys_lang": "en_US.UTF-8"},
      "mirror_config": {"custom_repositories": [], "custom_servers": [], "mirror_regions": {}, "optional_repositories": []},
      "network_config": {"type": "iso"}, "ntp": true, "packages": [],
      "parallel_downloads": $build_jobs,
      "profile_config": {"gfx_driver": null, "greeter": null, "profile": {}},
      "script": null, "services": [], "swap": true, "timezone": "UTC", "version": "4.4",
      "omarchy_install": {"mode": "protected", "defer_provisioning": true,
        "target_mount": $target,
        "boot": {"backend": "asahi-grub", "esp_mount": "/boot/efi",
          "esp_path": "/EFI/BOOT", "efi_binary": "BOOTAA64.EFI",
          "enable_fallback": true, "register_firmware": false},
        "storage": {"root_device": $root_device, "boot_device": $boot_device,
          "boot_mount": "/boot", "esp_device": $esp_device,
          "kernel": $kernel, "grow_root": true}}}' >"$config"
}

run_orchestrator_stage() {
  local stage=$1 state=$2 runner=$3 stage_source_root=$4
  local config=$work/$stage-configuration.json
  local credentials=$work/user-credentials.json
  local configured_timing_evidence=$run_evidence/configured-orchestrator-timing.json
  write_install_config "$config"
  printf '{"users":[]}\n' >"$credentials"
  export OMARCHY_INSTALL_CONFIG="$config"
  export OMARCHY_INSTALL_CREDS="$credentials"
  export OMARCHY_INSTALL_STATE_DIR="$state"
  export OMARCHY_ASAHI_ORCHESTRATOR_RUN_EVIDENCE="$run_evidence/$stage-orchestrator-timing.json"
  export OMARCHY_ASAHI_CONFIGURED_TIMING_EVIDENCE="$configured_timing_evidence"
  export OMARCHY_INSTALL_LOG_FILE="$run_evidence/$stage-orchestrator.log"
  export OMARCHY_INSTALL_TIMING_FILE="$run_evidence/$stage-orchestrator-timing.json"
  if [[ $stage == finalized && ! -f $configured_timing_evidence ]]; then
    unset OMARCHY_ASAHI_CONFIGURED_TIMING_EVIDENCE
  fi
  export OMARCHY_MIRROR="${OMARCHY_MIRROR:-stable}"
  export OMARCHY_ISO_REF="${OMARCHY_ISO_REF:-quattro}"
  export OMARCHY_ISO_MEDIA_ROOT="$stage_source_root"
  export OMARCHY_OFFLINE_MIRROR_ROOT="$offline_mirror"
  export PYTHONDONTWRITEBYTECODE=1
  export PYTHONPATH="$stage_source_root"
  cd "$stage_source_root"
  python3 "$runner"
}
