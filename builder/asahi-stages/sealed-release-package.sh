#!/bin/bash

# The m1n1 boot image embeds the kernel's device trees, so its byte-exact
# branding pin is per kernel.
branding_manifest=/builder/branding/branding-manifest.json
[[ ${kernel_package:-linux-asahi} != linux-aurora ]] ||
  branding_manifest=/builder/branding/branding-manifest-aurora.json
[[ ${boot_backend:-asahi-grub} != asahi-limine ]] ||
  branding_manifest=/builder/branding/branding-manifest-limine-private.json

run_sealed_release_package_stage() {
  archive_options=$work/archive-options.json
  printf '%s\n' \
    '{"format":"bsdtar-zip","workers":1,"members":["esp","boot.img","root.img","omarchy-volume.icns"]}' \
    >"$archive_options"
  sealed_identity=$work/sealed-release-package.identity.json
  sealed_package=$work/sealed-package.zip
  create_stage_identity sealed-release-package "$sealed_identity" \
    --input finalized-root="$finalized_directory/root.img" \
    --input finalized-boot="$finalized_directory/boot.img" \
    --input finalized-esp="$finalized_directory/esp" \
    --input volume-icon=/builder/branding/omarchy-volume.icns \
    --input branding-manifest="$branding_manifest" \
    --input archive-options="$archive_options"
  if restore_stage sealed-release-package "$sealed_identity" \
    --destination release-package="$sealed_package"; then
    return
  fi

  sealed_started=$SECONDS
  bsdtar --format zip -cf "$sealed_package" -C "$finalized_directory" \
    esp boot.img root.img \
    -C /builder/branding omarchy-volume.icns
  store_stage sealed-release-package "$sealed_identity" \
    "$((SECONDS - sealed_started))" \
    --output release-package="$sealed_package"
}
