#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
validator="$ROOT/configs/airootfs/usr/local/bin/omarchy-apple-media-validate"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fixture="$work/root"
stubs="$work/stubs"
fixture_dirs=(
  "$fixture/usr/share/omarchy-iso"
  "$fixture/proc/device-tree"
  "$fixture/dev"
  "$fixture/run"
  "$stubs"
)
mkdir -p "${fixture_dirs[@]}"

cat >"$fixture/usr/share/omarchy-iso/apple-media-validation" <<'EOF'
schema_version=1
mode=read-only-canary
source_commit=0123456789abcdef0123456789abcdef01234567
EOF
printf 'aarch64/apple-silicon\n' >"$fixture/usr/share/omarchy-iso/media-target"
printf '%s\n' 'archisobasedir=arch systemd.gpt_auto=0 rd.systemd.gpt_auto=0 fstab=no rd.fstab=no' >"$fixture/proc/cmdline"
printf 'Apple MacBook Pro\0' >"$fixture/proc/device-tree/model"
printf 'apple,j293\0apple,arm-platform\0' >"$fixture/proc/device-tree/compatible"
touch "$fixture/dev/nvme0n1" "$fixture/dev/nvme0n1p1"

cat >"$stubs/udevadm" <<'STUB'
#!/bin/bash
exit 0
STUB
cat >"$stubs/findmnt" <<'STUB'
#!/bin/bash
if [[ -f $TEST_ROOT/run/rw-mount ]]; then
  printf '/dev/nvme0n1p1 /mnt rw,relatime\n'
fi
STUB
cat >"$stubs/swapon" <<'STUB'
#!/bin/bash
if [[ -f $TEST_ROOT/run/active-swap && ! -f $TEST_ROOT/run/swap-disabled ]]; then
  printf '/dev/nvme0n1p1\n'
fi
STUB
cat >"$stubs/swapoff" <<'STUB'
#!/bin/bash
printf '%s\n' "$1" >>"$TEST_ROOT/run/swapoff.log"
touch "$TEST_ROOT/run/swap-disabled"
STUB
cat >"$stubs/blockdev" <<'STUB'
#!/bin/bash
device=${2##*/}
case "$1" in
  --setro) touch "$TEST_ROOT/run/ro-$device" ;;
  --getro) [[ -f $TEST_ROOT/run/ro-$device ]] && printf '1\n' || printf '0\n' ;;
  *) exit 2 ;;
esac
STUB
cat >"$stubs/lsblk" <<'STUB'
#!/bin/bash
printf 'NAME TYPE SIZE RO MOUNTPOINTS\nnvme0n1 disk 1T 1\n'
STUB
chmod +x "$stubs"/*

run_validator() {
  validator_env=(
    "TEST_ROOT=$fixture"
    "OMARCHY_VALIDATION_ROOT=$fixture"
    "OMARCHY_VALIDATION_NO_SHELL=1"
    "PATH=$stubs:/usr/bin:/bin"
  )
  env "${validator_env[@]}" "$BASH" "$validator"
}

touch "$fixture/run/active-swap"
run_validator >"$work/out"
jq -e '
  .verification_kind == "physical-apple-canary-preflight" and
  .media_target == "aarch64/apple-silicon" and
  .source_commit == "0123456789abcdef0123456789abcdef01234567" and
  .checks.installer_entrypoints_absent == true and
  .checks.automatic_disk_discovery_disabled == true and
  .checks.internal_nvme_swap_absent == true and
  .checks.internal_nvme_read_only == true and
  .readonly_devices == ["/dev/nvme0n1", "/dev/nvme0n1p1"]
' "$fixture/run/omarchy-apple-validation.json" >/dev/null
grep -Fxq '/dev/nvme0n1p1' "$fixture/run/swapoff.log"
grep -Fq 'No installation is available' "$work/out"
echo "ok - validation console disables NVMe swap and proves block-layer read-only state"

mkdir -p "$fixture/usr/local/bin"
touch "$fixture/usr/local/bin/omarchy-iso-install"
if run_validator >"$work/out" 2>"$work/error"; then
  echo "validation console accepted an installer entry point" >&2
  exit 1
fi
grep -Fq 'mutation-capable entry point remains: /usr/local/bin/omarchy-iso-install' "$work/error"
rm "$fixture/usr/local/bin/omarchy-iso-install"
echo "ok - validation console fails closed when an installer entry point remains"

sed -i.bak 's/ rd.fstab=no//' "$fixture/proc/cmdline"
if run_validator >"$work/out" 2>"$work/error"; then
  echo "validation console accepted an incomplete no-discovery command line" >&2
  exit 1
fi
grep -Fq 'kernel command line is missing rd.fstab=no' "$work/error"
printf '%s\n' 'archisobasedir=arch systemd.gpt_auto=0 rd.systemd.gpt_auto=0 fstab=no rd.fstab=no' >"$fixture/proc/cmdline"
echo "ok - validation console requires every automatic-discovery guard"

touch "$fixture/run/rw-mount"
if run_validator >"$work/out" 2>"$work/error"; then
  echo "validation console accepted a read-write internal NVMe mount" >&2
  exit 1
fi
grep -Fq 'internal NVMe is already mounted read-write at /mnt' "$work/error"
echo "ok - validation console rejects pre-existing read-write NVMe mounts"

echo "Apple media canary safety tests passed"
