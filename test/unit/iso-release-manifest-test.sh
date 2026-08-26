#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
SNAPSHOT="$ROOT/builder/apple-platform-snapshot.json"
SOURCE_COMMIT=1823960c6772179d6878d9bc3938f7f94f0c0fa1

pass() {
  printf 'ok - %s\n' "$1"
}

fail() {
  local description="$1"
  local detail="${2:-}"

  [[ -n $detail ]] && printf '%s\n' "$detail" >&2
  printf 'not ok - %s\n' "$description" >&2
  exit 1
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/stubs"

cat >"$work/stubs/gpgv" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >>"$GPGV_LOG"
[[ ${GPGV_FAIL:-0} == 1 ]] && exit 1
exit 0
STUB
chmod +x "$work/stubs/gpgv"

export GPGV_LOG="$work/gpgv.log"
: >"$GPGV_LOG"
touch "$work/release-keyring.gpg"
iso="$work/omarchy-5.0.0-apple-silicon.iso"
printf 'deterministic test ISO bytes\n' >"$iso"
media_evidence="$iso.apple-media-evidence.json"

write_media_evidence() {
  local iso_sha256 snapshot_sha256
  iso_sha256=$(sha256sum "$iso")
  iso_sha256=${iso_sha256%% *}
  snapshot_sha256=$(sha256sum "$SNAPSHOT")
  snapshot_sha256=${snapshot_sha256%% *}
  jq -n -S \
    --arg filename "${iso##*/}" \
    --argjson size "$(stat -c '%s' "$iso")" \
    --arg iso_sha256 "$iso_sha256" \
    --arg snapshot_sha256 "$snapshot_sha256" '
    {
      schema_version: 1,
      verification_kind: "static-apple-media",
      artifact: {filename: $filename, size: $size, sha256: $iso_sha256},
      layout: {
        schema_version: 1,
        target: {
          architecture: "aarch64",
          artifact_kind: "iso",
          boot_backend: "asahi-grub",
          platform: "apple-silicon"
        },
        checks: {
          bootaa64_pe_architecture: "aarch64",
          generic_arm_kernel_absent: true,
          initramfs_asahi_hook: true,
          iso_tree_bootaa64_matches_esp: true,
          limine_boot_artifacts_absent: true,
          live_kernel: "linux-asahi"
        },
        hashes: {
          bootaa64_sha256: "0000000000000000000000000000000000000000000000000000000000000000",
          initramfs_sha256: "1111111111111111111111111111111111111111111111111111111111111111",
          kernel_sha256: "2222222222222222222222222222222222222222222222222222222222222222",
          platform_snapshot_sha256: $snapshot_sha256
        }
      },
      boot: {
        blocker: "disposable-asahi-boot-evidence-absent",
        verified: false
      }
    }
  ' >"$media_evidence"
}

write_media_evidence

make_manifest() {
  local sequence="$1"
  local version="${2:-5.0.0}"

  "$ROOT/bin/omarchy-iso-manifest" \
    --sequence "$sequence" \
    --version "$version" \
    --source-commit "$SOURCE_COMMIT" \
    --package-snapshot "$SNAPSHOT" \
    --media-evidence "$media_evidence" \
    "$iso" >/dev/null
  touch "$iso.manifest.json.sig"
}

verify_manifest() {
  PATH="$work/stubs:$PATH" "$ROOT/bin/omarchy-iso-manifest-verify" \
    --keyring "$work/release-keyring.gpg" \
    --state "$work/sequence-state.json" \
    --package-snapshot "$SNAPSHOT" \
    --media-evidence "$media_evidence" \
    "$@"
}

make_manifest 7
manifest="$iso.manifest.json"
first_hash=$(sha256sum "$manifest")
first_hash=${first_hash%% *}
make_manifest 7
second_hash=$(sha256sum "$manifest")
second_hash=${second_hash%% *}
[[ $first_hash == "$second_hash" ]] ||
  fail "manifest generation is reproducible" "$first_hash != $second_hash"
pass "manifest generation is reproducible"

jq -e --arg iso_name "${iso##*/}" --arg snapshot_sha "$(sha256sum "$SNAPSHOT" | cut -d ' ' -f 1)" '
  .schema_version == 1 and
  .sequence == 7 and
  .release.version == "5.0.0" and
  .release.source_commit == "1823960c6772179d6878d9bc3938f7f94f0c0fa1" and
  .target == {
    architecture: "aarch64",
    artifact_kind: "iso",
    boot_backend: "asahi-grub",
    media_target: "aarch64/apple-silicon",
    platform: "apple-silicon"
  } and
  .artifact.filename == $iso_name and
  .package_snapshot.sha256 == $snapshot_sha
' "$manifest" >/dev/null || fail "manifest binds the exact Apple target and package snapshot"
pass "manifest binds the exact Apple target and package snapshot"

verify_manifest "$manifest" "$iso" >/dev/null ||
  fail "signed manifest verifies before anti-rollback state exists"
[[ ! -e $work/sequence-state.json ]] ||
  fail "read-only verification does not create anti-rollback state"
grep -qF "$work/release-keyring.gpg $manifest.sig $manifest" "$GPGV_LOG" ||
  fail "verification checks the detached signature with the selected keyring" "$(cat "$GPGV_LOG")"
pass "signed manifest and exact ISO verify without mutating state"

cp "$media_evidence" "$work/original-evidence.json"
jq -S '.layout.hashes.kernel_sha256 = "3333333333333333333333333333333333333333333333333333333333333333"' \
  "$work/original-evidence.json" >"$media_evidence"
set +e
verify_manifest "$manifest" "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -qF "media evidence does not match the signed release manifest" "$work/error" ||
  fail "changed media evidence is rejected" "$(cat "$work/error")"
cp "$work/original-evidence.json" "$media_evidence"
pass "the signature binds the exact static media-evidence bytes"

set +e
GPGV_FAIL=1 verify_manifest "$manifest" "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -qF "signature verification failed" "$work/error" ||
  fail "an invalid detached signature is rejected" "$(cat "$work/error")"
pass "an invalid detached signature is rejected"

cp "$manifest" "$work/original-manifest"
printf 'tampered\n' >>"$iso"
set +e
verify_manifest "$manifest" "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -Eq "ISO bytes do not match|media evidence does not bind the exact ISO" "$work/error" ||
  fail "ISO byte changes are rejected" "$(cat "$work/error")"
pass "ISO byte changes are rejected"
printf 'deterministic test ISO bytes\n' >"$iso"

cp "$SNAPSHOT" "$work/renamed-snapshot.json"
set +e
PATH="$work/stubs:$PATH" "$ROOT/bin/omarchy-iso-manifest-verify" \
  --keyring "$work/release-keyring.gpg" \
  --state "$work/sequence-state.json" \
  --package-snapshot "$work/renamed-snapshot.json" \
  --media-evidence "$media_evidence" \
  "$manifest" "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -qF "snapshot does not match" "$work/error" ||
  fail "a renamed platform snapshot is rejected" "$(cat "$work/error")"
pass "the signed snapshot filename and bytes are both enforced"

jq -S '.unexpected = true' "$work/original-manifest" >"$work/unknown.json"
touch "$work/unknown.json.sig"
set +e
verify_manifest "$work/unknown.json" "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -qF "schema or Apple target identity is invalid" "$work/error" ||
  fail "unknown manifest members are rejected" "$(cat "$work/error")"
pass "unknown manifest members are rejected"

sed '1a\  "sequence": 7,' "$work/original-manifest" >"$work/duplicate.json"
touch "$work/duplicate.json.sig"
set +e
verify_manifest "$work/duplicate.json" "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -qF "duplicate JSON member" "$work/error" ||
  fail "duplicate manifest members are rejected" "$(cat "$work/error")"
pass "duplicate manifest members are rejected"

jq -c . "$work/original-manifest" >"$work/noncanonical.json"
touch "$work/noncanonical.json.sig"
set +e
verify_manifest "$work/noncanonical.json" "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -qF "not canonical JSON" "$work/error" ||
  fail "noncanonical manifest encodings are rejected" "$(cat "$work/error")"
pass "noncanonical manifest encodings are rejected"

cp "$work/original-manifest" "$manifest"
verify_manifest --commit-state "$manifest" "$iso" >/dev/null ||
  fail "verified sequence can be committed"
[[ $(jq -r '.highest_sequence' "$work/sequence-state.json") == 7 ]] ||
  fail "sequence 7 is persisted in trusted state" "$(cat "$work/sequence-state.json")"
pass "verified sequence is committed atomically"

make_manifest 6
set +e
verify_manifest "$manifest" "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -qF "older than trusted sequence 7" "$work/error" ||
  fail "a rollback manifest is rejected" "$(cat "$work/error")"
pass "a rollback manifest is rejected"

make_manifest 7 5.0.0-rebuilt
set +e
verify_manifest "$manifest" "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -qF "conflicts with the trusted manifest" "$work/error" ||
  fail "sequence equivocation is rejected" "$(cat "$work/error")"
pass "the same sequence cannot identify different manifest bytes"

make_manifest 8
verify_manifest "$manifest" "$iso" >/dev/null ||
  fail "a newer sequence verifies"
[[ $(jq -r '.highest_sequence' "$work/sequence-state.json") == 7 ]] ||
  fail "read-only verification leaves the stored sequence unchanged"
verify_manifest --commit-state "$manifest" "$iso" >/dev/null ||
  fail "a newer sequence can be committed"
[[ $(jq -r '.highest_sequence' "$work/sequence-state.json") == 8 ]] ||
  fail "newer sequence advances trusted state" "$(cat "$work/sequence-state.json")"
verify_manifest --commit-state "$manifest" "$iso" >/dev/null ||
  fail "committing the exact same manifest is idempotent"
pass "newer sequences advance state and exact replays are idempotent"

cp "$work/sequence-state.json" "$work/canonical-state.json"
jq -c . "$work/canonical-state.json" >"$work/sequence-state.json"
set +e
verify_manifest "$manifest" "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -qF "state is not canonical JSON" "$work/error" ||
  fail "noncanonical anti-rollback state is rejected" "$(cat "$work/error")"
pass "anti-rollback state has a strict canonical format"

set +e
"$ROOT/bin/omarchy-iso-manifest" \
  --sequence 0 --version 5.0.0 --source-commit "$SOURCE_COMMIT" \
  --package-snapshot "$SNAPSHOT" --media-evidence "$media_evidence" \
  "$iso" >"$work/out" 2>"$work/error"
status=$?
set -e
(( status != 0 )) && grep -qF "Sequence must be" "$work/error" ||
  fail "sequence zero is rejected" "$(cat "$work/error")"
pass "release sequences must be positive bounded integers"
