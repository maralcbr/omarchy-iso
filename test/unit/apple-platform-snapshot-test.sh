#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
validator="$ROOT/builder/validate-apple-platform-snapshot.sh"
artifact_verifier="$ROOT/builder/verify-apple-platform-artifacts.sh"
snapshot="$ROOT/builder/apple-platform-snapshot.json"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

"$validator" "$snapshot" | grep -Fxq \
  'Apple platform package snapshot is valid but media remains blocked'

jq '.target.boot_backend = "limine"' "$snapshot" >"$work/limine.json"
if "$validator" "$work/limine.json" >/dev/null 2>&1; then
  echo "Apple platform snapshot accepted Limine" >&2
  exit 1
fi

jq 'del(.packages[0])' "$snapshot" >"$work/incomplete.json"
if "$validator" "$work/incomplete.json" >/dev/null 2>&1; then
  echo "Apple platform snapshot accepted an incomplete package set" >&2
  exit 1
fi

jq '.media_readiness.ready = true' "$snapshot" >"$work/false-ready.json"
if "$validator" "$work/false-ready.json" >/dev/null 2>&1; then
  echo "Apple platform snapshot accepted false media readiness" >&2
  exit 1
fi

grep -Fq 'sha256sum --check --status' "$artifact_verifier"
grep -Fq 'actual_fingerprint == "$expected_fingerprint"' "$artifact_verifier"
grep -Fq 'gpg --batch --homedir "$verify_home" --verify' "$artifact_verifier"
grep -Fq "grep -Fxq 'usr/lib/asahi-boot/m1n1.bin'" "$artifact_verifier"
grep -Fq "grep -Fxq 'usr/lib/asahi-boot/u-boot-nodtb.bin'" "$artifact_verifier"
grep -Fq "grep -Fxq 'usr/bin/lzfse'" "$artifact_verifier"
grep -Fq "grep -Fxq 'usr/lib/initcpio/hooks/asahi'" "$artifact_verifier"
grep -Fq "grep -Fxq 'usr/bin/asahi-fwextract'" "$artifact_verifier"

echo "Apple platform snapshot tests passed"
