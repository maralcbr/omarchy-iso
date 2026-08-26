#!/bin/bash

set -euo pipefail

if (( $# != 3 )); then
  echo "Usage: validate-apple-media-evidence.sh EVIDENCE_JSON ISO APPLE_PLATFORM_SNAPSHOT" >&2
  exit 1
fi

evidence=$1
iso=$2
snapshot=$3
for required in "$evidence" "$iso" "$snapshot"; do
  [[ -f $required ]] || { echo "Required media-evidence input not found: $required" >&2; exit 1; }
done

canonical=$(mktemp)
trap 'rm -f "$canonical"' EXIT
python3 - "$evidence" >"$canonical" <<'PY'
import json
import sys


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


try:
    with open(sys.argv[1], "r", encoding="utf-8") as stream:
        document = json.load(stream, object_pairs_hook=reject_duplicates)
except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
    print(f"Invalid Apple media evidence: {error}", file=sys.stderr)
    raise SystemExit(1)

json.dump(document, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
sys.stdout.write("\n")
PY
cmp -s -- "$canonical" "$evidence" || {
  echo "Apple media evidence is not canonical JSON" >&2
  exit 1
}

if ! jq -e '
  keys == ["artifact", "boot", "layout", "schema_version", "verification_kind"] and
  .schema_version == 1 and
  .verification_kind == "static-apple-media" and
  (.artifact | keys == ["filename", "sha256", "size"]) and
  (.artifact.filename | type == "string" and length > 0 and contains("/") == false) and
  (.artifact.size | type == "number" and floor == . and . >= 0) and
  (.artifact.sha256 | type == "string" and test("^[0-9a-f]{64}$")) and
  (.layout | keys == ["checks", "hashes", "schema_version", "target"]) and
  .layout.schema_version == 1 and
  .layout.target == {
    architecture: "aarch64",
    artifact_kind: "iso",
    boot_backend: "asahi-grub",
    platform: "apple-silicon"
  } and
  .layout.checks == {
    bootaa64_pe_architecture: "aarch64",
    generic_arm_kernel_absent: true,
    initramfs_asahi_hook: true,
    iso_tree_bootaa64_matches_esp: true,
    limine_boot_artifacts_absent: true,
    live_kernel: "linux-asahi"
  } and
  (.layout.hashes | keys == ["bootaa64_sha256", "initramfs_sha256", "kernel_sha256", "platform_snapshot_sha256"]) and
  all(.layout.hashes[]; type == "string" and test("^[0-9a-f]{64}$")) and
  .boot == {
    blocker: "disposable-asahi-boot-evidence-absent",
    verified: false
  }
' "$evidence" >/dev/null; then
  echo "Apple media evidence schema or target identity is invalid" >&2
  exit 1
fi

iso_sha256=$(sha256sum -- "$iso")
iso_sha256=${iso_sha256%% *}
snapshot_sha256=$(sha256sum -- "$snapshot")
snapshot_sha256=${snapshot_sha256%% *}
if [[ $(jq -r '.artifact.filename' "$evidence") != "${iso##*/}" ||
      $(jq -r '.artifact.size' "$evidence") != "$(stat -c '%s' -- "$iso")" ||
      $(jq -r '.artifact.sha256' "$evidence") != "$iso_sha256" ]]; then
  echo "Apple media evidence does not bind the exact ISO" >&2
  exit 1
fi
if [[ $(jq -r '.layout.hashes.platform_snapshot_sha256' "$evidence") != "$snapshot_sha256" ]]; then
  echo "Apple media evidence does not bind the exact platform snapshot" >&2
  exit 1
fi
