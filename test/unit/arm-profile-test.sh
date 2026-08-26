#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

export OMARCHY_ARCH=aarch64
export OMARCHY_MEDIA_TARGET=aarch64/generic
export OMARCHY_MIRROR=stable
export OMARCHY_SETTINGS_PACKAGE=omarchy-settings-dev
source "$ROOT/builder/architecture.sh"

[[ $DISTRO_KEYRING_NAME == "archlinuxarm" ]]
[[ $NODE_DIST_ARCH == "arm64" ]]
[[ $LIVE_KERNEL == "linux-aarch64" ]]
[[ $LIVE_KERNEL_BOOT_NAME == "Image" ]]
[[ $LIVE_INITRAMFS_BOOT_NAME == "initramfs-linux.img" ]]
[[ $PROFILE_PACKAGES == "packages.aarch64" ]]
[[ $TARGET_BASE_PACKAGE_LIST == "omarchy-base-asahi.packages" ]]
[[ $TARGET_OTHER_PACKAGE_LIST == "omarchy-other-asahi.packages" ]]
[[ ${MKARCHISO[*]} == "/tmp/omarchy-mkarchiso-aarch64" ]]
[[ $OMARCHY_MEDIA_TARGET == "aarch64/generic" ]]
[[ $OMARCHY_PLATFORM == "generic" ]]
[[ $OMARCHY_BOOT_BACKEND == "limine" ]]
[[ $OMARCHY_ARTIFACT_KIND == "iso" ]]
(( OMARCHY_MEDIA_TARGET_READY == 1 ))

apple_contract=$(
  export OMARCHY_ARCH=aarch64 OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
  source "$ROOT/builder/architecture.sh"
  printf '%s|%s|%s|%s|%s\n' \
    "$OMARCHY_PLATFORM" "$OMARCHY_BOOT_BACKEND" \
    "$OMARCHY_ARTIFACT_KIND" "$OMARCHY_MEDIA_TARGET_READY" \
    "$OMARCHY_APPLE_PLATFORM_SNAPSHOT"
)
[[ $apple_contract == "apple-silicon|asahi-grub|iso|0|/builder/apple-platform-snapshot.json" ]]

apple_live_profile=$(
  export OMARCHY_ARCH=aarch64 OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
  source "$ROOT/builder/architecture.sh"
  printf '%s|%s|%s|%s\n' "$LIVE_KERNEL" "$LIVE_KERNEL_BOOT_NAME" \
    "$LIVE_INITRAMFS_BOOT_NAME" "${LIVE_PACKAGES[*]}"
)
[[ $apple_live_profile == linux-asahi\|vmlinuz-linux-asahi\|initramfs-linux-asahi.img\|linux-asahi\ asahi-scripts* ]]

filtered=$(printf '%s\n' linux linux-headers linux-asahi linux-asahi-headers \
  asahi-desktop-meta asahi-fwextract vulkan-asahi widevine amd-ucode tzupdate base |
  filter_target_packages)
[[ $filtered == $'linux-aarch64\nlinux-aarch64-headers\nlinux-aarch64\nlinux-aarch64-headers\nbase' ]]

apple_filtered=$(
  export OMARCHY_ARCH=aarch64 OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
  source "$ROOT/builder/architecture.sh"
  printf '%s\n' linux linux-headers linux-asahi linux-asahi-headers \
    asahi-desktop-meta asahi-fwextract vulkan-asahi widevine amd-ucode tzupdate \
    limine limine-mkinitcpio-hook limine-snapper-sync snapper sof-firmware base |
    filter_target_packages
)
[[ $apple_filtered == $'linux-asahi\nlinux-asahi-headers\nlinux-asahi\nlinux-asahi-headers\nasahi-desktop-meta\nasahi-fwextract\nvulkan-asahi\nwidevine\ngrub\nbase' ]]
grep -Fq 'required_package_files+=("${apple_package_names[@]}")' \
  "$ROOT/builder/build-iso.sh"
for package in asahi-audio asahi-fwextract asahi-scripts grub linux-asahi \
  linux-asahi-headers m1n1 speakersafetyd uboot-asahi; do
  grep -Eq "[[:space:]]${package}([[:space:]\\\\]|$)" "$ROOT/builder/build-iso.sh" || {
    echo "Apple target transaction does not explicitly select $package" >&2
    exit 1
  }
