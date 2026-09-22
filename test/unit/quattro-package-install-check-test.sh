#!/bin/bash
set -euo pipefail
ROOT=$(cd "${BASH_SOURCE[0]%/*}/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
source "$ROOT/builder/quattro-package-install-check.sh"
# Redirect the evidence and disposable-root paths into this fixture.
eval "$(declare -f run_quattro_package_install_check | sed "s|/out/build-evidence|$work/evidence|g; s|/configs/pacman-offline.conf|$ROOT/configs/pacman-offline.conf|g")"
OMARCHY_CANDIDATE_ROOT=$work/input
OMARCHY_BUILD_MODE=diagnostic
build_cache_dir=$work/cache
offline_mirror_dir=$work/mirror
HOST_UID=$(id -u); HOST_GID=$(id -g)
mkdir -p "$OMARCHY_CANDIDATE_ROOT" "$build_cache_dir"
cat >"$OMARCHY_CANDIDATE_ROOT/manifest.json" <<'JSON'
{"schema":1,"source_revision":"fixture", "packages":[{"name":"omarchy","version":"1-1"},{"name":"omarchy-settings","version":"1-1"},{"name":"omarchy-mac","version":"1-1"}]}
JSON
mktemp() { mkdir -p "$work/root"; printf '%s\n' "$work/root"; }
pacstrap() {
  [[ $1 == "-C" && $3 == "-G" && $4 == "-M" ]]
  grep -Fxq 'DisableSandbox' "$2"
  grep -Fxq "Server = file://$offline_mirror_dir" "$2"
  [[ ${*:6} == "base omarchy omarchy-settings omarchy-mac" ]]
  for name in omarchy omarchy-settings omarchy-mac; do
    path="usr/share/doc/$name/source-revision"
    [[ $name != "omarchy-mac" ]] || path=usr/share/omarchy-mac/source-revision
    mkdir -p "${5}/${path%/*}"
    printf 'fixture\n' >"${5}/$path"
  done
  case $failure in
    transaction) return 1 ;;
    hook) echo 'error: command failed to execute correctly' ;;
  esac
}
pacman() {
  printf '%s\n' 'omarchy 1-1' 'omarchy-settings 1-1'
  [[ $failure == "missing" ]] || printf '%s\n' 'omarchy-mac 1-1'
}
chown() { :; }
failure=none; OMARCHY_BUILD_RUN_ID=success
run_quattro_package_install_check
grep -q '"result": "passed"' "$work/evidence/success/package-install-check/result.json"
for failure in transaction hook missing; do
  OMARCHY_BUILD_RUN_ID=$failure
  if run_quattro_package_install_check; then exit 1; fi
  [[ ! -e $work/evidence/$failure/package-install-check/result.json ]]
done
# Exercise all five installation targets and the independent recipe revision.
python3 - "$OMARCHY_CANDIDATE_ROOT/manifest.json" <<'PYTEST'
import json, sys
from pathlib import Path
p=Path(sys.argv[1]); data=json.loads(p.read_text())
data['schema']=2
data['package_repository_revision']='recipe-fixture'
data['packages'] += [{'name':name,'version':'1-1'} for name in ('avd-fw','libva-v4l2_request-avd')]
p.write_text(json.dumps(data))
PYTEST
original_pacstrap=$(declare -f pacstrap)
eval "${original_pacstrap/pacstrap ()/desktop_pacstrap ()}"
pacstrap() {
  [[ ${*:6} == "base omarchy omarchy-settings omarchy-mac avd-fw libva-v4l2_request-avd" ]]
  desktop_pacstrap "${@:1:9}"
  for name in avd-fw libva-v4l2_request-avd; do
    mkdir -p "$5/usr/share/doc/$name"
    printf '%s\n' "${video_revision:-recipe-fixture}" >"$5/usr/share/doc/$name/source-revision"
  done
}
pacman() {
  for name in omarchy omarchy-settings omarchy-mac avd-fw libva-v4l2_request-avd; do
    [[ $failure != "$name" ]] || continue
    printf '%s 1-1\n' "$name"
  done
}
failure=none; OMARCHY_BUILD_RUN_ID=five
run_quattro_package_install_check
for failure in avd-fw libva-v4l2_request-avd; do
  OMARCHY_BUILD_RUN_ID=$failure
  if run_quattro_package_install_check; then exit 1; fi
  [[ ! -e $work/evidence/$failure/package-install-check/result.json ]]
