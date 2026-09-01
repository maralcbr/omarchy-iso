#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

[[ -x $ROOT/builder/build-asahi-os-package.sh ]] || {
  echo "full-OS package builder is not executable" >&2
  exit 1
}
[[ -x $ROOT/builder/omarchy-apple-installed-verify ]] || {
  echo "installed-system verifier is not executable" >&2
  exit 1
}
grep -Fq '/builder/omarchy-apple-installed-verify' \
  "$ROOT/builder/asahi-stages/finalized-boot.sh"
grep -Fq 'apple-silicon-full-os' \
  "$ROOT/builder/asahi-stages/finalized-boot.sh"
echo "ok - full-OS package embeds its post-boot verifier and product marker"

grep -Fq '/builder/brand-apple-silicon-boot.py patch-m1n1' \
  "$ROOT/builder/asahi-stages/finalized-boot.sh"
grep -Fq 'omarchy-volume.icns' \
  "$ROOT/builder/asahi-stages/sealed-release-package.sh"
echo "ok - finalized boot and sealed package wire exact Omarchy branding"

grep -Fq 'OMARCHY_ASAHI_CONFIGURED_CONTRACT_PROOF' \
  "$ROOT/bin/omarchy-iso-make"
grep -Fq ':/omarchy-asahi-configured-contract-proof.json:ro' \
  "$ROOT/bin/omarchy-iso-make"
echo "ok - host wrapper can mount the fail-closed configured-target proof"

image_runtime=$ROOT/builder/asahi-stages/image-runtime.sh
grep -Fq 'umount -R -- "$target"' "$image_runtime"
grep -Fq 'fuser -k -TERM -m -M "$target"' \
  "$image_runtime"
if grep -Eq 'umount[[:space:]]+-[^[:space:]]*l' \
  "$image_runtime"; then
  echo "full-OS package builder must not lazy-unmount writable images" >&2
  exit 1
fi
echo "ok - image cleanup is recursive and terminates only target filesystem holders"

mkdir -p "$work/package/esp/m1n1" "$work/package/esp/EFI/BOOT"
cp -- "$ROOT/builder/branding/omarchy-volume.icns" \
  "$work/package/omarchy-volume.icns"

cat >"$work/pacman-offline.conf" <<'CONF'
[options]
Architecture = aarch64

[offline]
SigLevel = Never
Server = file:///var/cache/omarchy/mirror/offline/
CONF

"$ROOT/builder/prepare-asahi-pacman-config.sh" \
  "$work/pacman-offline.conf" "$work/pacman-offline.builder.conf"
grep -Fxq "DisableSandbox" "$work/pacman-offline.builder.conf"
grep -Fxq "[offline]" "$work/pacman-offline.builder.conf"
grep -Fq "Server = file://" "$work/pacman-offline.builder.conf"
echo "ok - Asahi builder pacman config retains its offline repository"

if "$ROOT/builder/prepare-asahi-pacman-config.sh" \
  "$work/pacman-offline.conf" "$work/pacman-offline.conf" \
  >"$work/out" 2>"$work/error"; then
  echo "aliased pacman config paths unexpectedly passed" >&2
  exit 1
fi
grep -Fq "must differ" "$work/error"
grep -Fxq "[offline]" "$work/pacman-offline.conf"
echo "ok - aliased pacman config paths are rejected without truncation"

python3 - "$work" <<'PY'
from pathlib import Path
import struct
import sys
import zipfile

root = Path(sys.argv[1])
package = root / "package"

pe = bytearray(4096)
pe[:2] = b"MZ"
struct.pack_into("<I", pe, 0x3C, 0x80)
pe[0x80:0x84] = b"PE\0\0"
struct.pack_into("<H", pe, 0x84, 0xAA64)
(package / "esp/EFI/BOOT/BOOTAA64.EFI").write_bytes(pe)
(package / "esp/m1n1/boot.bin").write_bytes(b"pinned m1n1 stage 2")

boot = bytearray(8192)
boot[0x438:0x43A] = b"\x53\xef"
(package / "boot.img").write_bytes(boot)

root_image = bytearray(131072)
root_image[0x10040:0x10048] = b"_BHRfS_M"
(package / "root.img").write_bytes(root_image)

