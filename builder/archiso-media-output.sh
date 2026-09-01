#!/bin/bash

# Generic/validation ISO producer. The Asahi full-OS package exits through a
# separate dispatch path, so media-only implementation changes remain terminal.

prepare_archiso_media_profile() {
  local profile=$1
  local archiso_config="$profile/airootfs/etc/mkinitcpio.conf.d/archiso.conf"
  local grub_config="$profile/grub/grub.cfg"
  local loopback_config="$profile/grub/loopback.cfg"

  [[ $OMARCHY_ARCH == aarch64 ]] || return 0
  rm -f \
    "$profile/airootfs/etc/mkinitcpio.d/linux.preset" \
    "$profile/airootfs/etc/mkinitcpio.d/linux-t2.preset"
  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
    cat >"$profile/airootfs/etc/mkinitcpio.d/linux-asahi.preset" <<'EOF'
PRESETS=('archiso')
ALL_kver='/boot/vmlinuz-linux-asahi'
archiso_config='/etc/mkinitcpio.conf.d/archiso.conf'
archiso_image='/boot/initramfs-linux-asahi.img'
EOF
  fi
  sed -i.bak -e 's/ microcode / /' -e 's/ memdisk / /' "$archiso_config"
  rm -f -- "$archiso_config.bak"
  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
    sed -i.bak -e 's/ filesystems/ asahi filesystems/' "$archiso_config"
    rm -f -- "$archiso_config.bak"
    sed -i.bak -E \
      '/^[[:space:]]*linux / s/$/ systemd.gpt_auto=0 rd.systemd.gpt_auto=0 fstab=no rd.fstab=no/' \
      "$grub_config" "$loopback_config"
    rm -f -- "$grub_config.bak" "$loopback_config.bak"
  fi
  sed -i.bak \
    -e "s/vmlinuz-linux-t2/$LIVE_KERNEL_BOOT_NAME/g" \
    -e "s/initramfs-linux-t2\\.img/$LIVE_INITRAMFS_BOOT_NAME/g" \
    "$grub_config" "$loopback_config"
  rm -f -- "$grub_config.bak" "$loopback_config.bak"
}

prepare_archiso_profile() {
  prepare_package_profile "$1"
  prepare_archiso_media_profile "$1"
}

seal_apple_validation_profile() {
  local profile=$1
  local live_root="$profile/airootfs"
  local marker="$live_root/usr/share/omarchy-iso/apple-media-validation"

  [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]] || return 0
  mkdir -p "${marker%/*}"
  cat >"$marker" <<EOF
schema_version=1
mode=read-only-canary
source_commit=${OMARCHY_ISO_SOURCE_COMMIT:-unknown}
EOF
  rm -f \
    "$live_root/root/configurator" \
    "$live_root/usr/local/bin/omarchy-cidata-load" \
    "$live_root/usr/local/bin/omarchy-install-dashboard" \
    "$live_root/usr/local/bin/omarchy-iso-cleanup-disk" \
    "$live_root/usr/local/bin/omarchy-iso-install" \
    "$live_root/usr/share/omarchy-iso/disk-partitioning.sh" \
    "$live_root/usr/share/omarchy-iso/setup-form.sh"
  rm -rf "$live_root/usr/share/omarchy-iso/orchestrator"
}

