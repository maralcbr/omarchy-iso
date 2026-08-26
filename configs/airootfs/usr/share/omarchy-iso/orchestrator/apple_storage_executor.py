"""TOCTOU-resistant execution boundary for an Apple storage plan.

Nothing imports this module from the live phase graph yet. It exists so the
future Apple route can hold kernel references to the three verified devices,
recheck their topology and extents, and expose mutation only for the exact
Asahi-created target partition.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import subprocess

from omarchy.apple_handoff import HandoffError
from omarchy.apple_boot_contract import GRUB_FALLBACK_PATH, VerifiedBootFileContract
from .apple_backend import AppleStoragePlan


KERNEL_SECTOR_BYTES = 512


@dataclass(frozen=True)
class KernelDeviceIdentity:
    device_number: int
    sysfs_path: Path
    size_sectors: int
    start_sectors: int | None
    logical_sector_bytes: int


def validate_kernel_identities(plan, disk, target, paired_esp) -> None:
    """Match pinned kernel devices to every signed storage identity."""
    if len({disk.device_number, target.device_number, paired_esp.device_number}) != 3:
        raise HandoffError("disk, target partition, and paired ESP identities overlap")
    if target.sysfs_path.parent != disk.sysfs_path:
        raise HandoffError("target partition is no longer on the signed disk")
    if paired_esp.sysfs_path.parent != disk.sysfs_path:
        raise HandoffError("paired ESP is no longer on the signed disk")
    if disk.logical_sector_bytes != plan._handoff.disk.logical_sector_bytes:
        raise HandoffError("disk logical sector size changed after handoff verification")
    if disk.size_sectors * KERNEL_SECTOR_BYTES != plan._handoff.disk.size_bytes:
        raise HandoffError("disk size changed after handoff verification")
    _validate_partition_extent(target, plan._handoff.install_partition.extent.offset_bytes,
                               plan._handoff.install_partition.extent.length_bytes, "target partition")
    _validate_partition_extent(paired_esp, plan._handoff.paired_esp.extent.offset_bytes,
                               plan._handoff.paired_esp.extent.length_bytes, "paired ESP")


def _validate_partition_extent(identity, expected_offset, expected_length, name) -> None:
    if identity.start_sectors is None:
        raise HandoffError(f"{name} is no longer a partition")
    if identity.start_sectors * KERNEL_SECTOR_BYTES != expected_offset:
        raise HandoffError(f"{name} offset changed after handoff verification")
    if identity.size_sectors * KERNEL_SECTOR_BYTES != expected_length:
        raise HandoffError(f"{name} length changed after handoff verification")


class AppleStorageExecutor:
    """Hold stable device inodes and execute only the target format/mount pair."""

    def __init__(self, plan: AppleStoragePlan, *, sysfs_root=Path("/sys"), runner=subprocess.run):
        self.plan = plan
        self.sysfs_root = sysfs_root
        self.runner = runner
        self._fds = {}

    def __enter__(self):
        flags = os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_PATH", os.O_RDONLY)
        try:
            self._fds = {
                "disk": os.open(self.plan.disk_device, flags),
                "target": os.open(self.plan.target_device, flags),
                "esp": os.open(self.plan.paired_esp_device, flags),
            }
            self.revalidate()
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, _type, _value, _traceback):
        self.close()

    def close(self):
        for descriptor in self._fds.values():
            os.close(descriptor)
        self._fds = {}

    def revalidate(self):
        if set(self._fds) != {"disk", "target", "esp"}:
            raise HandoffError("Apple device lease is not active")
        validate_kernel_identities(
            self.plan,
            self._identity(self._fds["disk"]),
            self._identity(self._fds["target"]),
            self._identity(self._fds["esp"]),
        )

    def format_and_mount_target(self):
        """Format and mount only the pinned target; never expose disk/ESP writes."""
        self.revalidate()
        target = f"/proc/self/fd/{self._fds['target']}"
        self.runner((*self.plan.format_command[:-1], target), check=True,
                    pass_fds=(self._fds["target"],))
        self.revalidate()
        self.runner((*self.plan.mount_command[:-2], target, self.plan.target_mount), check=True,
                    pass_fds=(self._fds["target"],))

    def _identity(self, descriptor):
        status = os.fstat(descriptor)
        if not stat.S_ISBLK(status.st_mode):
            raise HandoffError("Apple storage lease requires block devices")
        device_number = status.st_rdev
        link = self.sysfs_root / "dev/block" / f"{os.major(device_number)}:{os.minor(device_number)}"
        try:
            sysfs_path = link.resolve(strict=True)
            size_sectors = _read_uint(sysfs_path / "size")
            start_path = sysfs_path / "start"
            start_sectors = _read_uint(start_path) if start_path.exists() else None
            queue = sysfs_path / "queue/logical_block_size"
            if not queue.exists():
                queue = sysfs_path.parent / "queue/logical_block_size"
            logical_sector_bytes = _read_uint(queue)
        except (OSError, ValueError) as error:
            raise HandoffError("cannot resolve pinned device identity in sysfs") from error
        return KernelDeviceIdentity(device_number, sysfs_path, size_sectors,
                                    start_sectors, logical_sector_bytes)


def _read_uint(path):
    value = int(path.read_text().strip())
    if value < 0:
        raise ValueError("negative sysfs value")
    return value


def install_authorized_grub(
    esp_root: Path,
    content: bytes,
    contract: VerifiedBootFileContract,
) -> None:
    """Atomically replace only Asahi's GRUB fallback file under a mounted ESP."""
    contract.authorize(GRUB_FALLBACK_PATH, content)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    root_fd = os.open(esp_root, directory_flags)
    descriptors = [root_fd]
    temporary_name = f".BOOTAA64.EFI.omarchy-{contract.installation_id}.new"
    try:
        efi_fd = os.open("EFI", directory_flags, dir_fd=root_fd)
        descriptors.append(efi_fd)
        boot_fd = os.open("BOOT", directory_flags, dir_fd=efi_fd)
        descriptors.append(boot_fd)
        m1n1_fd = os.open("m1n1", directory_flags, dir_fd=root_fd)
        descriptors.append(m1n1_fd)
        preserved_before = _regular_file_identity(m1n1_fd, "boot.bin")

        try:
            destination = os.stat("BOOTAA64.EFI", dir_fd=boot_fd, follow_symlinks=False)
            if not stat.S_ISREG(destination.st_mode):
                raise HandoffError("existing GRUB fallback path is not a regular file")
        except FileNotFoundError:
            pass

        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o644,
            dir_fd=boot_fd,
        )
        try:
            _write_all(temporary_fd, content)
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
        os.replace(
            temporary_name,
            "BOOTAA64.EFI",
            src_dir_fd=boot_fd,
            dst_dir_fd=boot_fd,
        )
        os.fsync(boot_fd)
        preserved_after = _regular_file_identity(m1n1_fd, "boot.bin")
        if preserved_after != preserved_before:
            raise HandoffError("Asahi m1n1 boot object changed during GRUB deployment")
    except Exception:
        try:
            os.unlink(temporary_name, dir_fd=locals().get("boot_fd", root_fd))
        except OSError:
            pass
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _regular_file_identity(directory_fd: int, name: str) -> tuple[int, int, int, str]:
    descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise HandoffError(f"preserved ESP object is not a regular file: {name}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65_536):
            digest.update(chunk)
        return status.st_dev, status.st_ino, status.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    written = 0
    while written < len(content):
        count = os.write(descriptor, content[written:])
        if count <= 0:
            raise HandoffError("short write while deploying GRUB fallback")
        written += count
