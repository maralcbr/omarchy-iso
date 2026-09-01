#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
from typing import Any


MAX_MANIFEST_BYTES = 65_536
MAX_M1N1_BYTES = 16 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BrandingError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise BrandingError(f"{label} digest is invalid")
    if any(character not in "0123456789abcdef" for character in value):
        raise BrandingError(f"{label} digest is invalid")
    return value


def _require_size(value: Any, label: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BrandingError(f"{label} size is invalid")
    if value <= 0 or value > maximum:
        raise BrandingError(f"{label} size is invalid")
    return value


def _require_filename(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BrandingError(f"{label} filename is invalid")
    if value in {".", ".."} or Path(value).name != value:
        raise BrandingError(f"{label} filename is invalid")
    return value


def _read_regular_file(path: Path, label: str, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise BrandingError(f"{label} is missing") from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise BrandingError(f"{label} is unsafe")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        raise BrandingError(f"{label} size is unsafe")
    data = path.read_bytes()
    if len(data) != metadata.st_size:
        raise BrandingError(f"{label} changed while reading")
    return data


def load_manifest(path: Path) -> dict[str, Any]:
    raw = _read_regular_file(path, "branding manifest", MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrandingError("branding manifest is not canonical JSON") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise BrandingError("unsupported branding manifest schema")
    return manifest


def _verify_descriptor(
    descriptor: dict[str, Any],
    path: Path,
    label: str,
    maximum: int,
) -> bytes:
    expected_size = _require_size(descriptor.get("size_bytes"), label, maximum)
    expected_digest = _require_digest(descriptor.get("sha256"), label)
    data = _read_regular_file(path, label, maximum)
    if len(data) != expected_size:
        raise BrandingError(f"{label} size mismatch")
    if _sha256(data) != expected_digest:
        raise BrandingError(f"{label} digest mismatch")
    return data


def _png_dimensions(data: bytes, label: str) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise BrandingError(f"{label} is not a PNG representation")
    return struct.unpack(">II", data[16:24])


def _verify_icns(data: bytes, representations: Any) -> None:
    if len(data) < 8 or data[:4] != b"icns":
        raise BrandingError("volume icon has no ICNS header")
    if struct.unpack(">I", data[4:8])[0] != len(data):
        raise BrandingError("volume icon ICNS length mismatch")
    if not isinstance(representations, list) or not representations:
        raise BrandingError("volume icon representation contract is missing")
    expected = {}
    for item in representations:
        if not isinstance(item, dict):
            raise BrandingError("volume icon representation contract is invalid")
        tag = item.get("tag")
        if not isinstance(tag, str) or len(tag) != 4 or tag in expected:
            raise BrandingError("volume icon representation tag is invalid")
        expected[tag] = item
    actual: dict[str, bytes] = {}
    offset = 8
    while offset < len(data):
        if offset + 8 > len(data):
            raise BrandingError("volume icon chunk header is truncated")
        try:
            tag = data[offset : offset + 4].decode("ascii")
        except UnicodeDecodeError as error:
            raise BrandingError("volume icon chunk tag is invalid") from error
        chunk_size = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        if chunk_size < 8 or offset + chunk_size > len(data):
            raise BrandingError("volume icon chunk length is invalid")
        if tag in actual:
            raise BrandingError("volume icon contains a duplicate representation")
        actual[tag] = data[offset + 8 : offset + chunk_size]
        offset += chunk_size
    if offset != len(data) or set(actual) != set(expected):
        raise BrandingError("volume icon representation set mismatch")
    for tag, payload in actual.items():
        descriptor = expected[tag]
        if _sha256(payload) != _require_digest(
            descriptor.get("sha256"),
            f"volume icon {tag}",
        ):
            raise BrandingError(f"volume icon {tag} digest mismatch")
        width, height = _png_dimensions(payload, f"volume icon {tag}")
        if width != descriptor.get("width") or height != descriptor.get("height"):
            raise BrandingError(f"volume icon {tag} dimensions mismatch")


def verify_assets(manifest: dict[str, Any], asset_directory: Path) -> None:
    source_logo = manifest.get("source_logo")
    volume_icon = manifest.get("volume_icon")
    if not isinstance(source_logo, dict) or not isinstance(volume_icon, dict):
        raise BrandingError("branding asset descriptors are missing")
    logo_filename = _require_filename(source_logo.get("filename"), "source logo")
    icon_filename = _require_filename(volume_icon.get("filename"), "volume icon")
    _verify_descriptor(
        source_logo,
        asset_directory / logo_filename,
        "source logo",
        16 * 1024 * 1024,
    )
    icon = _verify_descriptor(
        volume_icon,
        asset_directory / icon_filename,
        "volume icon",
        16 * 1024 * 1024,
    )
    _verify_icns(icon, volume_icon.get("representations"))
    m1n1 = manifest.get("m1n1")
    if not isinstance(m1n1, dict) or not isinstance(m1n1.get("replacements"), list):
        raise BrandingError("m1n1 branding contract is missing")
    for index, replacement in enumerate(m1n1["replacements"]):
        if not isinstance(replacement, dict) or not isinstance(
            replacement.get("replacement"),
            dict,
        ):
            raise BrandingError("m1n1 replacement contract is invalid")
        descriptor = replacement["replacement"]
        filename = _require_filename(
            descriptor.get("filename"),
            f"m1n1 replacement {index}",
        )
        _verify_descriptor(
            descriptor,
            asset_directory / filename,
            f"m1n1 replacement {index}",
            MAX_M1N1_BYTES,
        )


def patch_m1n1_boot(
    manifest: dict[str, Any],
    asset_directory: Path,
    input_path: Path,
    output_path: Path,
) -> None:
    m1n1 = manifest.get("m1n1")
    if not isinstance(m1n1, dict):
        raise BrandingError("m1n1 branding contract is missing")
    input_descriptor = m1n1.get("input")
    output_descriptor = m1n1.get("output")
    replacements = m1n1.get("replacements")
    if (
        not isinstance(input_descriptor, dict)
        or not isinstance(output_descriptor, dict)
        or not isinstance(replacements, list)
    ):
        raise BrandingError("m1n1 branding contract is invalid")
    source = _read_regular_file(input_path, "m1n1 input", MAX_M1N1_BYTES)
    input_size = _require_size(
        input_descriptor.get("size_bytes"),
        "m1n1 input",
        MAX_M1N1_BYTES,
    )
    if len(source) != input_size:
        raise BrandingError("m1n1 input size mismatch")
    if _sha256(source) != _require_digest(
        input_descriptor.get("sha256"),
        "m1n1 input",
    ):
        raise BrandingError("m1n1 input digest mismatch")

    branded = bytearray(source)
    previous_end = 0
    for index, replacement in enumerate(replacements):
        if not isinstance(replacement, dict):
            raise BrandingError("m1n1 replacement contract is invalid")
        offset = replacement.get("offset")
        size_bytes = replacement.get("size_bytes")
        descriptor = replacement.get("replacement")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < previous_end
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes <= 0
            or not isinstance(descriptor, dict)
        ):
            raise BrandingError("m1n1 replacement range is invalid")
        end = offset + size_bytes
        if end > len(source):
            raise BrandingError("m1n1 replacement range exceeds the input")
        source_region = source[offset:end]
        if _sha256(source_region) != _require_digest(
            replacement.get("source_sha256"),
            f"m1n1 source logo region {index}",
        ):
            raise BrandingError("m1n1 source logo region digest mismatch")
        filename = _require_filename(
            descriptor.get("filename"),
            f"m1n1 replacement {index}",
        )
        replacement_data = _verify_descriptor(
            descriptor,
            asset_directory / filename,
            f"m1n1 replacement {index}",
            MAX_M1N1_BYTES,
        )
        if len(replacement_data) != size_bytes:
            raise BrandingError("m1n1 replacement size does not match its region")
        branded[offset:end] = replacement_data
        previous_end = end

    output_size = _require_size(
        output_descriptor.get("size_bytes"),
        "m1n1 output",
        MAX_M1N1_BYTES,
    )
    if len(branded) != output_size:
        raise BrandingError("m1n1 output size mismatch")
    if _sha256(branded) != _require_digest(
        output_descriptor.get("sha256"),
        "m1n1 output",
    ):
        raise BrandingError("m1n1 output digest mismatch")
    _write_atomic(input_path, output_path, bytes(branded))


def _write_atomic(input_path: Path, output_path: Path, data: bytes) -> None:
    input_resolved = input_path.resolve(strict=True)
    output_resolved = output_path.resolve(strict=False)
    if output_path.exists() or output_path.is_symlink():
        metadata = output_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or output_path.is_symlink():
            raise BrandingError("m1n1 output is unsafe")
        if output_resolved != input_resolved:
            raise BrandingError("refusing to overwrite a different m1n1 output")
    parent = output_path.parent
    parent_metadata = parent.lstat()
    if not stat.S_ISDIR(parent_metadata.st_mode) or parent.is_symlink():
        raise BrandingError("m1n1 output directory is unsafe")
    mode = stat.S_IMODE(input_path.lstat().st_mode)
    temporary = parent / f".{output_path.name}.branding-{os.getpid()}"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, output_path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify-assets")
    verify_parser.add_argument("manifest", type=Path)
    verify_parser.add_argument("asset_directory", type=Path)
    patch_parser = subparsers.add_parser("patch-m1n1")
    patch_parser.add_argument("manifest", type=Path)
    patch_parser.add_argument("asset_directory", type=Path)
    patch_parser.add_argument("input", type=Path)
    patch_parser.add_argument("output", type=Path)
    arguments = parser.parse_args(argv)
    manifest = load_manifest(arguments.manifest)
    if arguments.command == "verify-assets":
        verify_assets(manifest, arguments.asset_directory)
    else:
        verify_assets(manifest, arguments.asset_directory)
        patch_m1n1_boot(
            manifest,
            arguments.asset_directory,
            arguments.input,
            arguments.output,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BrandingError as error:
        print(f"brand-apple-silicon-boot: {error}", file=sys.stderr)
        raise SystemExit(1) from error
