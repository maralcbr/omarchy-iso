#!/bin/bash

set -euo pipefail

destination=${1:?Usage: fetch-arm-package-snapshots.sh DESTINATION}
builder_root=${BUILDER_ROOT:-/builder}
source "$builder_root/arm-package-snapshots.conf"

[[ $ARM_REPOSITORY_RELEASE =~ ^asahi-packages-(stable|candidate)-[0-9a-f]{40}$ ]]
[[ $ARM_REPOSITORY_DESCRIPTOR_RELEASE =~ ^asahi-packages-candidate-[0-9a-f]{40}$ ]]
[[ $ARM_REPOSITORY_DESCRIPTOR_SHA256 =~ ^[0-9a-f]{64}$ ]]
[[ $ARM_REPOSITORY_SOURCE_COMMIT =~ ^[0-9a-f]{40}$ ]]
[[ $ARM_REPOSITORY_SIGNING_FINGERPRINT =~ ^[A-F0-9]{40}$ ]]
[[ $ARM_REPOSITORY_PACKAGE_COUNT =~ ^[1-9][0-9]*$ ]]
[[ $ARM_RUNTIME_RELEASE =~ ^(asahi-quattro-[0-9a-f]{8}|asahi-packages-candidate-[0-9a-f]{40})$ ]]
[[ $ARM_RUNTIME_MANIFEST_SHA256 =~ ^[0-9a-f]{64}$ ]]
[[ $ARM_RUNTIME_SOURCE_COMMIT =~ ^[0-9a-f]{40}$ ]]
[[ $ARM_RUNTIME_SIGNING_FINGERPRINT =~ ^[A-F0-9]{40}$ ]]
[[ $ARM_RUNTIME_CHANNEL_SEQUENCE =~ ^[1-9][0-9]*$ ]]
[[ $ARM_RUNTIME_CHANNEL_TAG == "asahi-quattro-${ARM_RUNTIME_SOURCE_COMMIT:0:8}" ]]
[[ $ARM_RUNTIME_CHANNEL_SIGNING_FINGERPRINT =~ ^[A-F0-9]{40}$ ]]

# The cached package files and the repository the installed system syncs from
# must come from the same release. When they differ, a package built in both
# releases has one version and two checksums, and pacman aborts the very first
# install of it with no transaction and nothing in the log (this is what broke
# the VS Code install on the 2026-09-04 M1 reinstall). The two tags live in
# separate hand-edited files, so compare them here rather than trusting a
# repoint to touch both.
installed_pacman_conf=${INSTALLED_PACMAN_CONF:-$builder_root/../configs/airootfs/usr/share/omarchy-iso/pacman-online-installed-arm.conf}
if [[ -r $installed_pacman_conf ]]; then
  installed_release=$(sed -n 's#^Server = https://github.com/maralcbr/omarchy-pkgs/releases/download/##p' "$installed_pacman_conf" | head -1)
  [[ $installed_release == "$ARM_REPOSITORY_RELEASE" ]] || {
    echo "ERROR: the cached packages and the installed system name different releases" >&2
    echo "       arm-package-snapshots.conf:        $ARM_REPOSITORY_RELEASE" >&2
    echo "       pacman-online-installed-arm.conf:  ${installed_release:-<none>}" >&2
    echo "       Point both at the same release before building." >&2
    exit 1
  }
else
  echo "ERROR: cannot read the installed pacman configuration: $installed_pacman_conf" >&2
  exit 1
fi

repository_base="https://github.com/maralcbr/omarchy-pkgs/releases/download"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

mkdir -p "$destination"
# Keep immutable downloads outside the pruned offline closure. Every reuse
# still checks the signed descriptor's hash and a freshly verified signature.
snapshot_cache="$destination.snapshot-cache"
[[ ! -L $snapshot_cache ]]
mkdir -p "$snapshot_cache"

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

  local cached
  for cached in "$snapshot_cache/$checksum" "$destination/$filename"; do
    if [[ -f $cached && ! -L $cached && $(sha256sum "$cached" | cut -d' ' -f1) == "$checksum" ]]; then
      cp "$cached" "$work/$filename"
      break
    fi
  done
  if [[ ! -f $work/$filename ]]; then
    download "$repository_base/$release/$filename" "$work/$filename"
  fi
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

  # Do not persist unverified bytes, even if a download had the right name.
  if [[ ! -f $snapshot_cache/$checksum || -L $snapshot_cache/$checksum ||
    $(sha256sum "$snapshot_cache/$checksum" | cut -d' ' -f1) != "$checksum" ]]; then
    rm -f "$snapshot_cache/$checksum"
    install -m 0644 "$work/$filename" "$snapshot_cache/$checksum"
  fi
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
grep -Fxq "package_count=$ARM_REPOSITORY_PACKAGE_COUNT" "$work/CANDIDATE"
(( $(grep -c '^package=' "$work/CANDIDATE") == ARM_REPOSITORY_PACKAGE_COUNT ))

