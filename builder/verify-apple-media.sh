#!/bin/bash

set -euo pipefail

if (( $# < 2 || $# > 3 )); then
  echo "Usage: verify-apple-media.sh ISO APPLE_PLATFORM_SNAPSHOT [EVIDENCE_JSON]" >&2
  exit 1
fi

iso=$1
snapshot=$2
evidence=${3:-$iso.apple-media-evidence.json}

if [[ ! -f $iso || ! -f $snapshot ]]; then
  echo "ISO or Apple platform snapshot not found" >&2
  exit 1
fi

for command in bsdtar dd jq mcopy objdump sfdisk sha256sum unsquashfs; do
  command -v "$command" >/dev/null || {
    echo "Required Apple media verification command is missing: $command" >&2
    exit 1
  }
done

work=$(mktemp -d)
temporary=$(mktemp "$evidence.tmp.XXXXXX")
trap 'rm -rf "$work"; rm -f "$temporary"' EXIT
iso_tree="$work/iso"
airootfs="$work/airootfs"
mkdir -p "$iso_tree"

bsdtar -xf "$iso" -C "$iso_tree" \
  EFI/BOOT/BOOTAA64.EFI \
  arch/boot/aarch64/vmlinuz-linux-asahi \
  arch/boot/aarch64/initramfs-linux-asahi.img \
  arch/pkglist.aarch64.txt \
  arch/aarch64/airootfs.sfs \
  boot/grub/grub.cfg

unsquashfs -no-progress -d "$airootfs" "$iso_tree/arch/aarch64/airootfs.sfs" >/dev/null

partition_table=$(sfdisk --json "$iso")
sector_size=$(jq -er '.partitiontable.sectorsize' <<<"$partition_table")
mapfile -t esp_partitions < <(
  jq -r '
    .partitiontable.partitions[] |
    select((.type | ascii_downcase) == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b") |
    [.start, .size] | @tsv
  ' <<<"$partition_table"
)
if (( ${#esp_partitions[@]} != 1 )); then
  echo "Apple ISO must contain exactly one appended EFI System Partition" >&2
  exit 1
fi
read -r esp_start esp_size <<<"${esp_partitions[0]}"
if [[ ! $sector_size =~ ^[1-9][0-9]*$ || ! $esp_start =~ ^[0-9]+$ || ! $esp_size =~ ^[1-9][0-9]*$ ]]; then
  echo "Apple ISO EFI System Partition geometry is invalid" >&2
  exit 1
fi

esp_image="$work/esp.img"
esp_bootaa64="$work/esp-BOOTAA64.EFI"
dd if="$iso" of="$esp_image" bs="$sector_size" skip="$esp_start" count="$esp_size" status=none
mcopy -i "$esp_image" ::/EFI/BOOT/BOOTAA64.EFI "$esp_bootaa64"

layout_verifier=/builder/verify-apple-media-layout.sh
[[ -x $layout_verifier ]] ||
  layout_verifier="${BASH_SOURCE[0]%/*}/verify-apple-media-layout.sh"
layout=$(
  "$layout_verifier" \
    "$iso_tree" "$airootfs" "$esp_bootaa64" "$snapshot"
)
iso_sha256=$(sha256sum -- "$iso")
iso_sha256=${iso_sha256%% *}
iso_size=$(wc -c <"$iso")
iso_size=${iso_size//[[:space:]]/}

jq -n -S \
  --argjson layout "$layout" \
  --arg iso_filename "${iso##*/}" \
  --argjson iso_size "$iso_size" \
  --arg iso_sha256 "$iso_sha256" '{
    schema_version: 1,
    verification_kind: "static-apple-media",
    artifact: {
      filename: $iso_filename,
      size: $iso_size,
      sha256: $iso_sha256
    },
    layout: $layout,
    boot: {
      verified: false,
      blocker: "disposable-asahi-boot-evidence-absent"
    }
  }' >"$temporary"

chmod 644 "$temporary"
mv -f -- "$temporary" "$evidence"
trap - EXIT
rm -rf "$work"
echo "Apple media layout verified; boot remains unverified: $evidence"
