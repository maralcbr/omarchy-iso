#!/bin/bash

# Fetch and verify the Aurora kernel release into the offline mirror.
#
# The Aurora kernel lives in its own immutable release, separate from the
# [omarchy] repository, so this runs alongside fetch-arm-package-snapshots.sh
# rather than replacing any part of it. Only a build of the aurora product
# calls it; the Asahi lane never does.

set -euo pipefail

destination=${1:?Usage: fetch-aurora-package-snapshot.sh DESTINATION}
builder_root=${BUILDER_ROOT:-/builder}
source "$builder_root/aurora-package-snapshots.conf"

if [[ $AURORA_REPOSITORY_RELEASE == PENDING ]]; then
  echo "ERROR: the Aurora kernel release is not pinned yet" >&2
  echo "       Run omarchy-pkgs release-aurora-package.yml with publish=true," >&2
  echo "       then pin builder/aurora-package-snapshots.conf and the" >&2
  echo "       [omarchy-aurora] Server line from the release it creates." >&2
  exit 1
fi

[[ $AURORA_REPOSITORY_RELEASE =~ ^aurora-packages-[0-9a-f]{40}$ ]]
[[ $AURORA_REPOSITORY_DESCRIPTOR_SHA256 =~ ^[0-9a-f]{64}$ ]]
[[ $AURORA_REPOSITORY_SOURCE_COMMIT =~ ^[0-9a-f]{40}$ ]]
[[ $AURORA_REPOSITORY_SIGNING_FINGERPRINT =~ ^[A-F0-9]{40}$ ]]
[[ $AURORA_REPOSITORY_PACKAGE_COUNT =~ ^[1-9][0-9]*$ ]]

# The cached kernel and the repository the installed system syncs from must
# come from the same release, for the same reason the Asahi lane compares its
# two pins: one package, one version, two checksums aborts the first install of
# it with nothing in the log.
installed_pacman_conf=${INSTALLED_AURORA_PACMAN_CONF:-$builder_root/../configs/airootfs/usr/share/omarchy-iso/pacman-online-installed-arm-aurora.conf}
if [[ -r $installed_pacman_conf ]]; then
  installed_release=$(sed -n 's#^Server = https://github.com/maralcbr/omarchy-pkgs/releases/download/\(aurora-packages-[0-9a-f]\{40\}\)$#\1#p' "$installed_pacman_conf" | head -1)
  [[ $installed_release == "$AURORA_REPOSITORY_RELEASE" ]] || {
    echo "ERROR: the cached kernel and the installed system name different releases" >&2
    echo "       aurora-package-snapshots.conf:            $AURORA_REPOSITORY_RELEASE" >&2
    echo "       pacman-online-installed-arm-aurora.conf:  ${installed_release:-<none>}" >&2
    echo "       Point both at the same release before building." >&2
    exit 1
  }
else
  echo "ERROR: cannot read the installed Aurora pacman configuration: $installed_pacman_conf" >&2
  exit 1
fi

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
  local record="$1"
  local signature_checksum="$2"
  local index package version architecture filename checksum signature signature_path

  IFS='|' read -r index package version architecture filename checksum signature _ <<<"$record"
  [[ $index =~ ^[1-9][0-9]*$ ]]
  [[ $package =~ ^[a-z0-9@._+-]+$ ]]
  [[ -n $version ]]
  [[ $architecture == "aarch64" || $architecture == "any" ]]
  [[ $filename =~ ^[a-zA-Z0-9@._+-]+\.pkg\.tar\.(xz|zst)$ ]]
  [[ $checksum =~ ^[0-9a-f]{64}$ ]]

  download "$repository_base/$AURORA_REPOSITORY_RELEASE/$filename" "$work/$filename"
  [[ $(sha256sum "$work/$filename" | cut -d' ' -f1) == "$checksum" ]]

  signature=${signature:-$filename.sig}
  [[ $signature =~ ^[a-zA-Z0-9@._+-]+\.sig$ ]]
  signature_path="$work/$signature"
  download "$repository_base/$AURORA_REPOSITORY_RELEASE/$signature" "$signature_path"
  if [[ -n $signature_checksum ]]; then
    [[ $signature_checksum =~ ^[0-9a-f]{64}$ ]]
    [[ $(sha256sum "$signature_path" | cut -d' ' -f1) == "$signature_checksum" ]]
  fi
  verify_signature "$builder_root/omarchy-arm-repository.asc" "$signature_path" \
    "$work/$filename" "$AURORA_REPOSITORY_SIGNING_FINGERPRINT" "$work/gnupg-aurora"

  install -m 0644 "$work/$filename" "$destination/$filename"
  install -m 0644 "$signature_path" "$destination/$signature"
}

repository_url="$repository_base/$AURORA_REPOSITORY_RELEASE"
download "$repository_url/AURORA" "$work/AURORA"
download "$repository_url/AURORA.sig" "$work/AURORA.sig"
[[ $(sha256sum "$work/AURORA" | cut -d' ' -f1) == "$AURORA_REPOSITORY_DESCRIPTOR_SHA256" ]]
verify_signature "$builder_root/omarchy-arm-repository.asc" "$work/AURORA.sig" \
  "$work/AURORA" "$AURORA_REPOSITORY_SIGNING_FINGERPRINT" "$work/gnupg-aurora"
grep -Fxq 'format=1' "$work/AURORA"
grep -Fxq 'channel=aurora' "$work/AURORA"
grep -Fxq "release_tag=$AURORA_REPOSITORY_RELEASE" "$work/AURORA"
grep -Fxq "source_commit=$AURORA_REPOSITORY_SOURCE_COMMIT" "$work/AURORA"
grep -Fxq "signing_fingerprint=$AURORA_REPOSITORY_SIGNING_FINGERPRINT" "$work/AURORA"
grep -Fxq "package_count=$AURORA_REPOSITORY_PACKAGE_COUNT" "$work/AURORA"
(( $(grep -c '^package=' "$work/AURORA") == AURORA_REPOSITORY_PACKAGE_COUNT ))

while IFS= read -r record; do
  IFS='|' read -r _ _ _ _ _ _ _ signature_checksum <<<"$record"
  verify_package_record "$record" "$signature_checksum"
done < <(sed -n 's/^package=//p' "$work/AURORA")

install -m 0644 "$work/AURORA" "$destination/AURORA"
sed -n 's/^package=//p' "$work/AURORA" | cut -d'|' -f5 >"$destination/AURORA-PACKAGES"
(( $(wc -l <"$destination/AURORA-PACKAGES") == AURORA_REPOSITORY_PACKAGE_COUNT ))
