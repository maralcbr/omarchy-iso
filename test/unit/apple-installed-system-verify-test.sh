#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
verifier="$ROOT/builder/omarchy-apple-installed-verify"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fixture="$work/root"
stubs="$work/stubs"
mkdir -p \
  "$fixture/usr/share/omarchy" \
  "$fixture/proc/device-tree" \
  "$fixture/boot/efi/m1n1" \
  "$fixture/boot/efi/EFI/BOOT" \
  "$fixture/boot/grub" \
  "$fixture/usr/bin" \
  "$fixture/var/lib/omarchy/provisioning" \
  "$stubs"

cat >"$fixture/usr/share/omarchy/apple-silicon-full-os" <<'EOF'
schema_version=1
product_id=omarchy-mx-mac
mode=installed-full-os
EOF
printf 'Apple MacBook Pro\0' >"$fixture/proc/device-tree/model"
printf 'apple,j314s\0apple,arm-platform\0' >"$fixture/proc/device-tree/compatible"
printf 'm1n1' >"$fixture/boot/efi/m1n1/boot.bin"
printf 'grub-efi' >"$fixture/boot/efi/EFI/BOOT/BOOTAA64.EFI"
printf 'Omarchy linux-asahi' >"$fixture/boot/grub/grub.cfg"
printf 'kernel' >"$fixture/boot/vmlinuz-linux-asahi"
printf 'initramfs' >"$fixture/boot/initramfs-linux-asahi.img"
printf '#!/bin/bash\n' >"$fixture/usr/bin/omarchy"

cat >"$stubs/uname" <<'STUB'
#!/bin/bash
case "$1" in
  -m) printf 'aarch64\n' ;;
  -r) printf '7.1.6-1-1-ARCH\n' ;;
  *) exit 2 ;;
esac
STUB
cat >"$stubs/findmnt" <<'STUB'
#!/bin/bash
target=${@: -1}
case "$target" in
  "$TEST_ROOT"|"$TEST_ROOT/") printf '/dev/nvme0n1p7 btrfs\n' ;;
  "$TEST_ROOT/boot") printf '/dev/nvme0n1p6 ext4\n' ;;
  "$TEST_ROOT/boot/efi") printf '/dev/nvme0n1p5 vfat\n' ;;
  *) exit 1 ;;
esac
STUB
cat >"$stubs/lsblk" <<'STUB'
#!/bin/bash
printf '%s\n' \
  7c3457ef-0000-11aa-aa11-00306543ecac \
  7c3457ef-0000-11aa-aa11-00306543ecac \
  52637672-7900-11aa-aa11-00306543ecac \
  c12a7328-f81f-11d2-ba4b-00a0c93ec93b
STUB
cat >"$stubs/pacman" <<'STUB'
#!/bin/bash
[[ $1 == -Q && $2 == linux-asahi ]] || exit 2
printf 'linux-asahi 7.1.6.asahi1-1\n'
STUB
cat >"$stubs/sha256sum" <<'STUB'
#!/bin/bash
printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa  %s\n' "$1"
STUB
chmod +x "$stubs"/*

run_verifier() {
  env \
    TEST_ROOT="$fixture" \
    OMARCHY_VERIFY_ROOT="$fixture" \
    PATH="$stubs:/usr/bin:/bin" \
    "$BASH" "$verifier"
}

run_verifier >"$work/evidence.json"
jq -e '
  .verification_kind == "omarchy-apple-installed-system" and
  .product_id == "omarchy-mx-mac" and
  .device_tree.compatible == ["apple,j314s", "apple,arm-platform"] and
  .kernel.architecture == "aarch64" and
  .kernel.package == "linux-asahi" and
  .filesystems.root.type == "btrfs" and
  .filesystems.boot.type == "ext4" and
  .filesystems.esp.type == "vfat" and
  .checks.full_os_marker == true and
  .checks.provisioning_completed == true and
  .checks.boot_chain_present == true and
  .checks.apfs_partitions_retained == true and
  .checks.macos_boot_verification_required == true and
  .apple_partition_layout.apfs_container_count == 2 and
  .apple_partition_layout.apfs_recovery_count == 1
' "$work/evidence.json" >/dev/null
echo "ok - installed Apple Silicon system emits exact boot and retained-APFS evidence"

touch "$fixture/var/lib/omarchy/provisioning/pending"
if run_verifier >"$work/out" 2>"$work/error"; then
  echo "installed verifier accepted incomplete owner provisioning" >&2
  exit 1
fi
grep -Fq 'owner provisioning is still pending' "$work/error"
echo "ok - installed verifier rejects incomplete first-owner provisioning"

# The opt-in profile verifies real runtime state and permits only a crypt
# mapper directly backed by an internal NVMe partition.
rm "$fixture/var/lib/omarchy/provisioning/pending"
printf '%s\n' '{"schema":1,"boot_profile":"limine","candidate_schema":4}' >"$fixture/usr/share/omarchy/apple-boot-profile.json"
mkdir -p "$fixture/boot/efi/EFI/Linux" "$fixture/etc/default"
printf 'Omarchy Limine' >"$fixture/boot/efi/limine.conf"
printf 'uki' >"$fixture/boot/efi/EFI/Linux/omarchy_linux-asahi.efi"
touch "$fixture/var/lib/omarchy/limine.enabled" "$fixture/etc/default/limine"
cat >"$stubs/omarchy-mac-kernel" <<'STUB'
#!/bin/bash
printf 'linux-asahi\n'
STUB
cat >"$stubs/omarchy-apple-silicon-boot-check" <<'STUB'
#!/bin/bash
[[ $OMARCHY_BOOT_CHECK_ROOT == "$TEST_ROOT" && $1 == linux-asahi ]]
[[ ! -e $TEST_ROOT/reject-boot ]]
STUB
sed -i 's|/dev/nvme0n1p7 btrfs|/dev/mapper/cryptroot[/@] btrfs|' "$stubs/findmnt"
python3 - "$stubs/lsblk" <<'PYEDIT'
import sys
from pathlib import Path
p=Path(sys.argv[1]);s=p.read_text().replace('#!/bin/bash\n', """#!/bin/bash
if [[ $1 == -dnro ]]; then
  case $2 in
    TYPE) printf 'crypt\\n' ;;
    PKNAME) cat "$TEST_ROOT/mapper-parent" ;;
    *) exit 1 ;;
  esac
  exit 0
fi
""",1);p.write_text(s)
PYEDIT
chmod +x "$stubs/omarchy-mac-kernel" "$stubs/omarchy-apple-silicon-boot-check"
printf 'nvme0n1p7\n' >"$fixture/mapper-parent"
run_verifier >"$work/limine.json"
jq -e '.boot_profile == "limine" and .filesystems.root.source == "/dev/mapper/cryptroot[/@]"' "$work/limine.json" >/dev/null
touch "$fixture/reject-boot"
if run_verifier >"$work/out" 2>"$work/error"; then exit 1; fi
grep -Fq 'installed Limine boot contract failed' "$work/error"
rm "$fixture/reject-boot"
printf 'sda3\n' >"$fixture/mapper-parent"
if run_verifier >"$work/out" 2>"$work/error"; then exit 1; fi
grep -Fq 'encrypted root is not directly backed by internal NVMe' "$work/error"
echo "ok - Limine verifier checks the runtime contract and internal encrypted root"
