#!/bin/bash

set -euo pipefail

destination=${1:?Usage: fetch-arm-package-snapshots.sh DESTINATION}
builder_root=${BUILDER_ROOT:-/builder}
source "$builder_root/arm-package-snapshots.conf"

[[ $ARM_REPOSITORY_RELEASE =~ ^asahi-packages-stable-[0-9a-f]{40}$ ]]
[[ $ARM_REPOSITORY_DESCRIPTOR_RELEASE =~ ^asahi-packages-candidate-[0-9a-f]{40}$ ]]
[[ $ARM_REPOSITORY_DESCRIPTOR_SHA256 =~ ^[0-9a-f]{64}$ ]]
[[ $ARM_REPOSITORY_SOURCE_COMMIT =~ ^[0-9a-f]{40}$ ]]
[[ $ARM_REPOSITORY_SIGNING_FINGERPRINT =~ ^[A-F0-9]{40}$ ]]
[[ $ARM_RUNTIME_RELEASE =~ ^asahi-quattro-[0-9a-f]{8}$ ]]
[[ $ARM_RUNTIME_MANIFEST_SHA256 =~ ^[0-9a-f]{64}$ ]]
[[ $ARM_RUNTIME_SOURCE_COMMIT =~ ^[0-9a-f]{40}$ ]]
[[ $ARM_RUNTIME_SIGNING_FINGERPRINT =~ ^[A-F0-9]{40}$ ]]

repository_base="https://github.com/maralcbr/omarchy-pkgs/releases/download"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

mkdir -p "$destination"

download() {
  local url="$1"
  local output="$2"
  curl --fail --location --silent --show-error --connect-timeout 15 --max-time 300 \
    --retry 3 --retry-all-errors "$url" --output "$output"
}

verify_signature() {
  local key="$1"
  local signature="$2"
  local payload="$3"
  local expected_fingerprint="$4"
  local gnupg_home="$5"
  local signature_status valid_fingerprint

  mkdir -p "$gnupg_home"
  chmod 0700 "$gnupg_home"
  GNUPGHOME="$gnupg_home" gpg --batch --import "$key" >/dev/null 2>&1
  signature_status=$(GNUPGHOME="$gnupg_home" gpg --batch --status-fd 1 \
    --verify "$signature" "$payload" 2>/dev/null)
  valid_fingerprint=$(awk '$2 == "VALIDSIG" { print $3; exit }' <<<"$signature_status")
  [[ $valid_fingerprint == "$expected_fingerprint" ]]
}

verify_package_record() {
  local release="$1"
  local key="$2"
  local signer="$3"
  local record="$4"
  local signature_checksum=${5:-}
  local gnupg_home="$6"
  local index package version architecture filename checksum signature signature_path

  IFS='|' read -r index package version architecture filename checksum signature _ <<<"$record"
  [[ $index =~ ^[1-9][0-9]*$ ]]
  [[ $package =~ ^[a-z0-9@._+-]+$ ]]
  [[ -n $version ]]
  [[ $architecture == "aarch64" || $architecture == "any" ]]
  [[ $filename =~ ^[a-zA-Z0-9@._+-]+\.pkg\.tar\.(xz|zst)$ ]]
  [[ $checksum =~ ^[0-9a-f]{64}$ ]]

  download "$repository_base/$release/$filename" "$work/$filename"
  [[ $(sha256sum "$work/$filename" | cut -d' ' -f1) == "$checksum" ]]

  signature=${signature:-$filename.sig}
  [[ $signature =~ ^[a-zA-Z0-9@._+-]+\.sig$ ]]
  signature_path="$work/$signature"
  download "$repository_base/$release/$signature" "$signature_path"
  if [[ -n $signature_checksum ]]; then
    [[ $signature_checksum =~ ^[0-9a-f]{64}$ ]]
    [[ $(sha256sum "$signature_path" | cut -d' ' -f1) == "$signature_checksum" ]]
  fi
  verify_signature "$key" "$signature_path" "$work/$filename" "$signer" \
    "$gnupg_home"

  install -m 0644 "$work/$filename" "$destination/$filename"
  install -m 0644 "$signature_path" "$destination/$signature"
}

