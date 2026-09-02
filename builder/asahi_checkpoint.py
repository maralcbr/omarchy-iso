#!/usr/bin/env python3
"""Fail-closed content-addressed checkpoints for Apple Silicon builds."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Callable


SCHEMA_VERSION = 1
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MANIFEST_KEYS = {
    "schema_version",
    "stage",
    "mode",
    "checkpoint_identity",
    "input_digest",
    "source_lock",
    "source_commits",
    "inputs",
    "outputs",
    "validation",
    "completed_at",
    "elapsed_seconds",
    "cache_hit",
    "immutable",
}
MIGRATED_MANIFEST_KEYS = MANIFEST_KEYS | {"migration"}


class CheckpointError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


_CHUNK_BYTES = 8 * 1024 * 1024
_ZERO_CHUNK = bytes(_CHUNK_BYTES)


def sha256_file(path: Path) -> str:
    """Digest a file's complete logical byte stream.

    Holes count as the zero bytes a reader would see, so the result equals a
    plain linear read. The holes are fed to the digest from a static zero
    buffer instead of being read: a 34 GB disk image that is mostly holes
    used to cost a full pass through the shared Docker mount for nothing.
    Filesystems without SEEK_DATA/SEEK_HOLE fall back to the linear read.
    """
    digest = hashlib.sha256()
    hashed = 0

    def hash_zeros_up_to(offset: int) -> None:
        nonlocal hashed
        while hashed < offset:
            span = min(_CHUNK_BYTES, offset - hashed)
            digest.update(memoryview(_ZERO_CHUNK)[:span])
            hashed += span

    with path.open("rb", buffering=0) as stream:
        descriptor = stream.fileno()
        size = os.fstat(descriptor).st_size
        position = 0
        while position < size:
            try:
                data_offset = os.lseek(descriptor, position, os.SEEK_DATA)
            except OSError as error:
                if error.errno == errno.ENXIO:
                    # No data beyond position: the rest of the file is a hole.
                    break
                # No sparse support here: hash the remainder linearly.
                stream.seek(position)
                while chunk := stream.read(_CHUNK_BYTES):
                    digest.update(chunk)
                    hashed += len(chunk)
                position = size
                break
            if data_offset >= size:
                break
            try:
                hole_offset = os.lseek(descriptor, data_offset, os.SEEK_HOLE)
            except OSError:
                hole_offset = size
            hash_zeros_up_to(data_offset)
            stream.seek(data_offset)
            remaining = min(hole_offset, size) - data_offset
            while remaining:
                chunk = stream.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    raise CheckpointError(f"unexpected EOF while hashing {path}")
                digest.update(chunk)
                hashed += len(chunk)
                remaining -= len(chunk)
            position = hole_offset
        hash_zeros_up_to(size)
    return digest.hexdigest()


def _require_safe_name(value: str, role: str) -> None:
    if not SAFE_NAME.fullmatch(value):
        raise CheckpointError(f"unsafe {role}: {value!r}")


def _require_real_path(path: Path, role: str) -> os.stat_result:
    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise CheckpointError(f"missing {role}: {path}") from error
    if stat.S_ISLNK(status.st_mode):
        raise CheckpointError(f"symlink is forbidden for {role}: {path}")
    return status


def _path_record(
    path: Path,
    *,
    include_restore_modes: bool,
    include_executable_modes: bool = False,
) -> dict[str, Any]:
    status = _require_real_path(path, "checkpoint path")
    if stat.S_ISREG(status.st_mode):
        record: dict[str, Any] = {
            "kind": "file",
            "size_bytes": status.st_size,
            "sha256": sha256_file(path),
        }
        if include_restore_modes:
            record["restore_mode"] = stat.S_IMODE(status.st_mode)
        if include_executable_modes:
            record["executable_mode"] = stat.S_IMODE(status.st_mode) & 0o111
        return record
    if not stat.S_ISDIR(status.st_mode):
        raise CheckpointError(f"special file is forbidden in checkpoint input: {path}")

    entries: list[dict[str, Any]] = []
    total_size = 0
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        child_status = child.lstat()
        relative = child.relative_to(path).as_posix()
        if stat.S_ISLNK(child_status.st_mode):
            raise CheckpointError(f"symlink is forbidden in checkpoint tree: {child}")
        if stat.S_ISDIR(child_status.st_mode):
            entry: dict[str, Any] = {"kind": "directory", "path": relative}
            if include_restore_modes:
                entry["restore_mode"] = stat.S_IMODE(child_status.st_mode)
            if include_executable_modes:
                entry["executable_mode"] = stat.S_IMODE(child_status.st_mode) & 0o111
            entries.append(entry)
            continue
        if not stat.S_ISREG(child_status.st_mode):
            raise CheckpointError(f"special file is forbidden in checkpoint tree: {child}")
        entry = {
            "kind": "file",
            "path": relative,
            "size_bytes": child_status.st_size,
            "sha256": sha256_file(child),
        }
        if include_restore_modes:
            entry["restore_mode"] = stat.S_IMODE(child_status.st_mode)
        if include_executable_modes:
            entry["executable_mode"] = stat.S_IMODE(child_status.st_mode) & 0o111
        total_size += child_status.st_size
        entries.append(entry)

    content_entries = [_without_restore_modes(entry) for entry in entries]
    record = {
        "kind": "directory",
        "size_bytes": total_size,
        "sha256": _json_digest(content_entries),
        "entries": entries,
    }
    if include_restore_modes:
        record["restore_mode"] = stat.S_IMODE(status.st_mode)
    if include_executable_modes:
        record["executable_mode"] = stat.S_IMODE(status.st_mode) & 0o111
    return record


def _without_restore_modes(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_restore_modes(item)
            for key, item in value.items()
            if key != "restore_mode"
        }
    if isinstance(value, list):
        return [_without_restore_modes(item) for item in value]
    return value


def _without_modes(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_modes(item)
            for key, item in value.items()
            if key not in {"restore_mode", "executable_mode"}
        }
    if isinstance(value, list):
        return [_without_modes(item) for item in value]
    return value


def _content_record(record: dict[str, Any]) -> dict[str, Any]:
    return _without_modes(
        {
            key: value
            for key, value in record.items()
            if key not in {"name", "storage"}
        }
    )


def _git(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CheckpointError(f"could not inspect source repository: {repository}") from error
    return completed.stdout


def _normalized_source_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or value in {"", "."} or ".." in path.parts or ".git" in path.parts:
        raise CheckpointError(f"unsafe source manifest path: {value!r}")
    return path.as_posix()


def build_source_manifest(repository: Path, paths: list[str]) -> dict[str, Any]:
    """Capture exact dirty and untracked stage inputs without host-specific paths."""

    repository_status = _require_real_path(repository, "source repository")
    if not stat.S_ISDIR(repository_status.st_mode):
        raise CheckpointError(f"source repository is not a directory: {repository}")
    repository = repository.resolve()
    top_level = Path(_git(repository, "rev-parse", "--show-toplevel").strip()).resolve()
    if top_level != repository:
        raise CheckpointError(f"source repository must be its Git top level: {repository}")
    normalized_paths = sorted({_normalized_source_path(value) for value in paths})
    if not normalized_paths:
        raise CheckpointError("source manifest requires at least one scoped path")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for relative in normalized_paths:
        root = repository / relative
        root_status = _require_real_path(root, "source manifest input")
        candidates = [root]
        if stat.S_ISDIR(root_status.st_mode):
            candidates.extend(sorted(root.rglob("*"), key=lambda item: item.relative_to(repository).as_posix()))
        for candidate in candidates:
            candidate_status = candidate.lstat()
            candidate_relative = candidate.relative_to(repository).as_posix()
            if candidate_relative in seen:
                continue
            seen.add(candidate_relative)
            if stat.S_ISLNK(candidate_status.st_mode):
                raise CheckpointError(f"symlink is forbidden in source manifest: {candidate}")
            if stat.S_ISDIR(candidate_status.st_mode):
                continue
            if not stat.S_ISREG(candidate_status.st_mode):
                raise CheckpointError(f"special source input is forbidden: {candidate}")
            entries.append(
                {
                    "path": candidate_relative,
                    "kind": "file",
                    "executable_mode": stat.S_IMODE(candidate_status.st_mode) & 0o111,
                    "size_bytes": candidate_status.st_size,
                    "sha256": sha256_file(candidate),
                }
            )

    raw_status = _git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *normalized_paths,
    )
    status_lines = sorted(line for line in raw_status.splitlines() if line)
    commit = _git(repository, "rev-parse", "HEAD").strip()
    tree_digest = _json_digest(
        {
            "paths": normalized_paths,
            "status": status_lines,
            "entries": entries,
        }
    )
    dirty = bool(status_lines)
    state = "dirty" if dirty else "clean"
    return {
        "schema_version": SCHEMA_VERSION,
        "commit": commit,
        "dirty": dirty,
        "commit_dirty_identity": f"{commit}+{state}:{tree_digest}",
        "paths": normalized_paths,
        "status": status_lines,
        "entries": entries,
        "tree_digest": tree_digest,
    }


def build_identity(
    *,
    stage: str,
    mode: str,
    source_lock: Path,
    source_commits: dict[str, str],
    inputs: dict[str, Path],
) -> dict[str, Any]:
    _require_safe_name(stage, "checkpoint stage")
    if mode not in {"diagnostic", "qualification"}:
        raise CheckpointError(f"unsupported checkpoint mode: {mode}")
    lock_status = _require_real_path(source_lock, "source lock")
    if not stat.S_ISREG(lock_status.st_mode):
        raise CheckpointError(f"source lock is not a regular file: {source_lock}")
    if not source_commits or any(not name or not value for name, value in source_commits.items()):
        raise CheckpointError("source commits must be non-empty")

    input_records = []
    for name, path in sorted(inputs.items()):
        _require_safe_name(name, "input name")
        record = _path_record(
            path,
            include_restore_modes=False,
            include_executable_modes=True,
        )
        record["name"] = name
        record["path"] = name
        input_records.append(record)

    identity: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "mode": mode,
        "source_lock": {
            "filename": source_lock.name,
            "size_bytes": lock_status.st_size,
            "sha256": sha256_file(source_lock),
        },
        "source_commits": dict(sorted(source_commits.items())),
        "inputs": input_records,
    }
    identity["input_digest"] = _json_digest(identity)
    identity["checkpoint_identity"] = _json_digest(identity)
    return identity


def _checkpoint_directory(cache_root: Path, identity: dict[str, Any]) -> Path:
    stage = identity.get("stage")
    checkpoint_identity = identity.get("checkpoint_identity")
    if not isinstance(stage, str) or not isinstance(checkpoint_identity, str):
        raise CheckpointError("checkpoint identity is incomplete")
    _require_safe_name(stage, "checkpoint stage")
    if not re.fullmatch(r"[0-9a-f]{64}", checkpoint_identity):
        raise CheckpointError("checkpoint identity digest is invalid")
    return cache_root / "checkpoints" / stage / checkpoint_identity


def _manifest_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: manifest[key]
        for key in (
            "schema_version",
            "stage",
            "mode",
            "source_lock",
            "source_commits",
            "inputs",
            "input_digest",
            "checkpoint_identity",
        )
    }


def _assert_identity(identity: dict[str, Any]) -> None:
    expected = dict(identity)
    checkpoint_identity = expected.pop("checkpoint_identity", None)
    input_digest = expected.pop("input_digest", None)
    actual_input_digest = _json_digest(expected)
    if input_digest != actual_input_digest:
        raise CheckpointError("checkpoint input digest is mismatched")
    expected_with_input = dict(expected)
    expected_with_input["input_digest"] = input_digest
    if checkpoint_identity != _json_digest(expected_with_input):
        raise CheckpointError("checkpoint identity digest is mismatched")


def _assert_immutable_tree(path: Path) -> None:
    for node in [path, *sorted(path.rglob("*"))]:
        status = node.lstat()
        if stat.S_ISLNK(status.st_mode):
            raise CheckpointError(f"symlink is forbidden in checkpoint: {node}")
        if status.st_mode & 0o222:
            raise CheckpointError(f"checkpoint path is writable: {node}")
        if not (stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode)):
            raise CheckpointError(f"special checkpoint path is forbidden: {node}")


def _object_path(cache_root: Path, sha256: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise CheckpointError("object digest is invalid")
    return cache_root / "objects" / "sha256" / sha256[:2] / sha256


def _validated_object_path(cache_root: Path, record: dict[str, Any]) -> Path:
    storage = record.get("storage")
    if storage != {"kind": "sha256-object", "sha256": record.get("sha256")}:
        raise CheckpointError("checkpoint object storage metadata is invalid")
    path = _object_path(cache_root, record["sha256"])
    status = _require_real_path(path, "checkpoint object")
    if not stat.S_ISREG(status.st_mode):
        raise CheckpointError(f"checkpoint object is not a regular file: {path}")
    if status.st_mode & 0o222:
        raise CheckpointError(f"checkpoint object is writable: {path}")
    if status.st_size != record.get("size_bytes"):
        raise CheckpointError(f"checkpoint object digest or size mismatch: {path}")
    return path


def _verify_object(cache_root: Path, record: dict[str, Any]) -> Path:
    path = _validated_object_path(cache_root, record)
    if sha256_file(path) != record["sha256"]:
        raise CheckpointError(f"checkpoint object digest or size mismatch: {path}")
    return path


def _store_object(
    cache_root: Path,
    source: Path,
    record: dict[str, Any],
    *,
    counters: dict[str, Any] | None = None,
) -> Path:
    path = _object_path(cache_root, record["sha256"])
    if path.exists() or path.is_symlink():
        return _verify_object(
            cache_root,
            record | {"storage": {"kind": "sha256-object", "sha256": record["sha256"]}},
        )
    parent = path.parent
    if parent.is_symlink():
        raise CheckpointError(f"object directory is a symlink: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{path.name}.{os.getpid()}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise CheckpointError(f"unsafe object temporary path: {temporary}")
    try:
        # The source is hashed as it is read, so it is never opened twice. The
        # written bytes are then read back once -- that read is what detects a
        # torn or short write, and dropping it would weaken the guarantee
        # rather than speed it up.
        streamed = _copy_sparse_file(source, temporary, counters=counters)
        os.chmod(temporary, stat.S_IMODE(temporary.stat().st_mode) & ~0o222)
        if streamed != record["sha256"]:
            raise CheckpointError(f"copied checkpoint object differs from source: {source}")
        if temporary.stat().st_size != record["size_bytes"] or sha256_file(temporary) != record["sha256"]:
            raise CheckpointError(f"copied checkpoint object differs from source: {source}")
        try:
            os.replace(temporary, path)
        except OSError:
            if not path.exists():
                raise
    finally:
        if temporary.exists():
            temporary.unlink()
    # Rename does not alter content, and the bytes behind this name were just
    # read back and matched. Re-hashing the same inode here would be a third
    # pass proving nothing new, so only the metadata invariants are rechecked.
    return _validated_object_path(
        cache_root,
        record | {"storage": {"kind": "sha256-object", "sha256": record["sha256"]}},
    )


def verify_checkpoint(
    cache_root: Path,
    identity: dict[str, Any],
    *,
    verify_object_content: bool = True,
) -> dict[str, Any]:
    """Verify a checkpoint's metadata, immutability, and stored content.

    Standalone callers get the full check, including a digest read of every
    file object. Restore passes verify_object_content=False because it
    authenticates each object's bytes while streaming them to the destination;
    reading them here as well would be the same bytes twice. Every other check,
    including the storage binding, type, mode and size of each object, is
    unchanged either way.
    """
    _assert_identity(identity)
    checkpoint = _checkpoint_directory(cache_root, identity)
    _require_real_path(checkpoint, "checkpoint directory")
    manifest_path = checkpoint / "manifest.json"
    manifest_status = _require_real_path(manifest_path, "checkpoint manifest")
    if not stat.S_ISREG(manifest_status.st_mode):
        raise CheckpointError(f"checkpoint manifest is not a regular file: {manifest_path}")
    if manifest_status.st_mode & 0o222:
        raise CheckpointError(f"checkpoint manifest is writable: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CheckpointError(f"checkpoint manifest is unreadable: {manifest_path}") from error
    manifest_keys = frozenset(manifest)
    if manifest_keys not in {
        frozenset(MANIFEST_KEYS),
        frozenset(MIGRATED_MANIFEST_KEYS),
    }:
        raise CheckpointError("checkpoint manifest has unknown or missing fields")
    if _manifest_identity(manifest) != identity:
        raise CheckpointError("checkpoint manifest identity is stale or mismatched")
    if manifest["validation"] != {"result": "passed"}:
        raise CheckpointError("checkpoint validation result is not passed")
    if manifest["cache_hit"] is not False or manifest["immutable"] is not True:
        raise CheckpointError("checkpoint immutability metadata is invalid")
    if "migration" in manifest:
        migration = manifest["migration"]
        if not isinstance(migration, dict) or set(migration) != {
            "source_checkpoint_identity",
            "reason",
            "transition_digest",
        }:
            raise CheckpointError("checkpoint migration metadata is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", migration["source_checkpoint_identity"]) is None:
            raise CheckpointError("checkpoint migration source identity is invalid")
        _require_safe_name(migration["reason"], "checkpoint migration reason")
        if re.fullmatch(r"[0-9a-f]{64}", migration["transition_digest"]) is None:
            raise CheckpointError("checkpoint migration transition digest is invalid")
    if not isinstance(manifest["elapsed_seconds"], (int, float)) or manifest["elapsed_seconds"] < 0:
        raise CheckpointError("checkpoint elapsed time is invalid")

    outputs_directory = checkpoint / "outputs"
    _require_real_path(outputs_directory, "checkpoint outputs")
    expected_inline_names = set()
    expected_names = set()
    for expected in manifest["outputs"]:
        name = expected.get("name")
        if not isinstance(name, str):
            raise CheckpointError("checkpoint output name is invalid")
        _require_safe_name(name, "output name")
        if name in expected_names:
            raise CheckpointError(f"duplicate checkpoint output: {name}")
        expected_names.add(name)
        if expected.get("kind") == "file":
            if verify_object_content:
                _verify_object(cache_root, expected)
            else:
                _validated_object_path(cache_root, expected)
        else:
            expected_inline_names.add(name)
            actual = _path_record(outputs_directory / name, include_restore_modes=False)
            if _content_record(expected) != actual:
                raise CheckpointError(f"checkpoint output digest or size mismatch: {name}")
    actual_names = {item.name for item in outputs_directory.iterdir()}
    if actual_names != expected_inline_names:
        raise CheckpointError("checkpoint output set is stale or mismatched")
    _assert_immutable_tree(checkpoint)
    result = dict(manifest)
    result["manifest_path"] = str(manifest_path)
    return result


def _transfer_counters() -> dict[str, Any]:
    """Return a fresh accumulator for one run's transfer accounting.

    ``bytes_read`` and ``bytes_written`` measure the streaming copy seam and
    nothing else: the logical bytes a copy consumed from its source and the
    logical bytes it materialized at its destination, holes counted as the
    zero bytes a reader sees. A store that finds its object already present
    copies nothing and truthfully records zero transfer; the digest reads that
    decided that are verification work and are timed, not counted as bytes
    moved. ``transfer_seconds`` is the wall time those copies spent, hashing
    included, because the hash rides the same pass as the copy.
    """
    return {"bytes_read": 0, "bytes_written": 0, "transfer_seconds": 0.0}


def _copy_sparse_file(
    source: Path,
    destination: Path,
    *,
    counters: dict[str, Any] | None = None,
) -> str:
    """Copy a regular file preserving holes, hashing what is read on the way.

    Returns the sha256 of the source's complete logical byte stream, holes
    counted as the zero bytes a reader would see. That is exactly what
    sha256_file computes for the same file, so a caller can authenticate what
    it copied without opening the source a second time.

    A caller that passes counters gets this copy's measured logical byte
    counts and elapsed time added to them.
    """
    transfer_started = time.perf_counter()
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_status = source.stat()
    size = source_status.st_size
    digest = hashlib.sha256()
    hashed = 0

    def hash_holes_up_to(offset: int) -> None:
        nonlocal hashed
        while hashed < offset:
            span = min(8 * 1024 * 1024, offset - hashed)
            digest.update(bytes(span))
            hashed += span

    with source.open("rb", buffering=0) as source_stream, destination.open("wb", buffering=0) as destination_stream:
        destination_stream.truncate(size)
        position = 0
        while position < size:
            try:
                data_offset = os.lseek(source_stream.fileno(), position, os.SEEK_DATA)
            except OSError:
                # No sparse support here: copy and hash the remainder linearly.
                hash_holes_up_to(position)
                source_stream.seek(position)
                destination_stream.seek(position)
                while chunk := source_stream.read(8 * 1024 * 1024):
                    destination_stream.write(chunk)
                    digest.update(chunk)
                    hashed += len(chunk)
                break
            if data_offset >= size:
                break
            try:
                hole_offset = os.lseek(source_stream.fileno(), data_offset, os.SEEK_HOLE)
            except OSError:
                hole_offset = size
            hash_holes_up_to(data_offset)
            source_stream.seek(data_offset)
            destination_stream.seek(data_offset)
            remaining = min(hole_offset, size) - data_offset
            while remaining:
                chunk = source_stream.read(min(8 * 1024 * 1024, remaining))
                if not chunk:
                    raise CheckpointError(f"unexpected EOF while copying {source}")
                destination_stream.write(chunk)
                digest.update(chunk)
                hashed += len(chunk)
                remaining -= len(chunk)
            position = hole_offset
    hash_holes_up_to(size)
    os.chmod(destination, stat.S_IMODE(source_status.st_mode))
    if counters is not None:
        # hashed is the count of logical source bytes actually fed to the
        # digest, and truncate established the destination's logical size, so
        # both numbers are measured rather than assumed.
        counters["bytes_read"] += hashed
        counters["bytes_written"] += destination.stat().st_size
        counters["transfer_seconds"] += time.perf_counter() - transfer_started
    return digest.hexdigest()


def _copy_path(
    source: Path,
    destination: Path,
    *,
    counters: dict[str, Any] | None = None,
) -> None:
    status = _require_real_path(source, "copy source")
    if stat.S_ISREG(status.st_mode):
        _copy_sparse_file(source, destination, counters=counters)
        return
    if not stat.S_ISDIR(status.st_mode):
        raise CheckpointError(f"special copy source is forbidden: {source}")
    # Populate through a private writable mode, then reproduce the source
    # directory mode. Immutable checkpoint directories are intentionally not
    # writable, so applying their final mode before their children are copied
    # makes an otherwise valid inline-directory rekey impossible.
    destination.mkdir(parents=True, mode=0o700)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        _copy_path(child, destination / child.name, counters=counters)
    os.chmod(destination, stat.S_IMODE(status.st_mode))


def _make_immutable(path: Path) -> None:
    nodes = [path, *sorted(path.rglob("*"), reverse=True)]
    for node in nodes:
        mode = stat.S_IMODE(node.lstat().st_mode)
        os.chmod(node, mode & ~0o222)


def _atomic_json(path: Path, value: dict[str, Any], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise CheckpointError(f"JSON destination is a symlink: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _run_record(
    *,
    manifest: dict[str, Any],
    cache_hit: bool,
    elapsed_seconds: float,
    manifest_path: Path,
    counters: dict[str, Any],
    checkpoint_verification_seconds: float,
    content_readback_seconds: float,
) -> dict[str, Any]:
    """Build the run record for one store or restore.

    The accounting fields are measured, not estimated, and each names exactly
    one seam so a reader cannot mistake it for a total:

    - bytes_read / bytes_written: the streaming copies only, as described on
      the counter factory.
    - checkpoint_verification_seconds: the metadata, immutability and storage
      binding pass over the checkpoint.
    - content_readback_seconds: the digest reads that authenticate content
      outside that pass -- the content address computed before a store and the
      read-back of what was written or restored.
    - transfer_seconds: the streaming copies, hashing included.

    The three durations do not add up to elapsed_seconds and are not meant to:
    identity work, renames and mode restoration sit outside all three.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": manifest["stage"],
        "mode": manifest["mode"],
        "checkpoint_identity": manifest["checkpoint_identity"],
        "input_digest": manifest["input_digest"],
        "source_lock": manifest["source_lock"],
        "source_commits": manifest["source_commits"],
        "inputs": manifest["inputs"],
        "outputs": manifest["outputs"],
        "validation": {"result": "passed"},
        "completed_at": _utc_now(),
        "elapsed_seconds": elapsed_seconds,
        "cache_hit": cache_hit,
        "bytes_read": counters["bytes_read"],
        "bytes_written": counters["bytes_written"],
        "verification_timing": {
            "checkpoint_verification_seconds": max(
                0.0, checkpoint_verification_seconds
            ),
            "content_readback_seconds": max(0.0, content_readback_seconds),
            "transfer_seconds": max(0.0, counters["transfer_seconds"]),
        },
        "checkpoint_manifest": str(manifest_path),
    }


