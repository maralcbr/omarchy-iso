#!/bin/bash

set -euo pipefail

snapshot=${1:?Usage: verify-apple-platform-artifacts.sh SNAPSHOT ARTIFACT_DIRECTORY}
artifact_dir=${2:?Usage: verify-apple-platform-artifacts.sh SNAPSHOT ARTIFACT_DIRECTORY}
builder_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

"$builder_root/validate-apple-platform-snapshot.sh" "$snapshot" >/dev/null

keyring_filename=$(jq -r '.trust.keyring.filename' "$snapshot")
keyring_sha256=$(jq -r '.trust.keyring.sha256' "$snapshot")
expected_fingerprint=$(jq -r '.trust.signing_fingerprint' "$snapshot")
keyring_package="$artifact_dir/$keyring_filename"

[[ -f $keyring_package ]] || {
  echo "Missing Apple platform keyring: $keyring_package" >&2
  exit 1
}
echo "$keyring_sha256  $keyring_package" | sha256sum --check --status

verify_home=$(mktemp -d)
trap 'rm -rf "$verify_home"' EXIT
chmod 0700 "$verify_home"
bsdtar -xOf "$keyring_package" usr/share/pacman/keyrings/asahi-alarm.gpg \
  >"$verify_home/asahi-alarm.gpg"
# Some sandboxed hosts cannot start gpg-agent and return 2 after importing a
# public key. The fingerprint lookup below is the authoritative import check.
gpg --batch --homedir "$verify_home" --import "$verify_home/asahi-alarm.gpg" \
  >/dev/null 2>&1 || true
actual_fingerprint=$(gpg --batch --homedir "$verify_home" --with-colons \
  --fingerprint "$expected_fingerprint" | awk -F: '$1 == "fpr" { print $10; exit }')
[[ $actual_fingerprint == "$expected_fingerprint" ]] || {
  echo "Apple platform signing fingerprint mismatch" >&2
  exit 1
}

while IFS=$'\t' read -r package_name filename package_sha256 signature_sha256; do
  package="$artifact_dir/$filename"
  signature="$package.sig"
  [[ -f $package && -f $signature ]] || {
    echo "Missing Apple platform package or signature: $filename" >&2
    exit 1
  }
  echo "$package_sha256  $package" | sha256sum --check --status
  echo "$signature_sha256  $signature" | sha256sum --check --status
  gpg --batch --homedir "$verify_home" --verify "$signature" "$package" \
    >/dev/null 2>&1
  package_listing=$(bsdtar -tf "$package")
  actual_package_name=$(bsdtar -xOf "$package" .PKGINFO |
    awk -F ' = ' '$1 == "pkgname" { print $2; exit }')
  [[ $actual_package_name == "$package_name" ]] || {
    echo "Apple platform package identity mismatch: $filename" >&2
    exit 1
  }

  case "$package_name" in
    linux-asahi)
      grep -Eq '^usr/lib/modules/[^/]+/vmlinuz$' <<<"$package_listing"
      for family in t8103 t6000 t6020; do
        grep -Eq "^usr/lib/modules/[^/]+/dtbs/$family-[^.]+\\.dtb$" \
          <<<"$package_listing"
      done
      ;;
    m1n1)
      grep -Fxq 'usr/lib/asahi-boot/m1n1.bin' <<<"$package_listing"
      ;;
    uboot-asahi)
      grep -Fxq 'usr/lib/asahi-boot/u-boot-nodtb.bin' <<<"$package_listing"
      for family in t8103 t6000 t6020; do
        grep -Eq "^usr/lib/asahi-boot/dtb/$family-[^.]+\\.dtb$" \
          <<<"$package_listing"
      done
      ;;
    asahi-scripts)
      grep -Fxq 'usr/lib/initcpio/hooks/asahi' <<<"$package_listing"
      grep -Fxq 'usr/lib/initcpio/install/asahi' <<<"$package_listing"
      grep -Fxq 'usr/bin/update-m1n1' <<<"$package_listing"
      ;;
    asahi-fwextract)
      grep -Fxq 'usr/bin/asahi-fwextract' <<<"$package_listing"
      grep -Eq '^usr/lib/python[^/]*/site-packages/asahi_firmware/core\.py$' \
        <<<"$package_listing"
      ;;
    lzfse)
      grep -Fxq 'usr/bin/lzfse' <<<"$package_listing"
      ;;
  esac
done < <(jq -r '.packages[] | [.name, .filename, .sha256, .signature_sha256] | @tsv' "$snapshot")

echo "Apple platform package bytes and detached signatures verified"
