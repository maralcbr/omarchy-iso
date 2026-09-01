#!/bin/bash

run_installer_metadata_stage() {
  metadata_identity=$work/installer-metadata.identity.json
  metadata_evidence=$work/installer-metadata-evidence.json
  metadata_installer_data=$work/installer-data.json
  create_stage_identity installer-metadata "$metadata_identity" \
    --input release-package="$package" \
    --input product="$product" \
    --input package-verifier=/builder/verify-asahi-os-package.py
  if ! restore_stage installer-metadata "$metadata_identity" \
    --destination package-evidence="$metadata_evidence" \
    --destination installer-data="$metadata_installer_data"; then
    metadata_started=$SECONDS
    python3 /builder/verify-asahi-os-package.py "$package" "$product" \
      >"$metadata_evidence"
    jq '.metadata' "$metadata_evidence" >"$metadata_installer_data"
    store_stage installer-metadata "$metadata_identity" \
      "$((SECONDS - metadata_started))" \
      --output package-evidence="$metadata_evidence" \
      --output installer-data="$metadata_installer_data"
  fi

  cp -- "$metadata_evidence" "$package.asahi-package-evidence.json"
  cp -- "$metadata_installer_data" "$package.installer-data.json"
  tmp_evidence=$work/evidence.json
  jq \
    --slurpfile installed_contents "$finalized_directory/installed-contents.json" \
    --slurpfile configured_contract "$configured_installed_contract" \
    --arg iso_source "${OMARCHY_ISO_SOURCE_IDENTITY:-${OMARCHY_ISO_SOURCE_COMMIT:-unknown}}" \
    --arg archiso_source "${OMARCHY_ARCHISO_SOURCE_COMMIT:-unknown}" \
    --arg platform_snapshot_sha256 \
      "$(sha256sum /builder/apple-platform-snapshot.json | cut -d' ' -f1)" \
    --arg uefi_filename "$uefi_filename" --arg uefi_sha256 "$uefi_sha256" \
    --argjson uefi_size "$uefi_size" \
    '. + {installed_contents: $installed_contents[0],
      configured_contract: $configured_contract[0],
      source: {omarchy_iso_identity: $iso_source, archiso_commit: $archiso_source,
        platform_snapshot_sha256: $platform_snapshot_sha256},
      uefi_payload: {filename: $uefi_filename, size_bytes: $uefi_size,
        sha256: $uefi_sha256}}' \
    "$package.asahi-package-evidence.json" >"$tmp_evidence"
  mv "$tmp_evidence" "$package.asahi-package-evidence.json"
}
