#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p \
  "$work/stubs" \
  "$work/source-iso/EFI/BOOT" \
  "$work/source-iso/arch/boot/aarch64" \
  "$work/source-iso/arch/aarch64" \
  "$work/source-iso/boot/grub" \
  "$work/source-airootfs/usr/share/omarchy-iso" \
  "$work/initramfs/hooks" \
  "$work/initramfs/usr/share/asahi-scripts"

printf 'test PE payload\n' >"$work/pe-payload"
objcopy -I binary -O pei-aarch64-little -B aarch64 \
  "$work/pe-payload" "$work/source-iso/EFI/BOOT/BOOTAA64.EFI"
cp "$work/source-iso/EFI/BOOT/BOOTAA64.EFI" "$work/esp-BOOTAA64.EFI"
printf 'test Asahi kernel\n' >"$work/source-iso/arch/boot/aarch64/vmlinuz-linux-asahi"
printf '#!/bin/ash\n' >"$work/initramfs/hooks/asahi"
printf '#!/bin/ash\n' >"$work/initramfs/usr/share/asahi-scripts/functions.sh"
/usr/bin/bsdtar -cf "$work/source-iso/arch/boot/aarch64/initramfs-linux-asahi.img" \
  -C "$work/initramfs" .
cat >"$work/source-iso/boot/grub/grub.cfg" <<'EOF'
linux /arch/boot/aarch64/vmlinuz-linux-asahi
initrd /arch/boot/aarch64/initramfs-linux-asahi.img
EOF
cat >"$work/source-iso/arch/pkglist.aarch64.txt" <<'EOF'
asahi-alarm-keyring 20241216-1
asahi-scripts 20260127.1-1
linux-asahi 6.18.0-1
EOF
: >"$work/source-iso/arch/aarch64/airootfs.sfs"
printf 'aarch64/apple-silicon\n' >"$work/source-airootfs/usr/share/omarchy-iso/media-target"
cat >"$work/source-airootfs/usr/share/omarchy-iso/media-target.json" <<'EOF'
{"schema_version":1,"architecture":"aarch64","platform":"apple-silicon","boot_backend":"asahi-grub","artifact_kind":"iso"}
EOF
cp "$ROOT/builder/apple-platform-snapshot.json" \
  "$work/source-airootfs/usr/share/omarchy-iso/apple-platform-snapshot.json"
printf 'synthetic ISO container bytes\n' >"$work/apple.iso"

cat >"$work/stubs/bsdtar" <<'STUB'
#!/bin/bash
set -euo pipefail
if [[ $1 == "-tf" ]]; then
  exec /usr/bin/bsdtar "$@"
fi
destination=""
previous=""
for argument in "$@"; do
  if [[ $previous == "-C" ]]; then
    destination="$argument"
  fi
  previous="$argument"
done
[[ -n $destination ]]
cp -a "$SOURCE_ISO_TREE/." "$destination/"
STUB
cat >"$work/stubs/unsquashfs" <<'STUB'
#!/bin/bash
set -euo pipefail
destination=""
previous=""
for argument in "$@"; do
  if [[ $previous == "-d" ]]; then
    destination="$argument"
  fi
  previous="$argument"
done
[[ -n $destination ]]
mkdir -p "$destination"
cp -a "$SOURCE_AIROOTFS/." "$destination/"
STUB
cat >"$work/stubs/sfdisk" <<'STUB'
#!/bin/bash
cat <<'JSON'
{"partitiontable":{"sectorsize":512,"partitions":[{"start":16,"size":64,"type":"C12A7328-F81F-11D2-BA4B-00A0C93EC93B"}]}}
JSON
STUB
cat >"$work/stubs/dd" <<'STUB'
#!/bin/bash
set -euo pipefail
for argument in "$@"; do
  case "$argument" in
    of=*) output=${argument#of=} ;;
  esac
done
cp "$ESP_IMAGE_FIXTURE" "$output"
STUB
cat >"$work/stubs/mcopy" <<'STUB'
#!/bin/bash
set -euo pipefail
destination=${!#}
cp "$ESP_BOOT_FIXTURE" "$destination"
STUB
chmod +x "$work/stubs"/*

export SOURCE_ISO_TREE="$work/source-iso"
export SOURCE_AIROOTFS="$work/source-airootfs"
export ESP_BOOT_FIXTURE="$work/esp-BOOTAA64.EFI"
export ESP_IMAGE_FIXTURE="$work/esp.img"
: >"$ESP_IMAGE_FIXTURE"
evidence="$work/evidence.json"

PATH="$work/stubs:$PATH" "$BASH" "$ROOT/builder/verify-apple-media.sh" \
  "$work/apple.iso" "$ROOT/builder/apple-platform-snapshot.json" "$evidence" >/dev/null

jq -e --arg filename apple.iso --arg sha "$(sha256sum "$work/apple.iso" | cut -d ' ' -f 1)" '
  keys == ["artifact", "boot", "layout", "schema_version", "verification_kind"] and
  .schema_version == 1 and
  .verification_kind == "static-apple-media" and
  .artifact.filename == $filename and
  .artifact.sha256 == $sha and
  .layout.target.platform == "apple-silicon" and
  .layout.checks.iso_tree_bootaa64_matches_esp == true and
  .boot == {
    blocker: "disposable-asahi-boot-evidence-absent",
    verified: false
  }
' "$evidence" >/dev/null
echo "ok - ISO wrapper binds structural evidence to exact artifact bytes"

printf 'wrong ESP EFI\n' >"$ESP_BOOT_FIXTURE"
if PATH="$work/stubs:$PATH" "$BASH" "$ROOT/builder/verify-apple-media.sh" \
  "$work/apple.iso" "$ROOT/builder/apple-platform-snapshot.json" \
  "$work/rejected.json" >"$work/out" 2>"$work/error"; then
  echo "wrapper accepted mismatched appended-ESP bytes" >&2
  exit 1
fi
grep -qF "BOOTAA64.EFI bytes differ" "$work/error"
[[ ! -e $work/rejected.json ]]
echo "ok - wrapper emits no evidence when appended-ESP validation fails"
