#!/usr/bin/env python3
"""Publish one qualified Asahi release without selecting or replacing stale output."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Iterable


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _NOFOLLOW | _DIRECTORY | _CLOEXEC
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC
_LOCK_NAME = ".omarchy-release-publication.lock"
_SIDECAR_SUFFIXES = (
    ".asahi-package-evidence.json",
    ".installer-data.json",
)
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.zip$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PublicationError(RuntimeError):
    """Raised when a release cannot be published without weakening admission."""


def _owners(values: Iterable[int]) -> frozenset[int]:
    result = frozenset(values)
    if not result or any(not isinstance(value, int) or value < 0 for value in result):
        raise PublicationError("allowed publication owners are invalid")
    return result


def _absolute(path: Path, role: str) -> Path:
    raw = os.fspath(path)
    if not raw or not os.path.isabs(raw):
        raise PublicationError(f"{role} must be an absolute path")
    return Path(os.path.abspath(raw))


def _require_safe_dir(fd: int, owners: frozenset[int], role: str) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise PublicationError(f"{role} is not a real directory")
    if metadata.st_uid not in owners:
        raise PublicationError(f"{role} has an untrusted owner")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise PublicationError(f"{role} is group/world writable")
    return metadata


def _open_safe_dir(path: Path, owners: frozenset[int], role: str) -> int:
    if not _NOFOLLOW or not _DIRECTORY:
        raise PublicationError("platform has no fail-closed nofollow support")
    absolute = _absolute(path, role)
    descriptor: int | None = None
    try:
        descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
        _require_safe_dir(descriptor, owners, f"{role} ancestor /")
        traversed = Path(os.sep)
        for component in absolute.parts[1:]:
            traversed /= component
            try:
                next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as error:
                raise PublicationError(
                    f"{role} has an unsafe, missing, or non-directory component: {traversed}"
                ) from error
            os.close(descriptor)
            descriptor = next_descriptor
            _require_safe_dir(descriptor, owners, f"{role} component {traversed}")
        result = descriptor
        descriptor = None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _require_safe_file(fd: int, owners: frozenset[int], role: str) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise PublicationError(f"{role} is an unsafe non-private regular file")
    if metadata.st_uid not in owners:
        raise PublicationError(f"{role} has an untrusted owner")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise PublicationError(f"{role} is group/world writable")
    return metadata


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _hash_open_file(fd: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _read_record(dir_fd: int, name: str, owners: frozenset[int], role: str) -> dict[str, object]:
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError as error:
        raise PublicationError(f"{role} is missing or unsafe: {name}") from error
    try:
        before = _require_safe_file(fd, owners, f"{role} {name}")
        digest, size = _hash_open_file(fd)
        after = _require_safe_file(fd, owners, f"{role} {name}")
        try:
            path_after = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as error:
            raise PublicationError(f"{role} changed while being verified: {name}") from error
        if not _same_identity(before, after) or not _same_identity(after, path_after):
            raise PublicationError(f"{role} changed while being verified: {name}")
        if size != after.st_size:
            raise PublicationError(f"{role} size changed while being verified: {name}")
        return {"filename": name, "sha256": digest, "size": size}
    finally:
        os.close(fd)


def _entry_exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _copy_to_release_temp(
    source_fd: int,
    source_name: str,
    release_fd: int,
    owners: frozenset[int],
) -> tuple[str, dict[str, object]]:
    before = _require_safe_file(source_fd, owners, f"private release input {source_name}")
    temporary_name = f".omarchy-publish-{os.getpid()}-{os.urandom(8).hex()}"
    destination_fd: int | None = None
    try:
        destination_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o400,
            dir_fd=release_fd,
        )
        digest = hashlib.sha256()
        size = 0
        os.lseek(source_fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
        os.fchmod(destination_fd, 0o444)
        after = _require_safe_file(source_fd, owners, f"private release input {source_name}")
        if not _same_identity(before, after) or size != after.st_size:
            raise PublicationError(f"private release input changed while copied: {source_name}")
        os.close(destination_fd)
        destination_fd = None

        verify_fd = os.open(temporary_name, _READ_FLAGS, dir_fd=release_fd)
        try:
            verified = _require_safe_file(verify_fd, owners, f"publication snapshot {source_name}")
            verified_digest, verified_size = _hash_open_file(verify_fd)
        finally:
            os.close(verify_fd)
        if verified_digest != digest.hexdigest() or verified_size != size or verified.st_size != size:
            raise PublicationError(f"publication snapshot verification failed: {source_name}")
        return temporary_name, {
            "filename": source_name,
            "sha256": verified_digest,
            "size": verified_size,
        }
    except BaseException:
        if destination_fd is not None:
            os.close(destination_fd)
        try:
            os.unlink(temporary_name, dir_fd=release_fd)
        except FileNotFoundError:
            pass
        raise


def _lock_release(release_fd: int, owners: frozenset[int]) -> int:
    try:
        fd = os.open(_LOCK_NAME, _LOCK_FLAGS, 0o600, dir_fd=release_fd)
    except OSError as error:
        raise PublicationError("release publication lock is unsafe") from error
    try:
        before = _require_safe_file(fd, owners, "release publication lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as error:
            raise PublicationError("release publication lock could not be acquired") from error
        after = _require_safe_file(fd, owners, "release publication lock")
        path_after = os.stat(_LOCK_NAME, dir_fd=release_fd, follow_symlinks=False)
        if not _same_identity(before, after) or not _same_identity(after, path_after):
            raise PublicationError("release publication lock changed while acquired")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_manifest(private_fd: int, manifest_name: str, result: dict[str, object]) -> None:
    if not manifest_name or manifest_name in {".", ".."} or "/" in manifest_name:
        raise PublicationError("publication manifest filename is unsafe")
    payload = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary_name = f".{manifest_name}.tmp-{os.getpid()}-{os.urandom(8).hex()}"
    fd: int | None = None
    try:
        fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o400,
            dir_fd=private_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o444)
        os.close(fd)
        fd = None
        os.replace(temporary_name, manifest_name, src_dir_fd=private_fd, dst_dir_fd=private_fd)
        os.fsync(private_fd)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary_name, dir_fd=private_fd)
        except FileNotFoundError:
            pass
        raise


def publish_release(
    *,
    private_root: Path,
    release_root: Path,
    package_filename: str,
    run_id: str,
    manifest_path: Path,
    allowed_owner_ids: Iterable[int],
) -> dict[str, object]:
    if not _PACKAGE_NAME.fullmatch(package_filename) or Path(package_filename).name != package_filename:
        raise PublicationError("package filename is unsafe")
    if not _RUN_ID.fullmatch(run_id):
        raise PublicationError("publication run ID is unsafe")
    owners = _owners(allowed_owner_ids)
    private_path = _absolute(private_root, "private publication root")
    release_path = _absolute(release_root, "release root")
    manifest = _absolute(manifest_path, "publication manifest")
    if manifest.parent != private_path:
        raise PublicationError("publication manifest must be inside the private run root")

    private_fd = _open_safe_dir(private_path, owners, "private publication root")
    release_fd = _open_safe_dir(release_path, owners, "release root")
    lock_fd: int | None = None
    temporary: dict[str, str] = {}
    created: dict[str, tuple[int, int]] = {}
    names = [package_filename + suffix for suffix in _SIDECAR_SUFFIXES]
    names.insert(0, package_filename)
    try:
        source_records = {
            name: _read_record(private_fd, name, owners, "private release input")
            for name in names
        }
        lock_fd = _lock_release(release_fd, owners)
        existing = {name: _entry_exists(release_fd, name) for name in names}
        if any(existing.values()) and not all(existing.values()):
            raise PublicationError("fixed release set is partial; refusing publication")
        reproducibility_match = all(existing.values())
        if reproducibility_match:
            final_records = {
                name: _read_record(release_fd, name, owners, "fixed release output")
                for name in names
            }
            if final_records != source_records:
                raise PublicationError("existing fixed release differs from this run")
        else:
            for name in names:
                source_fd = os.open(name, _READ_FLAGS, dir_fd=private_fd)
                try:
                    temp_name, record = _copy_to_release_temp(
                        source_fd, name, release_fd, owners
                    )
                finally:
                    os.close(source_fd)
                if record != source_records[name]:
                    raise PublicationError(f"private release input changed before publication: {name}")
                temporary[name] = temp_name

            # The package is linked last so consumers never see it before its sidecars.
            for name in [*names[1:], names[0]]:
                try:
                    os.link(
                        temporary[name],
                        name,
                        src_dir_fd=release_fd,
                        dst_dir_fd=release_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as error:
                    raise PublicationError(f"fixed release appeared concurrently: {name}") from error
                linked = os.stat(name, dir_fd=release_fd, follow_symlinks=False)
                created[name] = (linked.st_dev, linked.st_ino)
                os.unlink(temporary[name], dir_fd=release_fd)
                del temporary[name]
            os.fsync(release_fd)
            final_records = {
                name: _read_record(release_fd, name, owners, "fixed release output")
                for name in names
            }
            if final_records != source_records:
                raise PublicationError("published fixed release differs from private run output")

        result: dict[str, object] = {
            "schema_version": 1,
            "kind": "asahi-release-publication-v1",
            "result": "passed",
            "run_id": run_id,
            "package_filename": package_filename,
            "reproducibility_match": reproducibility_match,
            "outputs": final_records,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_manifest(private_fd, manifest.name, result)
        return result
    except BaseException:
        # Roll back only names whose exact inode this invocation created.
        for name, identity in reversed(list(created.items())):
            try:
                current = os.stat(name, dir_fd=release_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == identity:
                    os.unlink(name, dir_fd=release_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        for temporary_name in temporary.values():
            try:
                os.unlink(temporary_name, dir_fd=release_fd)
            except FileNotFoundError:
                pass
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        os.close(release_fd)
        os.close(private_fd)


def cleanup_private_release(
    *,
    private_root: Path,
    package_filename: str,
    manifest_name: str,
    expected_device: int,
    expected_inode: int,
    allowed_owner_ids: Iterable[int],
) -> None:
    if not _PACKAGE_NAME.fullmatch(package_filename) or Path(package_filename).name != package_filename:
        raise PublicationError("package filename is unsafe")
    if not manifest_name or manifest_name in {".", ".."} or "/" in manifest_name:
        raise PublicationError("publication manifest filename is unsafe")
    if expected_device < 0 or expected_inode <= 0:
        raise PublicationError("private publication identity is invalid")
    owners = _owners(allowed_owner_ids)
    private_path = _absolute(private_root, "private publication root")
    private_fd = _open_safe_dir(private_path, owners, "private publication root")
    parent_fd: int | None = None
    try:
        metadata = _require_safe_dir(private_fd, owners, "private publication root")
        if (metadata.st_dev, metadata.st_ino) != (expected_device, expected_inode):
            raise PublicationError("private publication root identity changed")
        allowed_names = {
            package_filename,
            *(package_filename + suffix for suffix in _SIDECAR_SUFFIXES),
            manifest_name,
        }
        entries = set(os.listdir(private_fd))
        unexpected = sorted(entries - allowed_names)
        if unexpected:
            raise PublicationError(
                f"private publication root contains unexpected entries: {unexpected}"
            )
        for name in sorted(entries):
            fd = os.open(name, _READ_FLAGS, dir_fd=private_fd)
            try:
                before = _require_safe_file(fd, owners, f"private cleanup input {name}")
                path_before = os.stat(name, dir_fd=private_fd, follow_symlinks=False)
                if not _same_identity(before, path_before):
                    raise PublicationError(f"private cleanup input changed: {name}")
                os.unlink(name, dir_fd=private_fd)
            finally:
                os.close(fd)
        os.fsync(private_fd)
        parent_fd = _open_safe_dir(
            private_path.parent, owners, "private publication parent"
        )
        path_metadata = os.stat(
            private_path.name, dir_fd=parent_fd, follow_symlinks=False
        )
        current = os.fstat(private_fd)
        if (
            current.st_dev,
            current.st_ino,
            path_metadata.st_dev,
            path_metadata.st_ino,
        ) != (expected_device, expected_inode, expected_device, expected_inode):
            raise PublicationError("private publication root changed before cleanup")
        os.rmdir(private_path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(private_fd)


def _owner_id(raw: str) -> int:
    try:
        value = int(raw, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError("owner ID must be an integer") from error
    if value < 0:
        raise argparse.ArgumentTypeError("owner ID must be non-negative")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    publish = subparsers.add_parser("publish")
    publish.add_argument("--private-root", type=Path, required=True)
    publish.add_argument("--release-root", type=Path, required=True)
    publish.add_argument("--package-filename", required=True)
    publish.add_argument("--run-id", required=True)
    publish.add_argument("--manifest", type=Path, required=True)
    publish.add_argument("--allowed-owner", type=_owner_id, action="append", required=True)
    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--private-root", type=Path, required=True)
    cleanup.add_argument("--package-filename", required=True)
    cleanup.add_argument("--manifest-name", required=True)
    cleanup.add_argument("--expected-device", type=int, required=True)
    cleanup.add_argument("--expected-inode", type=int, required=True)
    cleanup.add_argument("--allowed-owner", type=_owner_id, action="append", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "cleanup":
            cleanup_private_release(
                private_root=arguments.private_root,
                package_filename=arguments.package_filename,
                manifest_name=arguments.manifest_name,
                expected_device=arguments.expected_device,
                expected_inode=arguments.expected_inode,
                allowed_owner_ids=arguments.allowed_owner,
            )
            return 0
        result = publish_release(
            private_root=arguments.private_root,
            release_root=arguments.release_root,
            package_filename=arguments.package_filename,
            run_id=arguments.run_id,
            manifest_path=arguments.manifest,
            allowed_owner_ids=arguments.allowed_owner,
        )
    except PublicationError as error:
        print(f"asahi-release-publication: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