done
failure=none; video_revision=wrong; OMARCHY_BUILD_RUN_ID=wrong-video-revision
if run_quattro_package_install_check; then exit 1; fi
printf 'PASS: fast package check records success only after transaction and version checks\n'
# The dependency-snapshot path exercises the full base list, not just candidates.
python3 - "$OMARCHY_CANDIDATE_ROOT/manifest.json" <<'PYTEST'
import json, sys
from pathlib import Path
p=Path(sys.argv[1]); data=json.loads(p.read_text())
data['schema']=3
data['packages'] += [{'name':name,'version':'1-1'} for name in ('asdcontrol','tobi-try','qemu-user-static','qemu-user-static-binfmt')]
p.write_text(json.dumps(data))
PYTEST
OMARCHY_DEPENDENCY_ROOT=$work/dependencies
OMARCHY_NVIM_PACKAGE=omarchy-nvim
shipped_base_packages=$work/base.packages
printf '%s\n' '# base fixture' base neovim dotnet-runtime-bin >"$shipped_base_packages"
pacstrap() {
  [[ ${*:6} == "base omarchy omarchy-settings omarchy-mac avd-fw libva-v4l2_request-avd asdcontrol tobi-try qemu-user-static qemu-user-static-binfmt base neovim dotnet-runtime-bin omarchy-nvim" ]]
  desktop_pacstrap "${@:1:9}"
  for name in avd-fw libva-v4l2_request-avd asdcontrol tobi-try qemu-user-static qemu-user-static-binfmt; do
    mkdir -p "$5/usr/share/doc/$name"
    printf 'recipe-fixture\n' >"$5/usr/share/doc/$name/source-revision"
  done
}
pacman() {
  for name in omarchy omarchy-settings omarchy-mac avd-fw libva-v4l2_request-avd asdcontrol tobi-try qemu-user-static qemu-user-static-binfmt; do
    printf '%s 1-1\n' "$name"
  done
}
failure=none; OMARCHY_BUILD_RUN_ID=nine-with-full-base
run_quattro_package_install_check

# Schema 4 selects every boot candidate and refuses either replaced legacy
# boot package even if all candidate versions and source records match.
python3 - "$OMARCHY_CANDIDATE_ROOT/manifest.json" <<'PYTEST'
import json, sys
from pathlib import Path
p=Path(sys.argv[1]); data=json.loads(p.read_text())
data['schema']=4
data['packages'] += [{'name':name,'version':'1-1'} for name in
    ('omarchy-mac-boot','uboot-asahi','limine-mkinitcpio-hook','limine-snapper-sync')]
p.write_text(json.dumps(data))
PYTEST
full_candidates=(omarchy omarchy-settings omarchy-mac avd-fw libva-v4l2_request-avd
  asdcontrol tobi-try qemu-user-static qemu-user-static-binfmt
  omarchy-mac-boot uboot-asahi limine-mkinitcpio-hook limine-snapper-sync)
pacstrap() {
  [[ ${*:6} == "base ${full_candidates[*]} base neovim dotnet-runtime-bin omarchy-nvim" ]]
  desktop_pacstrap "${@:1:9}"
  for name in "${full_candidates[@]:3}"; do
    mkdir -p "$5/usr/share/doc/$name"
    printf 'recipe-fixture\n' >"$5/usr/share/doc/$name/source-revision"
  done
}
pacman() {
  for name in "${full_candidates[@]}"; do
    printf '%s 1-1\n' "$name"
  done
  case "$failure" in
    omarchy-apple-boot|omarchy-first-boot) printf '%s 1-1\n' "$failure" ;;
  esac
}
failure=none; OMARCHY_BUILD_RUN_ID=thirteen-with-full-base
run_quattro_package_install_check
grep -q '"result": "passed"' "$work/evidence/$OMARCHY_BUILD_RUN_ID/package-install-check/result.json"
for failure in omarchy-apple-boot omarchy-first-boot; do
  OMARCHY_BUILD_RUN_ID="thirteen-with-$failure"
  if run_quattro_package_install_check >"$work/out" 2>"$work/error"; then exit 1; fi
  grep -Fq 'Legacy boot package installed alongside Limine candidate' "$work/error"
  [[ ! -e $work/evidence/$OMARCHY_BUILD_RUN_ID/package-install-check/result.json ]]
done
printf 'PASS: all thirteen candidates are selected and legacy boot packages are rejected\n'