done

profile="$work/profile"
mkdir -p \
  "$profile/airootfs/etc/mkinitcpio.d" \
  "$profile/airootfs/etc/mkinitcpio.conf.d" \
  "$profile/grub"
printf '%s\n' linux broadcom-wl memtest86+ base >"$profile/packages.x86_64"
printf '%s\n' \
  'HOOKS=(base udev microcode modconf kms memdisk archiso filesystems)' \
  >"$profile/airootfs/etc/mkinitcpio.conf.d/archiso.conf"
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
grep -Fq 'HOOKS=(base udev modconf kms archiso filesystems)' \
  "$profile/airootfs/etc/mkinitcpio.conf.d/archiso.conf"
grep -Fq 'linux /Image' "$profile/grub/grub.cfg"
grep -Fq 'initrd /initramfs-linux.img' "$profile/grub/grub.cfg"
grep -Fq 'kernel_images=("${pacstrap_dir}/boot/Image")' "$ROOT/builder/archiso-aarch64.patch"
grep -Fq 'efiboot_files+=("${work_dir}/BOOT${uefi_arch[$arch]}.EFI")' \
  "$ROOT/builder/archiso-aarch64.patch"
grep -Fq 'required_grubmodules=(configfile iso9660 linux normal search search_fs_uuid)' \
  "$ROOT/builder/archiso-aarch64.patch"
grep -Fq 'patch --forward --silent "${MKARCHISO[0]}" /builder/archiso-aarch64.patch' \
  "$ROOT/builder/build-iso.sh"

apple_profile="$work/apple-profile"
cp -a "$profile" "$apple_profile"
mv "$apple_profile/packages.aarch64" "$apple_profile/packages.x86_64"
rm -f "$apple_profile/airootfs/etc/mkinitcpio.d/linux-asahi.preset"
printf 'linux /vmlinuz-linux-t2\ninitrd /initramfs-linux-t2.img\n' \
  >"$apple_profile/grub/grub.cfg"
cp "$apple_profile/grub/grub.cfg" "$apple_profile/grub/loopback.cfg"
export OMARCHY_ARCH=aarch64 OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
source "$ROOT/builder/architecture.sh"
prepare_archiso_profile "$apple_profile"
grep -Fxq "PRESETS=('archiso')" \
  "$apple_profile/airootfs/etc/mkinitcpio.d/linux-asahi.preset"
grep -Fq "ALL_kver='/boot/vmlinuz-linux-asahi'" \
  "$apple_profile/airootfs/etc/mkinitcpio.d/linux-asahi.preset"
grep -Fq 'asahi filesystems' \
  "$apple_profile/airootfs/etc/mkinitcpio.conf.d/archiso.conf"
grep -Fq 'linux /vmlinuz-linux-asahi' "$apple_profile/grub/grub.cfg"
grep -Fq 'initrd /initramfs-linux-asahi.img' "$apple_profile/grub/grub.cfg"
grep -Fq 'aarch64' "$ROOT/builder/archiso-aarch64.patch"
grep -Fq -- '-e "${pacstrap_dir}/boot/Image"' "$ROOT/builder/archiso-aarch64.patch"

profile_values=$(
  export OMARCHY_ARCH=aarch64 SOURCE_DATE_EPOCH=0
  declare -A file_permissions=()
  source "$ROOT/configs/profiledef.sh"
  printf '%s|%s|%s\n' "$arch" "${bootmodes[*]}" "${airootfs_image_tool_options[*]}"
)
[[ $profile_values == *"aarch64|uefi.grub|-comp xz "* ]]
[[ $profile_values != *"zstd"* ]]

x86_profile_values=$(
  export OMARCHY_ARCH=x86_64 SOURCE_DATE_EPOCH=0
  declare -A file_permissions=()
  source "$ROOT/configs/profiledef.sh"
  printf '%s\n' "${airootfs_image_tool_options[*]}"
)
[[ $x86_profile_values == *"-comp zstd "* ]]

echo "ARM profile tests passed"
