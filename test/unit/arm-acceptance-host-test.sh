#!/bin/bash

set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
harness="$ROOT/bin/omarchy-iso-test"

bash -n "$harness"

grep -Fq -- '--cpus) CPUS="$2"' "$harness"
grep -Fq -- '-cpu host -machine virt,accel=hvf' "$harness"
grep -Fq -- '-smp "$CPUS"' "$harness"
grep -Fq -- 'edk2-aarch64-code.fd' "$harness"
grep -Fq -- 'edk2-arm-vars.fd' "$harness"
grep -Fq -- '-device qemu-xhci,id=xhci' "$harness"
grep -Fq -- '-device usb-kbd,bus=xhci.0' "$harness"
grep -Fq -- '-device virtio-scsi-pci,id=scsi0' "$harness"
grep -Fq -- '-device scsi-cd,drive=cdrom0,bus=scsi0.0,bootindex=2' "$harness"
grep -Fq -- 'mktemp -u "${TMPDIR:-/tmp}/omarchy-iso-test-qmp.XXXXXX").sock' "$harness"
grep -Fq -- 'mktemp -u "${TMPDIR:-/tmp}/omarchy-iso-test-serial.XXXXXX").sock' "$harness"
grep -Fq -- '-serial chardev:serial0' "$harness"
grep -Fq -- 'type_serial_line "$GUEST_PASSWORD"' "$harness"
! grep -Fq -- '${ch,,}' "$harness"
! grep -Fq -- '${text,,}' "$harness"
grep -Fq -- 'failure-installer-full-log' "$harness"

echo "ARM acceptance host tests passed"