def store_checkpoint(
    *,
    cache_root: Path,
    identity: dict[str, Any],
    outputs: dict[str, Path],
    elapsed_seconds: float,
    run_manifest: Path | None = None,
) -> dict[str, Any]:
    _assert_identity(identity)
    if elapsed_seconds < 0:
        raise CheckpointError("elapsed time must not be negative")
    if not outputs:
        raise CheckpointError("checkpoint requires at least one output")
    checkpoint = _checkpoint_directory(cache_root, identity)
    manifest_path = checkpoint / "manifest.json"
    counters = _transfer_counters()
    content_readback_seconds = 0.0
    if checkpoint.exists() or checkpoint.is_symlink():
        verification_started = time.perf_counter()
        manifest = verify_checkpoint(cache_root, identity)
        checkpoint_verification_seconds = time.perf_counter() - verification_started
        expected = {record["name"]: record for record in manifest["outputs"]}
        for name, source in sorted(outputs.items()):
            _require_safe_name(name, "output name")
            readback_started = time.perf_counter()
            actual = _path_record(source, include_restore_modes=False)
            content_readback_seconds += time.perf_counter() - readback_started
            if name not in expected or _content_record(expected[name]) != actual:
                raise CheckpointError(f"existing checkpoint output differs from rebuilt output: {name}")
        if set(expected) != set(outputs):
            raise CheckpointError("existing checkpoint output set differs from rebuilt output")
        # Nothing was copied on this path, so the transfer counters stay zero
        # and say so. reproducibility_match is what tells a reader the rebuilt
        # bytes were compared rather than moved.
        record = _run_record(
            manifest=manifest,
            cache_hit=False,
            elapsed_seconds=elapsed_seconds,
            manifest_path=manifest_path,
            counters=counters,
            checkpoint_verification_seconds=checkpoint_verification_seconds,
            content_readback_seconds=content_readback_seconds,
        )
        record["reproducibility_match"] = True
        if run_manifest is not None:
            _atomic_json(run_manifest, record)
        return record | {"manifest_path": str(manifest_path)}

    stage_directory = checkpoint.parent
    if stage_directory.is_symlink():
        raise CheckpointError(f"checkpoint stage directory is a symlink: {stage_directory}")
    stage_directory.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{identity['checkpoint_identity']}.", dir=stage_directory))
    try:
        output_directory = temporary / "outputs"
        output_directory.mkdir()
        output_records = []
        for name, source in sorted(outputs.items()):
            _require_safe_name(name, "output name")
            address_started = time.perf_counter()
            record = _path_record(source, include_restore_modes=True)
            content_readback_seconds += time.perf_counter() - address_started
            record["name"] = name
            if record["kind"] == "file":
                record["storage"] = {
                    "kind": "sha256-object",
                    "sha256": record["sha256"],
                }
                # Whatever this call spends beyond its copy is digest work:
                # the read-back of a freshly written object, or the digest of
                # an object that was already present and is reused instead.
                store_started = time.perf_counter()
                transfer_before = counters["transfer_seconds"]
                _store_object(cache_root, source, record, counters=counters)
                content_readback_seconds += max(
                    0.0,
                    (time.perf_counter() - store_started)
                    - (counters["transfer_seconds"] - transfer_before),
                )
            else:
                record["storage"] = {"kind": "inline-directory"}
                _copy_path(source, output_directory / name, counters=counters)
            output_records.append(record)

        manifest = dict(identity)
        manifest.update(
            {
                "outputs": output_records,
                "validation": {"result": "passed"},
                "completed_at": _utc_now(),
                "elapsed_seconds": elapsed_seconds,
                "cache_hit": False,
                "immutable": True,
            }
        )
        _atomic_json(temporary / "manifest.json", manifest, mode=0o444)
        _make_immutable(temporary)
        os.replace(temporary, checkpoint)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise

    # Every file object reached the store through one of two paths, and both
    # already authenticated its bytes: a freshly written object was read back
    # before it was renamed into place, and an object that was already present
    # was digested on the deduplication path. Re-reading them all here would be
    # a second pass over bytes just verified. The manifest, immutability, and
    # inline output trees are still fully checked.
    verification_started = time.perf_counter()
    verified = verify_checkpoint(cache_root, identity, verify_object_content=False)
    checkpoint_verification_seconds = time.perf_counter() - verification_started
    record = _run_record(
        manifest=verified,
        cache_hit=False,
        elapsed_seconds=elapsed_seconds,
        manifest_path=manifest_path,
        counters=counters,
        checkpoint_verification_seconds=checkpoint_verification_seconds,
        content_readback_seconds=content_readback_seconds,
    )
    if run_manifest is not None:
        _atomic_json(run_manifest, record)
    return record | {"manifest_path": str(manifest_path)}


