#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
source "$root/builder/asahi-stages/image-runtime.sh"
losetup() { printf '/dev/task-image-loop\n'; }
mount() {
  local destination=${!#}
  if [[ $destination == "$work/target/boot/efi" ]]; then
    # An ESP mount hides anything written before mounting. This simulates
    # that boundary and requires attach_images to mark the mounted ESP.
    rm -f "$destination/.builder"
    printf 'mounted\n' > "$destination/mounted-image"
  fi
}
loops=(); mounts=()
attach_images "$work/images"
[[ -f $target/boot/efi/mounted-image ]]
[[ -f $target/boot/efi/.builder && ! -L $target/boot/efi/.builder ]]
[[ ! -e $work/.builder && ! -e $target/boot/.builder ]]
printf 'PASS: attached image ESP is marked after mounting\n'
