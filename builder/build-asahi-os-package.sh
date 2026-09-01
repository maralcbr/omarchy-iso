#!/bin/bash

set -euo pipefail

product=${OMARCHY_ASAHI_PRODUCT:-/builder/products/omarchy-mx-mac.json}
output_dir=${OMARCHY_ASAHI_OUTPUT_DIR:-/out}
source_root=/usr/share/omarchy-iso
configured_source_root=${OMARCHY_ASAHI_CONFIGURED_SOURCE_ROOT:-$source_root}
finalized_source_root=${OMARCHY_ASAHI_FINALIZED_SOURCE_ROOT:-$source_root}
offline_mirror=/var/cache/airootfs/var/cache/omarchy/mirror/offline
pacman_config=${OMARCHY_ASAHI_PACMAN_CONFIG:-/var/cache/pacman-offline.builder.conf}
build_lock=${OMARCHY_ASAHI_BUILD_LOCK:-/builder/asahi-build-lock.json}
stage_input_root=${OMARCHY_ASAHI_STAGE_INPUT_ROOT:-/omarchy-asahi-stage-inputs}
build_mode=${OMARCHY_BUILD_MODE:-qualification}
run_id=${OMARCHY_BUILD_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}
[[ $run_id =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
  echo "build-asahi-os-package: unsafe build run ID: $run_id" >&2
  exit 1
}
run_evidence=$output_dir/build-evidence/$run_id
# The build-iso.sh flow legitimately pre-creates this directory: the early
# verified-package-cache and offline-repository phases record evidence under
# the same host-issued run ID before this builder starts. Adopt it only when
# it is provably their work: a real directory, owned by this user, not
# group/world writable, holding only known early-phase evidence files.
# Anything else — including stale reports from an earlier run — refuses
# before any work begins.
refuse_run_evidence() {
  echo "build-asahi-os-package: refusing to reuse existing build run evidence: $run_evidence" >&2
  echo "build-asahi-os-package: $1" >&2
  exit 1
}
if [[ -e $run_evidence || -L $run_evidence ]]; then
  [[ -d $run_evidence && ! -L $run_evidence ]] ||
    refuse_run_evidence "existing run evidence is not a real directory"
  evidence_owner=$(stat -c %u -- "$run_evidence") ||
    refuse_run_evidence "existing run evidence owner is unreadable"
  evidence_mode=$(stat -c %a -- "$run_evidence") ||
    refuse_run_evidence "existing run evidence mode is unreadable"
  [[ $evidence_owner == "$(id -u)" ]] ||
    refuse_run_evidence "existing run evidence has an untrusted owner"
  (( (8#$evidence_mode & 8#022) == 0 )) ||
    refuse_run_evidence "existing run evidence is group/world writable"
  while IFS= read -r -d '' evidence_entry; do
    evidence_name=${evidence_entry##*/}
    [[ -f $evidence_entry && ! -L $evidence_entry ]] ||
      refuse_run_evidence "unexpected non-file entry: $evidence_name"
    case "$evidence_name" in
      verified-package-cache.*|offline-repository-database.*|offline-repository-install-view.*) ;;
      *) refuse_run_evidence "unexpected entry: $evidence_name" ;;
    esac
  done < <(find "$run_evidence" -mindepth 1 -maxdepth 1 -print0)
fi
work=$(mktemp -d /var/cache/omarchy-asahi-package.XXXXXX)
mounts=()
loops=()
private_release_root=
private_release_device=
private_release_inode=

[[ $build_mode == qualification || $build_mode == diagnostic ]] || {
  echo "build-asahi-os-package: unsupported build mode: $build_mode" >&2
  exit 1
}
if [[ $build_mode == qualification && \
  ! ${SOURCE_DATE_EPOCH:-} =~ ^[0-9]+$ ]]; then
  echo "build-asahi-os-package: qualification requires a nonnegative SOURCE_DATE_EPOCH" >&2
  exit 1
fi
[[ -f $build_lock && ! -L $build_lock ]] || {
  echo "build-asahi-os-package: source lock is missing or unsafe: $build_lock" >&2
  exit 1
}
python3 /builder/asahi-lifecycle-lease.py ensure-directory \
  --path "$output_dir/build-evidence" \
  --allowed-owner 0 \
  --allowed-owner "${HOST_UID:-0}"
if [[ -e $run_evidence ]]; then
  # Verified by the adopt gate at startup; tighten to the strict mode.
  chmod 0700 -- "$run_evidence"
else
  mkdir -m 0700 -- "$run_evidence" || {
    echo "build-asahi-os-package: build run evidence already exists or is unsafe" >&2
    exit 1
  }
fi

# Checkpoint restoration and admission are a sourced control-plane adapter.
# The package builder remains responsible only for byte-producing orchestration.
[[ -f /builder/asahi-checkpoint-admission.sh &&
  ! -L /builder/asahi-checkpoint-admission.sh ]] || {
  echo "build-asahi-os-package: checkpoint admission adapter is missing or unsafe" >&2
  exit 1
}
source /builder/asahi-checkpoint-admission.sh

cleanup() {
  local index
  set +e
  if [[ -n ${target:-} ]] && mountpoint -q "$target"; then
    unmount_target_tree "$target" || true
  fi
  for ((index=${#mounts[@]} - 1; index >= 0; index--)); do
    mountpoint -q "${mounts[index]}" && umount "${mounts[index]}"
  done
  for ((index=${#loops[@]} - 1; index >= 0; index--)); do
    losetup -d "${loops[index]}" 2>/dev/null || true
  done
  if [[ -n $private_release_root && -n $private_release_device &&
    -n $private_release_inode ]]; then
    python3 /builder/asahi-release-publication.py cleanup \
      --private-root "$private_release_root" \
      --package-filename "$package_filename" \
      --manifest-name release-publication.json \
      --expected-device "$private_release_device" \
      --expected-inode "$private_release_inode" \
      --allowed-owner 0 \
      --allowed-owner "${HOST_UID:-0}" ||
      echo "build-asahi-os-package: private release cleanup failed" >&2
  fi
  rm -rf -- "$work"
}
trap cleanup EXIT

fail() {
  echo "build-asahi-os-package: $*" >&2
  exit 1
}

initialize_asahi_checkpoint_admission

[[ $(id -u) == 0 ]] || fail "root is required"
[[ -f $product ]] || fail "product configuration is missing: $product"
[[ -d $offline_mirror ]] || fail "verified offline package mirror is missing"
[[ -d $configured_source_root/orchestrator ]] ||
  fail "configured-stage installer source is missing"
[[ -d $finalized_source_root/orchestrator ]] ||
  fail "finalized-stage installer source is missing"
[[ -s $pacman_config ]] || fail "builder pacman config is missing or empty"
[[ -f $stage_input_root/index.json && ! -L $stage_input_root/index.json ]] ||
  fail "stage-specific input index is missing or unsafe"
[[ -f ${OMARCHY_OFFLINE_REPOSITORY_MANIFEST:-} && ! -L ${OMARCHY_OFFLINE_REPOSITORY_MANIFEST:-} ]] ||
  fail "verified offline repository manifest is missing or unsafe"
[[ -f ${OMARCHY_OFFLINE_REPOSITORY_IDENTITY:-} && ! -L ${OMARCHY_OFFLINE_REPOSITORY_IDENTITY:-} ]] ||
  fail "offline repository checkpoint identity is missing or unsafe"
[[ -f ${OMARCHY_ASAHI_OFFLINE_REPOSITORY_VIEW:-} && ! -L ${OMARCHY_ASAHI_OFFLINE_REPOSITORY_VIEW:-} ]] ||
  fail "install-time offline repository view is missing or unsafe"
jq -e '.validation == {result: "passed"} and
  .verification_kind == "offline-repository-install-view-v1"' \
  "$OMARCHY_ASAHI_OFFLINE_REPOSITORY_VIEW" >/dev/null ||
  fail "install-time offline repository view is invalid"
grep -Fxq '[offline]' "$pacman_config" || fail "builder pacman config has no offline repository"
grep -Eq '^Server[[:space:]]*=[[:space:]]*file://' "$pacman_config" ||
  fail "builder pacman config has no file repository server"

schema_version=$(jq -er '.schema_version' "$product")
[[ $schema_version == 1 ]] || fail "unsupported product schema"
package_filename=$(jq -er '.package_filename' "$product")
kernel_package=$(jq -er '.kernel_package' "$product")
esp_size=$(jq -er '.esp_size_bytes' "$product")
boot_size=$(jq -er '.boot_size_bytes' "$product")
root_size=$(jq -er '.root_size_bytes' "$product")
esp_volume_id=$(jq -er '.esp_volume_id' "$product")
boot_uuid=$(jq -er '.boot_filesystem_uuid' "$product")
root_uuid=$(jq -er '.root_filesystem_uuid' "$product")
boot_backend=$(jq -er '.boot_backend' "$product")
build_jobs=${OMARCHY_BUILD_JOBS:-10}
uefi_filename=$(jq -er '.uefi_payload.filename' "$product")
uefi_url=$(jq -er '.uefi_payload.url' "$product")
uefi_size=$(jq -er '.uefi_payload.size_bytes' "$product")
uefi_sha256=$(jq -er '.uefi_payload.sha256' "$product")

[[ $package_filename == "${package_filename##*/}" && $package_filename == *.zip ]] ||
  fail "invalid product package filename"
[[ $kernel_package =~ ^[a-z0-9][a-z0-9@._+-]+$ ]] || fail "invalid kernel package"
[[ $boot_backend == asahi-grub ]] || fail "unsupported Apple Silicon boot backend"
for size in "$esp_size" "$boot_size" "$root_size"; do
  [[ $size =~ ^[0-9]+$ ]] && (( size > 0 && size % 4096 == 0 )) ||
    fail "image sizes must be positive multiples of 4096"
done
[[ $build_jobs =~ ^[1-9][0-9]*$ ]] || fail "build concurrency must be a positive integer"

if [[ $build_mode == diagnostic ]]; then
  esp_size=$(jq -er '.modes.diagnostic.esp_size_bytes' "$build_lock")
  boot_size=$(jq -er '.modes.diagnostic.boot_size_bytes' "$build_lock")
  root_size=$(jq -er '.modes.diagnostic.root_size_bytes' "$build_lock")
fi

uefi_payload=$work/$uefi_filename
if [[ -n ${OMARCHY_ASAHI_UEFI_PAYLOAD:-} ]]; then
  cp -- "$OMARCHY_ASAHI_UEFI_PAYLOAD" "$uefi_payload"
else
  curl -fsSL "$uefi_url" -o "$uefi_payload"
fi
[[ $(wc -c <"$uefi_payload") == "$uefi_size" ]] || fail "UEFI payload size mismatch"
printf '%s  %s\n' "$uefi_sha256" "$uefi_payload" | sha256sum -c - >/dev/null ||
  fail "UEFI payload digest mismatch"

image_product=$work/image-product.json
jq -nS \
  --arg mode "$build_mode" \
  --arg kernel "$kernel_package" \
  --arg boot_backend "$boot_backend" \
  --arg esp_volume_id "$esp_volume_id" \
  --arg boot_uuid "$boot_uuid" \
  --arg root_uuid "$root_uuid" \
  --arg uefi_filename "$uefi_filename" \
  --arg uefi_sha256 "$uefi_sha256" \
  --argjson esp_size "$esp_size" \
  --argjson boot_size "$boot_size" \
  --argjson root_size "$root_size" \
  --argjson uefi_size "$uefi_size" \
  '{schema_version: 1, mode: $mode, kernel_package: $kernel,
    boot_backend: $boot_backend, esp_size_bytes: $esp_size,
    boot_size_bytes: $boot_size, root_size_bytes: $root_size,
    esp_volume_id: $esp_volume_id, boot_filesystem_uuid: $boot_uuid,
    root_filesystem_uuid: $root_uuid,
    uefi_payload: {filename: $uefi_filename, size_bytes: $uefi_size,
      sha256: $uefi_sha256}}' >"$image_product"

cp "$pacman_config" /etc/pacman.conf
sed -i "s#file:///var/cache/omarchy/mirror/offline/#file://$offline_mirror/#" \
  /etc/pacman.conf
node_runtime_identity=$work/node-runtime.identity.json
jq -eS '
  .node |
  select(keys == ["filename", "sha256", "size_bytes", "url", "version"]) |
  {schema_version: 1, verification_kind: "pinned-node-lock-v1",
    filename, sha256, size_bytes}
' "$build_lock" >"$node_runtime_identity" ||
  fail "pinned Node lock projection is invalid"
node_filename=$(jq -er '.filename' "$node_runtime_identity")
node_sha256=$(jq -er '.sha256' "$node_runtime_identity")
node_size=$(jq -er '.size_bytes' "$node_runtime_identity")
mkdir -p /opt/packages
python3 /builder/pinned-node-cache.py snapshot \
  --cache-root /var/cache/airootfs/opt/packages \
  --filename "$node_filename" \
  --destination-root /opt/packages \
  --sha256 "$node_sha256" \
  --size "$node_size" \
  --allowed-owner 0 || fail "verified Node runtime bundle is missing or stale"
node_tarball=/opt/packages/$node_filename

configured_runtime_manifest=$work/configured-runtime-inputs.json
finalized_runtime_manifest=$work/finalized-runtime-inputs.json
configured_product_manifest=$work/configured-product-inputs.json
finalized_product_manifest=$work/finalized-product-inputs.json
configured_runtime_setting_arguments=(
  --setting "OMARCHY_INSTALL_DEBUG=${OMARCHY_INSTALL_DEBUG:-}"
  --setting "OMARCHY_ISO_REF=${OMARCHY_ISO_REF:-quattro}"
  --setting "OMARCHY_MIRROR=${OMARCHY_MIRROR:-stable}"
  --setting "OMARCHY_NVIM_PACKAGE=${OMARCHY_NVIM_PACKAGE:-}"
  --setting "OMARCHY_OFFLINE_MIRROR_ROOT=$offline_mirror"
  --setting "OMARCHY_RUNTIME_PACKAGE=${OMARCHY_RUNTIME_PACKAGE:-}"
  --setting "OMARCHY_SETTINGS_PACKAGE=${OMARCHY_SETTINGS_PACKAGE:-}"
)
finalized_runtime_setting_arguments=(
  "${configured_runtime_setting_arguments[@]}"
  --setting "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-unset}"
)
python3 /builder/asahi_stage_inputs.py runtime-manifest \
  --root "$configured_source_root" \
  --spec /builder/asahi-stage-inputs.json \
  --stage configured-target \
  "${configured_runtime_setting_arguments[@]}" \
  --output "$configured_runtime_manifest"
python3 /builder/asahi_stage_inputs.py runtime-manifest \
  --root "$finalized_source_root" \
  --spec /builder/asahi-stage-inputs.json \
  --stage finalized-boot \
  "${finalized_runtime_setting_arguments[@]}" \
  --output "$finalized_runtime_manifest"
python3 /builder/asahi_stage_inputs.py product-manifest \
  --product "$product" \
  --stage configured-target \
  --output "$configured_product_manifest"
python3 /builder/asahi_stage_inputs.py product-manifest \
  --product "$product" \
  --stage finalized-boot \
  --output "$finalized_product_manifest"

# Each stage body lives in one auditable whole file. Source only the body that
# is about to execute so a downstream edit cannot become an upstream input.
source /builder/asahi-stages/base-images.sh
run_base_images_stage
source /builder/asahi-stages/image-runtime.sh
source /builder/asahi-stages/configured-target.sh
run_configured_target_stage
source /builder/asahi-stages/finalized-boot.sh
run_finalized_boot_stage
source /builder/asahi-build-reporting.sh

if [[ $build_mode == diagnostic ]]; then
  # diagnostic builds never emit a release ZIP and are never catalog eligible.
  record_diagnostic_retention_skip "$run_evidence"
  python3 /builder/summarize-asahi-build.py \
    --mode "$build_mode" --run-id "$run_id" --evidence-root "$run_evidence" \
    --output "$run_evidence/build-report.json"
  echo "Built diagnostic checkpoint evidence: $run_evidence"
  exit 0
fi

source /builder/asahi-stages/sealed-release-package.sh
run_sealed_release_package_stage

private_release_root=$output_dir/.omarchy-run-$run_id
mkdir -m 0700 -- "$private_release_root" ||
  fail "private release root already exists or is unsafe"
private_release_device=$(stat -c '%d' -- "$private_release_root")
private_release_inode=$(stat -c '%i' -- "$private_release_root")
package=$private_release_root/$package_filename
cp --sparse=always "$sealed_package" "$package"

source /builder/asahi-stages/installer-metadata.sh
run_installer_metadata_stage
chmod 0444 "$package" \
  "$package.asahi-package-evidence.json" \
  "$package.installer-data.json"
publication_manifest=$private_release_root/release-publication.json
python3 /builder/asahi-release-publication.py publish \
  --private-root "$private_release_root" \
  --release-root "$output_dir" \
  --package-filename "$package_filename" \
  --run-id "$run_id" \
  --manifest "$publication_manifest" \
  --allowed-owner 0 \
  --allowed-owner "${HOST_UID:-0}" >"$work/release-publication.result.json"
jq -e --arg run_id "$run_id" --arg package_filename "$package_filename" '
  .schema_version == 1 and .kind == "asahi-release-publication-v1" and
  .result == "passed" and .run_id == $run_id and
  .package_filename == $package_filename and
  (.reproducibility_match | type == "boolean")
' "$publication_manifest" >/dev/null || fail "release publication evidence is invalid"
cp -- "$publication_manifest" "$run_evidence/release-publication.json"
apply_checkpoint_retention
published_package=$output_dir/$package_filename
python3 /builder/summarize-asahi-build.py \
  --mode "$build_mode" --run-id "$run_id" --evidence-root "$run_evidence" \
  --package "$published_package" --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --output "$run_evidence/build-report.json"

echo "Built verified Asahi OS package: $published_package"
