#!/bin/bash

# Portable, fail-closed SHA-256 helpers for the macOS host wrapper and the
# pinned Linux builder. Callers must source this file.

_omarchy_sha256_digest() {
  local output=$1
  local digest=${output%% *}

  [[ $digest =~ ^[0-9a-f]{64}$ ]] || {
    echo "SHA-256 utility returned an invalid digest" >&2
    return 1
  }
  printf '%s\n' "$digest"
}

sha256_file() {
  [[ $# -eq 1 ]] || {
    echo "sha256_file requires exactly one path" >&2
    return 1
  }
  local path=$1
  local output

  [[ -f $path && ! -L $path ]] || {
    echo "SHA-256 input is missing or unsafe: $path" >&2
    return 1
  }
  if [[ -x /usr/bin/sha256sum ]]; then
    output=$(/usr/bin/sha256sum -- "$path") || return 1
  elif [[ -x /usr/bin/shasum ]]; then
    output=$(/usr/bin/shasum -a 256 -- "$path") || return 1
  else
    echo "No trusted SHA-256 utility is available" >&2
    return 1
  fi
  _omarchy_sha256_digest "$output"
}

sha256_stdin() {
  [[ $# -eq 0 ]] || {
    echo "sha256_stdin does not accept arguments" >&2
    return 1
  }
  local output

  if [[ -x /usr/bin/sha256sum ]]; then
    output=$(/usr/bin/sha256sum) || return 1
  elif [[ -x /usr/bin/shasum ]]; then
    output=$(/usr/bin/shasum -a 256) || return 1
  else
    echo "No trusted SHA-256 utility is available" >&2
    return 1
  fi
  _omarchy_sha256_digest "$output"
}
