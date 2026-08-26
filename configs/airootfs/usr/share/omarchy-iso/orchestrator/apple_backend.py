"""Build a fail-closed Apple Silicon install plan from a verified handoff.

This module is intentionally not wired into the live orchestrator yet.  It
describes the only storage preparation the Linux side may perform before a
separate, signed GRUB deployment contract exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from omarchy.apple_handoff import HandoffError, VerifiedHandoff


APPLE_PLATFORM = "apple-silicon"
APPLE_BOOT_BACKEND = "asahi-grub"
BOOT_BLOCKER = "signed path-scoped paired-ESP writer exists but is not wired into the live phase graph"
EXECUTION_BLOCKER = "TOCTOU-safe executor exists but is not wired into the live phase graph"
TARGET_MOUNT = "/mnt"


@dataclass(frozen=True)
class AppleStoragePlan:
    installation_id: str
    plan_digest: str
    disk_device: str
    target_device: str
    paired_esp_device: str
    target_mount: str
    _target_offset: int = field(repr=False, compare=False)
    _handoff: VerifiedHandoff = field(repr=False, compare=False)
    platform: str = APPLE_PLATFORM
    boot_backend: str = APPLE_BOOT_BACKEND
    execution_ready: bool = False
    execution_blocker: str = EXECUTION_BLOCKER
    boot_ready: bool = False
    boot_blocker: str = BOOT_BLOCKER

    @property
    def format_command(self) -> tuple[str, ...]:
        return (
            "mkfs.btrfs",
            "--force",
            "--label",
            "OMARCHY",
            self.target_device,
        )

    @property
    def mount_command(self) -> tuple[str, ...]:
        return (
            "mount",
            "--options",
            "noatime,compress=zstd",
            self.target_device,
            self.target_mount,
        )

    @property
    def paired_esp_mount_options(self) -> tuple[str, ...]:
        return ("ro", "nosuid", "nodev", "noexec")

    def assert_target_write(self, relative_offset: int, length: int) -> None:
        """Authorize a byte range relative to the verified target partition."""
        if relative_offset < 0:
            raise HandoffError("target-relative write offset cannot be negative")
        absolute_offset = self._target_offset + relative_offset
        if absolute_offset >= 2**64:
            raise HandoffError("target-relative write offset overflows UInt64")
        self._handoff.assert_write(absolute_offset, length)


def build_apple_storage_plan(handoff: VerifiedHandoff) -> AppleStoragePlan:
    """Return the non-partitioning Apple storage plan.

    The verified partition node is the sole writable block device.  The disk
    and paired ESP remain identity inputs, never mutation targets.
    """
    target = handoff.install_partition.node
    esp = handoff.paired_esp.node
    disk = handoff.disk.device
    if not all(_is_device_path(value) for value in (disk, target, esp)):
        raise HandoffError("Apple storage plan requires absolute /dev paths")
    if len({disk, target, esp}) != 3:
        raise HandoffError("disk, install partition, and paired ESP must be distinct")

    # Authorize the entire block device up front.  This proves every later
    # target-relative write can be checked against exactly the signed extent.
    handoff.assert_write(
        handoff.install_partition.extent.offset_bytes,
        handoff.install_partition.extent.length_bytes,
    )
    return AppleStoragePlan(
        installation_id=handoff.installation_id,
        plan_digest=handoff.plan_digest,
        disk_device=disk,
        target_device=target,
        paired_esp_device=esp,
        target_mount=TARGET_MOUNT,
        _target_offset=handoff.install_partition.extent.offset_bytes,
        _handoff=handoff,
    )


def _is_device_path(value: str) -> bool:
    path = Path(value)
    return (
        str(path) == value
        and value.startswith("/dev/")
        and ".." not in path.parts
    )