def _input_records_by_name(identity: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for record in identity["inputs"]:
        name = record.get("name")
        if not isinstance(name, str) or name in records:
            raise CheckpointError("checkpoint input names are invalid or duplicated")
        records[name] = record
    return records


def _comparable_input(record: dict[str, Any]) -> dict[str, Any]:
    return _without_modes(
        {
            key: value
            for key, value in record.items()
            if key not in {"name", "path"}
        }
    )


def seal_legacy_checkpoint(
    *,
    cache_root: Path,
    identity: dict[str, Any],
    expected_manifest: dict[str, Any],
    expected_outputs: dict[str, dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
    """Admit exact legacy bytes by removing write bits only after full proof."""
    _assert_identity(identity)
    _require_safe_name(reason, "legacy checkpoint admission reason")
    if set(expected_manifest) != {"sha256", "size_bytes"}:
        raise CheckpointError("legacy manifest expectation is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", expected_manifest.get("sha256", "")) is None:
        raise CheckpointError("legacy manifest digest is invalid")
    if not isinstance(expected_manifest.get("size_bytes"), int) or expected_manifest[
        "size_bytes"
    ] <= 0:
        raise CheckpointError("legacy manifest size is invalid")

    checkpoint_directory = _checkpoint_directory(cache_root, identity)
    checkpoint_status = _require_real_path(
        checkpoint_directory, "legacy checkpoint directory"
    )
    if not stat.S_ISDIR(checkpoint_status.st_mode):
        raise CheckpointError("legacy checkpoint path is not a directory")
    manifest_path = checkpoint_directory / "manifest.json"
    manifest_status = _require_real_path(manifest_path, "legacy checkpoint manifest")
    if not stat.S_ISREG(manifest_status.st_mode):
        raise CheckpointError("legacy checkpoint manifest is not a file")
    manifest_bytes = manifest_path.read_bytes()
    if (
        len(manifest_bytes) != expected_manifest["size_bytes"]
        or hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest["sha256"]
    ):
        raise CheckpointError("legacy manifest digest or size mismatch")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise CheckpointError("legacy checkpoint manifest is invalid JSON") from error
    if set(manifest) != MANIFEST_KEYS:
        raise CheckpointError("legacy checkpoint manifest schema is invalid")
    if _manifest_identity(manifest) != identity:
        raise CheckpointError("legacy checkpoint manifest identity is mismatched")
    if manifest.get("validation") != {"result": "passed"}:
        raise CheckpointError("legacy checkpoint validation is not passed")
    if manifest.get("cache_hit") is not False or manifest.get("immutable") is not True:
        raise CheckpointError("legacy checkpoint metadata is invalid")

    output_records = {record.get("name"): record for record in manifest["outputs"]}
    if None in output_records or len(output_records) != len(manifest["outputs"]):
        raise CheckpointError("legacy checkpoint output names are invalid")
    if set(output_records) != set(expected_outputs):
        raise CheckpointError("legacy checkpoint output set differs")
    paths_to_seal: set[Path] = {checkpoint_directory, manifest_path}
    outputs_directory = checkpoint_directory / "outputs"
    outputs_status = _require_real_path(outputs_directory, "legacy checkpoint outputs")
    if not stat.S_ISDIR(outputs_status.st_mode):
        raise CheckpointError("legacy checkpoint outputs path is not a directory")
    paths_to_seal.add(outputs_directory)
    expected_inline_names = set()
    for name, record in sorted(output_records.items()):
        _require_safe_name(name, "legacy checkpoint output name")
        expectation = expected_outputs[name]
        if set(expectation) != {"sha256", "size_bytes"}:
            raise CheckpointError(f"legacy output expectation is invalid: {name}")
        if (
            record.get("sha256") != expectation["sha256"]
            or record.get("size_bytes") != expectation["size_bytes"]
        ):
            raise CheckpointError(f"legacy output declaration differs: {name}")
        if record.get("kind") == "file":
            if record.get("storage") != {
                "kind": "sha256-object",
                "sha256": record.get("sha256"),
            }:
                raise CheckpointError(f"legacy object storage is invalid: {name}")
            object_path = _object_path(cache_root, record["sha256"])
            object_status = _require_real_path(object_path, "legacy checkpoint object")
            if not stat.S_ISREG(object_status.st_mode):
                raise CheckpointError(f"legacy checkpoint object is not a file: {name}")
            if (
                object_status.st_size != record["size_bytes"]
                or sha256_file(object_path) != record["sha256"]
            ):
                raise CheckpointError(f"legacy checkpoint object differs: {name}")
            paths_to_seal.add(object_path)
        else:
            expected_inline_names.add(name)
            inline_path = outputs_directory / name
            if _path_record(inline_path, include_restore_modes=False) != _content_record(
                record
            ):
                raise CheckpointError(f"legacy inline output differs: {name}")
            paths_to_seal.update([inline_path, *inline_path.rglob("*")])
    if {path.name for path in outputs_directory.iterdir()} != expected_inline_names:
        raise CheckpointError("legacy inline output set differs")

    for path in sorted(paths_to_seal, key=lambda item: len(item.parts), reverse=True):
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode) or not (
            stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)
        ):
            raise CheckpointError(f"unsafe legacy checkpoint path: {path}")
        os.chmod(path, stat.S_IMODE(status.st_mode) & ~0o222)
    return verify_checkpoint(cache_root, identity)


def rekey_checkpoint(
    *,
    cache_root: Path,
    source_identity: dict[str, Any],
    target_identity: dict[str, Any],
    equivalent_inputs: dict[str, str],
    allowed_added_inputs: set[str],
    allowed_removed_inputs: set[str],
    allow_source_lock_change: bool,
    allow_source_commits_change: bool,
    expected_outputs: dict[str, dict[str, Any]],
    reason: str,
    projected_equivalent_inputs: dict[str, str] | None = None,
    projected_equivalence_verifier: Callable[
        [dict[str, Any], dict[str, Any]], dict[str, Any]
    ]
    | None = None,
) -> dict[str, Any]:
    """Rekey immutable outputs only across one fully declared identity transition."""
    _assert_identity(source_identity)
    _assert_identity(target_identity)
    _require_safe_name(reason, "checkpoint migration reason")
    if source_identity["stage"] != target_identity["stage"]:
        raise CheckpointError("checkpoint rekey cannot cross stages")
    if source_identity["mode"] != target_identity["mode"]:
        raise CheckpointError("checkpoint rekey cannot cross build modes")
    if source_identity["checkpoint_identity"] == target_identity["checkpoint_identity"]:
        raise CheckpointError("checkpoint rekey source and target are identical")

    source_lock_changed = source_identity["source_lock"] != target_identity["source_lock"]
    if source_lock_changed and not allow_source_lock_change:
        raise CheckpointError("source lock transition is not allowed")
    commits_changed = source_identity["source_commits"] != target_identity["source_commits"]
    if commits_changed and not allow_source_commits_change:
        raise CheckpointError("source commit transition is not allowed")

    source_inputs = _input_records_by_name(source_identity)
    target_inputs = _input_records_by_name(target_identity)
    projected_equivalent_inputs = projected_equivalent_inputs or {}
    if projected_equivalent_inputs and projected_equivalence_verifier is None:
        raise CheckpointError("projected input equivalence requires an executed verifier")
    if not projected_equivalent_inputs and projected_equivalence_verifier is not None:
        raise CheckpointError("projected input verifier has no declared inputs")
    if set(equivalent_inputs) & set(projected_equivalent_inputs):
        raise CheckpointError("input cannot use exact and projected equivalence")
    exact_targets = set(equivalent_inputs.values())
    projected_targets = set(projected_equivalent_inputs.values())
    if exact_targets & projected_targets:
        raise CheckpointError("target input cannot use exact and projected equivalence")
    if set(equivalent_inputs) - set(source_inputs):
        raise CheckpointError("equivalent source input is missing")
    if len(set(equivalent_inputs.values())) != len(equivalent_inputs):
        raise CheckpointError("equivalent target inputs are duplicated")
    if set(equivalent_inputs.values()) - set(target_inputs):
        raise CheckpointError("equivalent target input is missing")
    for source_name, target_name in sorted(equivalent_inputs.items()):
        if _comparable_input(source_inputs[source_name]) != _comparable_input(
            target_inputs[target_name]
        ):
            raise CheckpointError(
                f"equivalent input differs: {source_name} -> {target_name}"
            )
    if set(projected_equivalent_inputs) - set(source_inputs):
        raise CheckpointError("projected source input is missing")
    if len(projected_targets) != len(projected_equivalent_inputs):
        raise CheckpointError("projected target inputs are duplicated")
    if projected_targets - set(target_inputs):
        raise CheckpointError("projected target input is missing")
    projection_proofs = {}
    if projected_equivalence_verifier is not None:
        for source_name, target_name in sorted(projected_equivalent_inputs.items()):
            proof = projected_equivalence_verifier(
                source_inputs[source_name], target_inputs[target_name]
            )
            if not isinstance(proof, dict) or set(proof) != {
                "kind",
                "proof_digest",
            }:
                raise CheckpointError("projected input verifier returned an invalid proof")
            kind = proof.get("kind")
            proof_digest = proof.get("proof_digest")
            if not isinstance(kind, str) or not SAFE_NAME.fullmatch(kind):
                raise CheckpointError("projected input verifier returned an invalid proof kind")
            if not isinstance(proof_digest, str) or re.fullmatch(
                r"[0-9a-f]{64}", proof_digest
            ) is None:
                raise CheckpointError("projected input verifier returned an invalid projection proof")
            projection_proofs[f"{source_name}->{target_name}"] = proof
    covered_source_inputs = set(equivalent_inputs) | set(projected_equivalent_inputs)
    covered_target_inputs = exact_targets | projected_targets
    actual_removed = set(source_inputs) - covered_source_inputs
    if actual_removed != allowed_removed_inputs:
        raise CheckpointError("removed input allowlist is incomplete or excessive")
    actual_added = set(target_inputs) - covered_target_inputs
    if actual_added != allowed_added_inputs:
        raise CheckpointError("added input allowlist is incomplete or excessive")

    source_manifest = verify_checkpoint(cache_root, source_identity)
    outputs = {record["name"]: record for record in source_manifest["outputs"]}
    if set(outputs) != set(expected_outputs):
        raise CheckpointError("expected output set does not match source checkpoint")
    for name, expectation in sorted(expected_outputs.items()):
        if set(expectation) != {"sha256", "size_bytes"}:
            raise CheckpointError(f"expected output declaration is invalid: {name}")
        if (
            outputs[name].get("sha256") != expectation["sha256"]
            or outputs[name].get("size_bytes") != expectation["size_bytes"]
        ):
            raise CheckpointError(f"expected output digest or size mismatch: {name}")

    target_checkpoint = _checkpoint_directory(cache_root, target_identity)
    if target_checkpoint.exists() or target_checkpoint.is_symlink():
        return verify_checkpoint(cache_root, target_identity)
    stage_directory = target_checkpoint.parent
    if stage_directory.is_symlink():
        raise CheckpointError(f"checkpoint stage directory is a symlink: {stage_directory}")
    stage_directory.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{target_identity['checkpoint_identity']}.",
            dir=stage_directory,
        )
    )
    try:
        output_directory = temporary / "outputs"
        output_directory.mkdir()
        for name, record in sorted(outputs.items()):
            if record["kind"] == "file":
                _verify_object(cache_root, record)
            else:
                source = Path(source_manifest["manifest_path"]).parent / "outputs" / name
                _copy_path(source, output_directory / name)
        transition = {
            "source_identity": source_identity,
            "target_identity": target_identity,
            "equivalent_inputs": dict(sorted(equivalent_inputs.items())),
            "projected_equivalent_inputs": dict(
                sorted(projected_equivalent_inputs.items())
            ),
            "projection_proofs": projection_proofs,
            "allowed_added_inputs": sorted(allowed_added_inputs),
            "allowed_removed_inputs": sorted(allowed_removed_inputs),
            "allow_source_lock_change": allow_source_lock_change,
            "allow_source_commits_change": allow_source_commits_change,
            "expected_outputs": expected_outputs,
            "reason": reason,
        }
        manifest = dict(target_identity)
        manifest.update(
            {
                "outputs": source_manifest["outputs"],
                "validation": {"result": "passed"},
                "completed_at": _utc_now(),
                "elapsed_seconds": 0.0,
                "cache_hit": False,
                "immutable": True,
                "migration": {
                    "source_checkpoint_identity": source_identity[
                        "checkpoint_identity"
                    ],
                    "reason": reason,
                    "transition_digest": _json_digest(transition),
                },
            }
        )
        _atomic_json(temporary / "manifest.json", manifest, mode=0o444)
        _make_immutable(temporary)
        os.replace(temporary, target_checkpoint)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    return verify_checkpoint(cache_root, target_identity)


