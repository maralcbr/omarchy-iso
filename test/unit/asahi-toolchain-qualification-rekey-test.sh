#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

stage_inputs=$work/stage-inputs
lock=$stage_inputs/builder-toolchain/source-lock.json
source_manifest=$stage_inputs/builder-toolchain/source-manifest.json
checkpoint_root=$work/checkpoints
rekey_plan_root=$work/rekey-plan
run_manifest=$work/run-manifest.json
legacy_identity=$(printf '1%.0s' {1..64})

python3 "$ROOT/builder/asahi_stage_inputs.py" generate \
  --repo-root "$ROOT" \
  --spec "$ROOT/builder/asahi-stage-inputs.json" \
  --build-lock "$ROOT/builder/asahi-build-lock.json" \
  --mode qualification \
  --output-root "$stage_inputs"
mkdir -p \
  "$checkpoint_root/builder-toolchain/$legacy_identity" \
  "$rekey_plan_root" \
  "$work/fake-bin"
printf '%s\n' '{"immutable":true,"validation":{"result":"passed"}}' \
  >"$checkpoint_root/builder-toolchain/$legacy_identity/manifest.json"
printf '%s\n' '{"schema_version":1,"reason":"stage-input-granularity-v1"}' \
  >"$rekey_plan_root/builder-toolchain.json"
cat >"$work/fake-bin/docker" <<'SH'
#!/bin/sh
touch "$OMARCHY_FAKE_DOCKER_CALLED"
exit 97
SH
chmod +x "$work/fake-bin/docker"

fingerprint_tree() {
  python3 - "$1" <<'PY'
import hashlib
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted((root, *root.rglob("*"))):
    relative = path.relative_to(root).as_posix() if path != root else "."
    metadata = os.lstat(path)
    digest.update(relative.encode())
    digest.update(b"\0")
    digest.update(f"{stat.S_IMODE(metadata.st_mode):04o}".encode())
    digest.update(b"\0")
    if path.is_file():
        digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
}

checkpoint_before=$(fingerprint_tree "$checkpoint_root")
plan_before=$(fingerprint_tree "$rekey_plan_root")
if PATH="$work/fake-bin:$PATH" \
  OMARCHY_BUILD_MODE=qualification \
  OMARCHY_ASAHI_CHECKPOINT_ROOT="$checkpoint_root" \
  OMARCHY_ASAHI_REKEY_PLAN_ROOT="$rekey_plan_root" \
  OMARCHY_ASAHI_TOOLCHAIN_RUN_MANIFEST="$run_manifest" \
  OMARCHY_FAKE_DOCKER_CALLED="$work/docker-called" \
  OMARCHY_ASAHI_TOOLCHAIN_LOCK="$lock" \
  OMARCHY_ASAHI_TOOLCHAIN_SOURCE_MANIFEST="$source_manifest" \
    "$ROOT/builder/ensure-asahi-toolchain-image.sh" \
    >"$work/out" 2>"$work/error"; then
  echo "qualification unexpectedly migrated a legacy toolchain checkpoint" >&2
  exit 1
fi
grep -Fq \
  'qualification requires a previously verified toolchain checkpoint' \
  "$work/error"
[[ ! -e $work/docker-called ]]
[[ ! -e $run_manifest ]]
[[ $(fingerprint_tree "$checkpoint_root") == "$checkpoint_before" ]]
[[ $(fingerprint_tree "$rekey_plan_root") == "$plan_before" ]]
grep -Fq \
  'elapsed_seconds: $elapsed_seconds, cache_hit: false, output: $output,' \
  "$ROOT/builder/ensure-asahi-toolchain-image.sh"

echo "ok - qualification rejects legacy toolchain rekey before Docker or checkpoint mutation"
