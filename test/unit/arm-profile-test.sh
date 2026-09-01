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
source "$ROOT/builder/archiso-media-output.sh"

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
[[ " ${BUILD_HOST_PACKAGES[*]} " != *" mkinitcpio "* ]]

apple_contract=$(
  export OMARCHY_ARCH=aarch64 OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
  source "$ROOT/builder/architecture.sh"
  printf '%s|%s|%s|%s|%s\n' \
    "$OMARCHY_PLATFORM" "$OMARCHY_BOOT_BACKEND" \
    "$OMARCHY_ARTIFACT_KIND" "$OMARCHY_MEDIA_TARGET_READY" \
    "$OMARCHY_APPLE_PLATFORM_SNAPSHOT"
)
[[ $apple_contract == "apple-silicon|asahi-grub|iso|0|/builder/apple-platform-snapshot.json" ]]

apple_build_host_packages=$(
  export OMARCHY_ARCH=aarch64 OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
  source "$ROOT/builder/architecture.sh"
  printf '%s\n' "${BUILD_HOST_PACKAGES[*]}"
)
[[ " $apple_build_host_packages " == *" mkinitcpio "* ]]

apple_live_profile=$(
  export OMARCHY_ARCH=aarch64 OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
  source "$ROOT/builder/architecture.sh"
  printf '%s|%s|%s|%s\n' "$LIVE_KERNEL" "$LIVE_KERNEL_BOOT_NAME" \
    "$LIVE_INITRAMFS_BOOT_NAME" "${LIVE_PACKAGES[*]}"
)
[[ $apple_live_profile == linux-asahi\|vmlinuz-linux-asahi\|initramfs-linux-asahi.img\|linux-asahi\ asahi-scripts* ]]
[[ $apple_live_profile == *asahi-alarm-keyring* ]]

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
package_stage="$ROOT/builder/asahi-stages/verified-package-cache.sh"
grep -Fq '"${apple_keyring_names[@]}" "${apple_package_names[@]}"' \
  "$package_stage"
for package in alsa-ucm-conf-asahi asahi-audio asahi-bless asahi-fwextract \
  asahi-scripts grub linux-asahi linux-asahi-headers m1n1 speakersafetyd \
  startup-disk uboot-asahi; do
  grep -Eq "[[:space:]]${package}([[:space:]\\\\]|$)" "$package_stage" || {
    echo "Apple target transaction does not explicitly select $package" >&2
    exit 1
  }
done
grep -Fq 'alsa-ucm-conf-asahi asahi-alarm-keyring' "$package_stage"

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
  "$ROOT/builder/archiso-media-output.sh"
grep -Fq 'printf '\''%s\n'\'' archlinuxarm-keyring >>"$shipped_base_packages"' \
  "$package_stage"

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
for guard in systemd.gpt_auto=0 rd.systemd.gpt_auto=0 fstab=no rd.fstab=no; do
  grep -Fq "$guard" "$apple_profile/grub/grub.cfg"
  grep -Fq "$guard" "$apple_profile/grub/loopback.cfg"
done

mkdir -p \
  "$apple_profile/airootfs/root" \
  "$apple_profile/airootfs/usr/local/bin" \
  "$apple_profile/airootfs/usr/share/omarchy-iso/orchestrator"
touch \
  "$apple_profile/airootfs/root/configurator" \
  "$apple_profile/airootfs/usr/local/bin/omarchy-cidata-load" \
  "$apple_profile/airootfs/usr/local/bin/omarchy-install-dashboard" \
  "$apple_profile/airootfs/usr/local/bin/omarchy-iso-cleanup-disk" \
  "$apple_profile/airootfs/usr/local/bin/omarchy-iso-install" \
  "$apple_profile/airootfs/usr/share/omarchy-iso/disk-partitioning.sh" \
  "$apple_profile/airootfs/usr/share/omarchy-iso/setup-form.sh"
export OMARCHY_ISO_SOURCE_COMMIT=0123456789abcdef0123456789abcdef01234567
seal_apple_validation_profile "$apple_profile"
grep -Fxq 'mode=read-only-canary' \
  "$apple_profile/airootfs/usr/share/omarchy-iso/apple-media-validation"
grep -Fxq 'source_commit=0123456789abcdef0123456789abcdef01234567' \
  "$apple_profile/airootfs/usr/share/omarchy-iso/apple-media-validation"
for forbidden in \
  root/configurator \
  usr/local/bin/omarchy-cidata-load \
  usr/local/bin/omarchy-install-dashboard \
  usr/local/bin/omarchy-iso-cleanup-disk \
  usr/local/bin/omarchy-iso-install \
  usr/share/omarchy-iso/orchestrator \
  usr/share/omarchy-iso/disk-partitioning.sh \
  usr/share/omarchy-iso/setup-form.sh; do
  [[ ! -e $apple_profile/airootfs/$forbidden ]]
done

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