with zipfile.ZipFile(root / "omarchy-test.zip", "w", allowZip64=True) as archive:
    for path in sorted(package.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(package).as_posix())
PY

cat >"$work/product.json" <<'JSON'
{
  "schema_version": 1,
  "product_id": "omarchy-mx-mac",
  "name": "Omarchy MX Mac",
  "default_os_name": "Omarchy",
  "package_filename": "omarchy-test.zip",
  "esp_size_bytes": 524288000,
  "esp_volume_id": "0x4f4d5801",
  "boot_size_bytes": 8192,
  "root_size_bytes": 131072,
  "boot_backend": "asahi-grub",
  "branding": {
    "m1n1_boot_sha256": "72a8e3edd2c3aa7fded1d1020277b34bb6bd3fb9de20d59415c2e3556c4392b2",
    "volume_icon_member": "omarchy-volume.icns",
    "volume_icon_sha256": "cf26ed5d2831db99c00d62ca046040e01a18e08e63363d629340d04ac6ec8c23",
    "volume_icon_size_bytes": 42899
  },
  "supported_fw": ["13.5", "14.8.3"]
}
JSON

evidence=$(
  python3 "$ROOT/builder/verify-asahi-os-package.py" \
    "$work/omarchy-test.zip" "$work/product.json"
)

jq -e '
  .schema_version == 1 and
  .product_id == "omarchy-mx-mac" and
  .checks.member_paths_safe == true and
  .checks.required_members_present == true and
  .checks.boot_filesystem == "ext4" and
  .checks.root_filesystem == "btrfs" and
  .checks.boot_backend == "asahi-grub" and
  .checks.branding_bound == true and
  .checks.bootaa64_machine == "aarch64" and
  .checks.crc_valid == true and
  .checks.members_streamed_once == true and
  .checks.declared_size_bound == true and
  .metadata.os_list[0].package == "omarchy-test.zip" and
  .metadata.os_list[0].icon == "omarchy-volume.icns" and
  .metadata.os_list[0].partitions[1].image == "boot.img" and
  .metadata.os_list[0].partitions[2].image == "root.img" and
  .metadata.os_list[0].partitions[2].expand == true and
  (.package.sha256 | test("^[0-9a-f]{64}$"))
' <<<"$evidence" >/dev/null
echo "ok - full Asahi OS package emits exact image and metadata evidence"

python3 - "$work/omarchy-test.zip" "$work/duplicate.zip" "$work/corrupt.zip" \
  "$work/wrong-content.zip" "$work/missing-member.zip" \
  "$work/wrong-branding.zip" <<'PY'
from pathlib import Path
import shutil
import struct
import sys
import zipfile

source, duplicate, corrupt, wrong_content, missing_member, wrong_branding = map(
    Path,
    sys.argv[1:],
)
shutil.copy2(source, duplicate)
with zipfile.ZipFile(duplicate, "a") as archive:
    archive.writestr("boot.img", b"duplicate")

shutil.copy2(source, corrupt)
with zipfile.ZipFile(corrupt) as archive:
    info = archive.getinfo("boot.img")
    with corrupt.open("r+b") as stream:
        stream.seek(info.header_offset + 26)
        filename_length, extra_length = struct.unpack("<HH", stream.read(4))
        data_offset = info.header_offset + 30 + filename_length + extra_length
        stream.seek(data_offset + 32)
        original = stream.read(1)
        stream.seek(data_offset + 32)
        stream.write(bytes([original[0] ^ 0x01]))

with zipfile.ZipFile(source) as source_archive:
    members = {
        info.filename: source_archive.read(info)
        for info in source_archive.infolist()
        if not info.is_dir()
    }

invalid_boot = bytearray(members["boot.img"])
invalid_boot[0x438:0x43A] = b"\x00\x00"
members["boot.img"] = bytes(invalid_boot)
with zipfile.ZipFile(wrong_content, "w", allowZip64=True) as archive:
    for name, content in sorted(members.items()):
        archive.writestr(name, content)

with zipfile.ZipFile(missing_member, "w", allowZip64=True) as archive:
    for name, content in sorted(members.items()):
        if name != "root.img":
            archive.writestr(name, content)

