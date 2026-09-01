#!/bin/bash

# Package-only architecture and role selection. This file deliberately contains
# no boot-profile, GRUB, initramfs, or release-media implementation so those
# downstream changes cannot invalidate the verified package cache.

select_omarchy_package_roles() {
  OMARCHY_ISO_REF=${OMARCHY_ISO_REF:-quattro}
  OMARCHY_ARCH=${OMARCHY_ARCH:-x86_64}

  # Edge, dev, local-source, and every ARM build consume the Quattro package
  # recipes explicitly. Other x86 releases use the published stable roles.
  case "$OMARCHY_ARCH:$OMARCHY_ISO_REF" in
    aarch64:*)
      : "${OMARCHY_RUNTIME_PACKAGE:=omarchy-dev}"
      : "${OMARCHY_SETTINGS_PACKAGE:=omarchy-settings-dev}"
      ;;
    x86_64:edge|x86_64:dev|x86_64:local)
      : "${OMARCHY_RUNTIME_PACKAGE:=omarchy-dev}"
      : "${OMARCHY_SETTINGS_PACKAGE:=omarchy-settings-dev}"
      ;;
    *)
      : "${OMARCHY_RUNTIME_PACKAGE:=omarchy}"
      : "${OMARCHY_SETTINGS_PACKAGE:=omarchy-settings}"
      ;;
  esac
  : "${OMARCHY_NVIM_PACKAGE:=omarchy-nvim}"
  export OMARCHY_RUNTIME_PACKAGE OMARCHY_SETTINGS_PACKAGE OMARCHY_NVIM_PACKAGE
}

configure_package_architecture() {
  case "$OMARCHY_ARCH" in
    x86_64)
      DISTRO_KEYRING_PACKAGE=archlinux-keyring
      DISTRO_KEYRING_NAME=archlinux
      NODE_DIST_ARCH=x64
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
      ;;
    aarch64)
      DISTRO_KEYRING_PACKAGE=archlinuxarm-keyring
      DISTRO_KEYRING_NAME=archlinuxarm
      NODE_DIST_ARCH=arm64
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
      ;;
    *)
      echo "Unsupported OMARCHY_ARCH: $OMARCHY_ARCH" >&2
      return 1
      ;;
  esac

  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
    # lsinitcpio is required to verify mkinitcpio's early-CPIO-plus-compressed
    # Asahi initramfs format.
    BUILD_HOST_PACKAGES+=(mkinitcpio)
    LIVE_PACKAGES=(
      linux-asahi asahi-scripts asahi-alarm-keyring
      git gum jq openssl plymouth omarchy-keyring
      "$OMARCHY_SETTINGS_PACKAGE"
    )
    if [[ $OMARCHY_ARTIFACT_KIND == asahi-os-package ]]; then
      BUILD_HOST_PACKAGES+=(archinstall btrfs-progs python)
    fi
  fi
}

uses_verified_package_checkpoint() {
  [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon &&
    $OMARCHY_ARTIFACT_KIND == asahi-os-package ]]
}

filter_target_packages() {
  local line

  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
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
    elif [[ $OMARCHY_ARCH == aarch64 ]]; then
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

prepare_package_profile() {
  local profile=$1

  [[ $OMARCHY_ARCH == aarch64 ]] || return 0
  mv "$profile/packages.x86_64" "$profile/packages.aarch64"
  sed -i.bak -E '/^(amd-ucode|broadcom-wl|edk2-shell|hyperv|intel-ucode|linux|memtest86\+|memtest86\+-efi|open-vm-tools|refind|reflector|syslinux|virtualbox-guest-utils-nox)$/d' \
    "$profile/packages.aarch64"
  rm -f -- "$profile/packages.aarch64.bak"
}
