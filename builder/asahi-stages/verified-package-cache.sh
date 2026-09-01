#!/bin/bash

source /builder/package-architecture.sh
source /builder/epochrealtime-stage-timing.sh

prepare_verified_package_runtime_manifest() {
  local runtime_root

  runtime_root=$(mktemp -d /tmp/omarchy-verified-package-runtime.XXXXXX)
  [[ -d $runtime_root && ! -L $runtime_root ]] || {
    echo "ERROR: verified package runtime-manifest root is unsafe" >&2
    return 1
  }
  verified_package_runtime_manifest=$runtime_root/runtime-manifest.json
  python3 /builder/asahi_stage_inputs.py runtime-manifest \
    --root "$runtime_root" \
    --spec /builder/asahi-stage-inputs.json \
    --stage verified-package-cache \
    --setting "OMARCHY_ARCH=$OMARCHY_ARCH" \
    --setting "OMARCHY_ARTIFACT_KIND=$OMARCHY_ARTIFACT_KIND" \
    --setting "OMARCHY_ISO_REF=$OMARCHY_ISO_REF" \
    --setting "OMARCHY_MEDIA_TARGET=$OMARCHY_MEDIA_TARGET" \
    --setting "OMARCHY_MIRROR=$OMARCHY_MIRROR" \
    --setting "OMARCHY_NVIM_PACKAGE=$OMARCHY_NVIM_PACKAGE" \
    --setting "OMARCHY_RUNTIME_PACKAGE=$OMARCHY_RUNTIME_PACKAGE" \
    --setting "OMARCHY_SETTINGS_PACKAGE=$OMARCHY_SETTINGS_PACKAGE" \
    --output "$verified_package_runtime_manifest"
  [[ -f $verified_package_runtime_manifest && \
    ! -L $verified_package_runtime_manifest ]] || {
    echo "ERROR: verified package runtime manifest is missing or unsafe" >&2
    return 1
  }
}

# Set up every byte-affecting prerequisite for the package-cache producer. The
# caller has already selected a valid architecture/media contract, but this
# module owns the tool environment, trust roots, and immutable snapshots used
# to select and verify package payloads.
initialize_verified_package_cache_stage() {
  build_cache_dir=/var/cache
  offline_mirror_dir="$build_cache_dir/airootfs/var/cache/omarchy/mirror/offline"
  asahi_build_lock=/builder/asahi-build-lock.json
  asahi_stage_input_root=${OMARCHY_ASAHI_STAGE_INPUT_ROOT:-/omarchy-asahi-stage-inputs}
  mkdir -p "$build_cache_dir" "$offline_mirror_dir"
  if uses_verified_package_checkpoint; then
    start_epochrealtime_timer verified_package_stage_started
    prepare_verified_package_runtime_manifest
  fi

  # Docker Desktop does not expose Landlock inside the disposable privileged
  # builder, so pacman 7's downloader sandbox cannot start there. This wrapper
  # applies only to the build container; emitted systems retain normal pacman
  # sandboxing.
  pacman() {
    /usr/bin/pacman --disable-sandbox "$@"
  }
  export -f pacman

  if [[ ${OMARCHY_ASAHI_TOOLCHAIN_PREPARED:-0} == 1 ]]; then
    [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]] || {
      echo "ERROR: prepared Asahi toolchain used outside the Apple target" >&2
      return 1
    }
    toolchain_source_lock=$asahi_stage_input_root/builder-toolchain/source-lock.json
    [[ -f $toolchain_source_lock && ! -L $toolchain_source_lock ]] || {
      echo "ERROR: prepared Asahi toolchain stage lock is missing" >&2
      return 1
    }
    expected_source_lock_sha256=$(sha256sum "$toolchain_source_lock")
    expected_source_lock_sha256=${expected_source_lock_sha256%% *}
    embedded_source_lock_sha256=$(cat \
      /usr/share/omarchy-asahi-toolchain/source-lock.sha256)
    if [[ $embedded_source_lock_sha256 != "$expected_source_lock_sha256" ]]; then
      legacy_source_lock_sha256=$(sha256sum "$asahi_build_lock")
      legacy_source_lock_sha256=${legacy_source_lock_sha256%% *}
      jq -e \
        --arg embedded "$embedded_source_lock_sha256" \
        --arg target "$expected_source_lock_sha256" \
        --arg legacy "$legacy_source_lock_sha256" \
        --arg image "${OMARCHY_BUILD_IMAGE:-}" \
        '.stage == "builder-toolchain" and .cache_hit == true and
          .validation.result == "passed" and .output.image_id == $image and
          .compatibility.schema_version == 1 and
          .compatibility.reason == "stage-input-granularity-v1" and
          .compatibility.source_lock_sha256 == $embedded and
          .compatibility.source_lock_sha256 == $legacy and
          .compatibility.target_lock_sha256 == $target' \
        "$OMARCHY_ASAHI_TOOLCHAIN_RUN_MANIFEST" >/dev/null || {
        echo "ERROR: prepared Asahi toolchain source lock mismatch" >&2
        return 1
      }
      jq -e --slurpfile projected "$toolchain_source_lock" \
        '.builder == $projected[0].inputs.builder' "$asahi_build_lock" >/dev/null || {
        echo "ERROR: migrated Asahi toolchain lock projection mismatch" >&2
        return 1
      }
    fi
    sha256sum -c /usr/share/omarchy-asahi-toolchain/packages.sha256 >/dev/null || {
      echo "ERROR: prepared Asahi toolchain inventory mismatch" >&2
      return 1
    }
    pacman-key --init
    pacman-key --populate "$DISTRO_KEYRING_NAME"
  else
    pacman-key --init
    pacman --noconfirm -Sy "$DISTRO_KEYRING_PACKAGE"
    pacman-key --populate "$DISTRO_KEYRING_NAME"
    # A cached mutable legacy image can be months behind its mirror. Upgrade it
    # fully before installing host tools so the transaction is never partial.
    pacman --noconfirm -Syu "${BUILD_HOST_PACKAGES[@]}"
  fi

  prepare_verified_package_snapshots_and_trust
}

