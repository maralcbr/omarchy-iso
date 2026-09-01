#!/bin/bash

# Materialize only the runtime data consumed while configuring the target.
# The package payload and repository stages have already completed; every file
# written here is subsequently hashed by the configured-target runtime manifest.

# The orchestrator module that decides what a configured install installs is
# the single definition of the target set; this stage only resolves that set
# against the verified offline repository. Reached the same way the runtime
# projector reaches it: the media source tree mounted in the build container.
omarchy_media_source_root=/configs/airootfs/usr/share/omarchy-iso

# A staged Tailscale auth key is an install-time input that only an autoinstall
# drive supplies, so --tailscale-authkey-staged is deliberately never passed
# here: no media build installs the package.
resolve_configured_expected_package_targets() {
  local base_packages=$build_cache_dir/airootfs/usr/share/omarchy-iso/omarchy-base.packages

  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$omarchy_media_source_root" \
    python3 -m orchestrator.target_packages \
    --media-target "$OMARCHY_MEDIA_TARGET" \
    --archinstall-packages "$archinstall_package_list" \
    --base-packages "$base_packages" \
    --runtime-package "$OMARCHY_RUNTIME_PACKAGE" \
    --settings-package "$OMARCHY_SETTINGS_PACKAGE" \
    --nvim-package "$OMARCHY_NVIM_PACKAGE"
}

resolve_configured_expected_package_closure() {
  local output=$1
  local resolve_root raw_records sorted_records expected_targets
  local resolved_package_files filename record
  local -a targets

  resolve_root=$(mktemp -d /tmp/omarchy-expected-packages.XXXXXX) || return 1
  raw_records=$(mktemp "${output}.unsorted.XXXXXX") || {
    rm -rf -- "$resolve_root"
    return 1
  }
  sorted_records=$(mktemp "${output}.sorted.XXXXXX") || {
    rm -rf -- "$resolve_root"
    rm -f -- "$raw_records"
    return 1
  }
  mkdir -p "$resolve_root/var/lib/pacman"
  if ! expected_targets=$(resolve_configured_expected_package_targets) ||
    [[ -z $expected_targets ]]; then
    echo "ERROR: could not determine the configured install's package targets." >&2
    rm -rf -- "$resolve_root"
    rm -f -- "$raw_records" "$sorted_records"
    return 1
  fi
  mapfile -t targets <<<"$expected_targets"

  pacman --config "$build_cache_dir/pacman-offline.conf" \
    --root "$resolve_root" --dbpath "$resolve_root/var/lib/pacman" \
    --noconfirm -Sy >/dev/null || {
    rm -rf -- "$resolve_root"
    rm -f -- "$raw_records" "$sorted_records"
    return 1
  }
  resolved_package_files="$(pacman --config "$build_cache_dir/pacman-offline.conf" \
    --root "$resolve_root" --dbpath "$resolve_root/var/lib/pacman" \
    --noconfirm -S --print --print-format '%f' "${targets[@]}")" || {
    rm -rf -- "$resolve_root"
    rm -f -- "$raw_records" "$sorted_records"
    return 1
  }

  : >"$raw_records"
  while IFS= read -r filename; do
    [[ -n $filename && $filename == "${filename##*/}" ]] || {
      rm -rf -- "$resolve_root"
      rm -f -- "$raw_records" "$sorted_records"
      return 1
    }
    record=$(jq -er --arg filename "$filename" '
      [.resolved_closure[] | select(.filename == $filename)] |
      if length == 1 and (.[0] | keys == ["filename", "name", "version"])
      then .[0] | [.name, .version] | @tsv
      else error("resolved package is not uniquely bound by the verified repository")
      end
    ' "$OMARCHY_OFFLINE_REPOSITORY_MANIFEST") || {
      rm -rf -- "$resolve_root"
      rm -f -- "$raw_records" "$sorted_records"
      return 1
    }
    printf '%s\n' "$record" >>"$raw_records"
  done <<<"$resolved_package_files"

  LC_ALL=C sort -u "$raw_records" >"$sorted_records"
  if [[ $(wc -l <"$raw_records") != $(wc -l <"$sorted_records") ]]; then
    rm -rf -- "$resolve_root"
    rm -f -- "$raw_records" "$sorted_records"
    return 1
  fi
  mv -f -- "$sorted_records" "$output"
  rm -rf -- "$resolve_root"
  rm -f -- "$raw_records"
}

prepare_configured_runtime_inputs() {
  local expected_packages
  local runtime_root="$build_cache_dir/airootfs/usr/share/omarchy-iso"
  local expected_package_closure="$runtime_root/expected-package-closure"

  mkdir -p "$runtime_root" /var/cache/omarchy/mirror
  install -m 0644 /configs/pacman-offline.conf \
    "$build_cache_dir/pacman-offline.conf"
  ln -sfn "$offline_mirror_dir" /var/cache/omarchy/mirror/offline

  cat >"$runtime_root/package-targets" <<EOF
OMARCHY_RUNTIME_PACKAGE=$OMARCHY_RUNTIME_PACKAGE
OMARCHY_SETTINGS_PACKAGE=$OMARCHY_SETTINGS_PACKAGE
OMARCHY_NVIM_PACKAGE=$OMARCHY_NVIM_PACKAGE
EOF
  if [[ ${OMARCHY_INSTALL_DEBUG:-} == 1 ]]; then
    : >"$runtime_root/install-debug"
  else
    rm -f -- "$runtime_root/install-debug"
  fi

  if [[ ! -f ${OMARCHY_OFFLINE_REPOSITORY_MANIFEST:-} ||
    -L ${OMARCHY_OFFLINE_REPOSITORY_MANIFEST:-} ]]; then
    echo "ERROR: verified offline repository manifest is missing or unsafe." >&2
    return 1
  fi
  if ! resolve_configured_expected_package_closure "$expected_package_closure"; then
    echo "ERROR: could not resolve the exact target package closure from the offline mirror." >&2
    echo "       pacman -S --print aborts when any target is absent, so the target" >&2
    echo "       transaction would fail in the same way." >&2
    return 1
  fi
  expected_packages=$(wc -l <"$expected_package_closure")
  if (( expected_packages < 600 || expected_packages > 2000 )); then
    echo "ERROR: resolved target package count is unsafe: $expected_packages" >&2
    return 1
  fi
  printf '%s\n' "$expected_packages" >"$runtime_root/expected-packages"
  echo "Target install resolves to $expected_packages packages."
}
