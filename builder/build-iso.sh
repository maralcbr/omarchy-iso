#!/bin/bash

set -e

OMARCHY_ISO_REF=${OMARCHY_ISO_REF:-quattro}
OMARCHY_MIRROR=${OMARCHY_MIRROR:-stable}
OMARCHY_ARCH=${OMARCHY_ARCH:-x86_64}
source /builder/architecture.sh

if (( OMARCHY_MEDIA_TARGET_READY == 0 )); then
  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon &&
    ${OMARCHY_APPLE_MEDIA_BUILD_PROBE:-0} == 1 ]]; then
    echo "Building an unverified Apple media validation artifact; release use is forbidden." >&2
  else
    echo "The $OMARCHY_MEDIA_TARGET target has no verified disposable build-and-boot evidence" >&2
    exit 1
  fi
fi

source /builder/asahi-stages/verified-package-cache.sh
initialize_verified_package_cache_stage

# ISO-only profile and output work stays outside the full-OS package path.
if [[ $OMARCHY_ARTIFACT_KIND != asahi-os-package ]]; then
  source /builder/archiso-media-output.sh
  prepare_archiso_media_inputs
fi

prepare_verified_package_cache
source /builder/checkpoint-offline-repository-database.sh
produce_offline_repository_database

source /builder/asahi-stages/configured-runtime-inputs.sh
prepare_configured_runtime_inputs
source /builder/asahi-stages/finalized-runtime-inputs.sh
prepare_finalized_runtime_inputs

if [[ $OMARCHY_ARTIFACT_KIND == asahi-os-package ]]; then
  source /builder/asahi-package-dispatch.sh
  run_asahi_package_dispatch
  exit 0
fi

build_archiso_media_output
