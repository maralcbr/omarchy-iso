#!/bin/bash

# Portable, fail-closed permission-mode helper for checkpoint admission.
# Callers must source this file.

file_mode() {
  [[ $# -eq 1 ]] || {
    echo "file_mode requires exactly one path" >&2
    return 1
  }
  local path=$1
  local mode operating_system

  [[ $path == /* && -f $path && ! -L $path ]] || {
    echo "File-mode input is missing or unsafe: $path" >&2
    return 1
  }
  operating_system=$(/usr/bin/uname -s) || return 1
  case "$operating_system" in
    Darwin)
      mode=$(/usr/bin/stat -f %Lp "$path") || return 1
      ;;
    Linux)
      mode=$(/usr/bin/stat -c %a -- "$path") || return 1
      ;;
    *)
      echo "Unsupported file-mode platform: $operating_system" >&2
      return 1
      ;;
  esac
  [[ $mode =~ ^[0-7]{3,4}$ ]] || {
    echo "stat returned an invalid permission mode" >&2
    return 1
  }
  printf '%s\n' "$mode"
}
