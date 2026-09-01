#!/bin/bash

run_finalized_boot_stage() {
  finalized_directory=$work/finalized-boot
  finalized_identity=$work/finalized-boot.identity.json
  mkdir -p "$finalized_directory"
  create_stage_identity finalized-boot "$finalized_identity" \
    --input configured-root="$configured_directory/root.img" \
    --input configured-boot="$configured_directory/boot.img" \
    --input configured-esp="$configured_directory/esp-build.img" \
    --input configured-state="$configured_directory/state" \
    --input configured-installed-contract="$configured_installed_contract" \
    --input finalized-runtime="$finalized_runtime_manifest" \
    --input finalized-product="$finalized_product_manifest" \
    --input node-runtime-identity="$node_runtime_identity" \
    --input installed-verifier=/builder/omarchy-apple-installed-verify \
    --input installed-config-verifier=/builder/verify-asahi-installed-system.py \
    --input content-capture=/builder/capture-asahi-os-package-contents.py \
    --input branding-tool=/builder/brand-apple-silicon-boot.py \
    --input branding-manifest=/builder/branding/branding-manifest.json \
    --input branding-logo-48=/builder/branding/bootlogo_48.bin \
    --input branding-logo-128=/builder/branding/bootlogo_128.bin \
    --input branding-logo-256=/builder/branding/bootlogo_256.bin
  admit_stage_identity finalized-boot "$finalized_identity"
  if restore_stage finalized-boot "$finalized_identity" \
    --destination root-image="$finalized_directory/root.img" \
    --destination boot-image="$finalized_directory/boot.img" \
    --destination esp-tree="$finalized_directory/esp" \
    --destination installed-contents="$finalized_directory/installed-contents.json" \
    --destination installed-config-verification="$finalized_directory/installed-config-verification.json"; then
    return
  fi

  finalized_started=$SECONDS
  cp --sparse=always "$configured_directory/root.img" "$finalized_directory/root.img"
  cp --sparse=always "$configured_directory/boot.img" "$finalized_directory/boot.img"
  cp --sparse=always "$configured_directory/esp-build.img" \
    "$finalized_directory/esp-build.img"
  cp -a "$configured_directory/state" "$finalized_directory/state"
  attach_images "$finalized_directory"
  run_orchestrator_stage finalized "$finalized_directory/state" \
    /builder/run-asahi-finalized-stage.py "$finalized_source_root"
  if [[ -e $target/var/log/omarchy-install.log ||
    -L $target/var/log/omarchy-install.log ||
    -e $target/var/log/omarchy-install-timing.json ||
    -L $target/var/log/omarchy-install-timing.json ]]; then
    fail "finalized target retained volatile orchestrator run evidence"
  fi
  install -m 0755 /builder/omarchy-apple-installed-verify \
    "$target/usr/bin/omarchy-apple-installed-verify"
  install -d -m 0755 "$target/usr/share/omarchy"
  printf '%s\n' 'schema_version=1' 'product_id=omarchy-mx-mac' \
    'mode=installed-full-os' >"$target/usr/share/omarchy/apple-silicon-full-os"
  chmod 0644 "$target/usr/share/omarchy/apple-silicon-full-os"
  if [[ -f $target/etc/pacman.conf ]]; then
    sed -i '/^DisableSandbox$/d' "$target/etc/pacman.conf"
  fi
  if [[ -d $target/etc/pacman.conf.d ]]; then
    find "$target/etc/pacman.conf.d" -type f \
      -exec sed -i '/^DisableSandbox$/d' {} +
  fi
  : >"$target/etc/machine-id"
  rm -f "$target/var/lib/systemd/random-seed"
  rm -f "$target/etc/ssh/ssh_host_"*
  python3 /builder/brand-apple-silicon-boot.py patch-m1n1 \
    /builder/branding/branding-manifest.json \
    /builder/branding \
    "$target/boot/efi/m1n1/boot.bin" \
    "$target/boot/efi/m1n1/boot.bin"
  sync
  fstrim "$target" >/dev/null 2>&1 || true
  fstrim "$target/boot" >/dev/null 2>&1 || true
  python3 /builder/capture-asahi-os-package-contents.py \
    "$target" "$node_runtime_identity" \
    >"$finalized_directory/installed-contents.json"
  python3 /builder/verify-asahi-installed-system.py \
    --root-tree "$target" --boot-tree "$target/boot" \
    >"$finalized_directory/installed-config-verification.json"
  mkdir -p "$finalized_directory/esp"
  cp -a "$target/boot/efi/." "$finalized_directory/esp/"
  detach_images
  rm -f "$finalized_directory/esp-build.img"
  rm -rf "$finalized_directory/state"
  if [[ -n ${SOURCE_DATE_EPOCH:-} ]]; then
    find "$finalized_directory/esp" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
    touch -d "@$SOURCE_DATE_EPOCH" \
      "$finalized_directory/boot.img" "$finalized_directory/root.img"
  fi
  store_stage finalized-boot "$finalized_identity" \
    "$((SECONDS - finalized_started))" \
    --output root-image="$finalized_directory/root.img" \
    --output boot-image="$finalized_directory/boot.img" \
    --output esp-tree="$finalized_directory/esp" \
    --output installed-contents="$finalized_directory/installed-contents.json" \
    --output installed-config-verification="$finalized_directory/installed-config-verification.json"
}
