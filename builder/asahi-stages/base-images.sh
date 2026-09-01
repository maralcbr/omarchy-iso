#!/bin/bash

run_base_images_stage() {
  base_directory=$work/base-images
  base_identity=$work/base-images.identity.json
  mkdir -p "$base_directory"
  create_stage_identity base-images "$base_identity" \
    --input offline-repository-identity="$OMARCHY_OFFLINE_REPOSITORY_IDENTITY" \
    --input image-product="$image_product" \
    --input uefi-payload="$uefi_payload"
  admit_stage_identity base-images "$base_identity"
  if restore_stage base-images "$base_identity" \
    --destination root-image="$base_directory/root.img" \
    --destination boot-image="$base_directory/boot.img" \
    --destination esp-image="$base_directory/esp-build.img"; then
    return
  fi

  base_started=$SECONDS
  truncate -s "$root_size" "$base_directory/root.img"
  truncate -s "$boot_size" "$base_directory/boot.img"
  truncate -s "$esp_size" "$base_directory/esp-build.img"
  mkfs.btrfs -q -f -L OMARCHY_ROOT -U "$root_uuid" "$base_directory/root.img"
  # The checkpoint store refuses a rebuilt output whose bytes differ from an
  # existing same-identity checkpoint, so image creation must be
  # byte-deterministic: pin the ext4 superblock timestamps and hash seed —
  # the only entropy mkfs.ext4 adds once the UUID is fixed.
  E2FSPROGS_FAKE_TIME=${SOURCE_DATE_EPOCH:?deterministic base images require SOURCE_DATE_EPOCH} \
    mkfs.ext4 -q -F -L OMARCHY_BOOT -U "$boot_uuid" \
    -E lazy_itable_init=0,lazy_journal_init=0,hash_seed="$boot_uuid" \
    "$base_directory/boot.img"
  mkfs.vfat -F 32 -n OMARCHYESP -i "${esp_volume_id#0x}" \
    "$base_directory/esp-build.img" >/dev/null
  base_root_loop=$(losetup --find --show "$base_directory/root.img")
  loops=("$base_root_loop")
  subvolume_root=$work/subvolumes
  mkdir -p "$subvolume_root"
  mount "$base_root_loop" "$subvolume_root"
  mounts=("$subvolume_root")
  for subvolume in @ @home @log @pkg; do
    btrfs subvolume create "$subvolume_root/$subvolume" >/dev/null
  done
  umount "$subvolume_root"
  mounts=()
  losetup -d "$base_root_loop"
  loops=()
  uefi_tree=$work/uefi
  mkdir -p "$uefi_tree"
  bsdtar -xf "$uefi_payload" -C "$uefi_tree"
  [[ -s $uefi_tree/esp/m1n1/boot.bin ]] ||
    fail "UEFI payload has no m1n1 stage 2"
  # Populate the ESP with mtools instead of a kernel mount: FAT directory
  # entries carry creation-time fields that mounted-filesystem writes stamp
  # from the wall clock and touch cannot rewrite, so a mounted copy is never
  # byte-reproducible. mtools derives every FAT timestamp field from
  # SOURCE_DATE_EPOCH.
  mmd -i "$base_directory/esp-build.img" ::/m1n1
  mcopy -i "$base_directory/esp-build.img" \
    "$uefi_tree/esp/m1n1/boot.bin" ::/m1n1/boot.bin
  store_stage base-images "$base_identity" "$((SECONDS - base_started))" \
    --output root-image="$base_directory/root.img" \
    --output boot-image="$base_directory/boot.img" \
    --output esp-image="$base_directory/esp-build.img"
}