members["omarchy-volume.icns"] = b"x" * len(members["omarchy-volume.icns"])
with zipfile.ZipFile(wrong_branding, "w", allowZip64=True) as archive:
    for name, content in sorted(members.items()):
        archive.writestr(name, content)
PY

jq '.package_filename = "duplicate.zip"' "$work/product.json" >"$work/duplicate-product.json"
if python3 "$ROOT/builder/verify-asahi-os-package.py" \
  "$work/duplicate.zip" "$work/duplicate-product.json" \
  >"$work/out" 2>"$work/error"; then
  echo "duplicate ZIP member unexpectedly passed" >&2
  exit 1
fi
grep -Fq "duplicate package member" "$work/error"
echo "ok - duplicate ZIP members are rejected before extraction"

jq '.package_filename = "corrupt.zip"' "$work/product.json" >"$work/corrupt-product.json"
if python3 "$ROOT/builder/verify-asahi-os-package.py" \
  "$work/corrupt.zip" "$work/corrupt-product.json" \
  >"$work/out" 2>"$work/error"; then
  echo "corrupt ZIP member unexpectedly passed" >&2
  exit 1
fi
grep -Fq "invalid package ZIP" "$work/error"
echo "ok - each member's CRC is checked during its only decompression pass"

jq '.package_filename = "wrong-content.zip"' \
  "$work/product.json" >"$work/wrong-content-product.json"
if python3 "$ROOT/builder/verify-asahi-os-package.py" \
  "$work/wrong-content.zip" "$work/wrong-content-product.json" \
  >"$work/out" 2>"$work/error"; then
  echo "structurally invalid content with a valid CRC unexpectedly passed" >&2
  exit 1
fi
grep -Fq "boot image is not ext4" "$work/error"
echo "ok - valid-CRC content corruption is rejected by filesystem proof"

jq '.package_filename = "omarchy-test.zip" | .boot_size_bytes += 4096' \
  "$work/product.json" >"$work/wrong-size-product.json"
if python3 "$ROOT/builder/verify-asahi-os-package.py" \
  "$work/omarchy-test.zip" "$work/wrong-size-product.json" \
  >"$work/out" 2>"$work/error"; then
  echo "incorrect declared member size unexpectedly passed" >&2
  exit 1
fi
grep -Fq "boot image size does not match product" "$work/error"
echo "ok - incorrect declared image sizes are rejected"

jq '.package_filename = "missing-member.zip"' \
  "$work/product.json" >"$work/missing-member-product.json"
if python3 "$ROOT/builder/verify-asahi-os-package.py" \
  "$work/missing-member.zip" "$work/missing-member-product.json" \
  >"$work/out" 2>"$work/error"; then
  echo "package with a missing member unexpectedly passed" >&2
  exit 1
fi
grep -Fq "missing package members: root.img" "$work/error"
echo "ok - missing required package members are rejected"

jq '.package_filename = "wrong-branding.zip"' \
  "$work/product.json" >"$work/wrong-branding-product.json"
if python3 "$ROOT/builder/verify-asahi-os-package.py" \
  "$work/wrong-branding.zip" "$work/wrong-branding-product.json" \
  >"$work/out" 2>"$work/error"; then
  echo "package with mismatched branding unexpectedly passed" >&2
  exit 1
fi
grep -Fq "volume icon digest does not match product" "$work/error"
echo "ok - Startup Options branding is bound to the exact product asset"

python3 - "$work/omarchy-test.zip" <<'PY'
from pathlib import Path
import sys
import zipfile

path = Path(sys.argv[1])
with zipfile.ZipFile(path, "a") as archive:
    archive.writestr("../escape", b"unsafe")
PY

if python3 "$ROOT/builder/verify-asahi-os-package.py" \
  "$work/omarchy-test.zip" "$work/product.json" \
  >"$work/out" 2>"$work/error"; then
  echo "unsafe ZIP member unexpectedly passed" >&2
  exit 1
fi
grep -Fq "unsafe package member" "$work/error"
echo "ok - path traversal in an OS package is rejected"

echo "Asahi OS package contract tests passed"
