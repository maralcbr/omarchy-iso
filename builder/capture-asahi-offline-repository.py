#!/usr/bin/env python3
"""Capture exact signed inputs for a reusable Apple Silicon offline repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Callable


class RepositoryCaptureError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def canonical_ownertrust_digest(ownertrust: str) -> str:
    """Hash only exact ownertrust assignments, excluding volatile comments."""
    records: dict[str, str] = {}
    for raw_line in ownertrust.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}):([0-9]+):", line)
        if match is None:
            raise RepositoryCaptureError("ownertrust export contains an invalid record")
        fingerprint, trust = match.groups()
        fingerprint = fingerprint.upper()
        trust = str(int(trust, 10))
        if fingerprint in records and records[fingerprint] != trust:
            raise RepositoryCaptureError("ownertrust export contains conflicting records")
        records[fingerprint] = trust
    if not records:
        raise RepositoryCaptureError("ownertrust export contains no assignments")
    canonical = "".join(
        f"{fingerprint}:{records[fingerprint]}:\n" for fingerprint in sorted(records)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def repository_database_projection(manifest: dict) -> dict:
    """Return only inputs that can affect repo-add database bytes.

    Snapshot locks prove where signed package bytes came from and remain part of
    the package-cache identity. The repository database itself is a pure
    function of the exact package/signature records, resolved closure, trust
    state, and repo-add implementation captured here.
    """
    expected_keys = {
        "schema_version",
        "verification_kind",
        "packages",
        "requested_package_files",
        "resolved_closure",
        "snapshot_locks",
        "trust",
        "repo_add",
        "validation",
        "identity",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise RepositoryCaptureError("repository manifest schema is invalid")
    identity = manifest.get("identity")
    unsigned = {key: value for key, value in manifest.items() if key != "identity"}
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise RepositoryCaptureError("repository manifest identity is invalid")
    if canonical_digest(unsigned) != identity:
        raise RepositoryCaptureError("repository manifest identity is stale")
    if manifest.get("schema_version") != 1 or manifest.get(
        "verification_kind"
    ) != "asahi-offline-repository-inputs":
        raise RepositoryCaptureError("repository manifest kind is invalid")
    if manifest.get("validation") != {
        "result": "passed",
        "signatures": "required",
    }:
        raise RepositoryCaptureError("repository manifest validation is not passed")
    projection = {
        key: value
        for key, value in unsigned.items()
        if key != "snapshot_locks"
    }
    return projection | {"projection_identity": canonical_digest(projection)}


def regular_file(path: Path, role: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise RepositoryCaptureError(f"missing or unsafe {role}: {path}")


def path_record(path: Path) -> dict:
    regular_file(path, "identity input")
    return {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _load_json_file(path: Path, role: str) -> dict:
    regular_file(path, role)
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RepositoryCaptureError(f"invalid {role}: {path}") from error
    if not isinstance(value, dict):
        raise RepositoryCaptureError(f"{role} must be an object: {path}")
    return value


def verify_repository_database_transition(
    *,
    source_manifest: dict,
    target_manifest: dict,
    legacy_build_lock: Path,
    package_source_lock: Path,
    mode: str,
    ownertrust_transition: dict | None = None,
) -> dict:
    """Prove the one authorized broad-lock to package-lock migration."""
    if mode not in {"diagnostic", "qualification"}:
        raise RepositoryCaptureError("repository transition mode is invalid")
    source_projection = repository_database_projection(source_manifest)
    target_projection = repository_database_projection(target_manifest)
    if source_projection != target_projection:
        expected_transition_keys = {
            "kind",
            "source_ownertrust_sha256",
            "target_ownertrust_sha256",
        }
        if (
            not isinstance(ownertrust_transition, dict)
            or set(ownertrust_transition) != expected_transition_keys
            or ownertrust_transition.get("kind") != "ownertrust-canonicalization-v1"
        ):
            raise RepositoryCaptureError("repository database projection differs")
        source_ownertrust = source_manifest.get("trust", {}).get("ownertrust_sha256")
        target_ownertrust = target_manifest.get("trust", {}).get("ownertrust_sha256")
        if (
            source_ownertrust != ownertrust_transition["source_ownertrust_sha256"]
            or target_ownertrust != ownertrust_transition["target_ownertrust_sha256"]
            or not all(
                re.fullmatch(r"[0-9a-f]{64}", value or "")
                for value in (source_ownertrust, target_ownertrust)
            )
            or source_ownertrust == target_ownertrust
        ):
            raise RepositoryCaptureError("repository ownertrust transition differs")
        normalized_source = json.loads(json.dumps(source_manifest))
        normalized_source.pop("identity")
        normalized_source["trust"]["ownertrust_sha256"] = target_ownertrust
        normalized_source["identity"] = canonical_digest(normalized_source)
        if repository_database_projection(normalized_source) != target_projection:
            raise RepositoryCaptureError("repository database projection differs")
    elif ownertrust_transition is not None:
        raise RepositoryCaptureError("repository ownertrust transition is unnecessary")

    source_locks = source_manifest["snapshot_locks"]
    target_locks = target_manifest["snapshot_locks"]
    if not isinstance(source_locks, dict) or not isinstance(target_locks, dict):
        raise RepositoryCaptureError("repository snapshot locks are invalid")
    if set(source_locks) - {"build-lock"} != set(target_locks) - {
        "package-source-lock"
    }:
        raise RepositoryCaptureError("repository snapshot lock set differs")
    for name in sorted(set(source_locks) - {"build-lock"}):
        if source_locks[name] != target_locks[name]:
            raise RepositoryCaptureError(f"repository snapshot lock differs: {name}")
    if source_locks.get("build-lock") != path_record(legacy_build_lock):
        raise RepositoryCaptureError("legacy repository build lock record differs")
    if target_locks.get("package-source-lock") != path_record(package_source_lock):
        raise RepositoryCaptureError("repository package lock projection record differs")

    legacy = _load_json_file(legacy_build_lock, "legacy build lock")
    package = _load_json_file(package_source_lock, "package source lock")
    expected_legacy_keys = {
        "schema_version",
        "builder",
        "compression",
        "modes",
        "node",
        "retention",
        "stages",
    }
    if set(legacy) != expected_legacy_keys or legacy.get("schema_version") != 1:
        raise RepositoryCaptureError("legacy build lock schema is invalid")
    if set(package) != {"schema_version", "stage", "mode", "inputs"}:
        raise RepositoryCaptureError("package source lock schema is invalid")
    if (
        package.get("schema_version") != 1
        or package.get("stage") != "verified-package-cache"
        or package.get("mode") != mode
        or package.get("inputs") != {"node": legacy.get("node")}
    ):
        raise RepositoryCaptureError("package source lock projection differs")
    proof_value = {
        "schema_version": 1,
        "kind": "repository-database-manifest-v1",
        "mode": mode,
        "source_manifest_identity": source_manifest["identity"],
        "target_manifest_identity": target_manifest["identity"],
        "database_projection_identity": source_projection["projection_identity"],
        "legacy_build_lock": path_record(legacy_build_lock),
        "package_source_lock": path_record(package_source_lock),
        "ownertrust_transition": ownertrust_transition,
    }
    return {
        "kind": proof_value["kind"],
        "proof_digest": canonical_digest(proof_value),
    }


def project_repository_manifest(
    *,
    source_manifest: dict,
    legacy_build_lock: Path,
    package_source_lock: Path,
    mode: str,
    target_ownertrust_sha256: str | None = None,
) -> dict:
    """Predict the exact manifest emitted after the scoped lock transition."""
    repository_database_projection(source_manifest)
    if source_manifest["snapshot_locks"].get("build-lock") != path_record(
        legacy_build_lock
    ):
        raise RepositoryCaptureError("source manifest does not bind the legacy build lock")
    target = json.loads(json.dumps(source_manifest))
    target.pop("identity")
    target["snapshot_locks"].pop("build-lock")
    target["snapshot_locks"]["package-source-lock"] = path_record(
        package_source_lock
    )
    target["snapshot_locks"] = dict(sorted(target["snapshot_locks"].items()))
    ownertrust_transition = None
    if target_ownertrust_sha256 is not None:
        if re.fullmatch(r"[0-9a-f]{64}", target_ownertrust_sha256) is None:
            raise RepositoryCaptureError("target ownertrust identity is invalid")
        source_ownertrust_sha256 = target["trust"]["ownertrust_sha256"]
        if source_ownertrust_sha256 != target_ownertrust_sha256:
            target["trust"]["ownertrust_sha256"] = target_ownertrust_sha256
            ownertrust_transition = {
                "kind": "ownertrust-canonicalization-v1",
                "source_ownertrust_sha256": source_ownertrust_sha256,
                "target_ownertrust_sha256": target_ownertrust_sha256,
            }
    target["identity"] = canonical_digest(target)
    verify_repository_database_transition(
        source_manifest=source_manifest,
        target_manifest=target,
        legacy_build_lock=legacy_build_lock,
        package_source_lock=package_source_lock,
        mode=mode,
        ownertrust_transition=ownertrust_transition,
    )
    return target


def capture_repository(
    *,
    mirror: Path,
    requested_list: Path,
    snapshot_locks: dict[str, Path],
    verify_signature: Callable[[Path, Path], str],
    package_metadata: Callable[[Path], tuple[str, str]],
    trust_state: dict,
    repo_add_version: str,
    repo_add_options: list[str],
) -> dict:
    if not mirror.is_dir() or mirror.is_symlink():
        raise RepositoryCaptureError(f"offline mirror is missing or unsafe: {mirror}")
    regular_file(requested_list, "requested package list")
    packages = sorted(
        path
        for path in mirror.glob("*.pkg.tar.*")
        if path.is_file() and not path.is_symlink() and not path.name.endswith(".sig")
    )
    signatures = sorted(
        path for path in mirror.glob("*.pkg.tar.*.sig") if path.is_file() and not path.is_symlink()
    )
    if not packages:
        raise RepositoryCaptureError("offline mirror contains no packages")
    package_names = {path.name for path in packages}
    signature_names = {path.name.removesuffix(".sig") for path in signatures}
    missing_signatures = sorted(package_names - signature_names)
    orphaned_signatures = sorted(signature_names - package_names)
    if missing_signatures:
        raise RepositoryCaptureError(
            "missing detached package signature: " + ",".join(missing_signatures)
        )
    if orphaned_signatures:
        raise RepositoryCaptureError(
            "orphan detached package signature: " + ",".join(orphaned_signatures)
        )

    requested = sorted(
        line.strip()
        for line in requested_list.read_text().splitlines()
        if line.strip()
    )
    if len(requested) != len(set(requested)):
        raise RepositoryCaptureError("requested package list contains duplicates")
    if set(requested) != package_names:
        raise RepositoryCaptureError("offline packages do not match requested closure")

    trusted_fingerprints = set(trust_state.get("fingerprints", []))
    if not trusted_fingerprints or not all(
        re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", value)
        for value in trusted_fingerprints
    ):
        raise RepositoryCaptureError("trust state contains no valid fingerprints")
    if not re.fullmatch(r"[0-9a-f]{64}", trust_state.get("ownertrust_sha256", "")):
        raise RepositoryCaptureError("ownertrust identity is invalid")

    records = []
    closure = []
    for package in packages:
        signature = package.with_name(package.name + ".sig")
        signer = verify_signature(package, signature).upper()
        if signer not in trusted_fingerprints:
            raise RepositoryCaptureError(
                f"package signer is absent from the exact trust state: {package.name}"
            )
        name, version = package_metadata(package)
        if not name or not version:
            raise RepositoryCaptureError(f"package metadata is incomplete: {package.name}")
        records.append(
            {
                "filename": package.name,
                "size_bytes": package.stat().st_size,
                "sha256": sha256_file(package),
                "signature_filename": signature.name,
                "signature_size_bytes": signature.stat().st_size,
                "signature_sha256": sha256_file(signature),
                "signer_fingerprint": signer,
            }
        )
        closure.append({"filename": package.name, "name": name, "version": version})

    lock_records = {}
    for name, path in sorted(snapshot_locks.items()):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            raise RepositoryCaptureError(f"unsafe snapshot lock name: {name}")
        regular_file(path, "snapshot lock")
        lock_records[name] = {
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }

    manifest = {
        "schema_version": 1,
        "verification_kind": "asahi-offline-repository-inputs",
        "packages": records,
        "requested_package_files": requested,
        "resolved_closure": closure,
        "snapshot_locks": lock_records,
        "trust": {
            "fingerprints": sorted(trusted_fingerprints),
            "ownertrust_sha256": trust_state["ownertrust_sha256"],
        },
        "repo_add": {
            "version": repo_add_version.strip(),
            "options": repo_add_options,
        },
        "validation": {"result": "passed", "signatures": "required"},
    }
    manifest["identity"] = canonical_digest(manifest)
    return manifest


def run(arguments: list[str], *, input_text: str | None = None) -> str:
    try:
        completed = subprocess.run(
            arguments,
            input=input_text,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RepositoryCaptureError(f"verification command failed: {arguments[0]}") from error
    return completed.stdout


def system_trust_state(gnupg_home: Path) -> dict:
    listing = run(
        ["gpg", "--homedir", str(gnupg_home), "--batch", "--with-colons", "--fingerprint"]
    )
    fingerprints = sorted(
        {fields[9].upper() for line in listing.splitlines() if (fields := line.split(":"))[0] == "fpr"}
    )
    ownertrust = run(["gpg", "--homedir", str(gnupg_home), "--batch", "--export-ownertrust"])
    return {
        "fingerprints": fingerprints,
        "ownertrust_sha256": canonical_ownertrust_digest(ownertrust),
    }


def system_signature_verifier(gnupg_home: Path) -> Callable[[Path, Path], str]:
    def verify(package: Path, signature: Path) -> str:
        output = run(
            [
                "gpg",
                "--homedir",
                str(gnupg_home),
                "--batch",
                "--status-fd=1",
                "--verify",
                str(signature),
                str(package),
            ]
        )
        valid = [line.split() for line in output.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
        if len(valid) != 1 or len(valid[0]) < 3:
            raise RepositoryCaptureError(f"detached signature produced no unique VALIDSIG: {package.name}")
        primary = valid[0][-1].upper()
        signer = valid[0][2].upper()
        return primary if re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", primary) else signer

    return verify


def system_package_metadata(package: Path) -> tuple[str, str]:
    output = run(["pacman", "-Qp", str(package)]).strip().split()
    if len(output) != 2:
        raise RepositoryCaptureError(f"could not read package name/version: {package.name}")
    return output[0], output[1]


def parse_assignments(values: list[str]) -> dict[str, Path]:
    result = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in result:
            raise RepositoryCaptureError(f"invalid snapshot lock assignment: {value}")
        result[name] = Path(path)
    return result


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RepositoryCaptureError(f"output is a symlink: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--requested-list", type=Path)
    parser.add_argument("--snapshot-lock", action="append", default=[])
    parser.add_argument("--gnupg-home", type=Path, default=Path("/etc/pacman.d/gnupg"))
    parser.add_argument("--project-from", type=Path)
    parser.add_argument("--legacy-build-lock", type=Path)
    parser.add_argument("--package-source-lock", type=Path)
    parser.add_argument("--target-ownertrust-sha256")
    parser.add_argument("--trust-state-only", action="store_true")
    parser.add_argument("--mode", choices=("diagnostic", "qualification"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.trust_state_only:
        if (
            args.project_from is not None
            or args.legacy_build_lock is not None
            or args.package_source_lock is not None
            or args.target_ownertrust_sha256 is not None
            or args.mode is not None
            or args.mirror is not None
            or args.requested_list is not None
            or args.snapshot_lock
        ):
            raise RepositoryCaptureError("trust-state probe arguments are invalid")
        atomic_json(args.output, system_trust_state(args.gnupg_home))
        return 0
    if args.project_from is not None:
        if (
            args.legacy_build_lock is None
            or args.package_source_lock is None
            or args.mode is None
            or args.mirror is not None
            or args.requested_list is not None
            or args.snapshot_lock
        ):
            raise RepositoryCaptureError("repository projection arguments are invalid")
        regular_file(args.project_from, "source repository manifest")
        try:
            source_manifest = json.loads(args.project_from.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise RepositoryCaptureError("source repository manifest is invalid") from error
        manifest = project_repository_manifest(
            source_manifest=source_manifest,
            legacy_build_lock=args.legacy_build_lock,
            package_source_lock=args.package_source_lock,
            mode=args.mode,
            target_ownertrust_sha256=args.target_ownertrust_sha256,
        )
        atomic_json(args.output, manifest)
        return 0
    if args.mirror is None or args.requested_list is None:
        raise RepositoryCaptureError("repository capture requires mirror and requested list")
    repo_add_version = run(["repo-add", "--version"]).strip()
    manifest = capture_repository(
        mirror=args.mirror,
        requested_list=args.requested_list,
        snapshot_locks=parse_assignments(args.snapshot_lock),
        verify_signature=system_signature_verifier(args.gnupg_home),
        package_metadata=system_package_metadata,
        trust_state=system_trust_state(args.gnupg_home),
        repo_add_version=repo_add_version,
        repo_add_options=["repo-add", "offline.db.tar.gz", "<sorted-packages>"],
    )
    atomic_json(args.output, manifest)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RepositoryCaptureError as error:
        raise SystemExit(f"capture-asahi-offline-repository: {error}") from error
