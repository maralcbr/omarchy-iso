#!/bin/bash
set -euo pipefail
ROOT=$(cd "${BASH_SOURCE[0]%/*}/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
source "$ROOT/builder/package-architecture.sh"
source "$ROOT/builder/quattro-candidate-packages.sh"
export OMARCHY_ARCH=aarch64 OMARCHY_MEDIA_TARGET=aarch64/apple-silicon
export OMARCHY_ARTIFACT_KIND=asahi-os-package OMARCHY_MIRROR=stable
select_omarchy_package_roles
configure_package_architecture
[[ $OMARCHY_RUNTIME_PACKAGE == "omarchy-dev" ]]
[[ $TARGET_BASE_PACKAGE_LIST == "omarchy-base-asahi.packages" ]]
[[ -z $(printf 'snapper\n' | filter_target_packages) ]]
export OMARCHY_CANDIDATE_ROOT=$work/candidate
select_omarchy_package_roles
configure_package_architecture
[[ $OMARCHY_RUNTIME_PACKAGE == "omarchy" && $OMARCHY_SETTINGS_PACKAGE == "omarchy-settings" ]]
[[ $TARGET_BASE_PACKAGE_LIST == "omarchy-base.packages" ]]
[[ $(printf 'snapper\nlinux\n' | filter_target_packages) == $'snapper\nlinux-asahi' ]]
# Shared optional manifests must not pull Intel/T2/NVIDIA drivers into Apple
# images; unknown packages remain visible so missing requirements fail closed.
[[ $(printf '%s\n' linux-t2 apple-bcm-firmware nvidia-utils intel-media-driver mise-bin omarchy-mac required-new-package | filter_target_packages) == $'mise\nomarchy-mac\nrequired-new-package' ]]
(
  unset OMARCHY_CANDIDATE_ROOT
  [[ $(printf '%s\n' linux-t2 mise-bin | filter_target_packages) == $'linux-t2\nmise-bin' ]]
)
candidate_package_files=(omarchy-1.pkg.tar.xz omarchy-settings-1.pkg.tar.xz omarchy-mac-1.pkg.tar.xz)
requested_package_files=$work/selected
printf '%s\n' "${candidate_package_files[@]}" >"$requested_package_files"
verify_quattro_candidate_selection
printf '%s\n' omarchy-dev-1.pkg.tar.xz >>"$requested_package_files"
if verify_quattro_candidate_selection; then exit 1; fi
printf '%s\n' "${candidate_package_files[0]}" >"$requested_package_files"
if verify_quattro_candidate_selection; then exit 1; fi
# Reject wrong targets/modes before invoking Docker or touching any cache.
for mode in qualification diagnostic; do
  if bash "$ROOT/bin/omarchy-iso-make" --target x86_64/pc --mode "$mode" \
    --candidate-packages "$work" "$(printf '%064d' 0)" "$(printf '%040d' 0)" >"$work/error" 2>&1; then
    exit 1
  fi
  grep -q 'Candidate packages require' "$work/error"
done
printf 'PASS: candidate roles, retained Snapper, exact package selection and CLI guards\n'

if env -u SOURCE_DATE_EPOCH bash "$ROOT/bin/omarchy-iso-make" \
  --target aarch64/apple-silicon --artifact asahi-os-package --mode diagnostic \
  --candidate-packages "$work" "$(printf '%064d' 0)" "$(printf '%040d' 0)" >"$work/error" 2>&1; then
  exit 1
fi
grep -q 'Candidate image builds require a nonnegative SOURCE_DATE_EPOCH' "$work/error"

(
  export OMARCHY_DEPENDENCY_ROOT=$work/dependencies
  [[ $(printf '%s\n' mise-bin dotnet-runtime omarchy-mac | filter_target_packages) == $'mise-bin\ndotnet-runtime-bin\nomarchy-mac' ]]
)

(
  export OMARCHY_CANDIDATE_SCHEMA=4
  [[ $(printf '%s\n' limine limine-mkinitcpio-hook limine-snapper-sync grub | filter_target_packages) == $'limine\nlimine-mkinitcpio-hook\nlimine-snapper-sync\ngrub' ]]
)
[[ $(printf '%s\n' limine limine-mkinitcpio-hook limine-snapper-sync | filter_target_packages) == grub ]]
