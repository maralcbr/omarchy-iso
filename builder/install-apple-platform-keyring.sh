#!/bin/bash

set -euo pipefail

snapshot=${1:?Usage: install-apple-platform-keyring.sh SNAPSHOT KEYRING_PACKAGE}
keyring_package=${2:?Usage: install-apple-platform-keyring.sh SNAPSHOT KEYRING_PACKAGE}
builder_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

"$builder_root/validate-apple-platform-snapshot.sh" "$snapshot" >/dev/null

keyring_filename=$(jq -r '.trust.keyring.filename' "$snapshot")
keyring_sha256=$(jq -r '.trust.keyring.sha256' "$snapshot")
keyring_signature_sha256=$(jq -r '.trust.keyring.signature_sha256' "$snapshot")
expected_fingerprint=$(jq -r '.trust.signing_fingerprint' "$snapshot")
keyring_signature="$keyring_package.sig"

[[ ${keyring_package##*/} == "$keyring_filename" ]] || {
  echo "Apple platform keyring filename mismatch" >&2
  exit 1
}
[[ -f $keyring_package && -f $keyring_signature ]] || {
  echo "Missing Apple platform keyring package or signature" >&2
  exit 1
}
echo "$keyring_sha256  $keyring_package" | sha256sum --check --status
echo "$keyring_signature_sha256  $keyring_signature" | sha256sum --check --status

actual_package_name=$(bsdtar -xOf "$keyring_package" .PKGINFO |
  awk -F ' = ' '$1 == "pkgname" { print $2; exit }')
[[ $actual_package_name == "asahi-alarm-keyring" ]] || {
  echo "Apple platform keyring package identity mismatch" >&2
  exit 1
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
chmod 0700 "$work"
bsdtar -xOf "$keyring_package" usr/share/pacman/keyrings/asahi-alarm.gpg \
  >"$work/asahi-alarm.gpg"
gpg --batch --homedir "$work" --import "$work/asahi-alarm.gpg" >/dev/null 2>&1 || true
actual_fingerprint=$(gpg --batch --homedir "$work" --with-colons \
  --fingerprint "$expected_fingerprint" | awk -F: '$1 == "fpr" { print $10; exit }')
[[ $actual_fingerprint == "$expected_fingerprint" ]] || {
  echo "Apple platform keyring source fingerprint mismatch" >&2
  exit 1
}
gpg --batch --homedir "$work" --verify "$keyring_signature" "$keyring_package" \
  >/dev/null 2>&1

cat >"$work/pacman-bootstrap.conf" <<'EOF'
[options]
Architecture = auto
SigLevel = Never
LocalFileSigLevel = Never
EOF
pacman --config "$work/pacman-bootstrap.conf" --noconfirm -U "$keyring_package"
pacman-key --populate asahi-alarm

pacman_gpg_dir=$(pacman-conf GPGDir)
installed_fingerprint=$(gpg --batch --homedir "$pacman_gpg_dir" --with-colons \
  --fingerprint "$expected_fingerprint" | awk -F: '$1 == "fpr" { print $10; exit }')
[[ $installed_fingerprint == "$expected_fingerprint" ]] || {
  echo "Installed Apple platform signing fingerprint mismatch" >&2
  exit 1
}
gpg --batch --homedir "$pacman_gpg_dir" --export-ownertrust |
  grep -Fxq "$expected_fingerprint:4:" || {
    echo "Installed Apple platform signing key is not fully trusted" >&2
    exit 1
  }

echo "Pacman trusts the exact pinned Apple platform signing key"
