#!/bin/bash

asahi_validate_cache_hit_requirement() {
  local requirement=${1:-}

  case "$requirement" in
    ""|configured-target)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

asahi_cache_hit_required() {
  local requirement=${1:-}
  local stage=${2:-}

  [[ $requirement == configured-target ]] || return 1
  [[ $stage == base-images || $stage == configured-target ]]
}
