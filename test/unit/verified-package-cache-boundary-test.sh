#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
controller="$ROOT/builder/build-iso.sh"
package_stage="$ROOT/builder/asahi-stages/verified-package-cache.sh"
package_architecture="$ROOT/builder/package-architecture.sh"
stage_timing="$ROOT/builder/epochrealtime-stage-timing.sh"
package_checkpoint="$ROOT/builder/checkpoint-verified-package-cache.sh"

bash -n "$controller" "$package_stage" "$package_architecture" \
  "$stage_timing" "$package_checkpoint"

source "$stage_timing"
start_epochrealtime_timer timer_started
for ((iteration = 0; iteration < 1000; iteration++)); do :; done
elapsed=$(elapsed_epochrealtime_timer "$timer_started")
[[ $elapsed =~ ^[0-9]+\.[0-9]{6}$ ]]
(( 10#${elapsed/./} > 0 ))
if /bin/bash -c "source '$stage_timing'; start_epochrealtime_timer started" \
  >/dev/null 2>&1; then
  echo "Apple Bash 3.2 unexpectedly passed the EPOCHREALTIME timer gate" >&2
  exit 1
fi

# The controller crosses the package seam exactly twice: initialize immutable
# prerequisites, then produce the cache after profile assembly. It must not
# retain a second implementation of any byte-producing package operation.
[[ $(grep -Fc 'source /builder/asahi-stages/verified-package-cache.sh' "$controller") == 1 ]]
[[ $(grep -Ec '^initialize_verified_package_cache_stage$' "$controller") == 1 ]]
[[ $(grep -Ec '^prepare_verified_package_cache$' "$controller") == 1 ]]
for implementation in \
  'pacman() {' \
  'NODE_DIST_URL=' \
  'fetch-arm-package-snapshots.sh' \
  'fetch-apple-platform-snapshot.sh' \
  'install-apple-platform-keyring.sh' \
  'build-omarchy-packages.sh' \
  'checkpoint_verified_package_cache'; do
  if grep -Fq "$implementation" "$controller"; then
    echo "Package implementation leaked into build-iso.sh: $implementation" >&2
    exit 1
  fi
  grep -Fq "$implementation" "$package_stage"
done
grep -Fxq 'source /builder/package-architecture.sh' "$package_stage"
grep -Fxq 'source /builder/epochrealtime-stage-timing.sh' "$package_stage"
grep -Fq 'start_epochrealtime_timer verified_package_stage_started' "$package_stage"
grep -Fq 'verified_package_stage_elapsed_seconds=$(elapsed_epochrealtime_timer' \
  "$package_checkpoint"
grep -Fq '"$verified_package_stage_started")' "$package_checkpoint"
if grep -Fq -- '--elapsed-seconds 0' "$package_checkpoint"; then
  echo "Verified package checkpoint still reports a fabricated zero duration" >&2
  exit 1
fi
grep -Fq -- '--elapsed-seconds "$verified_package_stage_elapsed_seconds"' \
  "$package_checkpoint"
grep -Fq -- '--input runtime-manifest="$runtime_manifest"' \
  "$package_checkpoint"
grep -Fq '"$verified_package_runtime_manifest"' "$package_checkpoint"
[[ $(grep -Ec '^[[:space:]]*if uses_verified_package_checkpoint; then$' \
  "$package_stage") == 2 ]]

# Package roles remain behaviorally identical on published, development, and
# Apple paths when exercised through the focused package interface.
(
  export OMARCHY_ARCH=x86_64 OMARCHY_ISO_REF=quattro
  export OMARCHY_MIRROR=stable OMARCHY_MEDIA_TARGET=x86_64/pc
  export OMARCHY_ARTIFACT_KIND=iso
  source "$package_architecture"
  select_omarchy_package_roles
  configure_package_architecture
  [[ $OMARCHY_RUNTIME_PACKAGE == omarchy ]]
  [[ $OMARCHY_SETTINGS_PACKAGE == omarchy-settings ]]
  [[ $OMARCHY_NVIM_PACKAGE == omarchy-nvim ]]
  [[ $NODE_DIST_ARCH == x64 ]]
  [[ $PROFILE_PACKAGES == packages.x86_64 ]]
  [[ " ${BUILD_HOST_PACKAGES[*]} " != *" mkinitcpio "* ]]
  if uses_verified_package_checkpoint; then
    echo "x86 unexpectedly selected the Apple package checkpoint" >&2
    exit 1
  fi
)
(
  export OMARCHY_ARCH=x86_64 OMARCHY_ISO_REF=edge
  export OMARCHY_MIRROR=edge OMARCHY_MEDIA_TARGET=x86_64/pc
  export OMARCHY_ARTIFACT_KIND=iso
  source "$package_architecture"
  select_omarchy_package_roles
  configure_package_architecture
  [[ $OMARCHY_RUNTIME_PACKAGE == omarchy-dev ]]
  [[ $OMARCHY_SETTINGS_PACKAGE == omarchy-settings-dev ]]
)
(
  export OMARCHY_ARCH=aarch64 OMARCHY_ISO_REF=quattro
  export OMARCHY_MIRROR=stable OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
  export OMARCHY_ARTIFACT_KIND=asahi-os-package
  source "$package_architecture"
  select_omarchy_package_roles
  configure_package_architecture
  [[ $OMARCHY_RUNTIME_PACKAGE == omarchy-dev ]]
  [[ $OMARCHY_SETTINGS_PACKAGE == omarchy-settings-dev ]]
  [[ $NODE_DIST_ARCH == arm64 ]]
  [[ $PROFILE_PACKAGES == packages.aarch64 ]]
  [[ " ${BUILD_HOST_PACKAGES[*]} " == *" mkinitcpio "* ]]
  [[ " ${BUILD_HOST_PACKAGES[*]} " == *" archinstall "* ]]
  [[ " ${BUILD_HOST_PACKAGES[*]} " == *" btrfs-progs "* ]]
  uses_verified_package_checkpoint
)
(
  export OMARCHY_ARCH=aarch64 OMARCHY_ISO_REF=quattro
  export OMARCHY_MIRROR=stable OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
  export OMARCHY_ARTIFACT_KIND=iso
  source "$package_architecture"
  select_omarchy_package_roles
  configure_package_architecture
  if uses_verified_package_checkpoint; then
    echo "Apple validation ISO unexpectedly selected the package checkpoint" >&2
    exit 1
  fi
)

# Exercise the content-addressed stage interface with fake in-memory edits. No
# package payload, cache, container, or network is touched by this proof.
python3 - "$ROOT" <<'PY'
import importlib.util
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
module_path = root / "builder/asahi_stage_inputs.py"
module_spec = importlib.util.spec_from_file_location("asahi_stage_inputs", module_path)
if module_spec is None or module_spec.loader is None:
    raise RuntimeError(f"could not load {module_path}")
module = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(module)
specification = module.load_specification(root / "builder/asahi-stage-inputs.json")
build_lock = json.loads((root / "builder/asahi-build-lock.json").read_text())
verified = specification["stages"]["verified-package-cache"]
expected_settings = {
    "OMARCHY_ARCH",
    "OMARCHY_ARTIFACT_KIND",
    "OMARCHY_ISO_REF",
    "OMARCHY_MEDIA_TARGET",
    "OMARCHY_MIRROR",
    "OMARCHY_NVIM_PACKAGE",
    "OMARCHY_RUNTIME_PACKAGE",
    "OMARCHY_SETTINGS_PACKAGE",
}
assert set(verified["runtime_settings"]) == expected_settings

runtime_settings = {name: f"pinned-{name}" for name in expected_settings}
runtime_before = module.build_stage_runtime_manifest(
    root=root,
    stage="verified-package-cache",
    declaration=verified,
    settings=runtime_settings,
)
for name in sorted(expected_settings):
    changed_settings = runtime_settings | {name: runtime_settings[name] + "-changed"}
    runtime_after = module.build_stage_runtime_manifest(
        root=root,
        stage="verified-package-cache",
        declaration=verified,
        settings=changed_settings,
    )
    assert runtime_after["input_digest"] != runtime_before["input_digest"], name

def fingerprints(overrides=None):
    return module.declared_stage_fingerprints(
        repository=root,
        specification=specification,
        build_lock=build_lock,
        mode="diagnostic",
        content_overrides=overrides or {},
    )

before = fingerprints()
for relative in (
    "builder/package-architecture.sh",
    "builder/asahi-stages/verified-package-cache.sh",
    "builder/apple-platform-snapshot.json",
):
    changed = fingerprints({relative: (root / relative).read_bytes() + b"\n# fake edit\n"})
    assert changed["builder-toolchain"] == before["builder-toolchain"], relative
    for stage in specification["stage_order"][1:]:
        assert changed[stage] != before[stage], (relative, stage)

for relative in (
    "builder/architecture.sh",
    "configs/profiledef.sh",
    "builder/branding/omarchy-logo.png",
):
    changed = fingerprints({relative: (root / relative).read_bytes() + b"\n# fake edit\n"})
    assert changed["verified-package-cache"] == before["verified-package-cache"], relative

assert "builder/package-architecture.sh" in verified["source_paths"]
assert "builder/epochrealtime-stage-timing.sh" in verified["source_paths"]
assert "builder/architecture.sh" not in verified["source_paths"]
PY

echo "Verified package-cache boundary tests passed"
