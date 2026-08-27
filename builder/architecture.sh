#!/bin/bash

OMARCHY_ARCH=${OMARCHY_ARCH:-x86_64}
if [[ -z ${OMARCHY_MEDIA_TARGET:-} ]]; then
  if [[ $OMARCHY_ARCH == "x86_64" ]]; then
    OMARCHY_MEDIA_TARGET=x86_64/pc
  else
    OMARCHY_MEDIA_TARGET=aarch64/generic
  fi
fi

case "$OMARCHY_ARCH:$OMARCHY_MEDIA_TARGET" in
  x86_64:x86_64/pc)
    OMARCHY_PLATFORM=pc
    OMARCHY_BOOT_BACKEND=limine
    OMARCHY_ARTIFACT_KIND=iso
    OMARCHY_MEDIA_TARGET_READY=1
    ;;
  aarch64:aarch64/generic)
    OMARCHY_PLATFORM=generic
    OMARCHY_BOOT_BACKEND=limine
    OMARCHY_ARTIFACT_KIND=iso
    OMARCHY_MEDIA_TARGET_READY=1
    ;;
  aarch64:aarch64/apple-silicon)
    OMARCHY_PLATFORM=apple-silicon
    OMARCHY_BOOT_BACKEND=asahi-grub
    OMARCHY_ARTIFACT_KIND=iso
    OMARCHY_APPLE_PLATFORM_SNAPSHOT=/builder/apple-platform-snapshot.json
    OMARCHY_MEDIA_TARGET_READY=0
    ;;
  *)
    echo "Unsupported architecture/media target: $OMARCHY_ARCH:$OMARCHY_MEDIA_TARGET" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac
export OMARCHY_MEDIA_TARGET OMARCHY_PLATFORM OMARCHY_BOOT_BACKEND
export OMARCHY_ARTIFACT_KIND OMARCHY_MEDIA_TARGET_READY
export OMARCHY_APPLE_PLATFORM_SNAPSHOT

case "$OMARCHY_ARCH" in
  x86_64)
    DISTRO_KEYRING_PACKAGE=archlinux-keyring
    DISTRO_KEYRING_NAME=archlinux
    NODE_DIST_ARCH=x64
    LIVE_KERNEL=linux-t2
    PROFILE_PACKAGES=packages.x86_64
    TARGET_BASE_PACKAGE_LIST=omarchy-base.packages
    TARGET_OTHER_PACKAGE_LIST=omarchy-other.packages
    PACMAN_ONLINE_CONFIG="/configs/pacman-online-${OMARCHY_MIRROR}.conf"
    BUILD_HOST_PACKAGES=(
      archiso git sudo base-devel jq grub imagemagick neovim nodejs npm tree-sitter-cli
    )
    LIVE_PACKAGES=(
      linux-t2 git gum jq openssl plymouth ttfx tzupdate omarchy-keyring
      "$OMARCHY_SETTINGS_PACKAGE" lvm2 cryptsetup parted
    )
    MKARCHISO=(mkarchiso)
    ;;
  aarch64)
    DISTRO_KEYRING_PACKAGE=archlinuxarm-keyring
    DISTRO_KEYRING_NAME=archlinuxarm
    NODE_DIST_ARCH=arm64
    LIVE_KERNEL=linux-aarch64
    LIVE_KERNEL_BOOT_NAME=Image
    LIVE_INITRAMFS_BOOT_NAME=initramfs-linux.img
    PROFILE_PACKAGES=packages.aarch64
    TARGET_BASE_PACKAGE_LIST=omarchy-base-asahi.packages
    TARGET_OTHER_PACKAGE_LIST=omarchy-other-asahi.packages
    PACMAN_ONLINE_CONFIG=/configs/pacman-online-arm.conf
    BUILD_HOST_PACKAGES=(
      arch-install-scripts dosfstools e2fsprogs findutils grub gzip libarchive
      libisoburn mtools openssl pacman sed squashfs-tools git sudo base-devel jq
      imagemagick neovim nodejs npm tree-sitter-cli
    )
    LIVE_PACKAGES=(
      linux-aarch64 git gum jq openssl plymouth omarchy-keyring
      "$OMARCHY_SETTINGS_PACKAGE" lvm2 cryptsetup parted
    )
    MKARCHISO=(/tmp/omarchy-mkarchiso-aarch64)
    ;;
  *)
    echo "Unsupported OMARCHY_ARCH: $OMARCHY_ARCH" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

