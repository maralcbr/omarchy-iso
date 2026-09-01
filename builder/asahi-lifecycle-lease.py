#!/usr/bin/env python3
"""Run one command while holding a fail-closed host lifecycle lease."""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from types import FrameType
from typing import Iterable


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _NOFOLLOW | _DIRECTORY | _CLOEXEC
_LEASE_FILE_FLAGS = (
    os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC | _NONBLOCK
)
_LEASE_FILENAME = ".omarchy-lifecycle.lease"
_RUN_RESERVATION_FILENAME = ".omarchy-run-reservation.json"
_RUN_RESERVATION_KIND = "asahi-build-run-reservation-v1"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NONCE = re.compile(r"^[0-9a-f]{64}$")
_READ_ONLY_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
_CREATE_READ_ONLY_FILE_FLAGS = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC
)


class LifecycleLeaseError(RuntimeError):
    """Raised when the lifecycle lease cannot be acquired safely."""


def _allowed_owners(values: Iterable[int]) -> frozenset[int]:
    owners = frozenset(values)
    if not owners or any(not isinstance(value, int) or value < 0 for value in owners):
        raise LifecycleLeaseError("allowed lease owners are invalid")
    return owners


def _absolute_path(path: Path) -> Path:
    raw = os.fspath(path)
    if not raw or not os.path.isabs(raw):
        raise LifecycleLeaseError("lease root must be an absolute path")
    return Path(os.path.abspath(raw))


def _require_platform_guards() -> None:
    if not _NOFOLLOW or not _DIRECTORY:
        raise LifecycleLeaseError(
            "platform has no fail-closed nofollow directory support"
        )


