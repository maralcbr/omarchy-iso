#!/bin/bash

# Package-only architecture and role selection. This file deliberately contains
# no boot-profile, GRUB, initramfs, or release-media implementation so those
# downstream changes cannot invalidate the verified package cache.

# The kernel the Apple Silicon payload installs. Read straight out of the
# product descriptor with sed rather than jq: this runs before the build host
# packages are installed, and a missing jq must not silently downgrade an
# Aurora build to the Asahi kernel.
asahi_kernel_package() {
  local product=${OMARCHY_ASAHI_PRODUCT:-}
  local kernel

  [[ -n $product ]] || { printf 'linux-asahi\n'; return 0; }
  [[ -r $product ]] || {
    echo "product descriptor is unreadable: $product" >&2
    return 1
  }
  kernel=$(sed -n 's/^[[:space:]]*"kernel_package"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    "$product" | head -1)
  [[ $kernel =~ ^linux-[a-z0-9]+$ ]] || {
    echo "product descriptor names no usable kernel package: $product" >&2
    return 1
  }
  printf '%s\n' "$kernel"
}

select_omarchy_package_roles() {
  OMARCHY_ISO_REF=${OMARCHY_ISO_REF:-quattro}
  OMARCHY_ARCH=${OMARCHY_ARCH:-x86_64}
  ASAHI_KERNEL_PACKAGE=$(asahi_kernel_package)

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
  if [[ -n ${OMARCHY_CANDIDATE_ROOT:-} ]]; then
    OMARCHY_RUNTIME_PACKAGE=omarchy
    OMARCHY_SETTINGS_PACKAGE=omarchy-settings
  fi
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

  if [[ -n ${OMARCHY_CANDIDATE_ROOT:-} ]]; then
    TARGET_BASE_PACKAGE_LIST=omarchy-base.packages
    TARGET_OTHER_PACKAGE_LIST=omarchy-other.packages
  fi
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
      # Shared Quattro manifests include other machines' optional drivers.
      # Keep this an explicit hardware policy, never a skip-if-unavailable rule:
      # missing required Apple packages must still fail dependency resolution.
      if [[ -n ${OMARCHY_CANDIDATE_ROOT:-} ]]; then
        case "$line" in
          apple-bcm-firmware|apple-t2-audio-config|broadcom-wl-dkms|dell-xps13-sidecar-amps|dell-xps-touchpad-haptics|intel-ipu7-camera|intel-lpmd|intel-media-driver|libva-intel-driver|linux-omarchy|linux-omarchy-headers|linux-t2|linux-t2-headers|macbook12-spi-driver-dkms|qmk-hid|t2fanrd|thermald|tuxedo-drivers-nocompatcheck-dkms|vpl-gpu-rt|yt6801-dkms|asusctl|vulkan-intel|vulkan-radeon|linux-firmware-marvell|libvpl|egl-wayland|nvidia-dkms|nvidia-open-dkms|nvidia-580xx-dkms|nvidia-580xx-utils|nvidia-utils|lib32-nvidia-580xx-utils|lib32-nvidia-utils|libva-nvidia-driver|yay-debug)
            continue
            ;;
          dotnet-runtime)
            [[ -z ${OMARCHY_DEPENDENCY_ROOT:-} ]] || line=dotnet-runtime-bin
            ;;
          mise-bin)
            [[ -n ${OMARCHY_DEPENDENCY_ROOT:-} ]] || line=mise
            ;;
        esac
      fi
      case "$line" in
        amd-ucode|intel-ucode|sof-firmware)
          continue
          ;;
        limine-mkinitcpio-hook|limine-snapper-sync)
          [[ -n ${OMARCHY_CANDIDATE_ROOT:-} && ${OMARCHY_CANDIDATE_SCHEMA:-3} == 4 ]] || continue
          ;;
        snapper)
          [[ -n ${OMARCHY_CANDIDATE_ROOT:-} ]] || continue
          ;;
        limine)
          [[ -n ${OMARCHY_CANDIDATE_ROOT:-} && ${OMARCHY_CANDIDATE_SCHEMA:-3} == 4 ]] || line=grub
          ;;
        linux|linux-asahi|linux-aurora)
          line=${ASAHI_KERNEL_PACKAGE:-linux-asahi}
          ;;
        linux-headers|linux-asahi-headers|linux-aurora-headers)
          line=${ASAHI_KERNEL_PACKAGE:-linux-asahi}-headers
          ;;
      esac
    elif [[ $OMARCHY_ARCH == aarch64 ]]; then
      case "$line" in
        amd-ucode|asahi-desktop-meta|asahi-fwextract|intel-ucode|vulkan-asahi|widevine)
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
