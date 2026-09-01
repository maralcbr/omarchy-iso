#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
HELPER=$ROOT/builder/ensure-offline-repository-links.sh
WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT

mkdir -p "$WORK/repository"
printf 'database\n' >"$WORK/repository/offline.db.tar.gz"
printf 'files\n' >"$WORK/repository/offline.files.tar.gz"

"$HELPER" "$WORK/repository"
[[ $(readlink "$WORK/repository/offline.db") == offline.db.tar.gz ]]
[[ $(readlink "$WORK/repository/offline.files") == offline.files.tar.gz ]]
echo "ok - missing canonical repository links are created"

"$HELPER" "$WORK/repository"
[[ $(readlink "$WORK/repository/offline.db") == offline.db.tar.gz ]]
[[ $(readlink "$WORK/repository/offline.files") == offline.files.tar.gz ]]
echo "ok - repo-add canonical links are accepted idempotently"

rm "$WORK/repository/offline.db"
ln -s unexpected.db.tar.gz "$WORK/repository/offline.db"
if "$HELPER" "$WORK/repository" >"$WORK/out" 2>"$WORK/error"; then
  echo "mismatched repository link was accepted" >&2
  exit 1
fi
grep -Fq 'repository link mismatch' "$WORK/error"
[[ $(readlink "$WORK/repository/offline.db") == unexpected.db.tar.gz ]]
echo "ok - mismatched repository links fail closed without replacement"

rm "$WORK/repository/offline.db"
printf 'owner data\n' >"$WORK/repository/offline.db"
if "$HELPER" "$WORK/repository" >"$WORK/out" 2>"$WORK/error"; then
  echo "regular repository link path was accepted" >&2
  exit 1
fi
grep -Fq 'repository link path is not a symlink' "$WORK/error"
grep -Fxq 'owner data' "$WORK/repository/offline.db"
echo "ok - regular files fail closed without replacement"

rm "$WORK/repository/offline.db" "$WORK/repository/offline.db.tar.gz"
if "$HELPER" "$WORK/repository" >"$WORK/out" 2>"$WORK/error"; then
  echo "missing repository archive was accepted" >&2
  exit 1
fi
grep -Fq 'repository archive is missing, linked, or not a regular file' "$WORK/error"
echo "ok - missing or unsafe repository archives fail closed"

echo "Asahi offline repository link tests passed"
