#!/usr/bin/env python3
"""Revalidate the exact offline repository view immediately before install."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


class RepositoryViewError(RuntimeError):
    pass


def canonical_digest(value: object) -> str:
    content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(content).hexdigest()


def _safe_filename(value: object, role: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != os.path.basename(value)
        or value in {".", ".."}
    ):
        raise RepositoryViewError(f"unsafe {role}")
    return value


def _exact_file(path: Path, expected_size: object, expected_sha256: object) -> dict:
    if (
        not isinstance(expected_size, int)
        or expected_size < 0
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise RepositoryViewError(f"invalid identity record: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RepositoryViewError(f"missing or unsafe repository file: {path.name}") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
        ):
            raise RepositoryViewError(f"unsafe repository file: {path.name}")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 8 * 1024 * 1024):
            total += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (before.st_dev, before.st_ino) != (path_after.st_dev, path_after.st_ino)
        ):
            raise RepositoryViewError(f"repository file changed while reading: {path.name}")
        if total != expected_size or digest.hexdigest() != expected_sha256:
            raise RepositoryViewError(f"repository digest or size mismatch: {path.name}")
        return {"filename": path.name, "size_bytes": total, "sha256": digest.hexdigest()}
    finally:
        os.close(descriptor)


def _validate_manifest(manifest: dict) -> None:
    expected_keys = {
        "schema_version", "verification_kind", "packages",
        "requested_package_files", "resolved_closure", "snapshot_locks",
        "trust", "repo_add", "validation", "identity",
    }
    if set(manifest) != expected_keys:
        raise RepositoryViewError("repository manifest schema is invalid")
    unsigned = {key: value for key, value in manifest.items() if key != "identity"}
    if (
        manifest.get("schema_version") != 1
        or manifest.get("verification_kind") != "asahi-offline-repository-inputs"
        or manifest.get("validation") != {"result": "passed", "signatures": "required"}
        or manifest.get("identity") != canonical_digest(unsigned)
        or not isinstance(manifest.get("packages"), list)
    ):
        raise RepositoryViewError("repository manifest identity or validation is invalid")


def _database_records(run_manifest: dict) -> dict[str, dict]:
    if (
        run_manifest.get("schema_version") != 1
        or run_manifest.get("stage") != "offline-repository-database"
        or run_manifest.get("validation") != {"result": "passed"}
        or not isinstance(run_manifest.get("outputs"), list)
    ):
        raise RepositoryViewError("repository database run manifest is invalid")
    records = {}
    for record in run_manifest["outputs"]:
        if not isinstance(record, dict) or record.get("name") in records:
            raise RepositoryViewError("repository database output record is invalid")
        records[record.get("name")] = record
    if set(records) != {"repository-db", "repository-files"}:
        raise RepositoryViewError("repository database outputs are incomplete")
    return records


def verify_offline_repository_view(
    *,
    mirror: Path,
    repository_manifest: dict,
    database_run_manifest: dict,
) -> dict:
    mirror = Path(mirror)
    if not mirror.is_dir() or mirror.is_symlink():
        raise RepositoryViewError("offline repository root is missing or unsafe")
    _validate_manifest(repository_manifest)
    database_records = _database_records(database_run_manifest)
    declared_packages: set[str] = set()
    declared_signatures: set[str] = set()
    verified = []
    expected_package_keys = {
        "filename", "size_bytes", "sha256", "signature_filename",
        "signature_size_bytes", "signature_sha256", "signer_fingerprint",
    }
    for record in repository_manifest["packages"]:
        if not isinstance(record, dict) or set(record) != expected_package_keys:
            raise RepositoryViewError("repository package record is invalid")
        filename = _safe_filename(record["filename"], "package filename")
        signature = _safe_filename(record["signature_filename"], "signature filename")
        if signature != filename + ".sig" or filename in declared_packages:
            raise RepositoryViewError("repository package records are duplicated or mismatched")
        declared_packages.add(filename)
        declared_signatures.add(signature)
        verified.append(_exact_file(mirror / filename, record["size_bytes"], record["sha256"]))
        _exact_file(
            mirror / signature,
            record["signature_size_bytes"],
            record["signature_sha256"],
        )
    actual_packages = {
        path.name for path in mirror.glob("*.pkg.tar.*")
        if not path.name.endswith(".sig")
    }
    actual_signatures = {
        path.name for path in mirror.glob("*.pkg.tar.*.sig")
    }
    if actual_packages != declared_packages or actual_signatures != declared_signatures:
        raise RepositoryViewError("offline repository package inventory differs")
    for name, record_name in (
        ("offline.db.tar.gz", "repository-db"),
        ("offline.files.tar.gz", "repository-files"),
    ):
        record = database_records[record_name]
        _exact_file(mirror / name, record.get("size_bytes"), record.get("sha256"))
    for alias, target in (
        ("offline.db", "offline.db.tar.gz"),
        ("offline.files", "offline.files.tar.gz"),
    ):
        path = mirror / alias
        if not path.is_symlink() or os.readlink(path) != target:
            raise RepositoryViewError(f"repository alias differs: {alias}")
    value = {
        "schema_version": 1,
        "verification_kind": "offline-repository-install-view-v1",
        "repository_identity": repository_manifest["identity"],
        "database_checkpoint_identity": database_run_manifest.get("checkpoint_identity"),
        "packages": len(verified),
        "payloads_sha256": canonical_digest(verified),
        "validation": {"result": "passed"},
    }
    return value | {"view_identity": canonical_digest(value)}


def _load_object(path: Path, role: str) -> dict:
    if not path.is_file() or path.is_symlink():
        raise RepositoryViewError(f"{role} is missing or unsafe")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RepositoryViewError(f"{role} is invalid") from error
    if not isinstance(value, dict):
        raise RepositoryViewError(f"{role} is not an object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", type=Path, required=True)
    parser.add_argument("--repository-manifest", type=Path, required=True)
    parser.add_argument("--database-run-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = verify_offline_repository_view(
        mirror=arguments.mirror,
        repository_manifest=_load_object(arguments.repository_manifest, "repository manifest"),
        database_run_manifest=_load_object(arguments.database_run_manifest, "database run manifest"),
    )
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        sys.stdout.write(encoded)
    else:
        arguments.output.write_text(encoded)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RepositoryViewError, OSError) as error:
        raise SystemExit(f"verify-asahi-offline-repository-view: {error}") from error
