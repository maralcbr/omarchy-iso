#!/usr/bin/env python3
"""Verify an Omarchy full-OS package for the pinned Asahi installer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import sys
import zipfile


CHUNK_SIZE = 1024 * 1024
REQUIRED_MEMBERS = {
    "boot.img",
    "root.img",
    "esp/EFI/BOOT/BOOTAA64.EFI",
    "esp/m1n1/boot.bin",
}
PRODUCT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,63}$")
VOLUME_ID_PATTERN = re.compile(r"^0x[0-9a-fA-F]{8}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PackageVerificationError(RuntimeError):
    pass


def load_product(path: Path) -> dict:
    try:
        product = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PackageVerificationError("invalid product configuration") from error

    required = {
        "schema_version",
        "product_id",
        "name",
        "default_os_name",
        "package_filename",
        "esp_size_bytes",
        "esp_volume_id",
        "boot_size_bytes",
        "root_size_bytes",
        "boot_backend",
        "branding",
        "supported_fw",
    }
    if not isinstance(product, dict) or not required.issubset(product):
        raise PackageVerificationError("incomplete product configuration")
    if product["schema_version"] != 1:
        raise PackageVerificationError("unsupported product schema")
    if PRODUCT_ID_PATTERN.fullmatch(product["product_id"]) is None:
        raise PackageVerificationError("invalid product identifier")
    for key in ("name", "default_os_name"):
        if not isinstance(product[key], str) or not product[key].strip():
            raise PackageVerificationError(f"invalid product {key}")
    package_filename = product["package_filename"]
    if (
        not isinstance(package_filename, str)
        or Path(package_filename).name != package_filename
        or not package_filename.endswith(".zip")
    ):
        raise PackageVerificationError("invalid package filename")
    for key in ("esp_size_bytes", "boot_size_bytes", "root_size_bytes"):
        value = product[key]
        if not isinstance(value, int) or value <= 0 or value % 4096 != 0:
            raise PackageVerificationError(f"invalid product {key}")
    if VOLUME_ID_PATTERN.fullmatch(product["esp_volume_id"]) is None:
        raise PackageVerificationError("invalid ESP volume identifier")
    if product["boot_backend"] != "asahi-grub":
        raise PackageVerificationError("unsupported Apple Silicon boot backend")
    branding = product["branding"]
    if not isinstance(branding, dict) or set(branding) != {
        "m1n1_boot_sha256",
        "volume_icon_member",
        "volume_icon_sha256",
        "volume_icon_size_bytes",
    }:
        raise PackageVerificationError("invalid product branding contract")
    if (
        branding["volume_icon_member"] != "omarchy-volume.icns"
        or DIGEST_PATTERN.fullmatch(branding["volume_icon_sha256"]) is None
        or DIGEST_PATTERN.fullmatch(branding["m1n1_boot_sha256"]) is None
        or not isinstance(branding["volume_icon_size_bytes"], int)
        or isinstance(branding["volume_icon_size_bytes"], bool)
        or branding["volume_icon_size_bytes"] <= 0
        or branding["volume_icon_size_bytes"] > 16 * 1024 * 1024
    ):
        raise PackageVerificationError("invalid product branding contract")
    supported_fw = product["supported_fw"]
    if (
        not isinstance(supported_fw, list)
        or not supported_fw
        or not all(isinstance(value, str) and value for value in supported_fw)
    ):
        raise PackageVerificationError("invalid supported firmware list")
    return product


def safe_members(
    archive: zipfile.ZipFile,
    volume_icon_member: str,
) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        file_type = (info.external_attr >> 16) & 0o170000
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or file_type == 0o120000
        ):
            raise PackageVerificationError(f"unsafe package member: {name}")
        if info.is_dir():
            continue
        if file_type not in {0, 0o100000}:
            raise PackageVerificationError(f"unsafe package member type: {name}")
        if name in members:
            raise PackageVerificationError(f"duplicate package member: {name}")
        if (
            name not in ("boot.img", "root.img", volume_icon_member)
            and not name.startswith("esp/")
        ):
            raise PackageVerificationError(f"unexpected package member: {name}")
        members[name] = info
    missing = sorted((REQUIRED_MEMBERS | {volume_icon_member}) - set(members))
    if missing:
        raise PackageVerificationError(
            "missing package members: " + ",".join(missing)
        )
    return members


def digest_stream(stream) -> str:
    hasher = hashlib.sha256()
    while chunk := stream.read(CHUNK_SIZE):
        hasher.update(chunk)
    return hasher.hexdigest()


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return digest_stream(stream)


def inspect_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    prefix_bytes: int = 0,
) -> tuple[str, bytes, int]:
    """Stream one member exactly once; a complete read enforces its ZIP CRC."""

    hasher = hashlib.sha256()
    prefix = bytearray()
    total = 0
    with archive.open(info) as stream:
        while chunk := stream.read(CHUNK_SIZE):
            total += len(chunk)
            hasher.update(chunk)
            if len(prefix) < prefix_bytes:
                prefix.extend(chunk[: prefix_bytes - len(prefix)])
    if total != info.file_size:
        raise PackageVerificationError(f"package member size changed while reading: {info.filename}")
    return hasher.hexdigest(), bytes(prefix), total


def pe_machine(data: bytes) -> str:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise PackageVerificationError("BOOTAA64.EFI is not a PE image")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 6 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise PackageVerificationError("BOOTAA64.EFI has no PE header")
    machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
    if machine != 0xAA64:
        raise PackageVerificationError("BOOTAA64.EFI is not AArch64")
    return "aarch64"


def metadata(product: dict) -> dict:
    return {
        "os_list": [
            {
                "name": product["name"],
                "default_os_name": product["default_os_name"],
                "omarchy_target": "apple-silicon-full-os",
                "boot_object": "m1n1.bin",
                "next_object": "m1n1/boot.bin",
                "package": product["package_filename"],
                "icon": product["branding"]["volume_icon_member"],
                "supported_fw": product["supported_fw"],
                "partitions": [
                    {
                        "name": "EFI",
                        "type": "EFI",
                        "size": f'{product["esp_size_bytes"]}B',
                        "format": "fat",
                        "volume_id": product["esp_volume_id"],
                        "copy_firmware": True,
                        "copy_installer_data": True,
                        "source": "esp",
                    },
                    {
                        "name": "Boot",
                        "type": "Linux",
                        "size": f'{product["boot_size_bytes"]}B',
                        "image": "boot.img",
                    },
                    {
                        "name": "Root",
                        "type": "Linux",
                        "size": f'{product["root_size_bytes"]}B',
                        "expand": True,
                        "image": "root.img",
                    },
                ],
            }
        ]
    }


def verify(package_path: Path, product_path: Path) -> dict:
    product = load_product(product_path)
    if package_path.name != product["package_filename"]:
        raise PackageVerificationError("package filename does not match product")
    package_size = package_path.stat().st_size
    package_sha256 = digest_file(package_path)
    try:
        with zipfile.ZipFile(package_path) as archive:
            branding = product["branding"]
            volume_icon_member = branding["volume_icon_member"]
            members = safe_members(archive, volume_icon_member)
            if members["boot.img"].file_size != product["boot_size_bytes"]:
                raise PackageVerificationError("boot image size does not match product")
            if members["root.img"].file_size != product["root_size_bytes"]:
                raise PackageVerificationError("root image size does not match product")
            if members[volume_icon_member].file_size != branding["volume_icon_size_bytes"]:
                raise PackageVerificationError("volume icon size does not match product")
            esp_size = sum(
                info.file_size for name, info in members.items() if name.startswith("esp/")
            )
            if esp_size <= 0 or esp_size > product["esp_size_bytes"]:
                raise PackageVerificationError("ESP content exceeds its declared partition")
            declared_content_limit = (
                product["esp_size_bytes"]
                + product["boot_size_bytes"]
                + product["root_size_bytes"]
                + branding["volume_icon_size_bytes"]
            )
            if sum(info.file_size for info in members.values()) > declared_content_limit:
                raise PackageVerificationError("package content exceeds declared partition sizes")

            prefixes = {
                "boot.img": 0x43A,
                "root.img": 0x10048,
                "esp/EFI/BOOT/BOOTAA64.EFI": 1024 * 1024,
            }
            image_digests: dict[str, str] = {}
            member_sizes: dict[str, int] = {}
            captured: dict[str, bytes] = {}
            for name, info in members.items():
                digest, prefix, size = inspect_member(
                    archive,
                    info,
                    prefix_bytes=prefixes.get(name, 0),
                )
                image_digests[name] = digest
                member_sizes[name] = size
                if prefix:
                    captured[name] = prefix

            if image_digests[volume_icon_member] != branding["volume_icon_sha256"]:
                raise PackageVerificationError(
                    "volume icon digest does not match product"
                )
            if image_digests["esp/m1n1/boot.bin"] != branding["m1n1_boot_sha256"]:
                raise PackageVerificationError(
                    "m1n1 boot branding digest does not match product"
                )

            boot_header = captured["boot.img"]
            if len(boot_header) < 0x43A or boot_header[0x438:0x43A] != b"\x53\xef":
                raise PackageVerificationError("boot image is not ext4")
            root_header = captured["root.img"]
            if (
                len(root_header) < 0x10048
                or root_header[0x10040:0x10048] != b"_BHRfS_M"
            ):
                raise PackageVerificationError("root image is not btrfs")
            machine = pe_machine(captured["esp/EFI/BOOT/BOOTAA64.EFI"])
    except (OSError, zipfile.BadZipFile) as error:
        raise PackageVerificationError("invalid package ZIP") from error

    return {
        "schema_version": 1,
        "verification_kind": "asahi-full-os-package",
        "product_id": product["product_id"],
        "package": {
            "filename": package_path.name,
            "size_bytes": package_size,
            "sha256": package_sha256,
        },
        "images": {
            "boot": {
                "filename": "boot.img",
                "size_bytes": product["boot_size_bytes"],
                "sha256": image_digests["boot.img"],
            },
            "root": {
                "filename": "root.img",
                "size_bytes": product["root_size_bytes"],
                "sha256": image_digests["root.img"],
                "expand": True,
            },
        },
        "checks": {
            "member_paths_safe": True,
            "required_members_present": True,
            "boot_filesystem": "ext4",
            "root_filesystem": "btrfs",
            "boot_backend": product["boot_backend"],
            "bootaa64_machine": machine,
            "m1n1_stage2_present": True,
            "branding_bound": True,
            "crc_valid": True,
            "members_streamed_once": True,
            "member_count": len(members),
            "declared_size_bound": True,
        },
        "members": {
            name: {"size_bytes": member_sizes[name], "sha256": image_digests[name]}
            for name in sorted(members)
        },
        "esp": {
            "bootaa64_sha256": image_digests["esp/EFI/BOOT/BOOTAA64.EFI"],
            "m1n1_boot_sha256": image_digests["esp/m1n1/boot.bin"],
            "size_bytes": product["esp_size_bytes"],
            "volume_id": product["esp_volume_id"],
        },
        "branding": {
            "m1n1_boot_sha256": image_digests["esp/m1n1/boot.bin"],
            "volume_icon_member": volume_icon_member,
            "volume_icon_sha256": image_digests[volume_icon_member],
            "volume_icon_size_bytes": member_sizes[volume_icon_member],
        },
        "metadata": metadata(product),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "Usage: verify-asahi-os-package.py <package.zip> <product.json>",
            file=sys.stderr,
        )
        return 2
    try:
        evidence = verify(Path(argv[1]), Path(argv[2]))
    except PackageVerificationError as error:
        print(f"verify-asahi-os-package: {error}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
