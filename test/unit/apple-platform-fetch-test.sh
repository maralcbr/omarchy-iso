#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
builder="$work/builder"
remote="$work/remote"
stubs="$work/stubs"
destination="$work/destination"
mkdir -p "$builder" "$remote" "$stubs"
cp "$ROOT/builder/apple-platform-snapshot.json" "$builder/snapshot.json"
cp "$ROOT/builder/validate-apple-platform-snapshot.sh" "$builder/"

keyring=$(jq -r '.trust.keyring.filename' "$builder/snapshot.json")
printf 'test keyring\n' >"$remote/$keyring"
while IFS= read -r package; do
  printf 'package %s\n' "$package" >"$remote/$package"
  printf 'signature %s\n' "$package" >"$remote/$package.sig"
done < <(jq -r '.packages[].filename' "$builder/snapshot.json")

cat >"$builder/verify-apple-platform-artifacts.sh" <<'STUB'
#!/bin/bash
set -euo pipefail
if [[ ${TEST_VERIFY_FAIL:-0} != 0 ]]; then
  exit 1
fi
[[ -f $2/$(jq -r '.trust.keyring.filename' "$1") ]]
while IFS= read -r package; do
  [[ -f $2/$package && -f $2/$package.sig ]]
done < <(jq -r '.packages[].filename' "$1")
STUB

cat >"$stubs/curl" <<'STUB'
#!/bin/bash
set -euo pipefail
url=""
output=""
while (($#)); do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
cp "$TEST_REMOTE/${url##*/}" "$output"
STUB
chmod +x "$builder"/*.sh "$stubs/curl"

TEST_REMOTE="$remote" BUILDER_ROOT="$builder" \
  APPLE_PLATFORM_SNAPSHOT="$builder/snapshot.json" PATH="$stubs:$PATH" \
  bash "$ROOT/builder/fetch-apple-platform-snapshot.sh" "$destination"
expected_count=$(jq -r '.packages | length' "$builder/snapshot.json")
(( $(wc -l <"$destination/APPLE-PACKAGES") == expected_count ))
(( $(find "$destination" -maxdepth 1 -type f -name '*.pkg.tar.xz' | wc -l) == expected_count ))
(( $(find "$destination" -maxdepth 1 -type f -name '*.pkg.tar.xz.sig' | wc -l) == expected_count ))

if TEST_VERIFY_FAIL=1 TEST_REMOTE="$remote" BUILDER_ROOT="$builder" \
  APPLE_PLATFORM_SNAPSHOT="$builder/snapshot.json" PATH="$stubs:$PATH" \
  bash "$ROOT/builder/fetch-apple-platform-snapshot.sh" "$work/rejected" 2>/dev/null; then
  echo "Apple platform fetch accepted failed artifact verification" >&2
  exit 1
fi
[[ ! -e $work/rejected/APPLE-PACKAGES ]]

echo "Apple platform fetch tests passed"
