#!/bin/bash

set -euo pipefail

if (( $# != 4 )); then
  echo "Usage: verify-apple-media-layout.sh ISO_TREE AIROOTFS_TREE ESP_BOOTAA64.EFI APPLE_PLATFORM_SNAPSHOT" >&2
  exit 1
fi

iso_tree=$1
airootfs=$2
esp_bootaa64=$3
snapshot=$4

iso_bootaa64="$iso_tree/EFI/BOOT/BOOTAA64.EFI"
kernel="$iso_tree/arch/boot/aarch64/vmlinuz-linux-asahi"
initramfs="$iso_tree/arch/boot/aarch64/initramfs-linux-asahi.img"
grub_config="$iso_tree/boot/grub/grub.cfg"
package_list="$iso_tree/arch/pkglist.aarch64.txt"
media_target="$airootfs/usr/share/omarchy-iso/media-target"
media_descriptor="$airootfs/usr/share/omarchy-iso/media-target.json"
shipped_snapshot="$airootfs/usr/share/omarchy-iso/apple-platform-snapshot.json"

for required in \
  "$iso_bootaa64" "$esp_bootaa64" "$kernel" "$initramfs" "$grub_config" \
  "$package_list" "$media_target" "$media_descriptor" "$shipped_snapshot" "$snapshot"; do
  if [[ ! -f $required ]]; then
    echo "Required Apple media input not found: $required" >&2
    exit 1
  fi
done

if [[ $(<"$media_target") != "aarch64/apple-silicon" ]]; then
  echo "Live root does not carry the exact Apple media target marker" >&2
  exit 1
fi

if ! jq -e '
  keys == ["architecture", "artifact_kind", "boot_backend", "platform", "schema_version"] and
  . == {
    schema_version: 1,
    architecture: "aarch64",
    platform: "apple-silicon",
    boot_backend: "asahi-grub",
    artifact_kind: "iso"
  }
' "$media_descriptor" >/dev/null; then
  echo "Live root Apple media descriptor is invalid" >&2
  exit 1
fi

if ! cmp -s -- "$snapshot" "$shipped_snapshot"; then
  echo "Live root does not contain the exact pinned Apple platform snapshot" >&2
  exit 1
fi
"${BASH_SOURCE[0]%/*}/validate-apple-platform-snapshot.sh" "$snapshot" >/dev/null

if ! cmp -s -- "$iso_bootaa64" "$esp_bootaa64"; then
  echo "ISO9660 and appended-ESP BOOTAA64.EFI bytes differ" >&2
  exit 1
fi
if ! objdump -f "$iso_bootaa64" 2>/dev/null | grep -qF 'file format pei-aarch64-little'; then
  echo "BOOTAA64.EFI is not a PE/COFF AArch64 image" >&2
  exit 1
fi
if ! objdump -f "$iso_bootaa64" 2>/dev/null | grep -qF 'architecture: aarch64'; then
  echo "BOOTAA64.EFI does not declare the AArch64 machine architecture" >&2
  exit 1
fi

[[ -s $kernel ]] || { echo "Apple live kernel is empty" >&2; exit 1; }
[[ -s $initramfs ]] || { echo "Apple live initramfs is empty" >&2; exit 1; }

if command -v lsinitcpio >/dev/null; then
  # Real mkinitcpio images start with an uncompressed early CPIO followed by the
  # compressed main archive. bsdtar lists only that first archive; lsinitcpio
  # understands the concatenated format. The fallback keeps synthetic unit
  # fixtures and older single-archive images verifiable.
  if ! initramfs_listing=$(TERM="${TERM:-dumb}" lsinitcpio --nocolor --list "$initramfs"); then
    echo "Apple live initramfs could not be listed" >&2
    exit 1
  fi
else
  initramfs_listing=$(bsdtar -tf "$initramfs")
fi

if ! grep -Eq '(^|/)hooks/asahi$' <<<"$initramfs_listing"; then
  echo "Apple live initramfs does not contain the Asahi runtime hook" >&2
  exit 1
fi
if ! grep -Eq '(^|/)usr/share/asahi-scripts/functions\.sh$' <<<"$initramfs_listing"; then
  echo "Apple live initramfs does not contain Asahi firmware-mount helpers" >&2
  exit 1
fi

for expected in \
  '/arch/boot/aarch64/vmlinuz-linux-asahi' \
  '/arch/boot/aarch64/initramfs-linux-asahi.img'; do
  if ! grep -Fq -- "$expected" "$grub_config"; then
    echo "GRUB configuration does not reference $expected" >&2
    exit 1
  fi
done
if grep -Eq 'linux-aarch64|limine' "$grub_config"; then
  echo "Apple GRUB configuration contains a generic-ARM or Limine boot path" >&2
  exit 1
fi

for package in linux-asahi asahi-scripts asahi-alarm-keyring; do
  if [[ $(grep -Ec "^${package} " "$package_list") != 1 ]]; then
    echo "Apple live package list must contain exactly one $package entry" >&2
    exit 1
  fi
done
if grep -Eq '^(linux-aarch64|limine) ' "$package_list"; then
  echo "Apple live package list contains a generic-ARM kernel or Limine" >&2
  exit 1
fi

for forbidden in \
  "$iso_tree/EFI/BOOT/limine_aa64.efi" \
  "$iso_tree/boot/limine" \
  "$iso_tree/limine.conf"; do
  if [[ -e $forbidden ]]; then
    echo "Apple media contains forbidden Limine boot artifact: $forbidden" >&2
    exit 1
  fi
done

hash_file() {
  local digest
  digest=$(sha256sum -- "$1")
  printf '%s' "${digest%% *}"
}

jq -n -S \
  --arg bootaa64_sha256 "$(hash_file "$iso_bootaa64")" \
  --arg kernel_sha256 "$(hash_file "$kernel")" \
  --arg initramfs_sha256 "$(hash_file "$initramfs")" \
  --arg platform_snapshot_sha256 "$(hash_file "$snapshot")" '{
    schema_version: 1,
    target: {
      architecture: "aarch64",
      platform: "apple-silicon",
      boot_backend: "asahi-grub",
      artifact_kind: "iso"
    },
    checks: {
      iso_tree_bootaa64_matches_esp: true,
      bootaa64_pe_architecture: "aarch64",
      live_kernel: "linux-asahi",
      initramfs_asahi_hook: true,
      generic_arm_kernel_absent: true,
      limine_boot_artifacts_absent: true
    },
    hashes: {
      bootaa64_sha256: $bootaa64_sha256,
      kernel_sha256: $kernel_sha256,
      initramfs_sha256: $initramfs_sha256,
      platform_snapshot_sha256: $platform_snapshot_sha256
    }
  }'
