#!/bin/bash

# The installed-system config validator must accept a healthy finalized tree
# and reject each known way an image installed fine but broke on first use:
# missing pacman repositories (the [asahi-alarm] regression), disabled
# platform services, a wrong Wi-Fi backend, and a builder-local GRUB root
# selector.

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
VERIFIER="$ROOT/builder/verify-asahi-installed-system.py"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

build_fixture() {
  local fixture=$1
  local root="$fixture/root" boot="$fixture/boot"
  local system="$root/etc/systemd/system"

  mkdir -p "$root/etc/NetworkManager/conf.d" "$system/multi-user.target.wants" \
    "$root/usr/share/omarchy" "$root/var/lib/pacman/local" \
    "$boot/grub"

  cat >"$root/etc/pacman.conf" <<'CONF'
[options]
Architecture = aarch64
SigLevel = Required DatabaseOptional

[omarchy]
Server = https://github.com/maralcbr/omarchy-pkgs/releases/download/asahi-packages-stable-afd72814

[asahi-alarm]
Server = https://github.com/asahi-alarm/asahi-alarm/releases/download/aarch64

[core]
Server = https://ca.us.mirror.archlinuxarm.org/$arch/$repo

[extra]
Server = https://ca.us.mirror.archlinuxarm.org/$arch/$repo

[alarm]
Server = https://ca.us.mirror.archlinuxarm.org/$arch/$repo

[aur]
Server = https://ca.us.mirror.archlinuxarm.org/$arch/$repo
CONF

  printf '[device]\nwifi.backend=iwd\n' \
    >"$root/etc/NetworkManager/conf.d/wifi_backend.conf"

  local unit
  for unit in NetworkManager.service omarchy-vendor-firmware.service \
    speakersafetyd.service; do
    ln -s "/usr/lib/systemd/system/$unit" \
      "$system/multi-user.target.wants/$unit"
  done
  ln -s /usr/lib/systemd/system/bluetooth.service \
    "$system/dbus-org.bluez.service"
  ln -s /usr/lib/systemd/system/sddm.service "$system/display-manager.service"
  printf '[Service]\nExecStart=/usr/bin/tar -xf /boot/efi/vendorfw/firmware.tar\n' \
    >"$system/omarchy-vendor-firmware.service"

  local package
  for package in linux-asahi asahi-fwextract asahi-desktop-meta vulkan-asahi \
    speakersafetyd alsa-ucm-conf-asahi iwd networkmanager bluez wireplumber; do
    mkdir -p "$root/var/lib/pacman/local/$package-1.0-1"
  done

  printf '4.0.1-mac.2\n' >"$root/usr/share/omarchy/version"
  printf 'schema_version=1\nproduct_id=omarchy-mx-mac\nmode=installed-full-os\n' \
    >"$root/usr/share/omarchy/apple-silicon-full-os"

  cat >"$boot/grub/grub.cfg" <<'GRUB'
menuentry 'Omarchy Linux' {
	linux	/vmlinuz-linux-asahi root=UUID=4f4d5801-524f-4f54-8000-000000000001 rw rootflags=subvol=@ zswap.enabled=0 rootfstype=btrfs quiet
	initrd	/initramfs-linux-asahi.img
}
GRUB
  printf 'kernel' >"$boot/vmlinuz-linux-asahi"
  printf 'initramfs' >"$boot/initramfs-linux-asahi.img"
}

run_verifier() {
  local fixture=$1 evidence=$2
  python3 "$VERIFIER" --root-tree "$fixture/root" --boot-tree "$fixture/boot" \
    >"$evidence" 2>"$evidence.err"
}

# --- healthy tree passes every check -------------------------------------

fixture="$work/healthy"
build_fixture "$fixture"
run_verifier "$fixture" "$work/healthy.json"
jq -e '
  .schema_version == 1 and
  .verification_kind == "asahi-installed-system-config-v1" and
  .result == "passed" and
  (.failed_checks | length) == 0 and
  ([.checks[] | select(.result != "passed")] | length) == 0 and
  (.checks | length) >= 16
