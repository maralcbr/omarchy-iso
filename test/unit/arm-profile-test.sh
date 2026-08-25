#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

export OMARCHY_ARCH=aarch64
export OMARCHY_MIRROR=stable
export OMARCHY_SETTINGS_PACKAGE=omarchy-settings-dev
source "$ROOT/builder/architecture.sh"

[[ $DISTRO_KEYRING_NAME == "archlinuxarm" ]]
[[ $NODE_DIST_ARCH == "arm64" ]]
[[ $LIVE_KERNEL == "linux-aarch64" ]]
[[ $PROFILE_PACKAGES == "packages.aarch64" ]]
[[ $TARGET_BASE_PACKAGE_LIST == "omarchy-base-asahi.packages" ]]
[[ $TARGET_OTHER_PACKAGE_LIST == "omarchy-other-asahi.packages" ]]
[[ ${MKARCHISO[*]} == "/archiso/archiso/mkarchiso" ]]

filtered=$(printf '%s\n' linux linux-headers linux-asahi linux-asahi-headers \
  asahi-desktop-meta asahi-fwextract vulkan-asahi widevine amd-ucode tzupdate base |
  filter_target_packages)
[[ $filtered == $'linux-aarch64\nlinux-aarch64-headers\nlinux-aarch64\nlinux-aarch64-headers\nbase' ]]

profile="$work/profile"
mkdir -p "$profile/airootfs/etc/mkinitcpio.d" "$profile/grub"
printf '%s\n' linux broadcom-wl memtest86+ base >"$profile/packages.x86_64"
touch \
  "$profile/airootfs/etc/mkinitcpio.d/linux.preset" \
  "$profile/airootfs/etc/mkinitcpio.d/linux-t2.preset"
printf 'linux /vmlinuz-linux-t2\ninitrd /initramfs-linux-t2.img\n' >"$profile/grub/grub.cfg"
cp "$profile/grub/grub.cfg" "$profile/grub/loopback.cfg"

prepare_archiso_profile "$profile"

[[ -f $profile/packages.aarch64 && ! -e $profile/packages.x86_64 ]]
[[ $(cat "$profile/packages.aarch64") == "base" ]]
[[ ! -e $profile/airootfs/etc/mkinitcpio.d/linux.preset ]]
[[ ! -e $profile/airootfs/etc/mkinitcpio.d/linux-t2.preset ]]
grep -Fq 'linux /Image' "$profile/grub/grub.cfg"
grep -Fq 'initrd /initramfs-linux.img' "$profile/grub/grub.cfg"

profile_values=$(
  export OMARCHY_ARCH=aarch64 SOURCE_DATE_EPOCH=0
  declare -A file_permissions=()
  source "$ROOT/configs/profiledef.sh"
  printf '%s|%s\n' "$arch" "${bootmodes[*]}"
)
[[ $profile_values == "aarch64|uefi.grub" ]]

echo "ARM profile tests passed"