while IFS= read -r record; do
  IFS='|' read -r _ _ _ _ _ _ _ signature_checksum <<<"$record"
  verify_package_record "$ARM_REPOSITORY_RELEASE" "$builder_root/omarchy-arm-repository.asc" \
    "$ARM_REPOSITORY_SIGNING_FINGERPRINT" "$record" "$signature_checksum" \
    "$work/gnupg-repository"
done < <(sed -n 's/^package=//p' "$work/CANDIDATE")

runtime_url="$repository_base/$ARM_RUNTIME_RELEASE"
runtime_key="$builder_root/omarchy-arm-runtime.asc"
if [[ $ARM_RUNTIME_RELEASE == asahi-packages-candidate-* ]]; then
  runtime_key="$builder_root/omarchy-arm-repository.asc"
fi
download "$runtime_url/asahi-quattro-bundle.manifest" "$work/runtime.manifest"
download "$runtime_url/asahi-quattro-bundle.manifest.sig" "$work/runtime.manifest.sig"
[[ $(sha256sum "$work/runtime.manifest" | cut -d' ' -f1) == "$ARM_RUNTIME_MANIFEST_SHA256" ]]
verify_signature "$runtime_key" "$work/runtime.manifest.sig" \
  "$work/runtime.manifest" "$ARM_RUNTIME_SIGNING_FINGERPRINT" "$work/gnupg-runtime"
grep -Fxq 'format=2' "$work/runtime.manifest"
grep -Fxq 'bundle=asahi-quattro' "$work/runtime.manifest"
grep -Fxq "source_commit=$ARM_RUNTIME_SOURCE_COMMIT" "$work/runtime.manifest"
grep -Fxq 'package_count=6' "$work/runtime.manifest"
(( $(grep -c '^package=' "$work/runtime.manifest") == 6 ))

while IFS= read -r record; do
  verify_package_record "$ARM_RUNTIME_RELEASE" "$runtime_key" \
    "$ARM_RUNTIME_SIGNING_FINGERPRINT" "$record" "" "$work/gnupg-runtime"
done < <(sed -n 's/^package=//p' "$work/runtime.manifest")

# The channel record is what omarchy-update-asahi-bundle compares an installed
# system against. Fetch the pinned channel release and prove it names exactly
# the runtime pinned above, so the state seeded into the image cannot claim a
# bundle the channel does not actually serve.
channel_url="$repository_base/asahi-quattro-channel-$ARM_RUNTIME_CHANNEL_SEQUENCE"
download "$channel_url/asahi-quattro-channel" "$work/channel"
download "$channel_url/asahi-quattro-channel.sig" "$work/channel.sig"
verify_signature "$builder_root/omarchy-arm-runtime.asc" "$work/channel.sig" \
  "$work/channel" "$ARM_RUNTIME_CHANNEL_SIGNING_FINGERPRINT" "$work/gnupg-channel"
grep -Fxq 'format=1' "$work/channel"
grep -Fxq 'channel=asahi-quattro' "$work/channel"
grep -Fxq "sequence=$ARM_RUNTIME_CHANNEL_SEQUENCE" "$work/channel"
grep -Fxq "release_tag=$ARM_RUNTIME_CHANNEL_TAG" "$work/channel"
grep -Fxq "source_commit=$ARM_RUNTIME_SOURCE_COMMIT" "$work/channel"
grep -Fxq "manifest_sha256=$ARM_RUNTIME_MANIFEST_SHA256" "$work/channel"

install -m 0644 "$work/CANDIDATE" "$destination/ARM-REPOSITORY"
install -m 0644 "$work/runtime.manifest" "$destination/ARM-RUNTIME"
# Same shape omarchy-install-asahi-fresh writes on a scripted install.
cat >"$destination/ARM-RUNTIME-CHANNEL" <<EOF
format=1
sequence=$ARM_RUNTIME_CHANNEL_SEQUENCE
tag=$ARM_RUNTIME_CHANNEL_TAG
source_commit=$ARM_RUNTIME_SOURCE_COMMIT
package_source_commit=$ARM_REPOSITORY_SOURCE_COMMIT
EOF
chmod 0644 "$destination/ARM-RUNTIME-CHANNEL"
{
  sed -n 's/^package=//p' "$work/CANDIDATE"
  sed -n 's/^package=//p' "$work/runtime.manifest"
} | cut -d'|' -f5 >"$destination/ARM-PACKAGES"
(( $(wc -l <"$destination/ARM-PACKAGES") == ARM_REPOSITORY_PACKAGE_COUNT + 6 ))
