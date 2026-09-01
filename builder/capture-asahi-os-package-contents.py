#!/usr/bin/env python3
"""Capture fail-closed evidence from mounted Asahi full-OS images."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


class ContentEvidenceError(RuntimeError):
    pass


ARTIFACTS = {
    "esp_m1n1": "boot/efi/m1n1/boot.bin",
    "esp_bootaa64": "boot/efi/EFI/BOOT/BOOTAA64.EFI",
    "boot_kernel": "boot/vmlinuz-linux-asahi",
    "boot_initramfs": "boot/initramfs-linux-asahi.img",
    "boot_grub_config": "boot/grub/grub.cfg",
    "root_omarchy": "usr/bin/omarchy",
    "root_provision_owner": "usr/bin/omarchy-provision-owner",
    "root_installed_verify": "usr/bin/omarchy-apple-installed-verify",
    "root_full_os_marker": "usr/share/omarchy/apple-silicon-full-os",
    "root_btrfs_fsck": "usr/bin/fsck.btrfs",
    "root_legacy_apple_probe": (
        "usr/share/omarchy/install/hardware/apple/fix-spi-keyboard.sh"
    ),
    "root_fstab": "etc/fstab",
    "root_locale_conf": "etc/locale.conf",
    "root_locale_archive": "usr/lib/locale/locale-archive",
    "root_provision_service": "etc/systemd/system/omarchy-provision-owner.service",
}


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _target_file(target: Path, relative: str) -> Path:
    logical = target / relative
    try:
        status = logical.lstat()
    except OSError as error:
        raise ContentEvidenceError(
            f"missing installed artifact: {relative}"
        ) from error

    resolved = logical
    if stat.S_ISLNK(status.st_mode):
        destination = Path(os.readlink(logical))
        resolved = (
            target / str(destination).lstrip("/")
            if destination.is_absolute()
            else logical.parent / destination
        )
    try:
        resolved = resolved.resolve(strict=True)
        resolved.relative_to(target.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ContentEvidenceError(
            f"unsafe installed artifact: {relative}"
        ) from error
    if not resolved.is_file() or resolved.stat().st_size <= 0:
        raise ContentEvidenceError(f"empty installed artifact: {relative}")
    return resolved


def _artifact(target: Path, relative: str) -> dict:
    path = _target_file(target, relative)
    return {
        "path": "/" + relative,
        "size_bytes": path.stat().st_size,
        "sha256": _digest(path),
    }


def _installed_root_uuid(fstab: str) -> str:
    for raw_line in fstab.splitlines():
        fields = raw_line.partition("#")[0].split()
        if len(fields) < 2 or fields[1] != "/":
            continue
        if not fields[0].startswith("UUID=") or len(fields[0]) == len("UUID="):
            raise ContentEvidenceError(
                "installed root filesystem is not identified by UUID"
            )
        return fields[0].removeprefix("UUID=")
    raise ContentEvidenceError("installed fstab has no root filesystem")


def _validate_grub_root_contract(grub: str, root_uuid: str) -> dict:
    linux_entries = []
    for raw_line in grub.splitlines():
        fields = raw_line.partition("#")[0].split()
        if fields and fields[0] == "linux":
            linux_entries.append(fields)
    if not linux_entries:
        raise ContentEvidenceError("GRUB configuration has no Linux entry")

    expected_selector = f"root=UUID={root_uuid}"
    for fields in linux_entries:
        root_selectors = [field for field in fields if field.startswith("root=")]
        if root_selectors != [expected_selector]:
            raise ContentEvidenceError(
                "GRUB Linux root selector does not match installed root UUID"
            )
        root_flags = [field for field in fields if field.startswith("rootflags=")]
        valid_root_flags = len(root_flags) == 1 and "subvol=@" in (
            root_flags[0].split("=", 1)[1].split(",")
        )
        if not valid_root_flags:
            raise ContentEvidenceError(
                "GRUB Linux root flags do not select the Omarchy root subvolume"
            )

    return {
        "linux_entries": len(linux_entries),
        "root_selector": f"UUID={root_uuid}",
        "root_subvolume": "@",
    }


def _expected_node(identity: dict) -> dict:
    if (
        set(identity)
        != {
            "schema_version",
            "verification_kind",
            "filename",
            "sha256",
            "size_bytes",
        }
        or identity.get("schema_version") != 1
        or identity.get("verification_kind") != "pinned-node-lock-v1"
        or re.fullmatch(
            r"node-v[0-9][A-Za-z0-9._-]*-linux-arm64\.tar\.gz",
            identity.get("filename", ""),
        )
        is None
        or re.fullmatch(r"[0-9a-f]{64}", identity.get("sha256", "")) is None
        or not isinstance(identity.get("size_bytes"), int)
        or identity["size_bytes"] <= 0
    ):
        raise ContentEvidenceError("pinned Node lock projection is invalid")
    return {
        "filename": identity["filename"],
        "sha256": identity["sha256"],
        "size_bytes": identity["size_bytes"],
    }


def capture(target: Path, node_identity: dict) -> dict:
    try:
        status = target.lstat()
    except OSError as error:
        raise ContentEvidenceError("target root is unavailable") from error
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise ContentEvidenceError("target root is unsafe")
    detector_backup = (
        target
        / "usr/bin/omarchy-hw-apple-silicon.omarchy-image-original"
    )
    if detector_backup.exists() or detector_backup.is_symlink():
        raise ContentEvidenceError("temporary platform detector was not restored")

    artifacts = {
        name: _artifact(target, relative)
        for name, relative in ARTIFACTS.items()
    }
    grub = _target_file(target, ARTIFACTS["boot_grub_config"]).read_text(
        encoding="utf-8",
        errors="strict",
    )
    for token in (
        "Omarchy",
        "vmlinuz-linux-asahi",
        "initramfs-linux-asahi.img",
    ):
        if token not in grub:
            raise ContentEvidenceError(f"GRUB configuration is missing {token}")
    fstab = _target_file(target, ARTIFACTS["root_fstab"]).read_text(
        encoding="utf-8",
        errors="strict",
    )
    boot_contract = _validate_grub_root_contract(
        grub,
        _installed_root_uuid(fstab),
    )

    legacy_probe = _target_file(
        target,
        ARTIFACTS["root_legacy_apple_probe"],
    ).read_text(encoding="utf-8", errors="strict")
    if (
        'product_name="$(cat /sys/class/dmi/id/product_name '
        '2>/dev/null || true)"'
        not in legacy_probe
    ):
        raise ContentEvidenceError("legacy Apple DMI probe is not fail-safe")

    locale = _target_file(
        target,
        ARTIFACTS["root_locale_conf"],
    ).read_text(encoding="utf-8", errors="strict")
    if locale != "LANG=en_US.UTF-8\n":
        raise ContentEvidenceError("installed locale configuration changed")

    full_os_marker = _target_file(
        target,
        ARTIFACTS["root_full_os_marker"],
    ).read_text(encoding="utf-8", errors="strict")
    if full_os_marker != (
        "schema_version=1\n"
        "product_id=omarchy-mx-mac\n"
        "mode=installed-full-os\n"
    ):
        raise ContentEvidenceError("installed full-OS marker changed")

    module_roots = []
    for pkgbase in sorted((target / "usr/lib/modules").glob("*/pkgbase")):
        if pkgbase.read_text(encoding="utf-8").strip() != "linux-asahi":
            continue
        module_root = pkgbase.parent
        if not (module_root / "vmlinuz").is_file():
            raise ContentEvidenceError("linux-asahi module tree has no kernel")
        module_roots.append(module_root)
    if len(module_roots) != 1:
        raise ContentEvidenceError("expected exactly one linux-asahi module tree")

    pending = target / "var/lib/omarchy/provisioning/pending"
    if not pending.is_file() or pending.is_symlink():
        raise ContentEvidenceError("deferred provisioning is not pending")
    expected_node = _expected_node(node_identity)
    node_root = target / "var/lib/omarchy/provisioning/packages"
    if not node_root.is_dir() or node_root.is_symlink():
        raise ContentEvidenceError(
            "deferred provisioning Node archive inventory is unsafe"
        )
    node_archives = sorted(node_root.iterdir(), key=lambda path: path.name)
    if (
        [path.name for path in node_archives] != [expected_node["filename"]]
        or not node_archives[0].is_file()
        or node_archives[0].is_symlink()
        or node_archives[0].stat().st_size <= 0
    ):
        raise ContentEvidenceError(
            "deferred provisioning Node archive inventory is not exact"
        )
    actual_node = {
        "filename": node_archives[0].name,
        "sha256": _digest(node_archives[0]),
        "size_bytes": node_archives[0].stat().st_size,
    }
    if actual_node != expected_node:
        raise ContentEvidenceError(
            "installed Node archive differs from the pinned lock"
        )
    service_link = (
        target
        / "etc/systemd/system/multi-user.target.wants/omarchy-provision-owner.service"
    )
    if (
        not service_link.is_symlink()
        or os.readlink(service_link)
        != "/etc/systemd/system/omarchy-provision-owner.service"
    ):
        raise ContentEvidenceError("deferred provisioning service is not enabled")

    package_count = sum(
        1
        for description in (target / "var/lib/pacman/local").glob("*/desc")
        if description.is_file() and not description.is_symlink()
    )
    if package_count <= 0:
        raise ContentEvidenceError("installed package database is empty")

    return {
        "schema_version": 1,
        "content_kind": "asahi-full-os-images",
        "artifacts": artifacts,
        "kernel": {
            "package": "linux-asahi",
            "module_release": module_roots[0].name,
        },
        "boot_contract": boot_contract,
        "package_database": {"installed_packages": package_count},
        "provisioning": {
            "pending": True,
            "service_enabled": True,
            "node_archive": actual_node["filename"],
            "node_archive_size_bytes": actual_node["size_bytes"],
            "node_archive_sha256": actual_node["sha256"],
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "Usage: capture-asahi-os-package-contents.py <mounted-target> "
            "<node-identity.json>",
            file=sys.stderr,
        )
        return 2
    try:
        node_identity_path = Path(argv[2])
        if not node_identity_path.is_file() or node_identity_path.is_symlink():
            raise ContentEvidenceError("pinned Node lock projection is missing or unsafe")
        node_identity = json.loads(node_identity_path.read_text())
        if not isinstance(node_identity, dict):
            raise ContentEvidenceError("pinned Node lock projection is invalid")
        evidence = capture(Path(argv[1]), node_identity)
    except (ContentEvidenceError, OSError, json.JSONDecodeError) as error:
        print(f"capture-asahi-os-package-contents: {error}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
