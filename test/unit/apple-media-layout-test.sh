#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

iso_tree="$work/iso"
airootfs="$work/airootfs"
snapshot="$ROOT/builder/apple-platform-snapshot.json"
mkdir -p \
  "$work/stubs" \
  "$iso_tree/EFI/BOOT" \
  "$iso_tree/arch/boot/aarch64" \
  "$iso_tree/boot/grub" \
  "$airootfs/usr/share/omarchy-iso" \
  "$airootfs/usr/local/bin" \
  "$work/initramfs/hooks" \
  "$work/initramfs/usr/share/asahi-scripts"

python3 - "$iso_tree/EFI/BOOT/BOOTAA64.EFI" <<'PY'
from pathlib import Path
import struct
import sys

pe = bytearray(4096)
pe[:2] = b"MZ"
struct.pack_into("<I", pe, 0x3C, 0x80)
pe[0x80:0x84] = b"PE\0\0"
struct.pack_into("<H", pe, 0x84, 0xAA64)
Path(sys.argv[1]).write_bytes(pe)
PY
cat >"$work/stubs/objdump" <<'STUB'
#!/bin/bash
printf '%s\n' \
  "$2: file format pei-aarch64-little" \
  'architecture: aarch64, flags 0x0000012f:'
STUB
chmod +x "$work/stubs/objdump"
cp "$iso_tree/EFI/BOOT/BOOTAA64.EFI" "$work/esp-BOOTAA64.EFI"
printf 'test Asahi kernel\n' >"$iso_tree/arch/boot/aarch64/vmlinuz-linux-asahi"
printf '#!/bin/ash\n' >"$work/initramfs/hooks/asahi"
printf '#!/bin/ash\n' >"$work/initramfs/usr/share/asahi-scripts/functions.sh"
bsdtar -cf "$iso_tree/arch/boot/aarch64/initramfs-linux-asahi.img" \
  -C "$work/initramfs" .
cat >"$iso_tree/boot/grub/grub.cfg" <<'EOF'
linux /arch/boot/aarch64/vmlinuz-linux-asahi systemd.gpt_auto=0 rd.systemd.gpt_auto=0 fstab=no rd.fstab=no
initrd /arch/boot/aarch64/initramfs-linux-asahi.img
EOF
cat >"$iso_tree/arch/pkglist.aarch64.txt" <<'EOF'
asahi-alarm-keyring 20241216-1
asahi-scripts 20260127.1-1
linux-asahi 6.18.0-1
EOF
printf 'aarch64/apple-silicon\n' >"$airootfs/usr/share/omarchy-iso/media-target"
cat >"$airootfs/usr/share/omarchy-iso/media-target.json" <<'EOF'
{"schema_version":1,"architecture":"aarch64","platform":"apple-silicon","boot_backend":"asahi-grub","artifact_kind":"iso"}
EOF
cp "$snapshot" "$airootfs/usr/share/omarchy-iso/apple-platform-snapshot.json"
cat >"$airootfs/usr/share/omarchy-iso/apple-media-validation" <<'EOF'
schema_version=1
mode=read-only-canary
source_commit=0123456789abcdef0123456789abcdef01234567
EOF
cp "$ROOT/configs/airootfs/usr/local/bin/omarchy-apple-media-validate" \
  "$airootfs/usr/local/bin/omarchy-apple-media-validate"
chmod +x "$airootfs/usr/local/bin/omarchy-apple-media-validate"

drop_last_line() {
  local target=$1
  sed '$d' "$target" >"$work/without-last-line"
  mv "$work/without-last-line" "$target"
}

verify() {
  PATH="$work/stubs:$PATH" "$ROOT/builder/verify-apple-media-layout.sh" \
    "$iso_tree" "$airootfs" "$work/esp-BOOTAA64.EFI" "$snapshot"
}

cat >"$work/stubs/lsinitcpio" <<'STUB'
#!/bin/bash
set -euo pipefail
[[ $* == "--nocolor --list "* ]]
printf '%s\n' hooks/asahi usr/share/asahi-scripts/functions.sh
STUB
chmod +x "$work/stubs/lsinitcpio"

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
  .checks.validation_console == true and
  .checks.installer_entrypoints_absent == true and
  .checks.automatic_disk_discovery_disabled == true and
  (.hashes.platform_snapshot_sha256 | test("^[0-9a-f]{64}$"))
' <<<"$layout" >/dev/null
echo "ok - exact Apple media layout produces canonical structural evidence"

verify >/dev/null
grep -Fq 'lsinitcpio --nocolor --list' "$ROOT/builder/verify-apple-media-layout.sh"
echo "ok - concatenated mkinitcpio images use the format-aware lister"

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
drop_last_line "$iso_tree/boot/grub/grub.cfg"

printf 'linux-aarch64 6.17.0-1\n' >>"$iso_tree/arch/pkglist.aarch64.txt"
if verify >"$work/out" 2>"$work/error"; then
  echo "generic ARM live kernel package unexpectedly passed" >&2
  exit 1
fi
grep -qF "generic-ARM kernel or Limine" "$work/error"
echo "ok - generic ARM kernel packages are rejected"
drop_last_line "$iso_tree/arch/pkglist.aarch64.txt"

jq '.boot_backend = "limine"' \
  "$airootfs/usr/share/omarchy-iso/media-target.json" >"$work/wrong-target.json"
mv "$work/wrong-target.json" "$airootfs/usr/share/omarchy-iso/media-target.json"
if verify >"$work/out" 2>"$work/error"; then
  echo "wrong live media descriptor unexpectedly passed" >&2
  exit 1
fi
grep -qF "media descriptor is invalid" "$work/error"
echo "ok - live root must carry the exact Apple/Asahi-GRUB descriptor"