def _restore_modes(destination: Path, record: dict[str, Any]) -> None:
    if record["kind"] == "file":
        os.chmod(destination, record["restore_mode"])
        return
    by_path = {entry["path"]: entry for entry in record["entries"]}
    for relative, entry in sorted(by_path.items(), key=lambda item: item[0].count("/"), reverse=True):
        os.chmod(destination / relative, entry["restore_mode"])
    os.chmod(destination, record["restore_mode"])


def _remove_restore_temporary(path: Path) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        path.unlink()
        return

    os.chmod(path, stat.S_IMODE(status.st_mode) | 0o700)
    for directory, directory_names, _files in os.walk(
        path,
        topdown=True,
        followlinks=False,
    ):
        directory_path = Path(directory)
        for name in directory_names:
            child = directory_path / name
            child_status = child.lstat()
            if stat.S_ISDIR(child_status.st_mode) and not stat.S_ISLNK(
                child_status.st_mode
            ):
                os.chmod(child, stat.S_IMODE(child_status.st_mode) | 0o700)
    shutil.rmtree(path)


def restore_checkpoint(
    *,
    cache_root: Path,
    identity: dict[str, Any],
    destinations: dict[str, Path],
    run_manifest: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    counters = _transfer_counters()
    content_readback_seconds = 0.0
    # Object content is authenticated while it is streamed to the destination
    # below, so it is not read here as well. Everything else this checks --
    # manifest identity, immutability, inline output trees, each object's
    # storage binding, type, mode and size -- is unchanged.
    verification_started = time.perf_counter()
    verified = verify_checkpoint(cache_root, identity, verify_object_content=False)
    checkpoint_verification_seconds = time.perf_counter() - verification_started
    verified_at = time.monotonic()
    manifest_path = Path(verified["manifest_path"])
    expected = {record["name"]: record for record in verified["outputs"]}
    if set(destinations) != set(expected):
        raise CheckpointError("restore destination set does not match checkpoint outputs")
    restored: list[Path] = []
    try:
        for name, destination in sorted(destinations.items()):
            if destination.exists() or destination.is_symlink():
                raise CheckpointError(f"unsafe existing restore destination: {destination}")
            _require_safe_name(name, "output name")
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.restore")
            if temporary.exists() or temporary.is_symlink():
                raise CheckpointError(f"unsafe temporary restore destination: {temporary}")
            try:
                if expected[name]["kind"] == "file":
                    # Recheck the storage binding, type, mode and size, then
                    # authenticate the bytes while copying them: one read of the
                    # object, not one to verify and another to copy. A mismatch
                    # refuses here, before the temporary is ever renamed into
                    # place, and the destination digest below still detects a
                    # torn or altered write.
                    source = _validated_object_path(cache_root, expected[name])
                    streamed = _copy_sparse_file(source, temporary, counters=counters)
                    if streamed != expected[name]["sha256"]:
                        raise CheckpointError(
                            f"checkpoint object digest or size mismatch: {source}"
                        )
                else:
                    source = manifest_path.parent / "outputs" / name
                    _copy_path(source, temporary, counters=counters)
                _restore_modes(temporary, expected[name])
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, destination)
            finally:
                _remove_restore_temporary(temporary)
            restored.append(destination)
            readback_started = time.perf_counter()
            actual = _path_record(destination, include_restore_modes=False)
            content_readback_seconds += time.perf_counter() - readback_started
            expected_content = _content_record(expected[name])
            if actual != expected_content:
                raise CheckpointError(f"restored output digest or size mismatch: {name}")
    except Exception:
        for destination in restored:
            if destination.is_dir():
                shutil.rmtree(destination, ignore_errors=True)
            elif destination.exists():
                destination.unlink()
        raise

    completed_at = time.monotonic()
    record = _run_record(
        manifest=verified,
        cache_hit=True,
        elapsed_seconds=completed_at - started,
        manifest_path=manifest_path,
        counters=counters,
        checkpoint_verification_seconds=checkpoint_verification_seconds,
        content_readback_seconds=content_readback_seconds,
    )
    record["cache_hit_timing"] = {
        "lookup_and_verification_seconds": verified_at - started,
        "materialization_and_readback_seconds": completed_at - verified_at,
    }
    if run_manifest is not None:
        _atomic_json(run_manifest, record)
    return record | {"manifest_path": str(manifest_path)}


