#!/usr/bin/env python3
"""Validate an immutable configured-target checkpoint without changing it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any


SCHEMA_VERSION = 1
CONFIGURED_PHASES = (
    "Preparing live environment",
    "Preparing install target",
    "Installing Arch + Omarchy",
    "Configuring hibernation",
    "Configuring system",
    "Staging provisioning",
)
REQUIRED_PLATFORM_PACKAGES = {
    "base",
    "grub",
    "linux-asahi",
    "mkinitcpio",
    "systemd",
}


class ConfiguredTargetError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def with_digest(value: dict[str, Any]) -> dict[str, Any]:
    return value | {"input_digest": digest(value)}


def file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def load_object(path: Path, role: str) -> dict[str, Any]:
    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise ConfiguredTargetError(f"{role} is missing: {path}") from error
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise ConfiguredTargetError(f"{role} must be a real file: {path}")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ConfiguredTargetError(f"{role} is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise ConfiguredTargetError(f"{role} must be an object")
    return value


def _verify_embedded_digest(value: dict[str, Any], role: str) -> None:
    expected = value.get("input_digest")
    unsigned = {key: item for key, item in value.items() if key != "input_digest"}
    if not isinstance(expected, str) or expected != digest(unsigned):
        raise ConfiguredTargetError(f"{role} digest is invalid")


def build_runtime_manifest_for_test(
    root: Path,
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...],
) -> dict[str, Any]:
    entries = []
    for relative in sorted((*required, *optional)):
        path = root / relative
        if not path.exists():
            if relative in required:
                raise ConfiguredTargetError(f"missing test runtime input: {relative}")
            entries.append({"path": relative, "present": False})
            continue
        status = path.lstat()
        entries.append(
            {
                "path": relative,
                "present": True,
                "size_bytes": status.st_size,
                "sha256": file_sha256(path),
                "executable_mode": stat.S_IMODE(status.st_mode) & 0o111,
            }
        )
    return with_digest(
        {
            "schema_version": 1,
            "stage": "configured-target",
            "root_role": "configured-target-runtime",
            "entries": entries,
            "settings": {},
        }
    )


def verify_runtime_inputs(root: Path, manifest: dict[str, Any]) -> None:
    _verify_embedded_digest(manifest, "configured runtime manifest")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("stage") != "configured-target"
        or manifest.get("root_role") != "configured-target-runtime"
        or not isinstance(manifest.get("entries"), list)
        or not isinstance(manifest.get("settings"), dict)
    ):
        raise ConfiguredTargetError("configured runtime manifest schema is invalid")
    seen: set[str] = set()
    for record in manifest["entries"]:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ConfiguredTargetError("configured runtime record is invalid")
        relative = record["path"]
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts or relative in seen:
            raise ConfiguredTargetError("configured runtime path is unsafe or duplicated")
        seen.add(relative)
        path = root / relative
        if record.get("present") is False:
            if set(record) != {"path", "present"} or path.exists() or path.is_symlink():
                raise ConfiguredTargetError(
                    f"optional runtime input presence mismatch: {relative}"
                )
            continue
        if set(record) != {
            "path",
            "present",
            "size_bytes",
            "sha256",
            "executable_mode",
        } or record.get("present") is not True:
            raise ConfiguredTargetError(f"configured runtime record is invalid: {relative}")
        try:
            status = path.lstat()
        except FileNotFoundError as error:
            raise ConfiguredTargetError(
                f"runtime input digest or size mismatch: {relative}"
            ) from error
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise ConfiguredTargetError(f"configured runtime input is unsafe: {relative}")
        if (
            status.st_size != record["size_bytes"]
            or file_sha256(path) != record["sha256"]
            or stat.S_IMODE(status.st_mode) & 0o111 != record["executable_mode"]
        ):
            raise ConfiguredTargetError(
                f"runtime input digest or size mismatch: {relative}"
            )


def verify_product_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    _verify_embedded_digest(manifest, "configured product manifest")
    if set(manifest) != {
        "schema_version",
        "stage",
        "inputs",
        "input_digest",
    } or manifest.get("schema_version") != SCHEMA_VERSION or manifest.get(
        "stage"
    ) != "configured-target":
        raise ConfiguredTargetError("configured product manifest schema is invalid")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {
        "boot_backend",
        "boot_filesystem_uuid",
        "esp_volume_id",
        "kernel_package",
        "root_filesystem_uuid",
    }:
        raise ConfiguredTargetError("configured product inputs are invalid")
    if inputs["boot_backend"] != "asahi-grub" or inputs["kernel_package"] != "linux-asahi":
        raise ConfiguredTargetError("configured product is not the supported Asahi target")
    return inputs


def _runtime_files_by_path(
    root: Path,
    manifest: dict[str, Any],
) -> dict[str, Path]:
    return {
        record["path"]: root / record["path"]
        for record in manifest["entries"]
        if record.get("present") is True
    }


def _package_targets(path: Path) -> set[str]:
    targets = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _name, value = line.split("=", 1)
        value = value.strip().strip('"\'')
        if value:
            targets.add(value)
    return targets


def _package_list(path: Path) -> set[str]:
    return {
        line
        for raw in path.read_text().splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    }


def expected_package_closure(path: Path) -> dict[str, str]:
    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise ConfiguredTargetError(
            "exact configured package closure is missing"
        ) from error
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise ConfiguredTargetError("exact configured package closure is unsafe")

    packages: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(errors="strict").splitlines(), 1):
        fields = raw.split("\t")
        if len(fields) != 2:
            raise ConfiguredTargetError(
                f"exact configured package closure record is invalid: {line_number}"
            )
        name, version = fields
        if (
            re.fullmatch(r"[a-z0-9@._+][a-z0-9@._+-]*", name) is None
            or not version
            or version.strip() != version
            or any(character.isspace() for character in version)
            or name in packages
        ):
            raise ConfiguredTargetError(
                f"exact configured package closure record is invalid: {line_number}"
            )
        packages[name] = version
    if not packages:
        raise ConfiguredTargetError("exact configured package closure is empty")
    return packages


def expected_package_count(path: Path) -> int:
    value = path.read_text(errors="strict")
    if re.fullmatch(r"[1-9][0-9]*\n?", value) is None:
        raise ConfiguredTargetError("configured expected package count is invalid")
    return int(value)


def _pacman_desc(path: Path) -> tuple[str, str]:
    fields: dict[str, str] = {}
    lines = path.read_text(errors="strict").splitlines()
    for index, line in enumerate(lines[:-1]):
        if line in {"%NAME%", "%VERSION%"}:
            fields[line] = lines[index + 1]
    if not fields.get("%NAME%") or not fields.get("%VERSION%"):
        raise ConfiguredTargetError(f"installed package metadata is invalid: {path}")
    return fields["%NAME%"], fields["%VERSION%"]


def installed_packages(target: Path) -> dict[str, str]:
    local = target / "var/lib/pacman/local"
    if not local.is_dir() or local.is_symlink():
        raise ConfiguredTargetError("installed package database is missing or unsafe")
    packages: dict[str, str] = {}
    for directory in sorted(local.iterdir(), key=lambda item: item.name):
        if directory.name == "ALPM_DB_VERSION":
            if (
                not directory.is_file()
                or directory.is_symlink()
                or re.fullmatch(r"[0-9]+\n?", directory.read_text()) is None
            ):
                raise ConfiguredTargetError("installed ALPM database version is invalid")
            continue
        if not directory.is_dir() or directory.is_symlink():
            raise ConfiguredTargetError("installed package database contains an unsafe entry")
        name, version = _pacman_desc(directory / "desc")
        if name in packages:
            raise ConfiguredTargetError(f"installed package is duplicated: {name}")
        packages[name] = version
    return packages


def _expected_esp_uuid(volume_id: str) -> str:
    if re.fullmatch(r"0x[0-9a-fA-F]{8}", volume_id) is None:
        raise ConfiguredTargetError("configured ESP volume id is invalid")
    value = volume_id[2:].upper()
    return f"{value[:4]}-{value[4:]}"


def verify_filesystems(
    target: Path,
    filesystems: dict[str, Any],
    product: dict[str, Any],
) -> None:
    if set(filesystems) != {"root", "boot", "esp"}:
        raise ConfiguredTargetError("configured filesystem evidence is incomplete")
    expected = {
        "root": ("btrfs", "OMARCHY_ROOT", product["root_filesystem_uuid"]),
        "boot": ("ext4", "OMARCHY_BOOT", product["boot_filesystem_uuid"]),
        "esp": ("vfat", "OMARCHYESP", _expected_esp_uuid(product["esp_volume_id"])),
    }
    for role, (filesystem_type, label, uuid) in expected.items():
        record = filesystems.get(role)
        if not isinstance(record, dict):
            raise ConfiguredTargetError(f"configured {role} filesystem evidence is invalid")
        if record.get("type") != filesystem_type or record.get("label") != label:
            raise ConfiguredTargetError(f"configured {role} filesystem type or label is invalid")
        if record.get("uuid") != uuid:
            raise ConfiguredTargetError(f"configured {role} UUID is invalid")
    options = filesystems["root"].get("mount_options")
    if not isinstance(options, list) or "ro" not in options or "subvol=/@" not in options:
        raise ConfiguredTargetError("configured root was not inspected read-only at subvol=@")

    fstab = (target / "etc/fstab").read_text()
    for uuid, mountpoint in (
        (product["root_filesystem_uuid"], "/"),
        (product["boot_filesystem_uuid"], "/boot"),
        (_expected_esp_uuid(product["esp_volume_id"]), "/boot/efi"),
    ):
        if f"UUID={uuid}" not in fstab or mountpoint not in fstab:
            raise ConfiguredTargetError(f"configured fstab is missing {mountpoint} UUID")
    if "subvol=@" not in fstab:
        raise ConfiguredTargetError("configured fstab has no root subvol=@")


def verify_node_identity(identity: dict[str, Any]) -> dict[str, Any]:
    if (
        set(identity)
        != {
            "schema_version",
            "verification_kind",
            "filename",
            "sha256",
            "size_bytes",
        }
        or identity.get("schema_version") != SCHEMA_VERSION
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
        raise ConfiguredTargetError("pinned Node lock projection is invalid")
    return {
        "filename": identity["filename"],
        "sha256": identity["sha256"],
        "size_bytes": identity["size_bytes"],
    }


def verify_bootstrap_contract(
    target: Path,
    node_identity: dict[str, Any],
) -> dict[str, Any]:
    expected = verify_node_identity(node_identity)
    init = target / "sbin/init"
    if not init.is_symlink() or os.readlink(init) != "../lib/systemd/systemd":
        raise ConfiguredTargetError("configured /sbin/init is not the systemd link")
    systemd = target / "usr/lib/systemd/systemd"
    if not systemd.is_file() or not os.access(systemd, os.X_OK):
        raise ConfiguredTargetError("configured systemd executable is missing")
    pending = target / "var/lib/omarchy/provisioning/pending"
    if not pending.is_file() or pending.is_symlink():
        raise ConfiguredTargetError("configured provisioning pending marker is missing")
    service = target / "etc/systemd/system/multi-user.target.wants/omarchy-provision-owner.service"
    if not service.is_symlink() or os.readlink(service) != (
        "/etc/systemd/system/omarchy-provision-owner.service"
    ):
        raise ConfiguredTargetError("configured provisioning service is not enabled")
    packages = target / "var/lib/omarchy/provisioning/packages"
    if not packages.is_dir() or packages.is_symlink():
        raise ConfiguredTargetError(
            "configured target Node archive inventory is not exact"
        )
    nodes = sorted(packages.iterdir(), key=lambda item: item.name)
    if (
        [node.name for node in nodes] != [expected["filename"]]
        or not nodes[0].is_file()
        or nodes[0].is_symlink()
    ):
        raise ConfiguredTargetError(
            "configured target Node archive inventory is not exact"
        )
    actual = {
        "filename": nodes[0].name,
        "sha256": file_sha256(nodes[0]),
        "size_bytes": nodes[0].stat().st_size,
    }
    if actual != expected:
        raise ConfiguredTargetError(
            "configured Node runtime differs from the pinned lock"
        )
    return actual


def verify_stage_state(state_dir: Path, installed_count: int, expected_count: int) -> dict[str, Any]:
    state = load_object(state_dir / "state.json", "configured stage state")
    phases = state.get("phases")
    if (
        state.get("total_phases") != len(CONFIGURED_PHASES)
        or state.get("current_phase") != "Installation complete"
        or not isinstance(phases, list)
        or [(item.get("name"), item.get("status")) for item in phases]
        != [(name, "ok") for name in CONFIGURED_PHASES]
    ):
        raise ConfiguredTargetError("configured stage did not complete every exact phase")
    if state.get("installed_packages") != installed_count:
        raise ConfiguredTargetError("configured installed package count is stale")
    if state.get("expected_packages") != expected_count:
        raise ConfiguredTargetError("configured expected package count is stale")
    if installed_count != expected_count:
        raise ConfiguredTargetError("configured package-count closure is not exact")
    stable = {
        "total_phases": state["total_phases"],
        "current_phase": state["current_phase"],
        "phases": [
            {"name": item["name"], "status": item["status"]} for item in phases
        ],
        "installed_packages": installed_count,
        "expected_packages": expected_count,
    }
    return stable | {"state_digest": digest(stable)}


def capture_configured_target(
    *,
    target: Path,
    state_dir: Path,
    runtime_root: Path,
    runtime_manifest: dict[str, Any],
    product_manifest: dict[str, Any],
    repository_manifest: dict[str, Any],
    checkpoint_manifest: dict[str, Any] | None,
    filesystems: dict[str, Any],
    node_identity: dict[str, Any],
) -> dict[str, Any]:
    verify_runtime_inputs(runtime_root, runtime_manifest)
    product = verify_product_manifest(product_manifest)
    verify_filesystems(target, filesystems, product)
    if repository_manifest.get("schema_version") != SCHEMA_VERSION or repository_manifest.get(
        "validation"
    ) != {"result": "passed", "signatures": "required"}:
        raise ConfiguredTargetError("verified repository manifest is not admitted")
    repository_identity = repository_manifest.get("identity")
    if not isinstance(repository_identity, str) or re.fullmatch(
        r"[0-9a-f]{64}", repository_identity
    ) is None:
        raise ConfiguredTargetError("verified repository identity is invalid")
    if checkpoint_manifest is not None:
        if (
            checkpoint_manifest.get("validation") != {"result": "passed"}
            or checkpoint_manifest.get("immutable") is not True
            or re.fullmatch(
                r"[0-9a-f]{64}", checkpoint_manifest.get("checkpoint_identity", "")
            )
            is None
        ):
            raise ConfiguredTargetError(
                "source configured checkpoint is not immutable and valid"
            )

    closure = repository_manifest.get("resolved_closure")
    if not isinstance(closure, list):
        raise ConfiguredTargetError("verified repository closure is invalid")
    repository_packages = {}
    for record in closure:
        if not isinstance(record, dict) or set(record) != {"filename", "name", "version"}:
            raise ConfiguredTargetError("verified repository closure record is invalid")
        name = record["name"]
        version = record["version"]
        if name in repository_packages or not isinstance(name, str) or not isinstance(version, str):
            raise ConfiguredTargetError("verified repository closure is duplicated or invalid")
        repository_packages[name] = version

    packages = installed_packages(target)
    for name, version in packages.items():
        if repository_packages.get(name) != version:
            raise ConfiguredTargetError(
                f"installed package is absent or differs from verified repository: {name}"
            )
    runtime_files = _runtime_files_by_path(runtime_root, runtime_manifest)
    required_packages = REQUIRED_PLATFORM_PACKAGES | _package_targets(
        runtime_files["package-targets"]
    ) | _package_list(runtime_files["omarchy-base.packages"])
    for name in sorted(required_packages):
        if name not in packages:
            raise ConfiguredTargetError(f"required configured package is absent: {name}")

    expected_packages = expected_package_closure(
        runtime_files["expected-package-closure"]
    )
    for name, version in expected_packages.items():
        if repository_packages.get(name) != version:
            raise ConfiguredTargetError(
                f"exact configured package is absent or differs from verified repository: {name}"
            )
    if packages != expected_packages:
        installed_only = sorted(set(packages) - set(expected_packages))
        expected_only = sorted(set(expected_packages) - set(packages))
        version_mismatches = sorted(
            f"{name} {expected_packages[name]}->{packages[name]}"
            for name in set(packages) & set(expected_packages)
            if packages[name] != expected_packages[name]
        )

        def _head(values: list[str]) -> str:
            if len(values) > 40:
                return ", ".join(values[:40]) + f", +{len(values) - 40} more"
            return ", ".join(values)

        raise ConfiguredTargetError(
            "installed package inventory differs from exact resolved closure: "
            f"{len(installed_only)} installed-only [{_head(installed_only)}]; "
            f"{len(expected_only)} expected-only [{_head(expected_only)}]; "
            f"{len(version_mismatches)} version-mismatched "
            f"[{_head(version_mismatches)}]"
        )
    declared_count = expected_package_count(runtime_files["expected-packages"])
    if declared_count != len(expected_packages):
        raise ConfiguredTargetError(
            "configured expected package count differs from exact closure"
        )
    state = verify_stage_state(state_dir, len(packages), len(expected_packages))
    node = verify_bootstrap_contract(target, node_identity)
    package_inventory = [
        {"name": name, "version": version} for name, version in sorted(packages.items())
    ]
    value = {
        "schema_version": SCHEMA_VERSION,
        "verification_kind": "configured-target-installed-state-v1",
        "validator_sha256": file_sha256(Path(__file__).resolve()),
        "repository_identity": repository_identity,
        "runtime_input_digest": runtime_manifest["input_digest"],
        "product_input_digest": product_manifest["input_digest"],
        "filesystems": filesystems,
        "installed_packages": len(packages),
        "package_inventory_sha256": digest(package_inventory),
        "stage_state": state,
        "staged_node": node,
        "validation": {"result": "passed"},
    }
    if checkpoint_manifest is not None:
        outputs = {
            record["name"]: {
                "sha256": record.get("sha256"),
                "size_bytes": record.get("size_bytes"),
            }
            for record in checkpoint_manifest.get("outputs", [])
        }
        if set(outputs) != {"root-image", "boot-image", "esp-image", "stage-state"}:
            raise ConfiguredTargetError(
                "source configured checkpoint outputs are incomplete"
            )
        value |= {
            "verification_kind": "configured-target-installed-contract-v1",
            "source_checkpoint_identity": checkpoint_manifest["checkpoint_identity"],
            "checkpoint_outputs": outputs,
        }
    return value | {"proof_digest": digest(value)}


def _blkid(device: str) -> dict[str, str]:
    completed = subprocess.run(
        ["blkid", "-o", "export", device],
        check=True,
        capture_output=True,
        text=True,
    )
    values = {}
    for line in completed.stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator:
            values[name] = value
    return values


def collect_filesystems(
    *,
    target: Path,
    root_device: str,
    boot_device: str,
    esp_device: str,
) -> dict[str, Any]:
    records = {}
    for role, device in (
        ("root", root_device),
        ("boot", boot_device),
        ("esp", esp_device),
    ):
        values = _blkid(device)
        records[role] = {
            "type": values.get("TYPE"),
            "label": values.get("LABEL") or values.get("LABEL_FATBOOT"),
            "uuid": values.get("UUID"),
        }
    completed = subprocess.run(
        ["findmnt", "-no", "OPTIONS", "--target", str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    records["root"]["mount_options"] = sorted(
        option for option in completed.stdout.strip().split(",") if option
    )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--product-manifest", type=Path, required=True)
    parser.add_argument("--repository-manifest", type=Path, required=True)
    parser.add_argument("--checkpoint-manifest", type=Path)
    parser.add_argument("--installed-state-only", action="store_true")
    parser.add_argument("--root-device", required=True)
    parser.add_argument("--boot-device", required=True)
    parser.add_argument("--esp-device", required=True)
    parser.add_argument("--node-identity", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if (arguments.checkpoint_manifest is None) == (not arguments.installed_state_only):
        raise ConfiguredTargetError(
            "select exactly one configured checkpoint or installed-state-only mode"
        )
    proof = capture_configured_target(
        target=arguments.target,
        state_dir=arguments.state_dir,
        runtime_root=arguments.runtime_root,
        runtime_manifest=load_object(arguments.runtime_manifest, "runtime manifest"),
        product_manifest=load_object(arguments.product_manifest, "product manifest"),
        repository_manifest=load_object(
            arguments.repository_manifest, "repository manifest"
        ),
        checkpoint_manifest=(
            None
            if arguments.installed_state_only
            else load_object(arguments.checkpoint_manifest, "checkpoint manifest")
        ),
        node_identity=load_object(arguments.node_identity, "Node identity"),
        filesystems=collect_filesystems(
            target=arguments.target,
            root_device=arguments.root_device,
            boot_device=arguments.boot_device,
            esp_device=arguments.esp_device,
        ),
    )
    arguments.output.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfiguredTargetError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"asahi-configured-target: {error}") from error
