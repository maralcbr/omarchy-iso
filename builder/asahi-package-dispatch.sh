#!/bin/bash

# Control-plane dispatch only. Byte transformations live in the configured and
# finalized runtime producers, immutable projector, and package stage modules.

ensure_host_ownership() {
  chown "$@" 2>/dev/null && return 0
  # On a shared host mount the mapping layer owns the host-side identity and
  # refuses in-container chown; the host user already owns these files there.
  # On a real filesystem a refused chown is a genuine failure.
  local fstype
  fstype=$(stat -f -c %T /out 2>/dev/null || echo unknown)
  case "$fstype" in
    virtiofs|9p|fuse|fuseblk|grpcfuse|osxfs|"UNKNOWN (0x6a656a63)")
      # The quoted magic is Docker Desktop's virtiofs, which coreutils stat
      # cannot name; observed as the v7 qualification build's only failure.
      echo "asahi-package-dispatch: ownership delegated to shared-mount mapping ($fstype)" >&2
      return 0
      ;;
  esac
  echo "asahi-package-dispatch: failed to set host ownership: $*" >&2
  return 1
}

run_asahi_package_dispatch() {
  local builder_pacman_config=/var/cache/pacman-offline.builder.conf
  local runtime_projection_parent configured_source_root finalized_source_root
  local repository_view_before repository_view

  /builder/prepare-asahi-pacman-config.sh \
    "$build_cache_dir/pacman-offline.conf" "$builder_pacman_config"
  runtime_projection_parent=$(mktemp -d /var/cache/omarchy-asahi-runtime.XXXXXX)
  configured_source_root=$runtime_projection_parent/configured
  finalized_source_root=$runtime_projection_parent/finalized
  PYTHONDONTWRITEBYTECODE=1 python3 /builder/asahi_runtime_projection.py \
    --repository / \
    --runtime-root "$build_cache_dir/airootfs/usr/share/omarchy-iso" \
    --spec /builder/asahi-stage-inputs.json \
    --stage configured-target \
    --output-root "$configured_source_root"
  PYTHONDONTWRITEBYTECODE=1 python3 /builder/asahi_runtime_projection.py \
    --repository / \
    --runtime-root "$build_cache_dir/airootfs/usr/share/omarchy-iso" \
    --spec /builder/asahi-stage-inputs.json \
    --stage finalized-boot \
    --output-root "$finalized_source_root"
  repository_view_before=$asahi_run_evidence/offline-repository-install-view.before-readonly.json
  repository_view=$asahi_run_evidence/offline-repository-install-view.json
  python3 /builder/verify-asahi-offline-repository-view.py \
    --mirror "$offline_mirror_dir" \
    --repository-manifest "$OMARCHY_OFFLINE_REPOSITORY_MANIFEST" \
    --database-run-manifest "$asahi_run_evidence/offline-repository-database.json" \
    --output "$repository_view_before"
  mount --bind "$offline_mirror_dir" "$offline_mirror_dir"
  mount -o remount,bind,ro "$offline_mirror_dir"
  python3 /builder/verify-asahi-offline-repository-view.py \
    --mirror "$offline_mirror_dir" \
    --repository-manifest "$OMARCHY_OFFLINE_REPOSITORY_MANIFEST" \
    --database-run-manifest "$asahi_run_evidence/offline-repository-database.json" \
    --output "$repository_view"
  OMARCHY_ASAHI_PACMAN_CONFIG="$builder_pacman_config" \
    OMARCHY_ASAHI_OFFLINE_REPOSITORY_VIEW="$repository_view" \
    OMARCHY_ASAHI_CONFIGURED_SOURCE_ROOT="$configured_source_root" \
    OMARCHY_ASAHI_FINALIZED_SOURCE_ROOT="$finalized_source_root" \
    bash /builder/build-asahi-os-package.sh
  if [[ -n ${HOST_UID:-} && -n ${HOST_GID:-} ]]; then
    local publication_manifest package_filename
    publication_manifest=$asahi_run_evidence/release-publication.json
    ensure_host_ownership -R "$HOST_UID:$HOST_GID" "$asahi_run_evidence"
    if [[ -f $publication_manifest ]] &&
      jq -e '.result == "passed" and
        (.reproducibility_match | type == "boolean")' \
        "$publication_manifest" >/dev/null; then
      package_filename=$(jq -er '.package_filename' "$publication_manifest")
      ensure_host_ownership "$HOST_UID:$HOST_GID" \
        "/out/$package_filename" \
        "/out/$package_filename.asahi-package-evidence.json" \
        "/out/$package_filename.installer-data.json"
    fi
  fi
}
