#!/bin/bash

# Bash 5's EPOCHREALTIME gives microsecond resolution without spawning a helper
# process whose clock origin could differ. Every consumer fails closed on Apple
# Bash 3.2, missing precision, or a non-positive interval.
epochrealtime_to_microseconds() {
  local value=$1
  local seconds fraction

  if (( BASH_VERSINFO[0] < 5 )); then
    echo "ERROR: stage timing requires Bash 5 or newer" >&2
    return 1
  fi
  if [[ ! $value =~ ^([0-9]+)\.([0-9]{6})$ ]]; then
    echo "ERROR: EPOCHREALTIME is unavailable or malformed" >&2
    return 1
  fi
  seconds=${BASH_REMATCH[1]}
  fraction=${BASH_REMATCH[2]}
  printf '%s\n' "$((10#$seconds * 1000000 + 10#$fraction))"
}

start_epochrealtime_timer() {
  local destination=$1
  local started=${EPOCHREALTIME-}

  [[ $destination =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
    echo "ERROR: unsafe stage timer destination" >&2
    return 1
  }
  epochrealtime_to_microseconds "$started" >/dev/null || return 1
  printf -v "$destination" '%s' "$started"
}

elapsed_epochrealtime_timer() {
  local started=$1
  local completed=${EPOCHREALTIME-}
  local started_microseconds completed_microseconds elapsed_microseconds

  started_microseconds=$(epochrealtime_to_microseconds "$started") || return 1
  completed_microseconds=$(epochrealtime_to_microseconds "$completed") || return 1
  elapsed_microseconds=$((completed_microseconds - started_microseconds))
  if (( elapsed_microseconds <= 0 )); then
    echo "ERROR: stage timer did not advance" >&2
    return 1
  fi
  printf '%d.%06d\n' \
    "$((elapsed_microseconds / 1000000))" \
    "$((elapsed_microseconds % 1000000))"
}
