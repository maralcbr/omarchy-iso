#!/bin/bash

set -euo pipefail

snapshot=${1:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/apple-platform-snapshot.json}
expected_packages='["asahi-audio","asahi-bless","asahi-desktop-meta","asahi-fwextract","asahi-scripts","bankstown","linux-asahi","linux-asahi-headers","lzfse","m1n1","speakersafetyd","startup-disk","tiny-dfr","uboot-asahi","virglrenderer","widevine"]'

jq -e --argjson expected_packages "$expected_packages" '
  def hash: type == "string" and test("^[0-9a-f]{64}$");
  .schema_version == 1 and
  .source.repository == "asahi-alarm/asahi-alarm" and
  .source.release_tag == "aarch64" and
  .source.tag_is_mutable == true and
  .target == {architecture: "aarch64", platform: "apple-silicon", boot_backend: "asahi-grub"} and
  (.trust.signing_fingerprint | test("^[0-9A-F]{40}$")) and
  (.trust.keyring.sha256 | hash) and
  (.trust.keyring.filename as $keyring_filename |
    .trust.keyring.url | endswith("/" + $keyring_filename)) and
  ([.packages[].name] | sort) == $expected_packages and
  ([.packages[].name] | unique | length) == 16 and
  all(.packages[];
    (.filename | type == "string" and length > 0) and
    (.sha256 | hash) and
    (.signature_sha256 | hash)
  ) and
  .verification.package_hashes_match == true and
  .verification.detached_signature_hashes_match == true and
  .verification.detached_signatures_verified == true and
  .verification.verification_key_fingerprint == .trust.signing_fingerprint and
  .media_readiness.ready == false and
  .media_readiness.blockers == [
    "device-tree-selection-unimplemented",
    "machine-firmware-handoff-unimplemented",
    "bootaa64-assembly-unverified"
  ]
' "$snapshot" >/dev/null || {
  echo "Apple platform snapshot is invalid: $snapshot" >&2
  exit 1
}

echo "Apple platform package snapshot is valid but media remains blocked"
