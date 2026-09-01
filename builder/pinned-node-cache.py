#!/usr/bin/env python3
"""Safely admit and snapshot the exact pinned Node payload."""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Iterable


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _NOFOLLOW | _DIRECTORY | _CLOEXEC
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
_COPY_CHUNK_SIZE = 1024 * 1024


class NodeCacheError(RuntimeError):
    """Raised when a Node cache path or payload is unsafe."""


class NodeCacheMiss(NodeCacheError):
    """Raised when a safe cache has no exact pinned payload."""


def _owners(values: Iterable[int]) -> frozenset[int]:
    owners = frozenset(values)
    if not owners or any(not isinstance(value, int) or value < 0 for value in owners):
        raise NodeCacheError("allowed cache owners are invalid")
    return owners


def _safe_absolute(path: Path, *, role: str) -> Path:
    raw = os.fspath(path)
    if not raw or not os.path.isabs(raw):
        raise NodeCacheError(f"{role} must be an absolute path")
    return Path(os.path.abspath(raw))


def _require_platform_guards() -> None:
    if not _NOFOLLOW or not _DIRECTORY:
        raise NodeCacheError("platform has no fail-closed nofollow directory support")


def _require_owned_directory(
    descriptor: int,
    *,
    allowed_owner_ids: frozenset[int],
    role: str,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise NodeCacheError(f"{role} is not a real directory")
    if metadata.st_uid not in allowed_owner_ids:
        raise NodeCacheError(f"{role} has an untrusted owner")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise NodeCacheError(f"{role} is group/world writable")
    return metadata


def _open_owned_directory(
    path: Path,
    allowed_owner_ids: Iterable[int],
    *,
    role: str,
) -> int:
    _require_platform_guards()
    owners = _owners(allowed_owner_ids)
    absolute = _safe_absolute(path, role=role)
    descriptors: list[int] = []
    try:
        descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
        descriptors.append(descriptor)
        _require_owned_directory(
            descriptor, allowed_owner_ids=owners, role=f"{role} ancestor /"
        )
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise NodeCacheError(
                    f"{role} has a symlinked or unsafe ancestor: {absolute}"
                ) from error
            descriptors.append(next_descriptor)
            descriptor = next_descriptor
            _require_owned_directory(
                descriptor,
                allowed_owner_ids=owners,
                role=f"{role} ancestor {component}",
            )
        result = os.dup(descriptor)
        return result
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def validate_cache_root(
    cache_root: Path,
    allowed_owner_ids: Iterable[int],
) -> None:
    descriptor = _open_owned_directory(
        Path(cache_root), allowed_owner_ids, role="Node cache root"
    )
    os.close(descriptor)


def _safe_filename(filename: str) -> str:
    if (
        not filename
        or filename != os.path.basename(filename)
        or filename in {".", ".."}
        or re.fullmatch(r"node-v[0-9][A-Za-z0-9._-]*-linux-(?:arm64|x64)\.tar\.gz", filename)
        is None
    ):
        raise NodeCacheError("pinned Node filename is unsafe")
    return filename


def _expected_identity(expected_sha256: str, expected_size: int) -> tuple[str, int]:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise NodeCacheError("pinned Node SHA-256 is invalid")
    if not isinstance(expected_size, int) or expected_size <= 0:
        raise NodeCacheError("pinned Node size is invalid")
    return expected_sha256, expected_size


def _require_owned_file(
    descriptor: int,
    *,
    allowed_owner_ids: frozenset[int],
    role: str,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise NodeCacheError(f"{role} is symlinked or unsafe")
    if metadata.st_uid not in allowed_owner_ids:
        raise NodeCacheError(f"{role} has an untrusted owner")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise NodeCacheError(f"{role} is group/world writable")
    return metadata


def _open_source(
    root_descriptor: int,
    filename: str,
    *,
    allowed_owner_ids: frozenset[int],
    missing_is_cache_miss: bool,
) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(filename, _FILE_FLAGS, dir_fd=root_descriptor)
    except FileNotFoundError as error:
        if missing_is_cache_miss:
            raise NodeCacheMiss("exact pinned Node payload is not cached") from error
        raise NodeCacheError("pinned Node source is missing") from error
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise NodeCacheError("pinned Node cache file is symlinked or unsafe") from error
        raise NodeCacheError("pinned Node cache file could not be opened safely") from error
    try:
        metadata = _require_owned_file(
            descriptor,
            allowed_owner_ids=allowed_owner_ids,
            role="pinned Node cache file",
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, metadata


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_nlink,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise NodeCacheError("pinned Node snapshot write made no progress")
        view = view[written:]


def _temporary_output(destination_descriptor: int, filename: str) -> tuple[str, int]:
    for _attempt in range(128):
        temporary = f".{filename}.{secrets.token_hex(12)}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=destination_descriptor,
            )
            return temporary, descriptor
        except FileExistsError:
            continue
        except OSError as error:
            raise NodeCacheError("private Node snapshot could not be created") from error
    raise NodeCacheError("private Node snapshot name could not be allocated")


def _path_metadata(root_descriptor: int, filename: str) -> os.stat_result:
    try:
        return os.stat(filename, dir_fd=root_descriptor, follow_symlinks=False)
    except OSError as error:
        raise NodeCacheError("pinned Node cache path changed while being read") from error


def _read_exact_file(
    descriptor: int,
    *,
    expected_sha256: str,
    expected_size: int,
) -> tuple[str, int]:
    result = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, _COPY_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > expected_size:
            return result.hexdigest(), total
        result.update(chunk)
    return result.hexdigest(), total


def _remove_entry(directory_descriptor: int, filename: str) -> None:
    try:
        os.unlink(filename, dir_fd=directory_descriptor)
    except FileNotFoundError:
        pass


def _remove_entry_if_identity(
    directory_descriptor: int,
    filename: str,
    expected_inode: tuple[int, int],
) -> None:
    try:
        current = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) == expected_inode:
            os.unlink(filename, dir_fd=directory_descriptor)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    except OSError:
        pass


def _verify_snapshot(
    destination_descriptor: int,
    filename: str,
    *,
    expected_sha256: str,
    expected_size: int,
    expected_inode: tuple[int, int],
    allowed_owner_ids: frozenset[int],
) -> None:
    descriptor = None
    try:
        descriptor = os.open(filename, _FILE_FLAGS, dir_fd=destination_descriptor)
        before = _require_owned_file(
            descriptor,
            allowed_owner_ids=allowed_owner_ids,
            role="installed Node snapshot",
        )
        if (before.st_dev, before.st_ino) != expected_inode:
            raise NodeCacheError("installed Node snapshot inode changed")
        digest, size = _read_exact_file(
            descriptor,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
        after = os.fstat(descriptor)
        path_after = _path_metadata(destination_descriptor, filename)
        if (
            digest != expected_sha256
            or size != expected_size
            or _metadata_identity(before) != _metadata_identity(after)
            or (before.st_dev, before.st_ino)
            != (path_after.st_dev, path_after.st_ino)
            or stat.S_IMODE(after.st_mode) != 0o444
        ):
            raise NodeCacheError("installed Node snapshot digest, size, or identity changed")
    except (OSError, NodeCacheError) as error:
        raise NodeCacheError("pinned Node snapshot verification failed") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _copy_exact(
    *,
    source_descriptor: int,
    source_before: os.stat_result,
    source_root_descriptor: int,
    source_filename: str,
    destination_root: Path,
    destination_filename: str,
    expected_sha256: str,
    expected_size: int,
    allowed_owner_ids: frozenset[int],
    mismatch_is_cache_miss: bool,
) -> Path:
    if source_before.st_size != expected_size:
        error = NodeCacheMiss if mismatch_is_cache_miss else NodeCacheError
        raise error("pinned Node payload size does not match the build lock")
    destination_descriptor = _open_owned_directory(
        destination_root,
        allowed_owner_ids,
        role="Node snapshot destination",
    )
    temporary = ""
    temporary_descriptor = None
    installed = False
    try:
        temporary, temporary_descriptor = _temporary_output(
            destination_descriptor, destination_filename
        )
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source_descriptor, _COPY_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                break
            digest.update(chunk)
            _write_all(temporary_descriptor, chunk)
        source_after = os.fstat(source_descriptor)
        source_path_after = _path_metadata(source_root_descriptor, source_filename)
        if (
            _metadata_identity(source_before) != _metadata_identity(source_after)
            or (source_before.st_dev, source_before.st_ino)
            != (source_path_after.st_dev, source_path_after.st_ino)
        ):
            raise NodeCacheError("pinned Node cache path changed while being read")
        if total != expected_size or digest.hexdigest() != expected_sha256:
            error = NodeCacheMiss if mismatch_is_cache_miss else NodeCacheError
            raise error("pinned Node payload digest or size does not match the build lock")
        os.fchmod(temporary_descriptor, 0o444)
        os.fsync(temporary_descriptor)
        temporary_metadata = os.fstat(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None
        os.replace(
            temporary,
            destination_filename,
            src_dir_fd=destination_descriptor,
            dst_dir_fd=destination_descriptor,
        )
        temporary = ""
        installed = True
        _verify_snapshot(
            destination_descriptor,
            destination_filename,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            expected_inode=(temporary_metadata.st_dev, temporary_metadata.st_ino),
            allowed_owner_ids=allowed_owner_ids,
        )
        return Path(destination_root) / destination_filename
    except Exception:
        if installed:
            _remove_entry_if_identity(
                destination_descriptor,
                destination_filename,
                (temporary_metadata.st_dev, temporary_metadata.st_ino),
            )
        raise
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary:
            _remove_entry(destination_descriptor, temporary)
        os.close(destination_descriptor)


def snapshot_cached_payload(
    *,
    cache_root: Path,
    filename: str,
    destination_root: Path,
    expected_sha256: str,
    expected_size: int,
    allowed_owner_ids: Iterable[int],
) -> Path:
    owners = _owners(allowed_owner_ids)
    filename = _safe_filename(filename)
    expected_sha256, expected_size = _expected_identity(
        expected_sha256, expected_size
    )
    cache_descriptor = _open_owned_directory(
        Path(cache_root), owners, role="Node cache root"
    )
    source_descriptor = None
    try:
        source_descriptor, source_before = _open_source(
            cache_descriptor,
            filename,
            allowed_owner_ids=owners,
            missing_is_cache_miss=True,
        )
        return _copy_exact(
            source_descriptor=source_descriptor,
            source_before=source_before,
            source_root_descriptor=cache_descriptor,
            source_filename=filename,
            destination_root=Path(destination_root),
            destination_filename=filename,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            allowed_owner_ids=owners,
            mismatch_is_cache_miss=True,
        )
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        os.close(cache_descriptor)


def publish_cached_payload(
    *,
    source: Path,
    cache_root: Path,
    filename: str,
    expected_sha256: str,
    expected_size: int,
    allowed_owner_ids: Iterable[int],
) -> Path:
    owners = _owners(allowed_owner_ids)
    filename = _safe_filename(filename)
    expected_sha256, expected_size = _expected_identity(
        expected_sha256, expected_size
    )
    source = _safe_absolute(Path(source), role="downloaded Node source")
    if source.name != filename:
        raise NodeCacheError("downloaded Node source filename differs from the lock")
    source_root_descriptor = _open_owned_directory(
        source.parent, owners, role="downloaded Node source root"
    )
    source_descriptor = None
    try:
        source_descriptor, source_before = _open_source(
            source_root_descriptor,
            filename,
            allowed_owner_ids=owners,
            missing_is_cache_miss=False,
        )
        return _copy_exact(
            source_descriptor=source_descriptor,
            source_before=source_before,
            source_root_descriptor=source_root_descriptor,
            source_filename=filename,
            destination_root=Path(cache_root),
            destination_filename=filename,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            allowed_owner_ids=owners,
            mismatch_is_cache_miss=False,
        )
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        os.close(source_root_descriptor)


def _add_owner_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allowed-owner",
        type=int,
        action="append",
        required=True,
        dest="allowed_owners",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-cache-root")
    validate.add_argument("--cache-root", type=Path, required=True)
    _add_owner_argument(validate)

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--cache-root", type=Path, required=True)
    snapshot.add_argument("--filename", required=True)
    snapshot.add_argument("--destination-root", type=Path, required=True)
    snapshot.add_argument("--sha256", required=True)
    snapshot.add_argument("--size", type=int, required=True)
    _add_owner_argument(snapshot)

    publish = subparsers.add_parser("publish")
    publish.add_argument("--source", type=Path, required=True)
    publish.add_argument("--cache-root", type=Path, required=True)
    publish.add_argument("--filename", required=True)
    publish.add_argument("--sha256", required=True)
    publish.add_argument("--size", type=int, required=True)
    _add_owner_argument(publish)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "validate-cache-root":
        validate_cache_root(arguments.cache_root, arguments.allowed_owners)
    elif arguments.command == "snapshot":
        snapshot_cached_payload(
            cache_root=arguments.cache_root,
            filename=arguments.filename,
            destination_root=arguments.destination_root,
            expected_sha256=arguments.sha256,
            expected_size=arguments.size,
            allowed_owner_ids=arguments.allowed_owners,
        )
    elif arguments.command == "publish":
        publish_cached_payload(
            source=arguments.source,
            cache_root=arguments.cache_root,
            filename=arguments.filename,
            expected_sha256=arguments.sha256,
            expected_size=arguments.size,
            allowed_owner_ids=arguments.allowed_owners,
        )
    else:
        raise NodeCacheError("unsupported Node cache operation")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NodeCacheMiss as error:
        print(f"pinned-node-cache: {error}", file=sys.stderr)
        raise SystemExit(3) from error
    except (NodeCacheError, OSError) as error:
        print(f"pinned-node-cache: {error}", file=sys.stderr)
        raise SystemExit(1) from error