def _require_owned_directory(
    descriptor: int,
    *,
    allowed_owner_ids: frozenset[int],
    role: str,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise LifecycleLeaseError(f"{role} is not a real directory")
    if metadata.st_uid not in allowed_owner_ids:
        raise LifecycleLeaseError(f"{role} has an untrusted owner")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise LifecycleLeaseError(f"{role} is group/world writable")
    return metadata


def _open_lease_root(
    path: Path,
    allowed_owner_ids: Iterable[int],
    *,
    create_missing: bool = False,
) -> tuple[int, frozenset[int]]:
    _require_platform_guards()
    owners = _allowed_owners(allowed_owner_ids)
    absolute = _absolute_path(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
        _require_owned_directory(
            descriptor,
            allowed_owner_ids=owners,
            role="lease root ancestor /",
        )
        traversed = Path(os.sep)
        for component in absolute.parts[1:]:
            traversed /= component
            try:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
            except FileNotFoundError as error:
                if not create_missing:
                    raise LifecycleLeaseError(
                        f"lease root has a symlinked, missing, or non-directory "
                        f"component: {traversed}"
                    ) from error
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                    next_descriptor = os.open(
                        component,
                        _DIRECTORY_FLAGS,
                        dir_fd=descriptor,
                    )
                except OSError as create_error:
                    raise LifecycleLeaseError(
                        f"lease root component could not be created safely: {traversed}"
                    ) from create_error
            except OSError as error:
                raise LifecycleLeaseError(
                    f"lease root has a symlinked, missing, or non-directory "
                    f"component: {traversed}"
                ) from error
            os.close(descriptor)
            descriptor = next_descriptor
            _require_owned_directory(
                descriptor,
                allowed_owner_ids=owners,
                role=f"lease root component {traversed}",
            )
        result = descriptor
        descriptor = None
        return result, owners
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _require_owned_lease_file(
    descriptor: int,
    *,
    allowed_owner_ids: frozenset[int],
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise LifecycleLeaseError(
            "lifecycle lease file is not a private regular file"
        )
    if metadata.st_uid not in allowed_owner_ids:
        raise LifecycleLeaseError("lifecycle lease file has an untrusted owner")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise LifecycleLeaseError("lifecycle lease file is group/world writable")
    return metadata


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
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


def _require_read_only_run_file(
    descriptor: int,
    *,
    allowed_owner_ids: frozenset[int],
    role: str,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise LifecycleLeaseError(f"{role} is not a private regular file")
    if metadata.st_uid not in allowed_owner_ids:
        raise LifecycleLeaseError(f"{role} has an untrusted owner")
    if stat.S_IMODE(metadata.st_mode) & 0o222:
        raise LifecycleLeaseError(f"{role} is writable")
    if metadata.st_size <= 0 or metadata.st_size > 4096:
        raise LifecycleLeaseError(f"{role} has an invalid size")
    return metadata


def _canonical_run_reservation(record: dict[str, object]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _validate_run_id(run_id: str) -> None:
    if not _RUN_ID.fullmatch(run_id):
        raise LifecycleLeaseError(f"build run ID is unsafe: {run_id}")


def _parse_run_reservation(payload: bytes, expected_run_id: str) -> dict[str, object]:
    try:
        record = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LifecycleLeaseError("run reservation is invalid JSON") from error
    if not isinstance(record, dict) or set(record) != {
        "schema_version",
        "kind",
        "run_id",
        "nonce",
    }:
        raise LifecycleLeaseError("run reservation has an invalid schema")
    if (
        record.get("schema_version") != 1
        or record.get("kind") != _RUN_RESERVATION_KIND
        or record.get("run_id") != expected_run_id
        or not isinstance(record.get("nonce"), str)
        or not _NONCE.fullmatch(record["nonce"])
    ):
        raise LifecycleLeaseError("run reservation is stale or mismatched")
    if payload != _canonical_run_reservation(record):
        raise LifecycleLeaseError("run reservation is not canonical")
    return record


def _read_run_file_at(
    directory_descriptor: int,
    filename: str,
    *,
    allowed_owner_ids: frozenset[int],
    role: str,
) -> bytes:
    try:
        descriptor = os.open(
            filename,
            _READ_ONLY_FILE_FLAGS,
            dir_fd=directory_descriptor,
        )
    except FileNotFoundError as error:
        raise LifecycleLeaseError(f"{role} is missing") from error
    except OSError as error:
        raise LifecycleLeaseError(f"{role} is missing or unsafe") from error
    try:
        before = _require_read_only_run_file(
            descriptor,
            allowed_owner_ids=allowed_owner_ids,
            role=role,
        )
        payload = bytearray()
        while len(payload) <= 4096:
            chunk = os.read(descriptor, min(4097 - len(payload), 4096))
            if not chunk:
                break
            payload.extend(chunk)
        after = _require_read_only_run_file(
            descriptor,
            allowed_owner_ids=allowed_owner_ids,
            role=role,
        )
        try:
            path_after = os.stat(
                filename,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise LifecycleLeaseError(f"{role} changed while being read") from error
        if (
            len(payload) != after.st_size
            or not _same_file_identity(before, after)
            or not _same_file_identity(after, path_after)
        ):
            raise LifecycleLeaseError(f"{role} changed while being read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _read_run_file(
    path: Path,
    allowed_owner_ids: Iterable[int],
    *,
    role: str,
) -> bytes:
    absolute = _absolute_path(path)
    parent_descriptor, owners = _open_lease_root(
        absolute.parent,
        allowed_owner_ids,
    )
    try:
        return _read_run_file_at(
            parent_descriptor,
            absolute.name,
            allowed_owner_ids=owners,
            role=role,
        )
    finally:
        os.close(parent_descriptor)


def _write_read_only_file_at(
    directory_descriptor: int,
    filename: str,
    payload: bytes,
    *,
    allowed_owner_ids: frozenset[int],
    role: str,
) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            filename,
            _CREATE_READ_ONLY_FILE_FLAGS,
            0o400,
            dir_fd=directory_descriptor,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        descriptor_metadata = _require_read_only_run_file(
            descriptor,
            allowed_owner_ids=allowed_owner_ids,
            role=role,
        )
        path_metadata = os.stat(
            filename,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if not _same_file_identity(descriptor_metadata, path_metadata):
            raise LifecycleLeaseError(f"{role} changed while being created")
        os.fsync(directory_descriptor)
    except FileExistsError as error:
        raise LifecycleLeaseError(f"{role} already exists") from error
    except OSError as error:
        raise LifecycleLeaseError(f"{role} could not be created safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def create_run_reservation(
    *,
    run_id: str,
    output: Path,
    allowed_owner_ids: Iterable[int],
) -> None:
    _validate_run_id(run_id)
    absolute = _absolute_path(output)
    parent_descriptor, owners = _open_lease_root(
        absolute.parent,
        allowed_owner_ids,
    )
    try:
        payload = _canonical_run_reservation(
            {
                "schema_version": 1,
                "kind": _RUN_RESERVATION_KIND,
                "run_id": run_id,
                "nonce": os.urandom(32).hex(),
            }
        )
        _write_read_only_file_at(
            parent_descriptor,
            absolute.name,
            payload,
            allowed_owner_ids=owners,
            role="host run reservation",
        )
    finally:
        os.close(parent_descriptor)


def _open_or_create_run_directory(
    parent_descriptor: int,
    name: str,
    *,
    allowed_owner_ids: frozenset[int],
    role: str,
    create_missing: bool,
) -> tuple[int, bool]:
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        created = False
    except FileNotFoundError as error:
        if not create_missing:
            raise LifecycleLeaseError(f"{role} is missing") from error
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
            created = True
        except OSError as create_error:
            raise LifecycleLeaseError(
                f"{role} could not be created safely"
            ) from create_error
    except OSError as error:
        raise LifecycleLeaseError(f"{role} is missing or unsafe") from error
    try:
        _require_owned_directory(
            descriptor,
            allowed_owner_ids=allowed_owner_ids,
            role=role,
        )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, created


def admit_run_evidence(
    *,
    run_id: str,
    reservation: Path,
    evidence_root: Path,
    allowed_owner_ids: Iterable[int],
    create_missing: bool,
) -> None:
    _validate_run_id(run_id)
    owners = _allowed_owners(allowed_owner_ids)
    reservation_payload = _read_run_file(
        reservation,
        owners,
        role="host run reservation",
    )
    _parse_run_reservation(reservation_payload, run_id)

    absolute = _absolute_path(evidence_root)
    if absolute.name != run_id or absolute.parent.name != "build-evidence":
        raise LifecycleLeaseError("run evidence root does not match the build run ID")
    output_descriptor, verified_owners = _open_lease_root(
        absolute.parent.parent,
        owners,
    )
    build_evidence_descriptor: int | None = None
    run_descriptor: int | None = None
    try:
        build_evidence_descriptor, _ = _open_or_create_run_directory(
            output_descriptor,
            "build-evidence",
            allowed_owner_ids=verified_owners,
            role="build evidence root",
            create_missing=create_missing,
        )
        run_descriptor, run_created = _open_or_create_run_directory(
            build_evidence_descriptor,
            run_id,
            allowed_owner_ids=verified_owners,
            role="build run evidence",
            create_missing=create_missing,
        )
        if run_created:
            _write_read_only_file_at(
                run_descriptor,
                _RUN_RESERVATION_FILENAME,
                reservation_payload,
                allowed_owner_ids=verified_owners,
                role="build run reservation marker",
            )
        else:
            marker_payload = _read_run_file_at(
                run_descriptor,
                _RUN_RESERVATION_FILENAME,
                allowed_owner_ids=verified_owners,
                role="build run reservation marker",
            )
            _parse_run_reservation(marker_payload, run_id)
            if marker_payload != reservation_payload:
                raise LifecycleLeaseError(
                    "build run reservation marker does not match the host reservation"
                )
    finally:
        if run_descriptor is not None:
            os.close(run_descriptor)
        if build_evidence_descriptor is not None:
            os.close(build_evidence_descriptor)
        os.close(output_descriptor)


def _path_metadata(directory_descriptor: int) -> os.stat_result:
    try:
        return os.stat(
            _LEASE_FILENAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise LifecycleLeaseError(
            "lifecycle lease file changed while it was acquired"
        ) from error


def acquire_lifecycle_lease(
    lease_root: Path,
    allowed_owner_ids: Iterable[int],
    *,
    create_root: bool = False,
) -> tuple[int, int]:
    root_descriptor, owners = _open_lease_root(
        lease_root,
        allowed_owner_ids,
        create_missing=create_root,
    )
    lease_descriptor: int | None = None
    try:
        try:
            lease_descriptor = os.open(
                _LEASE_FILENAME,
                _LEASE_FILE_FLAGS,
                0o600,
                dir_fd=root_descriptor,
            )
        except OSError as error:
            raise LifecycleLeaseError(
                "lifecycle lease file could not be opened safely"
            ) from error

        before = _require_owned_lease_file(
            lease_descriptor,
            allowed_owner_ids=owners,
        )
        try:
            fcntl.flock(lease_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise LifecycleLeaseError(
                    "lifecycle lease is already held by another process"
                ) from error
            raise LifecycleLeaseError("lifecycle lease could not be locked") from error

        after = _require_owned_lease_file(
            lease_descriptor,
            allowed_owner_ids=owners,
        )
        path_after = _path_metadata(root_descriptor)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or (after.st_dev, after.st_ino)
            != (path_after.st_dev, path_after.st_ino)
            or after.st_nlink != 1
        ):
            raise LifecycleLeaseError(
                "lifecycle lease file changed while it was acquired"
            )

        result = lease_descriptor
        lease_descriptor = None
        return root_descriptor, result
    except BaseException:
        if lease_descriptor is not None:
            os.close(lease_descriptor)
        os.close(root_descriptor)
        raise


def release_lifecycle_lease(
    root_descriptor: int,
    lease_descriptor: int,
) -> None:
    # Close rather than explicitly unlocking. If an escaped descendant still
    # inherited this open file description, the kernel keeps the lease held
    # until the final duplicate closes instead of reopening shared state early.
    os.close(lease_descriptor)
    os.close(root_descriptor)


def _forwarded_signals() -> tuple[int, ...]:
    values = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        values.append(signal.SIGHUP)
    return tuple(values)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group):
            return True
        time.sleep(0.02)
    return not _process_group_exists(process_group)


def _terminate_residual_process_group(process_group: int) -> bool:
    if not _process_group_exists(process_group):
        return False
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    if not _wait_for_process_group_exit(process_group, 1.0):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return True
        if not _wait_for_process_group_exit(process_group, 2.0):
            raise LifecycleLeaseError(
                "child process group remained alive after TERM and KILL"
            )
    return True


def _run_child(
    command: list[str],
    *,
    lease_descriptor: int,
    lease_root: Path,
) -> int:
    child_environment = os.environ.copy()
    child_environment["OMARCHY_ASAHI_LIFECYCLE_LEASE_FD"] = str(
        lease_descriptor
    )
    child_environment["OMARCHY_ASAHI_LIFECYCLE_LEASE_ROOT"] = os.fspath(
        _absolute_path(lease_root)
    )
    child_environment["OMARCHY_ASAHI_LIFECYCLE_LEASE_HELD"] = "1"
    try:
        process = subprocess.Popen(
            command,
            start_new_session=True,
            env=child_environment,
            pass_fds=(lease_descriptor,),
        )
    except FileNotFoundError:
        print(f"asahi-lifecycle-lease: command not found: {command[0]}", file=sys.stderr)
        return 127
    except PermissionError:
        print(
            f"asahi-lifecycle-lease: command is not executable: {command[0]}",
            file=sys.stderr,
        )
        return 126
    except OSError as error:
        raise LifecycleLeaseError(f"child command could not be started: {error}") from error

    previous_handlers: dict[int, signal.Handlers] = {}

    def forward(signum: int, _frame: FrameType | None) -> None:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    try:
        for signum in _forwarded_signals():
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, forward)
        returncode = process.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    if _terminate_residual_process_group(process.pid):
        raise LifecycleLeaseError(
            "child command exited while background descendants were still running"
        )

    if returncode < 0:
        return 128 + -returncode
    return returncode


def run_with_lifecycle_lease(
    *,
    lease_root: Path,
    allowed_owner_ids: Iterable[int],
    command: list[str],
    create_root: bool = False,
) -> int:
    if not command:
        raise LifecycleLeaseError("child command is required after --")
    root_descriptor, lease_descriptor = acquire_lifecycle_lease(
        lease_root,
        allowed_owner_ids,
        create_root=create_root,
    )
    try:
        return _run_child(
            command,
            lease_descriptor=lease_descriptor,
            lease_root=lease_root,
        )
    finally:
        release_lifecycle_lease(root_descriptor, lease_descriptor)


def validate_held_lifecycle_lease(
    *,
    lease_root: Path,
    lease_descriptor: int,
    allowed_owner_ids: Iterable[int],
) -> None:
    if lease_descriptor < 3:
        raise LifecycleLeaseError("inherited lifecycle lease descriptor is invalid")
    root_descriptor, owners = _open_lease_root(
        lease_root,
        allowed_owner_ids,
    )
    try:
        descriptor_metadata = _require_owned_lease_file(
            lease_descriptor,
            allowed_owner_ids=owners,
        )
        path_metadata = _path_metadata(root_descriptor)
        if (
            descriptor_metadata.st_dev,
            descriptor_metadata.st_ino,
        ) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise LifecycleLeaseError(
                "inherited lifecycle lease descriptor does not match the lease path"
            )
        try:
            # An inherited descriptor shares the already-locked open file
            # description. A merely forged numeric environment value cannot.
            fcntl.flock(lease_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise LifecycleLeaseError(
                "inherited lifecycle lease descriptor is not held"
            ) from error
    finally:
        os.close(root_descriptor)


def _owner_id(raw: str) -> int:
    try:
        value = int(raw, 10)
    except ValueError as error:
        raise argparse.ArgumentTypeError("owner ID must be an integer") from error
    if value < 0:
        raise argparse.ArgumentTypeError("owner ID must be non-negative")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--lease-root", type=Path, required=True)
    run.add_argument("--create-lease-root", action="store_true")
    run.add_argument(
        "--allowed-owner",
        type=_owner_id,
        action="append",
        required=True,
        dest="allowed_owners",
    )
    run.add_argument("child_command", nargs=argparse.REMAINDER)
    ensure = subparsers.add_parser("ensure-directory")
    ensure.add_argument("--path", type=Path, required=True)
    ensure.add_argument(
        "--allowed-owner",
        type=_owner_id,
        action="append",
        required=True,
        dest="allowed_owners",
    )
    validate = subparsers.add_parser("validate-held")
    validate.add_argument("--lease-root", type=Path, required=True)
    validate.add_argument("--lease-fd", type=int, required=True)
    validate.add_argument(
        "--allowed-owner",
        type=_owner_id,
        action="append",
        required=True,
        dest="allowed_owners",
    )
    create_reservation = subparsers.add_parser("create-run-reservation")
    create_reservation.add_argument("--run-id", required=True)
    create_reservation.add_argument("--output", type=Path, required=True)
    create_reservation.add_argument(
        "--allowed-owner",
        type=_owner_id,
        action="append",
        required=True,
        dest="allowed_owners",
    )
    for operation in ("admit-run-evidence", "verify-run-evidence"):
        evidence = subparsers.add_parser(operation)
        evidence.add_argument("--run-id", required=True)
        evidence.add_argument("--reservation", type=Path, required=True)
        evidence.add_argument("--evidence-root", type=Path, required=True)
        evidence.add_argument(
            "--allowed-owner",
            type=_owner_id,
            action="append",
            required=True,
            dest="allowed_owners",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.operation == "create-run-reservation":
        create_run_reservation(
            run_id=arguments.run_id,
            output=arguments.output,
            allowed_owner_ids=arguments.allowed_owners,
        )
        return 0
    if arguments.operation in {"admit-run-evidence", "verify-run-evidence"}:
        admit_run_evidence(
            run_id=arguments.run_id,
            reservation=arguments.reservation,
            evidence_root=arguments.evidence_root,
            allowed_owner_ids=arguments.allowed_owners,
            create_missing=arguments.operation == "admit-run-evidence",
        )
        return 0
    if arguments.operation == "ensure-directory":
        descriptor, _owners = _open_lease_root(
            arguments.path,
            arguments.allowed_owners,
            create_missing=True,
        )
        os.close(descriptor)
        return 0
    if arguments.operation == "validate-held":
        validate_held_lifecycle_lease(
            lease_root=arguments.lease_root,
            lease_descriptor=arguments.lease_fd,
            allowed_owner_ids=arguments.allowed_owners,
        )
        return 0
    if arguments.operation != "run":
        raise LifecycleLeaseError("unsupported lifecycle lease operation")
    child_command = arguments.child_command
    if child_command[:1] == ["--"]:
        child_command = child_command[1:]
    return run_with_lifecycle_lease(
        lease_root=arguments.lease_root,
        allowed_owner_ids=arguments.allowed_owners,
        command=child_command,
        create_root=arguments.create_lease_root,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LifecycleLeaseError, OSError) as error:
        print(f"asahi-lifecycle-lease: {error}", file=sys.stderr)
        raise SystemExit(1) from error