def _parse_assignments(values: list[str]) -> dict[str, str]:
    parsed = {}
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name or not item:
            raise CheckpointError(f"expected NAME=VALUE: {value!r}")
        if name in parsed:
            raise CheckpointError(f"duplicate assignment: {name}")
        parsed[name] = item
    return parsed


def _load_identity(path: Path) -> dict[str, Any]:
    status = _require_real_path(path, "identity file")
    if not stat.S_ISREG(status.st_mode):
        raise CheckpointError(f"identity is not a regular file: {path}")
    identity = json.loads(path.read_text())
    _assert_identity(identity)
    return identity


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    identity_parser = subparsers.add_parser("identity")
    identity_parser.add_argument("--stage", required=True)
    identity_parser.add_argument("--mode", required=True)
    identity_parser.add_argument("--source-lock", type=Path, required=True)
    identity_parser.add_argument("--source", action="append", default=[])
    identity_parser.add_argument("--input", action="append", default=[])

    source_parser = subparsers.add_parser("source-manifest")
    source_parser.add_argument("--repo-root", type=Path, required=True)
    source_parser.add_argument("--path", action="append", default=[])

    for command in ("store", "restore", "verify"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--cache-root", type=Path, required=True)
        command_parser.add_argument("--identity", type=Path, required=True)
        if command == "store":
            command_parser.add_argument("--output", action="append", default=[])
            command_parser.add_argument("--elapsed-seconds", type=float, required=True)
            command_parser.add_argument("--run-manifest", type=Path)
        elif command == "restore":
            command_parser.add_argument("--destination", action="append", default=[])
            command_parser.add_argument("--run-manifest", type=Path)

    seal_parser = subparsers.add_parser("seal-legacy")
    seal_parser.add_argument("--cache-root", type=Path, required=True)
    seal_parser.add_argument("--manifest", type=Path, required=True)
    seal_parser.add_argument("--expected-manifest", required=True)
    seal_parser.add_argument("--expected-output", action="append", default=[])
    seal_parser.add_argument("--reason", required=True)

    rekey_parser = subparsers.add_parser("rekey")
    rekey_parser.add_argument("--cache-root", type=Path, required=True)
    rekey_parser.add_argument("--source-identity", type=Path, required=True)
    rekey_parser.add_argument("--target-identity", type=Path, required=True)
    rekey_parser.add_argument("--equivalent-input", action="append", default=[])
    rekey_parser.add_argument("--allow-added-input", action="append", default=[])
    rekey_parser.add_argument("--allow-removed-input", action="append", default=[])
    rekey_parser.add_argument("--allow-source-lock-change", action="store_true")
    rekey_parser.add_argument("--allow-source-commits-change", action="store_true")
    rekey_parser.add_argument("--expected-output", action="append", default=[])
    rekey_parser.add_argument("--reason", required=True)

    args = parser.parse_args()
    if args.command == "source-manifest":
        print(
            json.dumps(
                build_source_manifest(args.repo_root, args.path),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "identity":
        sources = _parse_assignments(args.source)
        inputs = {name: Path(value) for name, value in _parse_assignments(args.input).items()}
        print(
            json.dumps(
                build_identity(
                    stage=args.stage,
                    mode=args.mode,
                    source_lock=args.source_lock,
                    source_commits=sources,
                    inputs=inputs,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.command == "rekey":
        expected_outputs = {}
        for name, value in _parse_assignments(args.expected_output).items():
            digest, separator, size_text = value.partition(":")
            if (
                not separator
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or not size_text.isdigit()
            ):
                raise CheckpointError(
                    f"expected output must be NAME=SHA256:SIZE: {name}={value}"
                )
            expected_outputs[name] = {
                "sha256": digest,
                "size_bytes": int(size_text),
            }
        result = rekey_checkpoint(
            cache_root=args.cache_root,
            source_identity=_load_identity(args.source_identity),
            target_identity=_load_identity(args.target_identity),
            equivalent_inputs=_parse_assignments(args.equivalent_input),
            allowed_added_inputs=set(args.allow_added_input),
            allowed_removed_inputs=set(args.allow_removed_input),
            allow_source_lock_change=args.allow_source_lock_change,
            allow_source_commits_change=args.allow_source_commits_change,
            expected_outputs=expected_outputs,
            reason=args.reason,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "seal-legacy":
        digest, separator, size_text = args.expected_manifest.partition(":")
        if (
            not separator
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not size_text.isdigit()
        ):
            raise CheckpointError("expected manifest must be SHA256:SIZE")
        manifest = json.loads(args.manifest.read_text())
        expected_outputs = {}
        for name, value in _parse_assignments(args.expected_output).items():
            output_digest, output_separator, output_size = value.partition(":")
            if (
                not output_separator
                or re.fullmatch(r"[0-9a-f]{64}", output_digest) is None
                or not output_size.isdigit()
            ):
                raise CheckpointError(
                    f"expected output must be NAME=SHA256:SIZE: {name}={value}"
                )
            expected_outputs[name] = {
                "sha256": output_digest,
                "size_bytes": int(output_size),
            }
        result = seal_legacy_checkpoint(
            cache_root=args.cache_root,
            identity=_manifest_identity(manifest),
            expected_manifest={"sha256": digest, "size_bytes": int(size_text)},
            expected_outputs=expected_outputs,
            reason=args.reason,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    identity = _load_identity(args.identity)
    if args.command == "store":
        outputs = {name: Path(value) for name, value in _parse_assignments(args.output).items()}
        result = store_checkpoint(
            cache_root=args.cache_root,
            identity=identity,
            outputs=outputs,
            elapsed_seconds=args.elapsed_seconds,
            run_manifest=args.run_manifest,
        )
    elif args.command == "restore":
        destinations = {
            name: Path(value) for name, value in _parse_assignments(args.destination).items()
        }
        result = restore_checkpoint(
            cache_root=args.cache_root,
            identity=identity,
            destinations=destinations,
            run_manifest=args.run_manifest,
        )
    else:
        result = verify_checkpoint(args.cache_root, identity)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CheckpointError, json.JSONDecodeError, OSError) as error:
        raise SystemExit(f"asahi-checkpoint: {error}") from error
