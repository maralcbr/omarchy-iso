#!/bin/bash
set -euo pipefail

# The dated ALARM snapshot pairs Hyprland's ABI 13 dependency with Aquamarine
# ABI 14. Overlay one official, signed ALARM rebuild; never follow rolling latest.
destination=${1:?Usage: fetch-quattro-hyprland.sh DESTINATION}
builder_root=${BUILDER_ROOT:-/builder}
record="$builder_root/quattro-hyprland-repair.json"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
filename=$(jq -er '.filename' "$record")
expected=$(jq -er '.sha256' "$record")
mkdir -p "$destination"
if [[ -f $destination/$filename ]] && echo "$expected  $destination/$filename" | sha256sum --check --status; then
  cp "$destination/$filename" "$work/$filename"
else
  curl --fail --location --silent --show-error --connect-timeout 15 --max-time 300 \
    --retry 3 --retry-all-errors "$(jq -er '.base_url' "$record")/$filename" --output "$work/$filename"
fi
echo "$expected  $work/$filename" | sha256sum --check --status
jq -er '.signature_base64' "$record" | base64 --decode >"$work/$filename.sig"
# An isolated home avoids local user keyring/configuration influencing verification.
mkdir -m 0700 "$work/gnupg"
gpg --batch --homedir "$work/gnupg" --import /usr/share/pacman/keyrings/archlinuxarm.gpg
gpg --batch --homedir "$work/gnupg" --status-fd 1 \
  --verify "$work/$filename.sig" "$work/$filename" >"$work/status"
awk -v signer="$(jq -er '.signer' "$record")" \
  '$2 == "VALIDSIG" && ($3 == signer || $NF == signer) { found=1 } END { exit !found }' "$work/status"
bsdtar -xOf "$work/$filename" .PKGINFO >"$work/pkginfo"
grep -Fx "pkgname = $(jq -er '.name' "$record")" "$work/pkginfo"
grep -Fx "pkgver = $(jq -er '.version' "$record")" "$work/pkginfo"
grep -Fx 'arch = aarch64' "$work/pkginfo"
grep -Fx "depend = $(jq -er '.required_dependency' "$record")" "$work/pkginfo"
install -m 0644 "$work/$filename" "$work/$filename.sig" "$destination/"
