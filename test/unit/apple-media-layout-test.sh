#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

iso_tree="$work/iso"
airootfs="$work/airootfs"
snapshot="$ROOT/builder/apple-platform-snapshot.json"
mkdir -p \
  "$iso_tree/EFI/BOOT" \
  "$iso_tree/arch/boot/aarch64" \
  "$iso_tree/boot/grub" \
  "$airootfs/usr/share/omarchy-iso" \
  "$work/initramfs/hooks" \
  "$work/initramfs/usr/share/asahi-scripts"

printf 'test PE payload\n' >"$work/pe-payload"
objcopy -I binary -O pei-aarch64-little -B aarch64 \
  "$work/pe-payload" "$iso_tree/EFI/BOOT/BOOTAA64.EFI"
cp "$iso_tree/EFI/BOOT/BOOTAA64.EFI" "$work/esp-BOOTAA64.EFI"
printf 'test Asahi kernel\n' >"$iso_tree/arch/boot/aarch64/vmlinuz-linux-asahi"
printf '#!/bin/ash\n' >"$work/initramfs/hooks/asahi"
printf '#!/bin/ash\n' >"$work/initramfs/usr/share/asahi-scripts/functions.sh"
bsdtar -cf "$iso_tree/arch/boot/aarch64/initramfs-linux-asahi.img" \
  -C "$work/initramfs" .
cat >"$iso_tree/boot/grub/grub.cfg" <<'EOF'
linux /arch/boot/aarch64/vmlinuz-linux-asahi
initrd /arch/boot/aarch64/initramfs-linux-asahi.img
EOF
cat >"$iso_tree/arch/pkglist.aarch64.txt" <<'EOF'
asahi-scripts 20260127.1-1
linux-asahi 6.18.0-1
EOF
printf 'aarch64/apple-silicon\n' >"$airootfs/usr/share/omarchy-iso/media-target"
cat >"$airootfs/usr/share/omarchy-iso/media-target.json" <<'EOF'
{"schema_version":1,"architecture":"aarch64","platform":"apple-silicon","boot_backend":"asahi-grub","artifact_kind":"iso"}
EOF
cp "$snapshot" "$airootfs/usr/share/omarchy-iso/apple-platform-snapshot.json"

verify() {
  "$ROOT/builder/verify-apple-media-layout.sh" \
    "$iso_tree" "$airootfs" "$work/esp-BOOTAA64.EFI" "$snapshot"
}

layout=$(verify)
jq -e '
  .schema_version == 1 and
  .target.platform == "apple-silicon" and
  .target.boot_backend == "asahi-grub" and
  .checks.iso_tree_bootaa64_matches_esp == true and
  .checks.bootaa64_pe_architecture == "aarch64" and
  .checks.live_kernel == "linux-asahi" and
  .checks.initramfs_asahi_hook == true and
  .checks.generic_arm_kernel_absent == true and
  .checks.limine_boot_artifacts_absent == true and
  (.hashes.platform_snapshot_sha256 | test("^[0-9a-f]{64}$"))
' <<<"$layout" >/dev/null
echo "ok - exact Apple media layout produces canonical structural evidence"

printf 'different EFI bytes\n' >"$work/esp-BOOTAA64.EFI"
if verify >"$work/out" 2>"$work/error"; then
  echo "mismatched ISO/ESP BOOTAA64.EFI unexpectedly passed" >&2
  exit 1
fi
grep -qF "BOOTAA64.EFI bytes differ" "$work/error"
echo "ok - ISO9660 and appended-ESP BOOTAA64.EFI must be byte-identical"
cp "$iso_tree/EFI/BOOT/BOOTAA64.EFI" "$work/esp-BOOTAA64.EFI"

printf 'linux /arch/boot/aarch64/linux-aarch64\n' >>"$iso_tree/boot/grub/grub.cfg"
if verify >"$work/out" 2>"$work/error"; then
  echo "generic ARM GRUB path unexpectedly passed" >&2
  exit 1
fi
grep -qF "generic-ARM or Limine" "$work/error"
echo "ok - generic ARM and Limine GRUB paths are rejected"
sed -i '$d' "$iso_tree/boot/grub/grub.cfg"

printf 'linux-aarch64 6.17.0-1\n' >>"$iso_tree/arch/pkglist.aarch64.txt"
if verify >"$work/out" 2>"$work/error"; then
  echo "generic ARM live kernel package unexpectedly passed" >&2
  exit 1
fi
grep -qF "generic-ARM kernel or Limine" "$work/error"
echo "ok - generic ARM kernel packages are rejected"
sed -i '$d' "$iso_tree/arch/pkglist.aarch64.txt"

jq '.boot_backend = "limine"' \
  "$airootfs/usr/share/omarchy-iso/media-target.json" >"$work/wrong-target.json"
mv "$work/wrong-target.json" "$airootfs/usr/share/omarchy-iso/media-target.json"
if verify >"$work/out" 2>"$work/error"; then
  echo "wrong live media descriptor unexpectedly passed" >&2
  exit 1
fi
grep -qF "media descriptor is invalid" "$work/error"
echo "ok - live root must carry the exact Apple/Asahi-GRUB descriptor"
