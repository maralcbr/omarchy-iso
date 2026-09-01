#!/bin/bash

source "${BASH_SOURCE[0]%/*}/package-architecture.sh"

OMARCHY_ARCH=${OMARCHY_ARCH:-x86_64}
OMARCHY_ARTIFACT_KIND=${OMARCHY_ARTIFACT_KIND:-iso}
if [[ -z ${OMARCHY_MEDIA_TARGET:-} ]]; then
  if [[ $OMARCHY_ARCH == "x86_64" ]]; then
    OMARCHY_MEDIA_TARGET=x86_64/pc
  else
    OMARCHY_MEDIA_TARGET=aarch64/generic
  fi
fi

case "$OMARCHY_ARCH:$OMARCHY_MEDIA_TARGET" in
  x86_64:x86_64/pc)
    OMARCHY_PLATFORM=pc
    OMARCHY_BOOT_BACKEND=limine
    [[ $OMARCHY_ARTIFACT_KIND == "iso" ]] || {
      echo "x86_64/pc supports only the iso artifact" >&2
      return 1 2>/dev/null || exit 1
    }
    OMARCHY_MEDIA_TARGET_READY=1
    ;;
  aarch64:aarch64/generic)
    OMARCHY_PLATFORM=generic
    OMARCHY_BOOT_BACKEND=limine
    [[ $OMARCHY_ARTIFACT_KIND == "iso" ]] || {
      echo "aarch64/generic supports only the iso artifact" >&2
      return 1 2>/dev/null || exit 1
    }
    OMARCHY_MEDIA_TARGET_READY=1
    ;;
  aarch64:aarch64/apple-silicon)
    OMARCHY_PLATFORM=apple-silicon
    OMARCHY_BOOT_BACKEND=asahi-grub
    OMARCHY_APPLE_PLATFORM_SNAPSHOT=/builder/apple-platform-snapshot.json
    case "$OMARCHY_ARTIFACT_KIND" in
      iso) OMARCHY_MEDIA_TARGET_READY=0 ;;
      asahi-os-package) OMARCHY_MEDIA_TARGET_READY=1 ;;
      *)
        echo "Unsupported Apple Silicon artifact: $OMARCHY_ARTIFACT_KIND" >&2
        return 1 2>/dev/null || exit 1
        ;;
    esac
    ;;
  *)
    echo "Unsupported architecture/media target: $OMARCHY_ARCH:$OMARCHY_MEDIA_TARGET" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac
export OMARCHY_MEDIA_TARGET OMARCHY_PLATFORM OMARCHY_BOOT_BACKEND
export OMARCHY_ARTIFACT_KIND OMARCHY_MEDIA_TARGET_READY
export OMARCHY_APPLE_PLATFORM_SNAPSHOT

select_omarchy_package_roles
configure_package_architecture

case "$OMARCHY_ARCH" in
  x86_64)
    LIVE_KERNEL=linux-t2
    MKARCHISO=(mkarchiso)
    ;;
  aarch64)
    LIVE_KERNEL=linux-aarch64
    LIVE_KERNEL_BOOT_NAME=Image
    LIVE_INITRAMFS_BOOT_NAME=initramfs-linux.img
    MKARCHISO=(/tmp/omarchy-mkarchiso-aarch64)
    ;;
  *)
    echo "Unsupported OMARCHY_ARCH: $OMARCHY_ARCH" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

if [[ $OMARCHY_MEDIA_TARGET == "aarch64/apple-silicon" ]]; then
  LIVE_KERNEL=linux-asahi
  LIVE_KERNEL_BOOT_NAME=vmlinuz-linux-asahi
  LIVE_INITRAMFS_BOOT_NAME=initramfs-linux-asahi.img
fi

export LIVE_KERNEL LIVE_KERNEL_BOOT_NAME LIVE_INITRAMFS_BOOT_NAME
