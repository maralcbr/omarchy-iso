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
{"source_revision":"fixture", "packages":[{"name":"omarchy","version":"1-1"},{"name":"omarchy-settings","version":"1-1"},{"name":"omarchy-mac","version":"1-1"}]}
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
printf 'PASS: fast package check records success only after transaction and version checks\n'
