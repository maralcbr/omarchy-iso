#!/usr/bin/env python3
"""Build an immutable, stage-scoped Omarchy runtime source projection."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any


MEDIA_SOURCE_PREFIX = PurePosixPath("configs/airootfs/usr/share/omarchy-iso")
PROJECTABLE_STAGES = frozenset({"configured-target", "finalized-boot"})
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


class RuntimeProjectionError(RuntimeError):
    """Raised when a stage runtime cannot be projected safely and exactly."""


def _safe_relative_path(value: object, *, role: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise RuntimeProjectionError(f"{role} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeProjectionError(f"{role} is unsafe: {value}")
    return path


def _open_directory(path: Path, *, role: str) -> int:
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise RuntimeProjectionError(f"{role} is unsafe: {path}") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise RuntimeProjectionError(f"{role} is unsafe: {path}")
    return descriptor


def _read_regular_file(
    root: Path,
    relative: PurePosixPath,
    *,
    role: str,
) -> tuple[bytes, int]:
    root_descriptor = _open_directory(root, role=f"{role} root")
    current_descriptor = root_descriptor
    try:
        for component in relative.parts[:-1]:
            try:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=current_descriptor,
                )
            except FileNotFoundError:
                raise
            except OSError as error:
                raise RuntimeProjectionError(f"{role} is unsafe: {relative}") from error
            if current_descriptor != root_descriptor:
                os.close(current_descriptor)
            current_descriptor = next_descriptor

        try:
            file_descriptor = os.open(
                relative.parts[-1],
                _FILE_FLAGS,
                dir_fd=current_descriptor,
            )
        except FileNotFoundError:
            raise
        except OSError as error:
            raise RuntimeProjectionError(f"{role} is unsafe: {relative}") from error
        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeProjectionError(f"{role} is unsafe: {relative}")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks), stat.S_IMODE(metadata.st_mode)
        finally:
            os.close(file_descriptor)
    finally:
        if current_descriptor != root_descriptor:
            os.close(current_descriptor)
        os.close(root_descriptor)


def _declared_stage(specification: dict[str, Any], stage: str) -> dict[str, Any]:
    if stage not in PROJECTABLE_STAGES:
        raise RuntimeProjectionError(f"stage is not projectable: {stage}")
    stages = specification.get("stages")
    if not isinstance(stages, dict) or not isinstance(stages.get(stage), dict):
        raise RuntimeProjectionError(f"stage declaration is missing: {stage}")
    return stages[stage]


def _collect_projection(
    *,
    repository: Path,
    runtime_root: Path,
    specification: dict[str, Any],
    stage: str,
) -> dict[PurePosixPath, tuple[bytes, int]]:
    declaration = _declared_stage(specification, stage)
    projection: dict[PurePosixPath, tuple[bytes, int]] = {}

    source_paths = declaration.get("source_paths")
    if not isinstance(source_paths, list):
        raise RuntimeProjectionError(f"{stage} source_paths must be a list")
    for raw_source in source_paths:
        source = _safe_relative_path(raw_source, role=f"{stage} source path")
        try:
            relative = source.relative_to(MEDIA_SOURCE_PREFIX)
        except ValueError:
            continue
        try:
            projection[relative] = _read_regular_file(
                repository,
                source,
                role="repository source",
            )
        except FileNotFoundError as error:
            raise RuntimeProjectionError(
                f"declared {stage} source is missing: {source}"
            ) from error

    runtime_inputs = declaration.get("runtime_inputs")
    if not isinstance(runtime_inputs, list):
        raise RuntimeProjectionError(f"{stage} runtime_inputs must be a list")
    for item in runtime_inputs:
        if not isinstance(item, dict) or not isinstance(item.get("required"), bool):
            raise RuntimeProjectionError(f"{stage} runtime input declaration is invalid")
        relative = _safe_relative_path(
            item.get("path"),
            role=f"{stage} runtime input",
        )
        if relative in projection:
            raise RuntimeProjectionError(
                f"{stage} projection path is declared more than once: {relative}"
            )
        try:
            projection[relative] = _read_regular_file(
                runtime_root,
                relative,
                role="runtime input",
            )
        except FileNotFoundError as error:
            if item["required"]:
                raise RuntimeProjectionError(
                    f"required {stage} runtime input is missing: {relative}"
                ) from error

    if PurePosixPath("orchestrator/__init__.py") not in projection:
        raise RuntimeProjectionError(
            f"{stage} projection omits orchestrator/__init__.py"
        )
    return projection


def _create_projection_root(output_root: Path) -> int:
    if not output_root.name or output_root.name in {".", ".."}:
        raise RuntimeProjectionError(f"output root is unsafe: {output_root}")
    parent_descriptor = _open_directory(output_root.parent, role="output parent")
    try:
        try:
            os.mkdir(output_root.name, mode=0o700, dir_fd=parent_descriptor)
        except OSError as error:
            raise RuntimeProjectionError(
                f"output root already exists or is unsafe: {output_root}"
            ) from error
        return os.open(output_root.name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _write_projection_file(
    root_descriptor: int,
    relative: PurePosixPath,
    content: bytes,
    source_mode: int,
) -> None:
    current_descriptor = os.dup(root_descriptor)
    try:
        for component in relative.parts[:-1]:
            try:
                os.mkdir(component, mode=0o700, dir_fd=current_descriptor)
            except FileExistsError:
                pass
            try:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=current_descriptor,
                )
            except OSError as error:
                raise RuntimeProjectionError(
                    f"projection output directory is unsafe: {relative}"
                ) from error
            os.close(current_descriptor)
            current_descriptor = next_descriptor

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(
                relative.parts[-1],
                flags,
                0o600,
                dir_fd=current_descriptor,
            )
        except OSError as error:
            raise RuntimeProjectionError(
                f"projection output file is unsafe: {relative}"
            ) from error
        try:
            view = memoryview(content)
            while view:
                written = os.write(file_descriptor, view)
                view = view[written:]
            os.fchmod(file_descriptor, 0o444 | (source_mode & 0o111))
        finally:
            os.close(file_descriptor)
    finally:
        os.close(current_descriptor)


def _seal_projection_directories(output_root: Path) -> None:
    directories = [path for path in output_root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        os.chmod(directory, 0o555, follow_symlinks=False)
    os.chmod(output_root, 0o555, follow_symlinks=False)


def project_stage_runtime(
    *,
    repository: Path,
    runtime_root: Path,
    output_root: Path,
    specification: dict[str, Any],
    stage: str,
) -> None:
    projection = _collect_projection(
        repository=Path(repository),
        runtime_root=Path(runtime_root),
        specification=specification,
        stage=stage,
    )
    output_descriptor = _create_projection_root(Path(output_root))
    try:
        for relative in sorted(projection, key=str):
            content, mode = projection[relative]
            _write_projection_file(output_descriptor, relative, content, mode)
    finally:
        os.close(output_descriptor)
    _seal_projection_directories(Path(output_root))


def _load_specification(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeProjectionError(f"stage specification is unsafe: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeProjectionError(f"could not read stage specification: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeProjectionError("stage specification must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--stage", choices=sorted(PROJECTABLE_STAGES), required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        project_stage_runtime(
            repository=arguments.repository,
            runtime_root=arguments.runtime_root,
            output_root=arguments.output_root,
            specification=_load_specification(arguments.spec),
            stage=arguments.stage,
        )
    except RuntimeProjectionError as error:
        parser.exit(1, f"asahi-runtime-projection: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