if [[ $OMARCHY_MEDIA_TARGET == "aarch64/apple-silicon" ]]; then
  LIVE_KERNEL=linux-asahi
  LIVE_KERNEL_BOOT_NAME=vmlinuz-linux-asahi
  LIVE_INITRAMFS_BOOT_NAME=initramfs-linux-asahi.img
  LIVE_PACKAGES=(
    linux-asahi asahi-scripts asahi-alarm-keyring
    git gum jq openssl plymouth omarchy-keyring
    "$OMARCHY_SETTINGS_PACKAGE" lvm2 cryptsetup parted
  )
fi

export LIVE_KERNEL LIVE_KERNEL_BOOT_NAME LIVE_INITRAMFS_BOOT_NAME

filter_target_packages() {
  local line

  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $OMARCHY_MEDIA_TARGET == "aarch64/apple-silicon" ]]; then
      case "$line" in
        amd-ucode|intel-ucode|limine-mkinitcpio-hook|limine-snapper-sync|snapper|sof-firmware|tzupdate)
          continue
          ;;
        limine)
          line=grub
          ;;
        linux|linux-asahi)
          line=linux-asahi
          ;;
        linux-headers|linux-asahi-headers)
          line=linux-asahi-headers
          ;;
      esac
    elif [[ $OMARCHY_ARCH == "aarch64" ]]; then
      case "$line" in
        amd-ucode|asahi-desktop-meta|asahi-fwextract|intel-ucode|tzupdate|vulkan-asahi|widevine)
          continue
          ;;
        linux|linux-asahi)
          line=linux-aarch64
          ;;
        linux-headers|linux-asahi-headers)
          line=linux-aarch64-headers
          ;;
      esac
    fi
    printf '%s\n' "$line"
  done
}

prepare_archiso_profile() {
  local profile="$1"

  [[ $OMARCHY_ARCH == "aarch64" ]] || return 0

  mv "$profile/packages.x86_64" "$profile/packages.aarch64"
  sed -i -E '/^(amd-ucode|broadcom-wl|edk2-shell|hyperv|intel-ucode|linux|memtest86\+|memtest86\+-efi|open-vm-tools|refind|reflector|syslinux|virtualbox-guest-utils-nox)$/d' \
    "$profile/packages.aarch64"
  rm -f \
    "$profile/airootfs/etc/mkinitcpio.d/linux.preset" \
    "$profile/airootfs/etc/mkinitcpio.d/linux-t2.preset"
  if [[ $OMARCHY_MEDIA_TARGET == "aarch64/apple-silicon" ]]; then
    cat >"$profile/airootfs/etc/mkinitcpio.d/linux-asahi.preset" <<'EOF'
PRESETS=('archiso')
ALL_kver='/boot/vmlinuz-linux-asahi'
archiso_config='/etc/mkinitcpio.conf.d/archiso.conf'
archiso_image='/boot/initramfs-linux-asahi.img'
EOF
  fi
  sed -i \
    -e 's/ microcode / /' \
    -e 's/ memdisk / /' \
    "$profile/airootfs/etc/mkinitcpio.conf.d/archiso.conf"
  if [[ $OMARCHY_MEDIA_TARGET == "aarch64/apple-silicon" ]]; then
    sed -i -e 's/ filesystems/ asahi filesystems/' \
      "$profile/airootfs/etc/mkinitcpio.conf.d/archiso.conf"
  fi
  sed -i \
    -e "s/vmlinuz-linux-t2/$LIVE_KERNEL_BOOT_NAME/g" \
    -e "s/initramfs-linux-t2\\.img/$LIVE_INITRAMFS_BOOT_NAME/g" \
    "$profile/grub/grub.cfg" "$profile/grub/loopback.cfg"
}