' "$work/healthy.json" >/dev/null
echo "ok - healthy installed tree passes every check"

# --- the [asahi-alarm] regression is caught ------------------------------

fixture="$work/no-asahi-alarm"
build_fixture "$fixture"
sed -i '/^\[asahi-alarm\]$/,/^$/d' "$fixture/root/etc/pacman.conf"
if run_verifier "$fixture" "$work/no-asahi-alarm.json"; then
  echo "not ok - a pacman.conf without [asahi-alarm] must fail" >&2
  exit 1
fi
jq -e '
  .result == "failed" and
  .checks["pacman-required-repositories"].result == "failed" and
  (.checks["pacman-required-repositories"].detail | contains("asahi-alarm"))
' "$work/no-asahi-alarm.json" >/dev/null
echo "ok - missing [asahi-alarm] repository fails the validation"

# --- a disabled speaker-safety service is caught -------------------------

fixture="$work/no-speakersafetyd"
build_fixture "$fixture"
rm "$fixture/root/etc/systemd/system/multi-user.target.wants/speakersafetyd.service"
if run_verifier "$fixture" "$work/no-speakersafetyd.json"; then
  echo "not ok - a disabled speakersafetyd must fail" >&2
  exit 1
fi
jq -e '.checks["unit-enabled-speakersafetyd"].result == "failed"' \
  "$work/no-speakersafetyd.json" >/dev/null
echo "ok - a disabled speakersafetyd service fails the validation"

# --- a disabled bluetooth service is caught ------------------------------

fixture="$work/no-bluetooth"
build_fixture "$fixture"
rm "$fixture/root/etc/systemd/system/dbus-org.bluez.service"
if run_verifier "$fixture" "$work/no-bluetooth.json"; then
  echo "not ok - a disabled bluetooth service must fail" >&2
  exit 1
fi
jq -e '.checks["unit-enabled-bluetooth"].result == "failed"' \
  "$work/no-bluetooth.json" >/dev/null
echo "ok - a disabled bluetooth service fails the validation"

# --- a wrong Wi-Fi backend is caught -------------------------------------

fixture="$work/wrong-backend"
build_fixture "$fixture"
printf '[device]\nwifi.backend=wpa_supplicant\n' \
  >"$fixture/root/etc/NetworkManager/conf.d/wifi_backend.conf"
if run_verifier "$fixture" "$work/wrong-backend.json"; then
  echo "not ok - a non-iwd Wi-Fi backend must fail" >&2
  exit 1
fi
jq -e '.checks["network-wifi-backend-iwd"].result == "failed"' \
  "$work/wrong-backend.json" >/dev/null
echo "ok - a non-iwd Wi-Fi backend fails the validation"

# --- the builder-local GRUB root selector regression is caught -----------

fixture="$work/builder-root"
build_fixture "$fixture"
sed -i \
  's|root=UUID=4f4d5801-524f-4f54-8000-000000000001|root=/var/cache/omarchy-asahi-package.xyz/finalized-boot/root.img|' \
  "$fixture/boot/grub/grub.cfg"
if run_verifier "$fixture" "$work/builder-root.json"; then
  echo "not ok - a builder-local root selector must fail" >&2
  exit 1
fi
jq -e '
  .checks["boot-grub-root-selector"].result == "failed" and
  .checks["boot-grub-no-builder-root"].result == "failed"
' "$work/builder-root.json" >/dev/null
echo "ok - a builder-local GRUB root selector fails the validation"

# --- a missing required package is caught --------------------------------

fixture="$work/no-package"
build_fixture "$fixture"
rm -r "$fixture/root/var/lib/pacman/local/speakersafetyd-1.0-1"
if run_verifier "$fixture" "$work/no-package.json"; then
  echo "not ok - a missing required package must fail" >&2
  exit 1
fi
jq -e '
  .checks["packages-required-present"].result == "failed" and
  (.checks["packages-required-present"].detail | contains("speakersafetyd"))
' "$work/no-package.json" >/dev/null
echo "ok - a missing required package fails the validation"

# --- the Aurora payload names its kernel; the checks follow it ------------

