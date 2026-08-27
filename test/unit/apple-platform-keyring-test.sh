#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
installer="$ROOT/builder/install-apple-platform-keyring.sh"
source_snapshot="$ROOT/builder/apple-platform-snapshot.json"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
stubs="$work/stubs"
package_root="$work/package-root"
pacman_gpg_dir="$work/pacman-gnupg"
call_log="$work/calls"
snapshot="$work/snapshot.json"
keyring_filename=asahi-alarm-keyring-test-any.pkg.tar.xz
keyring_package="$work/$keyring_filename"
expected_fingerprint=12CE6799A94A3F1B5DDFFE88F576553597FB8FEB
mkdir -p "$stubs" "$package_root/usr/share/pacman/keyrings" "$pacman_gpg_dir"

make_package() {
  local package_name=$1

  printf 'pkgname = %s\n' "$package_name" >"$package_root/.PKGINFO"
  printf 'synthetic public key\n' \
    >"$package_root/usr/share/pacman/keyrings/asahi-alarm.gpg"
  rm -f "$keyring_package"
  bsdtar -cf "$keyring_package" -C "$package_root" \
    .PKGINFO usr/share/pacman/keyrings/asahi-alarm.gpg
  printf 'synthetic detached signature\n' >"$keyring_package.sig"
}

make_snapshot() {
  local package_hash signature_hash

  package_hash=$(sha256sum "$keyring_package" | awk '{ print $1 }')
  signature_hash=$(sha256sum "$keyring_package.sig" | awk '{ print $1 }')
  jq --arg filename "$keyring_filename" \
    --arg url "https://example.invalid/$keyring_filename" \
    --arg package_hash "$package_hash" \
    --arg signature_hash "$signature_hash" \
    --arg fingerprint "$expected_fingerprint" '
      .trust.signing_fingerprint = $fingerprint |
      .trust.keyring.filename = $filename |
      .trust.keyring.url = $url |
      .trust.keyring.sha256 = $package_hash |
      .trust.keyring.signature_sha256 = $signature_hash
    ' "$source_snapshot" >"$snapshot"
}

cat >"$stubs/gpg" <<'STUB'
#!/bin/bash
set -euo pipefail
homedir=""
previous=""
for argument in "$@"; do
  if [[ $previous == "--homedir" ]]; then
    homedir=$argument
  fi
  previous=$argument
done
if [[ " $* " == *" --export-ownertrust "* ]]; then
  printf '%s:%s:\n' "${TEST_INSTALLED_FINGERPRINT:-$TEST_EXPECTED_FINGERPRINT}" \
    "${TEST_OWNERTRUST:-4}"
elif [[ " $* " == *" --fingerprint "* ]]; then
  if [[ $homedir == "$TEST_PACMAN_GPG_DIR" ]]; then
    fingerprint=${TEST_INSTALLED_FINGERPRINT:-$TEST_EXPECTED_FINGERPRINT}
  else
    fingerprint=${TEST_SOURCE_FINGERPRINT:-$TEST_EXPECTED_FINGERPRINT}
  fi
  printf 'fpr:::::::::%s:\n' "$fingerprint"
fi
STUB
cat >"$stubs/pacman" <<'STUB'
#!/bin/bash
set -euo pipefail
printf 'pacman %s\n' "$*" >>"$TEST_CALL_LOG"
config=""
previous=""
for argument in "$@"; do
  if [[ $previous == "--config" ]]; then
    config=$argument
  fi
  previous=$argument
done
grep -Fxq 'SigLevel = Never' "$config"
grep -Fxq 'LocalFileSigLevel = Never' "$config"
STUB
cat >"$stubs/pacman-key" <<'STUB'
#!/bin/bash
set -euo pipefail
[[ $* == "--populate asahi-alarm" ]]
printf 'pacman-key %s\n' "$*" >>"$TEST_CALL_LOG"
STUB
cat >"$stubs/pacman-conf" <<'STUB'
#!/bin/bash
set -euo pipefail
[[ $* == "GPGDir" ]]
printf '%s\n' "$TEST_PACMAN_GPG_DIR"
STUB
chmod +x "$stubs"/*

run_installer() {
  TEST_EXPECTED_FINGERPRINT="$expected_fingerprint" \
    TEST_PACMAN_GPG_DIR="$pacman_gpg_dir" TEST_CALL_LOG="$call_log" \
    PATH="$stubs:$PATH" "$installer" "$snapshot" "$keyring_package"
}

make_package asahi-alarm-keyring
make_snapshot
run_installer | grep -Fxq 'Pacman trusts the exact pinned Apple platform signing key'
grep -Fq 'pacman --config ' "$call_log"
grep -Fxq 'pacman-key --populate asahi-alarm' "$call_log"
echo "ok - exact pinned keyring is enrolled and fully trusted"

make_package unrelated-keyring
make_snapshot
if run_installer >"$work/out" 2>"$work/error"; then
  echo "wrong keyring package identity unexpectedly passed" >&2
  exit 1
fi
grep -Fq 'keyring package identity mismatch' "$work/error"
echo "ok - wrong keyring package identity fails closed"

make_package asahi-alarm-keyring
make_snapshot
: >"$call_log"
if TEST_SOURCE_FINGERPRINT=0000000000000000000000000000000000000000 \
  run_installer >"$work/out" 2>"$work/error"; then
  echo "wrong source fingerprint unexpectedly passed" >&2
  exit 1
fi
grep -Fq 'keyring source fingerprint mismatch' "$work/error"
[[ ! -s $call_log ]]
echo "ok - wrong source fingerprint is rejected before pacman"

: >"$call_log"
if TEST_INSTALLED_FINGERPRINT=0000000000000000000000000000000000000000 \
  run_installer >"$work/out" 2>"$work/error"; then
  echo "wrong installed fingerprint unexpectedly passed" >&2
  exit 1
fi
grep -Fq 'Installed Apple platform signing fingerprint mismatch' "$work/error"
[[ -s $call_log ]]
echo "ok - wrong installed fingerprint fails closed"

: >"$call_log"
if TEST_OWNERTRUST=3 run_installer >"$work/out" 2>"$work/error"; then
  echo "insufficient ownertrust unexpectedly passed" >&2
  exit 1
fi
grep -Fq 'signing key is not fully trusted' "$work/error"
echo "ok - insufficient ownertrust fails closed"

echo "Apple platform keyring tests passed"