prepare_archiso_media_inputs() {
  if [[ $OMARCHY_ARCH == aarch64 ]]; then
    cp /archiso/archiso/mkarchiso "${MKARCHISO[0]}"
    patch --forward --silent "${MKARCHISO[0]}" /builder/archiso-aarch64.patch
    chmod +x "${MKARCHISO[0]}"
  fi
  mkdir -p "$build_cache_dir" "$offline_mirror_dir"
  cp -r /archiso/configs/releng/* "$build_cache_dir/"
  rm "$build_cache_dir/airootfs/etc/motd"
  rm -rf \
    "$build_cache_dir/airootfs/etc/systemd/system/multi-user.target.wants/reflector.service" \
    "$build_cache_dir/airootfs/etc/systemd/system/reflector.service.d" \
    "$build_cache_dir/airootfs/etc/xdg/reflector"
  cp -r /configs/* "$build_cache_dir/"
  prepare_archiso_media_profile "$build_cache_dir"
  mkdir -p "$build_cache_dir/airootfs/usr/share/omarchy-iso"
  printf '%s\n' "$OMARCHY_MIRROR" >"$build_cache_dir/airootfs/root/omarchy_mirror"
  printf '%s\n' "$OMARCHY_ISO_REF" >"$build_cache_dir/airootfs/root/omarchy_iso_ref"
  printf '%s\n' "$OMARCHY_MEDIA_TARGET" \
    >"$build_cache_dir/airootfs/usr/share/omarchy-iso/media-target"
  jq -nc \
    --arg architecture "$OMARCHY_ARCH" --arg platform "$OMARCHY_PLATFORM" \
    --arg boot_backend "$OMARCHY_BOOT_BACKEND" \
    --arg artifact_kind "$OMARCHY_ARTIFACT_KIND" \
    '{schema_version: 1, architecture: $architecture, platform: $platform,
      boot_backend: $boot_backend, artifact_kind: $artifact_kind}' \
    >"$build_cache_dir/airootfs/usr/share/omarchy-iso/media-target.json"
  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
    install -m 0644 "$OMARCHY_APPLE_PLATFORM_SNAPSHOT" \
      "$build_cache_dir/airootfs/usr/share/omarchy-iso/apple-platform-snapshot.json"
  fi
}

build_archiso_media_output() {
  cp "$build_cache_dir/pacman-offline.conf" \
    "$build_cache_dir/airootfs/etc/pacman.conf"
  if [[ ${OMARCHY_INSTALL_DEBUG:-} == 1 ]]; then
    {
      echo 'debug=1'
      echo "built_at=$(date -Is)"
      echo "ref=$OMARCHY_ISO_REF"
      echo "mirror=$OMARCHY_MIRROR"
      echo "media_target=$OMARCHY_MEDIA_TARGET"
    } >"$build_cache_dir/airootfs/usr/share/omarchy-iso/build-info"
  fi
  seal_apple_validation_profile "$build_cache_dir"
  "${MKARCHISO[@]}" -v -w "$build_cache_dir/work/" -o /out/ "$build_cache_dir/"

  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
    local built_iso iso_sha256 iso_size snapshot_sha256
    built_iso=$(\ls -t /out/*-aarch64.iso | head -n1)
    /builder/verify-apple-media.sh "$built_iso" "$OMARCHY_APPLE_PLATFORM_SNAPSHOT"
    iso_sha256=$(sha256sum -- "$built_iso")
    iso_sha256=${iso_sha256%% *}
    iso_size=$(wc -c <"$built_iso")
    iso_size=${iso_size//[[:space:]]/}
    snapshot_sha256=$(sha256sum -- "$OMARCHY_APPLE_PLATFORM_SNAPSHOT")
    snapshot_sha256=${snapshot_sha256%% *}
    {
      printf '%s\n' \
        'schema_version=1' \
        'verification_kind=apple-build-environment' \
        "omarchy_iso_source_commit=${OMARCHY_ISO_SOURCE_COMMIT:-unknown}" \
        "archiso_source_commit=${OMARCHY_ARCHISO_SOURCE_COMMIT:-unknown}" \
        "source_date_epoch=${SOURCE_DATE_EPOCH:-unknown}" \
        "build_image=${OMARCHY_BUILD_IMAGE:-unknown}" \
        "architecture=$OMARCHY_ARCH" \
        "media_target=$OMARCHY_MEDIA_TARGET" \
        "artifact_filename=${built_iso##*/}" \
        "artifact_size=$iso_size" \
        "artifact_sha256=$iso_sha256" \
        "platform_snapshot_sha256=$snapshot_sha256" \
        "uname_machine=$(uname -m)" \
        '[container-os]'
      cat /etc/os-release
      echo '[build-host-packages]'
      pacman -Q | LC_ALL=C sort
    } >"$built_iso.apple-build-environment.txt"
  fi
  if [[ -n ${HOST_UID:-} && -n ${HOST_GID:-} ]]; then
    chown -R "$HOST_UID:$HOST_GID" /out/
  fi
}
