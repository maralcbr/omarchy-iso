#!/bin/bash

set -euo pipefail

destination=${1:?Usage: fetch-apple-platform-snapshot.sh DESTINATION}
builder_root=${BUILDER_ROOT:-/builder}
snapshot=${APPLE_PLATFORM_SNAPSHOT:-$builder_root/apple-platform-snapshot.json}
release_base=https://github.com/asahi-alarm/asahi-alarm/releases/download/aarch64
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

"$builder_root/validate-apple-platform-snapshot.sh" "$snapshot" >/dev/null
mkdir -p "$destination"

download() {
  local filename="$1"

  curl --fail --location --silent --show-error --connect-timeout 15 --max-time 300 \
    --retry 3 --retry-all-errors "$release_base/$filename" --output "$work/$filename"
}

keyring=$(jq -r '.trust.keyring.filename' "$snapshot")
download "$keyring"
download "$keyring.sig"
mapfile -t packages < <(jq -r '.packages[].filename' "$snapshot")
for package in "${packages[@]}"; do
  download "$package"
  download "$package.sig"
done

if ! "$builder_root/verify-apple-platform-artifacts.sh" "$snapshot" "$work" >/dev/null; then
  echo "Apple platform artifact verification failed" >&2
  exit 1
fi
install -m 0644 "$work/$keyring" "$destination/$keyring"
install -m 0644 "$work/$keyring.sig" "$destination/$keyring.sig"
for package in "${packages[@]}"; do
  install -m 0644 "$work/$package" "$destination/$package"
  install -m 0644 "$work/$package.sig" "$destination/$package.sig"
done
printf '%s\n' "$keyring" >"$destination/APPLE-KEYRING"
printf '%s\n' "${packages[@]}" >"$destination/APPLE-PACKAGES"

expected_count=$(jq -r '.packages | length' "$snapshot")
(( $(wc -l <"$destination/APPLE-PACKAGES") == expected_count ))