repository_url="$repository_base/$ARM_REPOSITORY_RELEASE"
download "$repository_url/CANDIDATE" "$work/CANDIDATE"
download "$repository_url/CANDIDATE.sig" "$work/CANDIDATE.sig"
[[ $(sha256sum "$work/CANDIDATE" | cut -d' ' -f1) == "$ARM_REPOSITORY_DESCRIPTOR_SHA256" ]]
verify_signature "$builder_root/omarchy-arm-repository.asc" "$work/CANDIDATE.sig" \
  "$work/CANDIDATE" "$ARM_REPOSITORY_SIGNING_FINGERPRINT" "$work/gnupg-repository"
grep -Fxq 'format=1' "$work/CANDIDATE"
grep -Fxq 'channel=candidate' "$work/CANDIDATE"
grep -Fxq "release_tag=$ARM_REPOSITORY_DESCRIPTOR_RELEASE" "$work/CANDIDATE"
grep -Fxq "source_commit=$ARM_REPOSITORY_SOURCE_COMMIT" "$work/CANDIDATE"
grep -Fxq "signing_fingerprint=$ARM_REPOSITORY_SIGNING_FINGERPRINT" "$work/CANDIDATE"
grep -Fxq 'package_count=21' "$work/CANDIDATE"
(( $(grep -c '^package=' "$work/CANDIDATE") == 21 ))

while IFS= read -r record; do
  IFS='|' read -r _ _ _ _ _ _ _ signature_checksum <<<"$record"
  verify_package_record "$ARM_REPOSITORY_RELEASE" "$builder_root/omarchy-arm-repository.asc" \
    "$ARM_REPOSITORY_SIGNING_FINGERPRINT" "$record" "$signature_checksum" \
    "$work/gnupg-repository"
done < <(sed -n 's/^package=//p' "$work/CANDIDATE")

runtime_url="$repository_base/$ARM_RUNTIME_RELEASE"
download "$runtime_url/asahi-quattro-bundle.manifest" "$work/runtime.manifest"
download "$runtime_url/asahi-quattro-bundle.manifest.sig" "$work/runtime.manifest.sig"
[[ $(sha256sum "$work/runtime.manifest" | cut -d' ' -f1) == "$ARM_RUNTIME_MANIFEST_SHA256" ]]
verify_signature "$builder_root/omarchy-arm-runtime.asc" "$work/runtime.manifest.sig" \
  "$work/runtime.manifest" "$ARM_RUNTIME_SIGNING_FINGERPRINT" "$work/gnupg-runtime"
grep -Fxq 'format=2' "$work/runtime.manifest"
grep -Fxq 'bundle=asahi-quattro' "$work/runtime.manifest"
grep -Fxq "source_commit=$ARM_RUNTIME_SOURCE_COMMIT" "$work/runtime.manifest"
grep -Fxq 'package_count=6' "$work/runtime.manifest"
(( $(grep -c '^package=' "$work/runtime.manifest") == 6 ))

while IFS= read -r record; do
  verify_package_record "$ARM_RUNTIME_RELEASE" "$builder_root/omarchy-arm-runtime.asc" \
    "$ARM_RUNTIME_SIGNING_FINGERPRINT" "$record" "" "$work/gnupg-runtime"
done < <(sed -n 's/^package=//p' "$work/runtime.manifest")

install -m 0644 "$work/CANDIDATE" "$destination/ARM-REPOSITORY"
install -m 0644 "$work/runtime.manifest" "$destination/ARM-RUNTIME"
{
  sed -n 's/^package=//p' "$work/CANDIDATE"
  sed -n 's/^package=//p' "$work/runtime.manifest"
} | cut -d'|' -f5 >"$destination/ARM-PACKAGES"
(( $(wc -l <"$destination/ARM-PACKAGES") == 27 ))
