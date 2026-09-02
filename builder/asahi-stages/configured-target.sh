#!/bin/bash

run_configured_target_stage() {
  configured_directory=$work/configured-target
  configured_identity=$work/configured-target.identity.json
  configured_installed_contract=$configured_directory/installed-contract.json
  configured_contract_proof=${OMARCHY_ASAHI_CONFIGURED_CONTRACT_PROOF:-}
  local -a configured_identity_inputs=(
    --input base-root="$base_directory/root.img"
    --input base-boot="$base_directory/boot.img"
    --input base-esp="$base_directory/esp-build.img"
    --input configured-runtime="$configured_runtime_manifest"
    --input configured-product="$configured_product_manifest"
    --input node-runtime="$node_tarball"
    --input node-runtime-identity="$node_runtime_identity"
    --input offline-repository-identity="$OMARCHY_OFFLINE_REPOSITORY_IDENTITY"
    --input offline-repository="$OMARCHY_OFFLINE_REPOSITORY_MANIFEST"
    --input offline-repository-view="$OMARCHY_ASAHI_OFFLINE_REPOSITORY_VIEW"
    --input pacman-config="$pacman_config"
  )
  mkdir -p "$configured_directory"
  if [[ -n $configured_contract_proof ]]; then
    [[ -f $configured_contract_proof && ! -L $configured_contract_proof ]] ||
      fail "configured contract proof is missing or unsafe"
    configured_identity_inputs+=(
      --input configured-contract-proof="$configured_contract_proof"
    )
  fi
  create_stage_identity configured-target "$configured_identity" \
    "${configured_identity_inputs[@]}"
  admit_stage_identity configured-target "$configured_identity"
  if restore_stage configured-target "$configured_identity" \
    --destination root-image="$configured_directory/root.img" \
    --destination boot-image="$configured_directory/boot.img" \
    --destination esp-image="$configured_directory/esp-build.img" \
    --destination stage-state="$configured_directory/state" \
    --destination installed-contract="$configured_installed_contract"; then
    return
  fi

  configured_started=$SECONDS
  cp --sparse=always "$base_directory/root.img" "$configured_directory/root.img"
  cp --sparse=always "$base_directory/boot.img" "$configured_directory/boot.img"
  cp --sparse=always "$base_directory/esp-build.img" \
    "$configured_directory/esp-build.img"
  mkdir -p "$configured_directory/state"
  attach_images "$configured_directory"
  run_orchestrator_stage configured "$configured_directory/state" \
    /builder/run-asahi-configured-stage.py "$configured_source_root"
  if [[ -e $target/var/log/omarchy-install.log ||
    -L $target/var/log/omarchy-install.log ||
    -e $target/var/log/omarchy-install-timing.json ||
    -L $target/var/log/omarchy-install-timing.json ]]; then
    fail "configured target retained volatile orchestrator run evidence"
  fi
  sync
  mount -o remount,ro "$target"
  python3 /builder/capture-asahi-configured-target.py \
    --target "$target" \
    --state-dir "$configured_directory/state" \
    --runtime-root "$configured_source_root" \
    --runtime-manifest "$configured_runtime_manifest" \
    --product-manifest "$configured_product_manifest" \
    --repository-manifest "$OMARCHY_OFFLINE_REPOSITORY_MANIFEST" \
    --installed-state-only \
    --root-device "$root_loop" \
    --boot-device "$boot_loop" \
    --esp-device "$esp_loop" \
    --node-identity "$node_runtime_identity" \
    --output "$configured_installed_contract"
  jq -e '.verification_kind == "configured-target-installed-state-v1" and
    .validation == {result: "passed"}' "$configured_installed_contract" \
    >/dev/null || fail "configured installed-content contract is invalid"
  detach_images
  dig_image_holes "$configured_directory/root.img" \
    "$configured_directory/boot.img" "$configured_directory/esp-build.img"
  store_stage configured-target "$configured_identity" \
    "$((SECONDS - configured_started))" \
    --output root-image="$configured_directory/root.img" \
    --output boot-image="$configured_directory/boot.img" \
    --output esp-image="$configured_directory/esp-build.img" \
    --output stage-state="$configured_directory/state" \
    --output installed-contract="$configured_installed_contract"
}