prepare_verified_package_snapshots_and_trust() {
  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
    /builder/validate-apple-platform-snapshot.sh "$OMARCHY_APPLE_PLATFORM_SNAPSHOT"
  fi

  if [[ $OMARCHY_ARCH == aarch64 ]]; then
    source /builder/arm-package-snapshots.conf
    bash /builder/fetch-arm-package-snapshots.sh "$offline_mirror_dir"
    mapfile -t snapshot_package_names <"$offline_mirror_dir/ARM-PACKAGES"
    if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
      bash /builder/fetch-apple-platform-snapshot.sh "$offline_mirror_dir"
      mapfile -t apple_keyring_names <"$offline_mirror_dir/APPLE-KEYRING"
      (( ${#apple_keyring_names[@]} == 1 ))
      bash /builder/install-apple-platform-keyring.sh \
        "$OMARCHY_APPLE_PLATFORM_SNAPSHOT" \
        "$offline_mirror_dir/${apple_keyring_names[0]}"
      mapfile -t apple_package_names <"$offline_mirror_dir/APPLE-PACKAGES"
      snapshot_package_names+=("${apple_keyring_names[@]}")
      snapshot_package_names+=("${apple_package_names[@]}")
    fi
    snapshot_packages=()
    for snapshot_package_name in "${snapshot_package_names[@]}"; do
      snapshot_packages+=("$offline_mirror_dir/$snapshot_package_name")
    done
    if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
      apple_platform_package_count=$(jq -r '.packages | length' \
        "$OMARCHY_APPLE_PLATFORM_SNAPSHOT")
      (( ${#snapshot_packages[@]} ==
        ARM_REPOSITORY_PACKAGE_COUNT + 7 + apple_platform_package_count ))
    else
      (( ${#snapshot_packages[@]} == ARM_REPOSITORY_PACKAGE_COUNT + 6 ))
    fi
    repo-add "$offline_mirror_dir/arm-snapshots.db.tar.gz" \
      "${snapshot_packages[@]}"
  fi

  # Trust only the target-specific repository keys before package selection.
  if [[ $OMARCHY_ARCH == aarch64 ]]; then
    pacman-key --add /builder/omarchy-arm-repository.asc
    pacman-key --lsign-key C81AC3E2A99556F9B21D5FEA3DD49BC9F8360BDC
    pacman-key --add /builder/omarchy-arm-runtime.asc
    pacman-key --lsign-key "$ARM_RUNTIME_SIGNING_FINGERPRINT"
  else
    pacman-key --add /builder/omarchy.gpg
    pacman-key --lsign-key 40DFB630FF42BCFFB047046CF0134EE680CAC571
  fi
  pacman --config "$PACMAN_ONLINE_CONFIG" --noconfirm -Sy omarchy-keyring
  pacman-key --populate omarchy

  # Local package builds can resolve Omarchy-only build dependencies through
  # the same selected online repository on x86.
  if [[ $OMARCHY_ARCH == x86_64 ]] &&
    ! grep -q '^\[omarchy\]' /etc/pacman.conf; then
    awk '/^\[omarchy\]/,/^$/' "$PACMAN_ONLINE_CONFIG" >>/etc/pacman.conf
  fi
}

prepare_verified_node_payload() {
  local node_destination=$build_cache_dir/airootfs/opt/packages
  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
    [[ -f $asahi_build_lock && ! -L $asahi_build_lock ]] || {
      echo "ERROR: exact Asahi build lock is missing or unsafe" >&2
      return 1
    }
    NODE_FILENAME=$(jq -er '.node.filename' "$asahi_build_lock")
    NODE_DIST_URL=$(jq -er '.node.url' "$asahi_build_lock")
    NODE_SIZE=$(jq -er '.node.size_bytes' "$asahi_build_lock")
    NODE_SHA=$(jq -er '.node.sha256' "$asahi_build_lock")
    node_cache=/var/cache/omarchy/node
    local -a node_owner_arguments=(--allowed-owner 0)
    if [[ ! ${HOST_UID:-} =~ ^[0-9]+$ ]]; then
      echo "ERROR: verified Node cache has no valid host-owner identity" >&2
      return 1
    fi
    if [[ $HOST_UID != 0 ]]; then
      node_owner_arguments+=(--allowed-owner "$HOST_UID")
    fi
    python3 /builder/pinned-node-cache.py validate-cache-root \
      --cache-root "$node_cache" "${node_owner_arguments[@]}"
    python3 /builder/asahi-lifecycle-lease.py ensure-directory \
      --path "$node_destination" \
      --allowed-owner 0

    local node_snapshot_status=0
    python3 /builder/pinned-node-cache.py snapshot \
      --cache-root "$node_cache" \
      --filename "$NODE_FILENAME" \
      --destination-root "$node_destination" \
      --sha256 "$NODE_SHA" \
      --size "$NODE_SIZE" \
      "${node_owner_arguments[@]}" || node_snapshot_status=$?
    if (( node_snapshot_status != 0 )); then
      echo "ERROR: private pinned Node view is missing or stale" >&2
      return "$node_snapshot_status"
    fi
  else
    mkdir -p "$node_destination"
    NODE_DIST_URL=https://nodejs.org/dist/latest
    NODE_SHASUMS=$(curl -fsSL "$NODE_DIST_URL/SHASUMS256.txt")
    NODE_FILENAME=$(printf '%s\n' "$NODE_SHASUMS" |
      grep "linux-$NODE_DIST_ARCH.tar.gz" | awk '{print $2}')
    NODE_SHA=$(printf '%s\n' "$NODE_SHASUMS" |
      grep "linux-$NODE_DIST_ARCH.tar.gz" | awk '{print $1}')
    node_archive=/tmp/$NODE_FILENAME
    curl -fsSL "$NODE_DIST_URL/$NODE_FILENAME" -o "$node_archive"
    printf '%s %s\n' "$NODE_SHA" "$node_archive" | sha256sum -c -
    cp "$node_archive" "$node_destination/"
  fi
}

prepare_verified_package_profile() {
  # Seed only the package inventory from releng. Boot/initramfs/profile mutation
  # remains in architecture.sh and therefore cannot rotate this producer.
  cp /archiso/configs/releng/packages.x86_64 \
    "$build_cache_dir/packages.x86_64"
  rm -f "$build_cache_dir/packages.aarch64"
  prepare_package_profile "$build_cache_dir"
}

# Produce the exact package payload inventory used by the live environment and
# target installer. The caller provides the prepared profile, trust roots, and
# snapshot payloads through the declared stage inputs; this module owns every
# package-selection, download, pruning, and verification decision.
prepare_verified_package_cache() {
  prepare_verified_package_profile

  if [[ -d /omarchy-source && -d /omarchy-pkgs ]]; then
    bash /builder/build-omarchy-packages.sh "$offline_mirror_dir"
    LOCAL_OMARCHY_BUILD=1
  fi

  prepare_verified_node_payload

  # Packages installed into the live ISO environment itself (NOT the target
  # system). The selected settings package is needed so its post-install hook
  # supplies the Plymouth configuration used by the live initramfs.
  printf '%s\n' "${LIVE_PACKAGES[@]}" >>"$build_cache_dir/$PROFILE_PACKAGES"

  # The x86 live ISO boots linux-t2. Stock linux and broadcom-wl are unused
  # there; keep the filter anchored so linux-t2 and linux-firmware survive.
  if [[ $OMARCHY_ARCH == x86_64 ]]; then
    sed -i -E '/^(linux|broadcom-wl)$/d' "$build_cache_dir/packages.x86_64"
  fi

  # Build the offline mirror from either the explicitly mounted source or the
  # package lists embedded in the selected Omarchy runtime package.
  if [[ -d /omarchy-source ]]; then
    base_pkg_lists=(
      "/omarchy-source/install/$TARGET_BASE_PACKAGE_LIST"
      "/omarchy-source/install/$TARGET_OTHER_PACKAGE_LIST"
    )
    setup_form=/omarchy-source/install/provisioning/setup-form.sh
  else
    local bootstrap_cache_dir=/tmp/omarchy-pkg-bootstrap
    local omarchy_pkg
    rm -rf "$bootstrap_cache_dir" /tmp/offlinedb-bootstrap /tmp/omarchy-pkglists
    mkdir -p "$bootstrap_cache_dir" /tmp/offlinedb-bootstrap
    pacman --config "$PACMAN_ONLINE_CONFIG" --noconfirm -Syw "$OMARCHY_RUNTIME_PACKAGE" --cachedir "$bootstrap_cache_dir" --dbpath /tmp/offlinedb-bootstrap >/dev/null
    omarchy_pkg=$(
      find "$bootstrap_cache_dir" -maxdepth 1 -type f -name "$OMARCHY_RUNTIME_PACKAGE-*.pkg.tar.*" ! -name '*.sig' |
        sort | head -1
    )
    if [[ -z $omarchy_pkg ]]; then
      echo "ERROR: downloaded package for $OMARCHY_RUNTIME_PACKAGE not found in $bootstrap_cache_dir" >&2
      return 1
    fi
    mkdir -p /tmp/omarchy-pkglists
    bsdtar -xf "$omarchy_pkg" -C /tmp/omarchy-pkglists "usr/share/omarchy/install/$TARGET_BASE_PACKAGE_LIST" "usr/share/omarchy/install/$TARGET_OTHER_PACKAGE_LIST"
    base_pkg_lists=(
      "/tmp/omarchy-pkglists/usr/share/omarchy/install/$TARGET_BASE_PACKAGE_LIST"
      "/tmp/omarchy-pkglists/usr/share/omarchy/install/$TARGET_OTHER_PACKAGE_LIST"
    )
    # A runtime predating the shared form may omit this member; emit the
    # actionable error below instead of bsdtar's bare missing-member failure.
    bsdtar -xf "$omarchy_pkg" -C /tmp/omarchy-pkglists usr/share/omarchy/install/provisioning/setup-form.sh 2>/dev/null || true
    setup_form=/tmp/omarchy-pkglists/usr/share/omarchy/install/provisioning/setup-form.sh
  fi

  mkdir -p "$build_cache_dir/airootfs/usr/share/omarchy-iso"
  shipped_base_packages="$build_cache_dir/airootfs/usr/share/omarchy-iso/omarchy-base.packages"
  shipped_other_packages="$build_cache_dir/airootfs/usr/share/omarchy-iso/omarchy-other.packages"
  filter_target_packages <"${base_pkg_lists[0]}" >"$shipped_base_packages"
  filter_target_packages <"${base_pkg_lists[1]}" >"$shipped_other_packages"
  if [[ $OMARCHY_ARCH == aarch64 ]] &&
    ! grep -Fxq archlinuxarm-keyring "$shipped_base_packages"; then
    printf '%s\n' archlinuxarm-keyring >>"$shipped_base_packages"
  fi
  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
    printf '%s\n' alsa-ucm-conf-asahi asahi-alarm-keyring asahi-audio asahi-bless asahi-fwextract asahi-scripts grub linux-asahi linux-asahi-headers m1n1 speakersafetyd startup-disk uboot-asahi >>"$shipped_base_packages"
    sort -u -o "$shipped_base_packages" "$shipped_base_packages"
  fi
  base_pkg_lists=("$shipped_base_packages" "$shipped_other_packages")

  if [[ ! -f $setup_form ]]; then
    local remedy
    if [[ -d /omarchy-source ]]; then
      echo "ERROR: the --local-source checkout ships no install/provisioning/setup-form.sh" >&2
      remedy="Update the checkout to a revision carrying the shared setup form."
    else
      echo "ERROR: $OMARCHY_RUNTIME_PACKAGE does not ship install/provisioning/setup-form.sh" >&2
      remedy="Publish a runtime carrying the shared setup form, or build with --local-source against a checkout that has it."
    fi
    echo "       The configurator sources its prompts from that file, so this ISO" >&2
    echo "       would boot into an installer with no questions to ask." >&2
    echo "       $remedy" >&2
    return 1
  fi
  cp "$setup_form" "$build_cache_dir/airootfs/usr/share/omarchy-iso/setup-form.sh"

  declare -a all_packages
  archinstall_package_list=/builder/archinstall.packages
  if [[ $OMARCHY_ARCH == aarch64 ]]; then
    archinstall_package_list=/tmp/archinstall.packages.aarch64
    filter_target_packages </builder/archinstall.packages >"$archinstall_package_list"
  fi
  mapfile -t all_packages < <(
    {
      cat "$build_cache_dir/$PROFILE_PACKAGES"
      grep -hv '^#\|^$' "${base_pkg_lists[@]}"
      grep -hv '^#\|^$' "$archinstall_package_list"
      printf '%s\n' "$OMARCHY_RUNTIME_PACKAGE" "$OMARCHY_SETTINGS_PACKAGE" "$OMARCHY_NVIM_PACKAGE"
    } | sort -u
  )

  if [[ -n ${LOCAL_OMARCHY_BUILD:-} ]]; then
    mapfile -t all_packages < <(
      printf '%s\n' "${all_packages[@]}" |
        grep -Fxv -e "$OMARCHY_RUNTIME_PACKAGE" -e "$OMARCHY_SETTINGS_PACKAGE" -e "$OMARCHY_NVIM_PACKAGE" || true
    )
  fi

  mkdir -p /tmp/offlinedb
  download_offline_packages() {
    pacman --config "$PACMAN_ONLINE_CONFIG" --noconfirm -Syw "${all_packages[@]}" --cachedir "$offline_mirror_dir/" --dbpath /tmp/offlinedb --needed
  }

  # Pacman can delete a cached file when a repository republishes the same
  # filename with another checksum, then fail the transaction. Retry exactly
  # once so the now-missing payload is fetched.
  if ! download_offline_packages; then
    echo "Offline package download failed; retrying after pacman cleaned invalid cached files..." >&2
    download_offline_packages
  fi

  local resolved_package_files
  if ! resolved_package_files=$(
    pacman --config "$PACMAN_ONLINE_CONFIG" --noconfirm --dbpath /tmp/offlinedb -S --print --print-format '%f' "${all_packages[@]}"
  ); then
    echo "ERROR: could not resolve the package files required by the offline mirror" >&2
    return 1
  fi
  mapfile -t required_package_files <<<"$resolved_package_files"
  if [[ $OMARCHY_MEDIA_TARGET == aarch64/apple-silicon ]]; then
    required_package_files+=(
      "${apple_keyring_names[@]}" "${apple_package_names[@]}"
    )
  fi

  if [[ -n ${LOCAL_OMARCHY_BUILD:-} ]]; then
    local local_package_name local_package_file candidate candidate_name
    for local_package_name in "$OMARCHY_RUNTIME_PACKAGE" "$OMARCHY_SETTINGS_PACKAGE" "$OMARCHY_NVIM_PACKAGE"; do
      local_package_file=
      for candidate in "$offline_mirror_dir/$local_package_name-"*.pkg.tar.*; do
        [[ -f $candidate && $candidate != *.sig ]] || continue
        read -r candidate_name _ < <(pacman -Qp "$candidate" 2>/dev/null) ||
          continue
        [[ $candidate_name == "$local_package_name" ]] || continue
        if [[ -n $local_package_file ]]; then
          echo "ERROR: multiple local builds found for $local_package_name" >&2
          return 1
        fi
        local_package_file=${candidate##*/}
      done
      if [[ -z $local_package_file ]]; then
        echo "ERROR: local build not found for $local_package_name" >&2
        return 1
      fi
      required_package_files+=("$local_package_file")
    done
  fi

  requested_package_files=/tmp/asahi-requested-package-files
  printf '%s\n' "${required_package_files[@]}" | LC_ALL=C sort -u >"$requested_package_files"
  bash /builder/prune-offline-mirror.sh "$offline_mirror_dir" <"$requested_package_files"

  if uses_verified_package_checkpoint; then
    source /builder/checkpoint-verified-package-cache.sh
    checkpoint_verified_package_cache
  fi
}
