#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Superseded assertion (until 2026-08-29): this predicate was pinned in
# builder/build-iso.sh. The schema-2 work dismantled that file (24,799 -> 1,346
# bytes) and moved the snapshot-count guard verbatim into the
# verified-package-cache stage. Re-pinned to the new owner; both count branches
# are pinned here, where only the generic branch was pinned before, so the
# replacement is strictly stronger.
snapshot_count_guard=$ROOT/builder/asahi-stages/verified-package-cache.sh
grep -Fq '${#snapshot_packages[@]} == ARM_REPOSITORY_PACKAGE_COUNT + 6' \
  "$snapshot_count_guard"
grep -Fq 'ARM_REPOSITORY_PACKAGE_COUNT + 7 + apple_platform_package_count' \
  "$snapshot_count_guard"

builder="$work/builder"
remote="$work/remote"
stubs="$work/stubs"
destination="$work/destination"
repository_release="asahi-packages-candidate-$(printf 'b%.0s' {1..40})"
descriptor_release="asahi-packages-candidate-$(printf 'b%.0s' {1..40})"
runtime_release=asahi-quattro-1234abcd
source_commit=$(printf 'c%.0s' {1..40})
runtime_commit=$(printf 'd%.0s' {1..40})
fingerprint=$(printf 'A%.0s' {1..40})

mkdir -p "$builder" "$remote/$repository_release" "$remote/$runtime_release" "$stubs"
touch "$builder/omarchy-arm-repository.asc" "$builder/omarchy-arm-runtime.asc"

repository_manifest="$remote/$repository_release/CANDIDATE"
cat >"$repository_manifest" <<EOF
format=1
channel=candidate
release_tag=$descriptor_release
source_commit=$source_commit
signing_fingerprint=$fingerprint
package_count=31
EOF

for index in $(seq 1 31); do
  package=$(printf 'repo-pkg-%02d' "$index")
  filename="$package-1-1-aarch64.pkg.tar.xz"
  signature="$filename.sig"
  printf 'repository package %s\n' "$index" >"$remote/$repository_release/$filename"
  printf 'signature %s\n' "$index" >"$remote/$repository_release/$signature"
  checksum=$(sha256sum "$remote/$repository_release/$filename" | cut -d' ' -f1)
  signature_checksum=$(sha256sum "$remote/$repository_release/$signature" | cut -d' ' -f1)
  printf 'package=%d|%s|1-1|aarch64|%s|%s|%s|%s\n' \
    "$index" "$package" "$filename" "$checksum" "$signature" "$signature_checksum" \
    >>"$repository_manifest"
done
printf 'descriptor signature\n' >"$remote/$repository_release/CANDIDATE.sig"

runtime_manifest="$remote/$runtime_release/asahi-quattro-bundle.manifest"
cat >"$runtime_manifest" <<EOF
format=2
bundle=asahi-quattro
source_commit=$runtime_commit
package_count=6
EOF

for index in $(seq 1 6); do
  package=$(printf 'runtime-pkg-%02d' "$index")
  filename="$package-1-1-any.pkg.tar.xz"
  printf 'runtime package %s\n' "$index" >"$remote/$runtime_release/$filename"
  printf 'signature %s\n' "$index" >"$remote/$runtime_release/$filename.sig"
  checksum=$(sha256sum "$remote/$runtime_release/$filename" | cut -d' ' -f1)
  printf 'package=%d|%s|1-1|any|%s|%s\n' \
    "$index" "$package" "$filename" "$checksum" >>"$runtime_manifest"
done
printf 'manifest signature\n' >"$remote/$runtime_release/asahi-quattro-bundle.manifest.sig"

cat >"$builder/arm-package-snapshots.conf" <<EOF
ARM_REPOSITORY_RELEASE=$repository_release
ARM_REPOSITORY_DESCRIPTOR_RELEASE=$descriptor_release
ARM_REPOSITORY_DESCRIPTOR_SHA256=$(sha256sum "$repository_manifest" | cut -d' ' -f1)
ARM_REPOSITORY_SOURCE_COMMIT=$source_commit
ARM_REPOSITORY_SIGNING_FINGERPRINT=$fingerprint
ARM_REPOSITORY_PACKAGE_COUNT=31
ARM_RUNTIME_RELEASE=$runtime_release
ARM_RUNTIME_MANIFEST_SHA256=$(sha256sum "$runtime_manifest" | cut -d' ' -f1)
ARM_RUNTIME_SOURCE_COMMIT=$runtime_commit
ARM_RUNTIME_SIGNING_FINGERPRINT=$fingerprint
EOF

cat >"$stubs/curl" <<'STUB'
#!/bin/bash
set -euo pipefail

url=""
output=""
while (( $# > 0 )); do
  case "$1" in
    --output)
      output="$2"
      shift 2
      ;;
    http*)
      url="$1"
      shift
      ;;
    *)
      shift
      ;;
  esac
done

path=${url#*/download/}
cp "$TEST_REMOTE/$path" "$output"
STUB

cat >"$stubs/gpg" <<'STUB'
#!/bin/bash
if [[ " $* " == *" --verify "* ]]; then
  printf '[GNUPG:] VALIDSIG %s 0 0 0 0 0 0 0 0 0\n' "$TEST_FINGERPRINT"
fi
STUB
chmod +x "$stubs/curl" "$stubs/gpg"

export TEST_REMOTE="$remote"
export TEST_FINGERPRINT="$fingerprint"
BUILDER_ROOT="$builder" PATH="$stubs:$PATH" \
  bash "$ROOT/builder/fetch-arm-package-snapshots.sh" "$destination"

package_count=$(find "$destination" -maxdepth 1 -type f -name '*.pkg.tar.*' ! -name '*.sig' | wc -l)
(( package_count == 37 ))
[[ -f $destination/ARM-REPOSITORY && -f $destination/ARM-RUNTIME ]]
(( $(wc -l <"$destination/ARM-PACKAGES") == 37 ))

printf 'corrupted\n' >>"$remote/$repository_release/repo-pkg-01-1-1-aarch64.pkg.tar.xz"
if BUILDER_ROOT="$builder" PATH="$stubs:$PATH" \
  bash "$ROOT/builder/fetch-arm-package-snapshots.sh" "$work/corrupt-destination" 2>/dev/null; then
  echo "snapshot verifier accepted a corrupted package" >&2
  exit 1
fi

grep -Fq 'asahi-packages-candidate-[0-9a-f]{40}' "$ROOT/builder/fetch-arm-package-snapshots.sh"
grep -Fq 'runtime_key="$builder_root/omarchy-arm-repository.asc"' "$ROOT/builder/fetch-arm-package-snapshots.sh"

echo "ARM package snapshot tests passed"
