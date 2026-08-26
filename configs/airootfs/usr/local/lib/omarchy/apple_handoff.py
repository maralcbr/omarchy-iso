"""Verify an Apple Silicon install handoff and expose a bounded write extent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass


UINT64_LIMIT = 2**64
MAX_MANIFEST_BYTES = 65_536
MAX_KEY_BYTES = 8_192
MAX_SIGNATURE_BYTES = 512
MAX_MEDIA_TARGET_BYTES = 128
APPLE_MEDIA_TARGET = "aarch64/apple-silicon"
EFI_SYSTEM_PARTITION_TYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
LINUX_FILESYSTEM_PARTITION_TYPE = "0fc63daf-8483-4772-8e79-3d69d8477de4"
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
DEVICE_IDENTIFIER = re.compile(r"^apple,[a-z0-9]+$")
LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")

ROOT_KEYS = {
    "schema_version",
    "sequence",
    "installation_id",
    "device_identifier",
    "plan_digest",
    "layout_digest",
    "disk",
    "install_partition",
    "paired_esp",
    "artifacts",
}
DISK_KEYS = {"gpt_disk_uuid", "size_bytes", "logical_sector_bytes"}
ESP_KEYS = {
    "partition_uuid",
    "partition_type",
    "offset_bytes",
    "length_bytes",
}
ARTIFACT_KEYS = {"engine_digest", "metadata_digest", "payload_digest"}


class HandoffError(ValueError):
    """The handoff cannot safely authorize an installation."""


def require_apple_media_target(raw: bytes | str) -> None:
    if isinstance(raw, bytes):
        try:
            target = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HandoffError("invalid media target marker") from error
    else:
        target = raw
    if target.strip() != APPLE_MEDIA_TARGET:
        raise HandoffError(
            "Apple handoff media target must be aarch64/apple-silicon"
        )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise HandoffError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _load_json(data: bytes | str) -> object:
    def reject_constant(value: str) -> None:
        raise HandoffError(f"non-finite JSON number: {value}")

    return json.loads(
        data,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=reject_constant,
    )


@dataclass(frozen=True)
class Extent:
    offset_bytes: int
    length_bytes: int

    @property
    def end_bytes(self) -> int:
        end = self.offset_bytes + self.length_bytes
        if end >= UINT64_LIMIT:
            raise HandoffError("extent overflows UInt64")
        return end

    def overlaps(self, other: "Extent") -> bool:
        return self.offset_bytes < other.end_bytes and other.offset_bytes < self.end_bytes

    def contains(self, other: "Extent") -> bool:
        return (
            self.offset_bytes <= other.offset_bytes
            and other.end_bytes <= self.end_bytes
        )


@dataclass(frozen=True)
class Partition:
    node: str
    partition_uuid: str
    partition_type: str
    extent: Extent


@dataclass(frozen=True)
class DiskLayout:
    device: str
    gpt_disk_uuid: str
    size_bytes: int
    logical_sector_bytes: int
    partitions: tuple[Partition, ...]


@dataclass(frozen=True)
class VerifiedHandoff:
    installation_id: str
    sequence: int
    device_identifier: str
    plan_digest: str
    disk: DiskLayout
    install_partition: Partition
    paired_esp: Partition

    @property
    def authorized_extent(self) -> Extent:
        return self.install_partition.extent

    def assert_write(self, offset_bytes: int, length_bytes: int) -> None:
        write = _extent(offset_bytes, length_bytes, "write")
        if not self.authorized_extent.contains(write):
            raise HandoffError("proposed write escapes the authorized extent")


def _exact(value: object, keys: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise HandoffError(f"unexpected {name} fields")
    return value


def _uint(value: object, name: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise HandoffError(f"{name} must be an integer")
    minimum = 1 if positive else 0
    if value < minimum or value >= UINT64_LIMIT:
        raise HandoffError(f"{name} is outside UInt64")
    return value


def _extent(offset: object, length: object, name: str) -> Extent:
    value = Extent(
        _uint(offset, f"{name} offset"),
        _uint(length, f"{name} length", positive=True),
    )
    value.end_bytes
    return value


def _uuid(value: object, name: str) -> str:
    if not isinstance(value, str) or not UUID.fullmatch(value):
        raise HandoffError(f"invalid {name}")
    return value


def _sha256(value: object, name: str, *, prefixed: bool) -> str:
    if not isinstance(value, str):
        raise HandoffError(f"invalid {name}")
    candidate = value[7:] if prefixed and value.startswith("sha256:") else value
    if prefixed and not value.startswith("sha256:"):
        raise HandoffError(f"invalid {name}")
    if not LOWER_HEX_64.fullmatch(candidate):
        raise HandoffError(f"invalid {name}")
    return value


def read_regular(path: str | os.PathLike[str], maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise HandoffError("handoff input must be a regular file")
        if status.st_size > maximum_bytes:
            raise HandoffError("handoff input is too large")
        chunks = []
        remaining = status.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise HandoffError("handoff input changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(data) > maximum_bytes:
        raise HandoffError("handoff input is too large")
    return data


def _verify_signature(
    manifest: bytes,
    signature: bytes,
    public_key: bytes,
    trusted_key_sha256: str,
) -> None:
    expected = _sha256(trusted_key_sha256, "trusted key digest", prefixed=True)
    with tempfile.TemporaryDirectory(prefix="omarchy-handoff.") as temporary:
        root = Path(temporary)
        manifest_path = root / "manifest.json"
        signature_path = root / "manifest.sig"
        key_path = root / "handoff-public.pem"
        manifest_path.write_bytes(manifest)
        signature_path.write_bytes(signature)
        key_path.write_bytes(public_key)

        result = subprocess.run(
            [
                "openssl", "pkey", "-pubin", "-in", str(key_path),
                "-outform", "DER",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise HandoffError("invalid handoff public key")
        actual = "sha256:" + hashlib.sha256(result.stdout).hexdigest()
        if actual != expected:
            raise HandoffError("handoff key is not the boot-chain trust anchor")

        result = subprocess.run(
            [
                "openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key_path),
                "-rawin", "-in", str(manifest_path), "-sigfile", str(signature_path),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise HandoffError("invalid handoff signature")


def parse_sfdisk_layout(raw: bytes, disk_size_bytes: int) -> DiskLayout:
    try:
        root = _load_json(raw)
        table = root["partitiontable"]
        sector_size = _uint(table["sectorsize"], "logical sector size", positive=True)
        if table["label"] != "gpt" or table["unit"] != "sectors":
            raise HandoffError("installation disk must use a sector-based GPT")
        partitions = []
        for entry in table["partitions"]:
            partitions.append(
                Partition(
                    node=entry["node"],
                    partition_uuid=_uuid(entry["uuid"].lower(), "partition UUID"),
                    partition_type=_uuid(entry["type"].lower(), "partition type"),
                    extent=_extent(
                        _uint(entry["start"], "partition start") * sector_size,
                        _uint(entry["size"], "partition size", positive=True) * sector_size,
                        "partition",
                    ),
                )
            )
        return DiskLayout(
            device=table["device"],
            gpt_disk_uuid=_uuid(table["id"].lower(), "GPT disk UUID"),
            size_bytes=_uint(disk_size_bytes, "disk size", positive=True),
            logical_sector_bytes=sector_size,
            partitions=tuple(partitions),
        )
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise HandoffError("invalid sfdisk layout") from error


def inspect_disk(device: str) -> DiskLayout:
    if not re.fullmatch(r"/dev/[a-zA-Z0-9._/-]+", device) or ".." in device:
        raise HandoffError("invalid disk device path")
    size = subprocess.run(
        ["blockdev", "--getsize64", device],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout.strip()
    layout = subprocess.run(
        ["sfdisk", "--json", device],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    return parse_sfdisk_layout(layout, int(size))


def read_boot_contract(cmdline: str) -> tuple[str, str, str]:
    names = {
        "omarchy.handoff_key_sha256": None,
        "omarchy.handoff_plan_digest": None,
        "omarchy.handoff_installation_id": None,
    }
    for token in cmdline.split():
        name, separator, value = token.partition("=")
        if name not in names:
            continue
        if not separator or not value or names[name] is not None:
            raise HandoffError(f"invalid or duplicate boot contract field: {name}")
        names[name] = value
    if any(value is None for value in names.values()):
        raise HandoffError("boot chain did not provide the complete handoff contract")
    return (
        _sha256(names["omarchy.handoff_key_sha256"], "boot key digest", prefixed=True),
        _sha256(names["omarchy.handoff_plan_digest"], "boot plan digest", prefixed=False),
        _uuid(
            names["omarchy.handoff_installation_id"],
            "boot installation ID",
        ),
    )


def verify_handoff(
    manifest: bytes,
    signature: bytes,
    public_key: bytes,
    trusted_key_sha256: str,
    expected_plan_digest: str,
    expected_installation_id: str,
    layout: DiskLayout,
) -> VerifiedHandoff:
    _verify_signature(manifest, signature, public_key, trusted_key_sha256)
    expected_plan = _sha256(expected_plan_digest, "expected plan digest", prefixed=False)
    try:
        root = _exact(_load_json(manifest), ROOT_KEYS, "manifest")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HandoffError("invalid handoff JSON") from error
    if root["schema_version"] != 1:
        raise HandoffError("unsupported handoff schema")
    sequence = _uint(root["sequence"], "handoff sequence", positive=True)
    installation_id = _uuid(root["installation_id"], "installation ID")
    if installation_id != _uuid(expected_installation_id, "expected installation ID"):
        raise HandoffError("handoff does not match the boot-chain installation")
    if (
        not isinstance(root["device_identifier"], str)
        or not DEVICE_IDENTIFIER.fullmatch(root["device_identifier"])
    ):
        raise HandoffError("invalid Apple device identifier")
    plan_digest = _sha256(root["plan_digest"], "plan digest", prefixed=False)
    if plan_digest != expected_plan:
        raise HandoffError("handoff does not match the boot-chain plan")
    _sha256(root["layout_digest"], "layout digest", prefixed=True)

    disk = _exact(root["disk"], DISK_KEYS, "disk")
    if _uuid(disk["gpt_disk_uuid"], "GPT disk UUID") != layout.gpt_disk_uuid:
        raise HandoffError("handoff disk identity changed")
    if _uint(disk["size_bytes"], "disk size", positive=True) != layout.size_bytes:
        raise HandoffError("handoff disk size changed")
    sector_size = _uint(
        disk["logical_sector_bytes"], "logical sector size", positive=True
    )
    if sector_size != layout.logical_sector_bytes:
        raise HandoffError("handoff logical sector size changed")

    install_value = _exact(root["install_partition"], ESP_KEYS, "install partition")
    install_uuid = _uuid(
        install_value["partition_uuid"], "install partition UUID"
    )
    install_type = _uuid(
        install_value["partition_type"], "install partition type"
    )
    if install_type != LINUX_FILESYSTEM_PARTITION_TYPE:
        raise HandoffError("install partition is not a Linux filesystem partition")
    install_extent = _extent(
        install_value["offset_bytes"],
        install_value["length_bytes"],
        "install partition",
    )
    if (
        install_extent.offset_bytes % sector_size
        or install_extent.length_bytes % sector_size
        or install_extent.offset_bytes < 1_048_576
        or install_extent.end_bytes > layout.size_bytes - 1_048_576
    ):
        raise HandoffError("install partition is not safely aligned inside the GPT")
    install_matches = [
        partition for partition in layout.partitions
        if partition.partition_uuid == install_uuid
    ]
    if len(install_matches) != 1:
        raise HandoffError("install partition is missing or ambiguous")
    install_partition = install_matches[0]
    if (
        install_partition.partition_type != install_type
        or install_partition.extent != install_extent
    ):
        raise HandoffError("install partition identity or extent changed")
    for partition in layout.partitions:
        if (
            partition.partition_uuid != install_uuid
            and partition.extent.overlaps(install_extent)
        ):
            raise HandoffError("another partition overlaps the install partition")

    esp_value = _exact(root["paired_esp"], ESP_KEYS, "paired ESP")
    esp_uuid = _uuid(esp_value["partition_uuid"], "paired ESP UUID")
    esp_type = _uuid(esp_value["partition_type"], "paired ESP type")
    if esp_type != EFI_SYSTEM_PARTITION_TYPE:
        raise HandoffError("paired partition is not an EFI System Partition")
    esp_extent = _extent(
        esp_value["offset_bytes"], esp_value["length_bytes"], "paired ESP"
    )
    if (
        esp_extent.offset_bytes % sector_size
        or esp_extent.length_bytes % sector_size
        or esp_extent.offset_bytes < 1_048_576
        or esp_extent.end_bytes > layout.size_bytes - 1_048_576
    ):
        raise HandoffError("paired ESP is not safely aligned inside the GPT")
    matches = [
        partition for partition in layout.partitions
        if partition.partition_uuid == esp_uuid
    ]
    if len(matches) != 1:
        raise HandoffError("paired ESP is missing or ambiguous")
    paired_esp = matches[0]
    if paired_esp.partition_type != esp_type or paired_esp.extent != esp_extent:
        raise HandoffError("paired ESP identity or extent changed")

    artifacts = _exact(root["artifacts"], ARTIFACT_KEYS, "artifacts")
    for name, value in artifacts.items():
        _sha256(value, name, prefixed=True)

    return VerifiedHandoff(
        installation_id=installation_id,
        sequence=sequence,
        device_identifier=root["device_identifier"],
        plan_digest=plan_digest,
        disk=layout,
        install_partition=install_partition,
        paired_esp=paired_esp,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify an Apple Silicon installation handoff"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--disk", required=True)
    args = parser.parse_args()

    require_apple_media_target(read_regular(
        "/usr/share/omarchy-iso/media-target",
        MAX_MEDIA_TARGET_BYTES,
    ))
    trusted_key, expected_plan, expected_installation = read_boot_contract(
        Path("/proc/cmdline").read_text(encoding="utf-8")
    )
    verified = verify_handoff(
        read_regular(args.manifest, MAX_MANIFEST_BYTES),
        read_regular(args.signature, MAX_SIGNATURE_BYTES),
        read_regular(args.public_key, MAX_KEY_BYTES),
        trusted_key,
        expected_plan,
        expected_installation,
        inspect_disk(args.disk),
    )
    print(json.dumps({
        "schema_version": 1,
        "installation_id": verified.installation_id,
        "plan_digest": verified.plan_digest,
        "disk": verified.disk.device,
        "install_partition_uuid": verified.install_partition.partition_uuid,
        "authorized_offset_bytes": verified.authorized_extent.offset_bytes,
        "authorized_length_bytes": verified.authorized_extent.length_bytes,
        "paired_esp_partition_uuid": verified.paired_esp.partition_uuid,
    }, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
