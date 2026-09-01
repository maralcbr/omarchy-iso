#!/bin/bash

set -euo pipefail

repository=${1:?Usage: ensure-offline-repository-links.sh REPOSITORY}

for name in offline.db offline.files; do
  target=$name.tar.gz
  archive=$repository/$target
  link=$repository/$name

  if [[ ! -f $archive || -L $archive ]]; then
    echo "ERROR: repository archive is missing, linked, or not a regular file: $archive" >&2
    exit 1
  fi

  if [[ -L $link ]]; then
    if [[ $(readlink "$link") != "$target" ]]; then
      echo "ERROR: repository link mismatch: $link" >&2
      exit 1
    fi
  elif [[ -e $link ]]; then
    echo "ERROR: repository link path is not a symlink: $link" >&2
    exit 1
  else
    ln -s "$target" "$link"
  fi
done