fixture="$work/aurora"
build_fixture "$fixture"
rm -rf "$fixture/root/var/lib/pacman/local/linux-asahi-1.0-1"
mkdir -p "$fixture/root/var/lib/pacman/local/linux-aurora-1.0-1"
sed -i 's/linux-asahi/linux-aurora/g' "$fixture/boot/grub/grub.cfg"
mv "$fixture/boot/vmlinuz-linux-asahi" "$fixture/boot/vmlinuz-linux-aurora"
mv "$fixture/boot/initramfs-linux-asahi.img" "$fixture/boot/initramfs-linux-aurora.img"
python3 "$VERIFIER" --root-tree "$fixture/root" --boot-tree "$fixture/boot" \
  --kernel linux-aurora >"$work/aurora.json" 2>"$work/aurora.json.err"
jq -e '.result == "passed" and (.failed_checks | length) == 0' "$work/aurora.json" >/dev/null
if python3 "$VERIFIER" --root-tree "$fixture/root" --boot-tree "$fixture/boot" \
  >"$work/aurora-as-asahi.json" 2>/dev/null; then
  echo "not ok - an Aurora tree must not pass as an Asahi one" >&2
  exit 1
fi
jq -e '
  .checks["packages-required-present"].result == "failed" and
  .checks["boot-kernel-present"].result == "failed"
' "$work/aurora-as-asahi.json" >/dev/null
echo "ok - the verifier checks whichever kernel the payload names"

# The Quattro add-on owns vendor Wi-Fi configuration; inspect NM's effective
# output and the selected package/feed contract, not the historical /etc leaf.
fixture="$work/quattro"
build_fixture "$fixture"
sed -i 's/\[omarchy\]/[omarchy-aarch64]/' "$fixture/root/etc/pacman.conf"
rm -r "$fixture/root/var/lib/pacman/local/asahi-desktop-meta-1.0-1"
rm "$fixture/root/etc/NetworkManager/conf.d/wifi_backend.conf"
for package in omarchy omarchy-settings omarchy-mac; do
  mkdir -p "$fixture/root/var/lib/pacman/local/$package-1.0-1"
done
printf '[device]\nwifi.backend=iwd\n' >"$work/effective.conf"
run_quattro_verifier() {
  python3 "$VERIFIER" --root-tree "$fixture/root" --boot-tree "$fixture/boot" \
    --package-profile quattro --networkmanager-config "$work/effective.conf" \
    >"$work/quattro.json" 2>"$work/quattro.err"
}
run_quattro_verifier
jq -e '.result == "passed"' "$work/quattro.json" >/dev/null
echo 'ok - Quattro packages and effective vendor Wi-Fi configuration pass'
printf '\n[device-conflict]\nmatch-device=interface-name:wlan0\nwifi.backend=wpa_supplicant\n' >>"$work/effective.conf"
if run_quattro_verifier; then
  echo 'not ok - conflicting effective Wi-Fi override passed' >&2
  exit 1
fi
jq -e '.checks["network-wifi-backend-iwd"].result == "failed"' "$work/quattro.json" >/dev/null
printf '[device]\nwifi.backend=iwd\n' >"$work/effective.conf"
printf '\n[omarchy]\nServer = https://example.invalid/fork\n' >>"$fixture/root/etc/pacman.conf"
if run_quattro_verifier; then
  echo 'not ok - inherited runtime repository passed' >&2
  exit 1
fi
jq -e '.checks["pacman-no-fork-runtime"].result == "failed"' "$work/quattro.json" >/dev/null
sed -i '/^\[omarchy\]$/,$d' "$fixture/root/etc/pacman.conf"
rm -r "$fixture/root/var/lib/pacman/local/omarchy-mac-1.0-1"
if run_quattro_verifier; then
  echo 'not ok - absent add-on passed' >&2
  exit 1
fi
jq -e '.checks["packages-required-present"].result == "failed"' "$work/quattro.json" >/dev/null
echo 'ok - Quattro verification rejects conflicting Wi-Fi, fork repos, and a missing add-on'
