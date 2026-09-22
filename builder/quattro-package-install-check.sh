#!/bin/bash

# Fast integration boundary: real pacman transaction and package hooks in a
# disposable directory root. No filesystem images, boot finalization or ZIP.
run_quattro_package_install_check() {
  [[ -n ${OMARCHY_CANDIDATE_ROOT:-} && $OMARCHY_BUILD_MODE == "diagnostic" ]] || return 1
  local target evidence started pacman_config
  local -a candidate_names
  mapfile -t candidate_names < <(jq -er '.packages[].name' "$OMARCHY_CANDIDATE_ROOT/manifest.json")
  (( ${#candidate_names[@]} == 3 || ${#candidate_names[@]} == 5 || ${#candidate_names[@]} == 9 || ${#candidate_names[@]} == 13 )) || return 1
  target=$(mktemp -d /var/tmp/quattro-package-root.XXXXXX)
  evidence=/out/build-evidence/$OMARCHY_BUILD_RUN_ID/package-install-check
  mkdir -p "$evidence"
  started=$SECONDS
  # The short path stops before configured-runtime-inputs, which normally
  # creates the offline config and mirror symlink. Use the verified mirror
  # directly and scope Docker's downloader-sandbox exception to this check.
  pacman_config=$evidence/pacman.conf
  awk -v mirror="$offline_mirror_dir" '
    /^\[options\]$/ { print; print "DisableSandbox"; next }
    /^Server[[:space:]]*=/ { print "Server = file://" mirror; next }
    { print }
  ' /configs/pacman-offline.conf >"$pacman_config" || return 1
  local -a install_targets=(base "${candidate_names[@]}")
  if [[ -n ${OMARCHY_DEPENDENCY_ROOT:-} ]]; then
    local -a base_targets
    mapfile -t base_targets < <(awk 'NF && $1 !~ /^#/ {print $1}' "$shipped_base_packages")
    install_targets+=("${base_targets[@]}" "$OMARCHY_NVIM_PACKAGE")
  fi
  # Package signatures and exact repository contents have already been
  # verified by the same stages used for the complete diagnostic image.
  if ! pacstrap -C "$pacman_config" -G -M \
    "$target" "${install_targets[@]}" >"$evidence/pacstrap.log" 2>&1; then
    cat "$evidence/pacstrap.log" >&2
    return 1
  fi
  if grep -Eq '^error:|failed to execute correctly' "$evidence/pacstrap.log"; then
    cat "$evidence/pacstrap.log" >&2
    return 1
  fi
  pacman --root "$target" -Q >"$evidence/installed-packages.txt" || return 1
  python3 - "$OMARCHY_CANDIDATE_ROOT/manifest.json" "$target" "$evidence" "$((SECONDS - started))" <<'PY' || return 1
import json
from pathlib import Path
import sys
manifest, target, evidence = map(Path, sys.argv[1:4])
data = json.loads(manifest.read_text())
installed = dict(line.split(' ', 1) for line in (evidence / 'installed-packages.txt').read_text().splitlines())
for package in data['packages']:
    name = package['name']
    if installed.get(name) != package['version']:
        raise SystemExit('Installed candidate version mismatch: ' + name)
    revision = 'usr/share/omarchy-mac/source-revision' if name == 'omarchy-mac' else f'usr/share/doc/{name}/source-revision'
    expected = data['package_repository_revision'] if name not in ('omarchy', 'omarchy-settings', 'omarchy-mac') else data['source_revision']
    if (target / revision).read_text().strip() != expected:
        raise SystemExit('Installed candidate source mismatch: ' + name)
if data['schema'] == 4 and {'omarchy-apple-boot', 'omarchy-first-boot'} & installed.keys():
    raise SystemExit('Legacy boot package installed alongside Limine candidate')
if {'omarchy-dev', 'omarchy-settings-dev'} & installed.keys():
    raise SystemExit('Old desktop installed alongside candidate')
(evidence / 'result.json').write_text(json.dumps({
    'schema': 1, 'result': 'passed', 'source_revision': data['source_revision'],
    'elapsed_seconds': int(sys.argv[4]), 'package_installation': 'passed',
    'hardware_setup': 'not run', 'boot': 'not tested', 'publication': 'none',
}, indent=2) + '\n')
PY
  echo "Package installation check passed: $evidence"
  # The disposable container owns the root; it disappears when Docker exits.
  chown -R "$HOST_UID:$HOST_GID" "$evidence"
}
