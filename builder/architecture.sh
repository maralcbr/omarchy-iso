#!/bin/bash

OMARCHY_ARCH=${OMARCHY_ARCH:-x86_64}

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
    MKARCHISO=(/archiso/archiso/mkarchiso)
    ;;
  *)
    echo "Unsupported OMARCHY_ARCH: $OMARCHY_ARCH" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

filter_target_packages() {
  local line

  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $OMARCHY_ARCH == "aarch64" ]]; then
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
  sed -i \
    -e 's/vmlinuz-linux-t2/Image/g' \
    -e 's/initramfs-linux-t2\.img/initramfs-linux.img/g' \
    "$profile/grub/grub.cfg" "$profile/grub/loopback.cfg"
}
