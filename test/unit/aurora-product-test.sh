#!/bin/bash

# The Aurora product selects a different kernel and nothing else. What matters
# is that selecting it changes the kernel everywhere the payload names one, and
# that its absence leaves the Asahi lane exactly as it was.

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
asahi_product=$ROOT/builder/products/omarchy-mx-mac.json
aurora_product=$ROOT/builder/products/omarchy-mx-mac-aurora.json

[[ -r $aurora_product ]] || { echo "not ok - aurora product is missing"; exit 1; }

# The two descriptors differ in the kernel and the artifact name only. Sharing
# every other field is what keeps the aurora payload the same OS.
differing=$(jq -rn \
  --slurpfile asahi "$asahi_product" \
  --slurpfile aurora "$aurora_product" \
  '($asahi[0] | keys) as $keys
   | [$keys[] | select($asahi[0][.] != $aurora[0][.])] | sort | join(",")')
[[ $differing == "branding,kernel_package,package_filename" ]] || {
  echo "not ok - aurora product differs beyond the kernel, filename and boot branding: $differing"
  exit 1
}
[[ $(jq -r '.kernel_package' "$aurora_product") == linux-aurora ]] || {
  echo "not ok - aurora product does not name the Aurora kernel"; exit 1
}
[[ $(jq -r '.package_filename' "$aurora_product") == *-aurora-os-package.zip ]] || {
  echo "not ok - aurora payload filename is not distinguishable"; exit 1
}
# Only the m1n1 digest moves inside branding: the boot image embeds the kernel's
# device trees, so it is pinned per kernel, and it must agree with the manifest.
[[ $(jq -r '.branding | del(.m1n1_boot_sha256)' "$aurora_product") == \
   $(jq -r '.branding | del(.m1n1_boot_sha256)' "$asahi_product") ]] || {
  echo "not ok - aurora branding differs beyond the m1n1 digest"; exit 1
}
[[ $(jq -r '.branding.m1n1_boot_sha256' "$aurora_product") == \
   $(jq -r '.m1n1.output.sha256' "$ROOT/builder/branding/branding-manifest-aurora.json") ]] || {
  echo "not ok - aurora product m1n1 digest does not match the aurora branding manifest"; exit 1
}
[[ $(jq -r '.schema_version' "$aurora_product") == 1 ]] || {
  echo "not ok - aurora product changed the product schema"; exit 1
}

# shellcheck source=/dev/null
source "$ROOT/builder/package-architecture.sh"

# No product configured is the case every non-Apple build is in.
[[ $(OMARCHY_ASAHI_PRODUCT= asahi_kernel_package) == linux-asahi ]] || {
  echo "not ok - an unconfigured product does not default to the Asahi kernel"; exit 1
}
[[ $(OMARCHY_ASAHI_PRODUCT=$asahi_product asahi_kernel_package) == linux-asahi ]] || {
  echo "not ok - the Asahi product does not select the Asahi kernel"; exit 1
}
[[ $(OMARCHY_ASAHI_PRODUCT=$aurora_product asahi_kernel_package) == linux-aurora ]] || {
  echo "not ok - the aurora product does not select the Aurora kernel"; exit 1
}

# A descriptor that names no kernel must stop the build rather than quietly
# fall back: a silent default would ship the wrong kernel in an Aurora payload.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
printf '{"schema_version": 1}\n' >"$work/no-kernel.json"
if OMARCHY_ASAHI_PRODUCT=$work/no-kernel.json asahi_kernel_package >/dev/null 2>&1; then
  echo "not ok - a product naming no kernel was accepted"; exit 1
fi
if OMARCHY_ASAHI_PRODUCT=$work/absent.json asahi_kernel_package >/dev/null 2>&1; then
  echo "not ok - an unreadable product was accepted"; exit 1
fi

# The base package list is rewritten to the selected kernel, so the payload
# installs the Aurora kernel instead of the Asahi one rather than both.
OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
OMARCHY_ARCH=aarch64
ASAHI_KERNEL_PACKAGE=linux-aurora
filtered=$(printf '%s\n' base linux-asahi linux-asahi-headers grub |
  filter_target_packages)
[[ $filtered == $'base\nlinux-aurora\nlinux-aurora-headers\ngrub' ]] || {
  echo "not ok - the base list was not rewritten to the Aurora kernel"
  printf '%s\n' "$filtered"
  exit 1
}
ASAHI_KERNEL_PACKAGE=linux-asahi
filtered=$(printf '%s\n' base linux-asahi linux-asahi-headers grub |
  filter_target_packages)
[[ $filtered == $'base\nlinux-asahi\nlinux-asahi-headers\ngrub' ]] || {
  echo "not ok - the Asahi base list changed"; exit 1
}

# The aurora repository must be pinned to the same release as the cached
# kernel, and must sit ahead of [omarchy] or pacman resolves the kernel
# elsewhere.
aurora_conf=$ROOT/configs/airootfs/usr/share/omarchy-iso/pacman-online-installed-arm-aurora.conf
[[ -r $aurora_conf ]] || { echo "not ok - aurora pacman configuration is missing"; exit 1; }
[[ $(grep -n '^\[omarchy-aurora\]$' "$aurora_conf" | cut -d: -f1) -lt \
   $(grep -n '^\[omarchy\]$' "$aurora_conf" | cut -d: -f1) ]] || {
  echo "not ok - [omarchy-aurora] does not precede [omarchy]"; exit 1
}
aurora_release=$(sed -n 's#^Server = https://github.com/maralcbr/omarchy-pkgs/releases/download/##p' \
  "$aurora_conf" | head -1)
pinned_release=$(sed -n 's/^AURORA_REPOSITORY_RELEASE=//p' \
  "$ROOT/builder/aurora-package-snapshots.conf")
[[ $aurora_release == "$pinned_release" ]] || {
  echo "not ok - the aurora repository and the pinned kernel name different releases"
  echo "         pacman conf: $aurora_release"
  echo "         snapshots:   $pinned_release"
  exit 1
}

echo 'ok - the aurora product selects the Aurora kernel and leaves the Asahi lane alone'
