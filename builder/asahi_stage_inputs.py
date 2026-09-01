#!/usr/bin/env python3
"""Exact whole-file input declarations for Apple Silicon build stages."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tokenize
from typing import Any


SCHEMA_VERSION = 1
LOCAL_REPOSITORY_ROOTS = ("archiso", "bin", "builder", "configs")
LOCAL_REPOSITORY_PATH = re.compile(
    r"(?<![A-Za-z0-9_./+@-])/?(?:archiso|bin|builder|configs)/"
    r"[A-Za-z0-9_./+@-]+"
)
SHELL_SOURCE = re.compile(
    r"(?m)^[ \t]*(?:source|\.)[ \t]+(?P<argument>\"[^\"\n]+\"|'[^'\n]+'|[^\s;&|]+)"
)
SHELL_RELATIVE_EXECUTION = re.compile(
    r"(?m)(?<![A-Za-z0-9_./+@-])"
    r"(?P<argument>\"(?:\./|\.\./)[^\"\n]+\"|'(?:\./|\.\./)[^'\n]+'|"
    r"(?:\./|\.\./)[A-Za-z0-9_./+@-]+)"
)
SHELL_VARIABLE_PREFIX = re.compile(
    r"^\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)/(?P<suffix>.+)$"
)
SHELL_VARIABLE_REPOSITORY_PATH = re.compile(
    r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)/"
    r"(?P<path>(?:archiso|bin|builder|configs)/[A-Za-z0-9_./+@-]+)"
)
SAFE_STAGE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SAFE_RUNTIME_SETTING = re.compile(r"^[A-Z][A-Z0-9_]*$")


class StageInputError(RuntimeError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _json_file_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _effective_stage_mode(stage: str, requested_mode: str) -> str:
    return "shared" if stage == "builder-toolchain" else requested_mode


def load_specification(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise StageInputError(f"stage input specification is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise StageInputError("stage input specification must be an object")
    return value


def _producer_paths(specification: dict[str, Any], stage: str) -> list[str]:
    return [
        *specification["common_producer_inputs"],
        *specification["stages"][stage]["source_paths"],
    ]


def _admission_paths(specification: dict[str, Any], stage: str) -> list[str]:
    return [
        *specification["common_admission_inputs"],
        *specification["stages"][stage]["admission_paths"],
    ]


def _producer_declaration(declaration: dict[str, Any]) -> dict[str, Any]:
    # `dispatches` is included because it is a suppression list: entries in it
    # exempt an executed path from the "executed input is omitted" and
    # "cross-stage dispatch is undeclared" guards. Widening it widens what a
    # stage may execute unchecked, so it must invalidate checkpoints produced
    # under the narrower declaration.
    return {
        key: declaration.get(key, [])
        for key in (
            "depends_on",
            "dispatches",
            "entrypoints",
            "source_paths",
            "lock_paths",
            "runtime_inputs",
            "runtime_settings",
        )
    }


def _admission_declaration(declaration: dict[str, Any]) -> dict[str, Any]:
    return {
        "control_entrypoints": declaration.get("control_entrypoints", []),
        "admission_paths": declaration["admission_paths"],
    }


def _validated_relative_path(relative: str, role: str) -> Path:
    candidate = Path(relative) if isinstance(relative, str) else Path("..")
    if (
        not isinstance(relative, str)
        or not relative
        or candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.as_posix() != relative
    ):
        raise StageInputError(f"unsafe {role}: {relative!r}")
    if ":" in relative:
        raise StageInputError(f"line-range source identities are forbidden: {relative}")
    if "__pycache__" in Path(relative).parts:
        raise StageInputError(f"mutable Python cache is forbidden as {role}: {relative}")
    return candidate


def _validate_relative_ancestors(
    repository: Path,
    candidate: Path,
    role: str,
) -> None:
    ancestor = repository
    for component in candidate.parts[:-1]:
        ancestor /= component
        try:
            ancestor_status = ancestor.lstat()
        except FileNotFoundError as error:
            raise StageInputError(
                f"missing {role}: {candidate.as_posix()}"
            ) from error
        if stat.S_ISLNK(ancestor_status.st_mode):
            raise StageInputError(
                f"symlinked ancestor is forbidden for {role}: "
                f"{candidate.as_posix()}"
            )
        if not stat.S_ISDIR(ancestor_status.st_mode):
            raise StageInputError(
                f"non-directory ancestor for {role}: {candidate.as_posix()}"
            )


def _validate_relative_file(repository: Path, relative: str, role: str) -> None:
    candidate = _validated_relative_path(relative, role)
    _validate_relative_ancestors(repository, candidate, role)
    path = repository / candidate
    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise StageInputError(f"missing {role}: {relative}") from error
    if stat.S_ISLNK(status.st_mode):
        raise StageInputError(f"symlink is forbidden for {role}: {relative}")
    if not stat.S_ISREG(status.st_mode):
        raise StageInputError(f"{role} must be a real whole file: {relative}")


def _read_regular_file_beneath(
    root: Path,
    relative: str,
    role: str,
) -> tuple[bytes, int]:
    candidate = _validated_relative_path(relative, role)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise StageInputError(f"safe reads are unsupported for {role}")
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        try:
            root_fd = os.open(root, os.O_RDONLY | directory | nofollow)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise StageInputError(f"unsafe root for {role}: {root}") from error
        directory_fds.append(root_fd)
        parent_fd = root_fd
        for component in candidate.parts[:-1]:
            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                raise
            except OSError as error:
                raise StageInputError(
                    f"symlinked ancestor is forbidden or unsafe for {role}: "
                    f"{relative}"
                ) from error
            directory_fds.append(child_fd)
            parent_fd = child_fd
        try:
            file_fd = os.open(
                candidate.name,
                os.O_RDONLY | nofollow,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            raise
        except OSError as error:
            raise StageInputError(
                f"symlink is forbidden or unsafe for {role}: {relative}"
            ) from error
        status = os.fstat(file_fd)
        if not stat.S_ISREG(status.st_mode):
            raise StageInputError(f"{role} must be a real whole file: {relative}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(status.st_mode) & 0o111
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _add_local_candidate(
    repository: Path,
    candidate: Path,
    discovered: set[str],
) -> None:
    repository = repository.absolute()
    candidate = candidate.absolute()
    try:
        relative = candidate.relative_to(repository)
    except ValueError:
        return
    if not relative.parts or relative.parts[0] not in LOCAL_REPOSITORY_ROOTS:
        return
    try:
        status = candidate.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
        discovered.add(relative.as_posix())


def _local_python_files(repository: Path) -> list[Path]:
    files: list[Path] = []
    for root_name in LOCAL_REPOSITORY_ROOTS:
        local_root = repository / root_name
        if not local_root.is_dir() or local_root.is_symlink():
            continue
        files.extend(path for path in local_root.rglob("*.py") if path.is_file())
    return files


def _add_python_package_initializers(
    repository: Path,
    candidate: Path,
    discovered: set[str],
) -> None:
    parent = candidate.parent
    while parent != repository and repository in parent.parents:
        initializer = parent / "__init__.py"
        _add_local_candidate(repository, initializer, discovered)
        parent = parent.parent


def _resolve_python_module(
    *,
    repository: Path,
    current_path: Path,
    module: str,
    local_python_files: list[Path],
    relative_base: Path | None = None,
) -> set[str]:
    discovered: set[str] = set()
    parts = tuple(part for part in module.split(".") if part)
    if not parts:
        return discovered

    direct_bases = [relative_base] if relative_base is not None else [
        current_path.parent,
        repository,
    ]
    for base in direct_bases:
        if base is None:
            continue
        module_path = base.joinpath(*parts)
        for candidate in (module_path.with_suffix(".py"), module_path / "__init__.py"):
            before = set(discovered)
            _add_local_candidate(repository, candidate, discovered)
            if discovered != before:
                _add_python_package_initializers(repository, candidate, discovered)

    if relative_base is None:
        for candidate in local_python_files:
            relative = candidate.relative_to(repository)
            module_parts = relative.with_suffix("").parts
            if module_parts[-1:] == ("__init__",):
                module_parts = module_parts[:-1]
            if len(module_parts) >= len(parts) and module_parts[-len(parts) :] == parts:
                _add_local_candidate(repository, candidate, discovered)
                _add_python_package_initializers(repository, candidate, discovered)
    return discovered


def _python_imports(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def _discover_python_imports(
    repository: Path,
    relative_path: str,
    text: str,
    local_python_files: list[Path],
) -> set[str]:
    try:
        tree = ast.parse(text, filename=relative_path)
    except SyntaxError as error:
        raise StageInputError(
            f"executed Python input is not parseable: {relative_path}"
        ) from error

    current_path = repository / relative_path
    discovered: set[str] = set()
    for statement in _python_imports(tree):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                discovered.update(
                    _resolve_python_module(
                        repository=repository,
                        current_path=current_path,
                        module=alias.name,
                        local_python_files=local_python_files,
                    )
                )
            continue

        relative_base: Path | None = None
        if statement.level:
            relative_base = current_path.parent
            for _ in range(statement.level - 1):
                relative_base = relative_base.parent
        module = statement.module or ""
        if module:
            discovered.update(
                _resolve_python_module(
                    repository=repository,
                    current_path=current_path,
                    module=module,
                    local_python_files=local_python_files,
                    relative_base=relative_base,
                )
            )
        for alias in statement.names:
            imported_module = ".".join(part for part in (module, alias.name) if part)
            discovered.update(
                _resolve_python_module(
                    repository=repository,
                    current_path=current_path,
                    module=imported_module,
                    local_python_files=local_python_files,
                    relative_base=relative_base,
                )
            )
    return discovered


def _shell_without_comments(text: str) -> str:
    """Remove real shell comments while preserving quoted hashes and layout."""
    result: list[str] = []
    in_single_quote = False
    in_double_quote = False
    escaped = False
    in_comment = False
    at_word_start = True

    for character in text:
        if in_comment:
            if character == "\n":
                result.append(character)
                in_comment = False
                at_word_start = True
            else:
                result.append(" ")
            continue

        if character == "\n":
            result.append(character)
            escaped = False
            at_word_start = True
            continue

        if escaped:
            result.append(character)
            escaped = False
            at_word_start = False
            continue

        if character == "\\" and not in_single_quote:
            result.append(character)
            escaped = True
            at_word_start = False
            continue

        if character == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            result.append(character)
            at_word_start = False
            continue

        if character == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            result.append(character)
            at_word_start = False
            continue

        if (
            character == "#"
            and not in_single_quote
            and not in_double_quote
            and at_word_start
        ):
            result.append(" ")
            in_comment = True
            continue

        result.append(character)
        at_word_start = (
            not in_single_quote
            and not in_double_quote
            and (character.isspace() or character in ";&|()<>")
        )

    return "".join(result)


def _discover_direct_local_inputs(
    repository: Path,
    relative_path: str,
    local_python_files: list[Path],
) -> tuple[set[str], set[str]]:
    path = repository / relative_path
    content, _ = _read_regular_file_beneath(
        repository,
        relative_path,
        "executed local input",
    )
    text = content.decode("utf-8", errors="ignore")
    scan_text = text
    if path.suffix == ".py":
        try:
            scan_text = tokenize.untokenize(
                token
                for token in tokenize.generate_tokens(io.StringIO(text).readline)
                if token.type != tokenize.COMMENT
            )
        except (IndentationError, tokenize.TokenError) as error:
            raise StageInputError(
                f"executed Python input is not tokenizable: {relative_path}"
            ) from error
    elif path.suffix in {".sh", ""}:
        scan_text = _shell_without_comments(text)
    discovered: set[str] = set()
    explicitly_executed: set[str] = set()
    for match in LOCAL_REPOSITORY_PATH.findall(scan_text):
        candidate = match.lstrip("/").rstrip(".,;:)\"]}'")
        _add_local_candidate(repository, repository / candidate, discovered)
    for match in SHELL_VARIABLE_REPOSITORY_PATH.finditer(scan_text):
        candidate = match.group("path").rstrip(".,;:)\"]}'")
        _add_local_candidate(repository, repository / candidate, discovered)

    if path.suffix in {".sh", ""}:
        for match in SHELL_SOURCE.finditer(scan_text):
            argument = match.group("argument").strip("\"'")
            variable_match = SHELL_VARIABLE_PREFIX.fullmatch(argument)
            candidates: list[Path] = []
            if argument.startswith("./") or argument.startswith("../"):
                candidates.append(path.parent / argument)
            elif variable_match is not None:
                suffix = variable_match.group("suffix")
                candidates.extend((path.parent / suffix, repository / suffix))
            elif not argument.startswith(("/", "$")):
                candidates.extend((path.parent / argument, repository / argument))
            for candidate in candidates:
                normalized = Path(os.path.normpath(candidate))
                before = set(discovered)
                _add_local_candidate(repository, normalized, discovered)
                explicitly_executed.update(discovered - before)
        for match in SHELL_RELATIVE_EXECUTION.finditer(scan_text):
            argument = match.group("argument").strip("\"'")
            normalized = Path(os.path.normpath(path.parent / argument))
            before = set(discovered)
            _add_local_candidate(repository, normalized, discovered)
            explicitly_executed.update(discovered - before)

    if path.suffix == ".py":
        imported = _discover_python_imports(
            repository,
            relative_path,
            text,
            local_python_files,
        )
        discovered.update(imported)
        explicitly_executed.update(imported)
    discovered.discard(relative_path)
    explicitly_executed.discard(relative_path)
    return discovered, explicitly_executed


def _is_executable_local_input(repository: Path, relative_path: str) -> bool:
    path = repository / relative_path
    if path.suffix in {".py", ".sh"}:
        return True
    try:
        status = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(status.st_mode) and bool(
        stat.S_IMODE(status.st_mode) & 0o111
    )


def _discover_executed_local_inputs(
    repository: Path,
    entrypoint: str,
    declared: set[str],
) -> set[str]:
    local_python_files = _local_python_files(repository)
    discovered: set[str] = set()
    visited: set[str] = set()
    pending = [entrypoint]
    while pending:
        relative_path = pending.pop()
        if relative_path in visited:
            continue
        visited.add(relative_path)
        direct, explicitly_executed = _discover_direct_local_inputs(
            repository,
            relative_path,
            local_python_files,
        )
        discovered.update(direct)
        traversable = explicitly_executed | {
            path
            for path in direct
            if _is_executable_local_input(repository, path)
        }
        pending.extend(sorted((traversable & declared) - visited, reverse=True))
    return discovered


def validate_specification(
    repository: Path,
    specification: dict[str, Any],
) -> None:
    if specification.get("schema_version") != SCHEMA_VERSION:
        raise StageInputError("unsupported stage input specification schema")
    if set(specification) != {
        "schema_version",
        "common_producer_inputs",
        "common_admission_inputs",
        "stage_order",
        "stages",
    }:
        raise StageInputError("stage input specification has unknown or missing fields")
    stage_order = specification.get("stage_order")
    stages = specification.get("stages")
    if not isinstance(stage_order, list) or not stage_order:
        raise StageInputError("stage_order must be a non-empty list")
    if not isinstance(stages, dict) or set(stage_order) != set(stages):
        raise StageInputError("stage_order and stages must name the same stages")
    if len(stage_order) != len(set(stage_order)):
        raise StageInputError("stage_order contains duplicates")

    common_producer = specification["common_producer_inputs"]
    common_admission = specification["common_admission_inputs"]
    for role, paths in (
        ("common producer inputs", common_producer),
        ("common admission inputs", common_admission),
    ):
        if not isinstance(paths, list) or len(paths) != len(set(paths)):
            raise StageInputError(f"{role} must be unique")
    if set(common_producer) & set(common_admission):
        raise StageInputError("common producer and admission inputs must be disjoint")
    for path in common_producer:
        _validate_relative_file(repository, path, "common producer input")
    for path in common_admission:
        _validate_relative_file(repository, path, "common admission input")

    execution_boundary_owners: dict[str, set[str]] = {}
    for owner, declaration in stages.items():
        if not isinstance(declaration, dict):
            continue
        for field in ("entrypoints", "control_entrypoints"):
            paths = declaration.get(field, [])
            if not isinstance(paths, list):
                continue
            for path in paths:
                if isinstance(path, str):
                    execution_boundary_owners.setdefault(path, set()).add(owner)

    def dependency_ancestors(stage: str) -> set[str]:
        ancestors: set[str] = set()
        pending = [stage]
        while pending:
            current = pending.pop()
            declaration = stages.get(current)
            if not isinstance(declaration, dict):
                continue
            dependencies = declaration.get("depends_on", [])
            if not isinstance(dependencies, list):
                continue
            for dependency in dependencies:
                if (
                    isinstance(dependency, str)
                    and dependency in stages
                    and dependency not in ancestors
                ):
                    ancestors.add(dependency)
                    pending.append(dependency)
        return ancestors

    stage_ancestors = {
        stage: dependency_ancestors(stage)
        for stage in stage_order
        if isinstance(stage, str)
    }
    earlier: set[str] = set()
    for stage in stage_order:
        if not isinstance(stage, str) or SAFE_STAGE.fullmatch(stage) is None:
            raise StageInputError(f"unsafe stage name: {stage!r}")
        declaration = stages[stage]
        if not isinstance(declaration, dict):
            raise StageInputError(f"stage declaration must be an object: {stage}")
        required_fields = {
            "depends_on",
            "entrypoints",
            "source_paths",
            "admission_paths",
            "lock_paths",
            "runtime_inputs",
            "runtime_settings",
        }
        allowed_fields = required_fields | {"control_entrypoints", "dispatches"}
        if not required_fields.issubset(declaration) or (
            set(declaration) - allowed_fields
        ):
            raise StageInputError(f"stage declaration has unknown or missing fields: {stage}")
        dependencies = declaration["depends_on"]
        source_paths = declaration["source_paths"]
        admission_paths = declaration["admission_paths"]
        entrypoints = declaration["entrypoints"]
        control_entrypoints = declaration.get("control_entrypoints", [])
        dispatches = declaration.get("dispatches", [])
        lock_paths = declaration["lock_paths"]
        runtime_inputs = declaration["runtime_inputs"]
        runtime_settings = declaration["runtime_settings"]
        for name, value in (
            ("depends_on", dependencies),
            ("source_paths", source_paths),
            ("admission_paths", admission_paths),
            ("entrypoints", entrypoints),
            ("control_entrypoints", control_entrypoints),
            ("dispatches", dispatches),
            ("lock_paths", lock_paths),
            ("runtime_settings", runtime_settings),
        ):
            if not isinstance(value, list) or len(value) != len(set(value)):
                raise StageInputError(f"{stage}.{name} must be a unique list")
        if not isinstance(runtime_inputs, list):
            raise StageInputError(f"{stage}.runtime_inputs must be a list")
        runtime_paths: set[str] = set()
        for runtime_input in runtime_inputs:
            if not isinstance(runtime_input, dict) or set(runtime_input) != {
                "path",
                "required",
            }:
                raise StageInputError(
                    f"{stage}.runtime_inputs contains an invalid declaration"
                )
            runtime_path = runtime_input["path"]
            candidate = Path(runtime_path) if isinstance(runtime_path, str) else Path("..")
            if (
                not isinstance(runtime_path, str)
                or not runtime_path
                or candidate.is_absolute()
                or ".." in candidate.parts
                or candidate.as_posix() != runtime_path
                or runtime_path in runtime_paths
            ):
                raise StageInputError(f"unsafe or duplicate runtime input in {stage}")
            if not isinstance(runtime_input["required"], bool):
                raise StageInputError(f"runtime input required flag is invalid in {stage}")
            runtime_paths.add(runtime_path)
        for setting in runtime_settings:
            if not isinstance(setting, str) or SAFE_RUNTIME_SETTING.fullmatch(setting) is None:
                raise StageInputError(f"unsafe runtime setting in {stage}: {setting!r}")
        if not set(dependencies).issubset(earlier):
            raise StageInputError(f"{stage} has a missing or forward dependency")
        for dispatch in dispatches:
            if not isinstance(dispatch, str):
                raise StageInputError(f"{stage}.dispatches contains an unsafe path")
            owners = execution_boundary_owners.get(dispatch, set()) - {stage}
            if not owners:
                raise StageInputError(
                    f"{stage} dispatch target is not a foreign stage boundary: {dispatch}"
                )
            comparable = any(
                owner in stage_ancestors.get(stage, set())
                or stage in stage_ancestors.get(owner, set())
                for owner in owners
            )
            if not comparable:
                raise StageInputError(
                    f"{stage} dispatch target is outside its dependency graph: {dispatch}"
                )
        for path in source_paths:
            _validate_relative_file(repository, path, f"{stage} source input")
        for path in admission_paths:
            _validate_relative_file(repository, path, f"{stage} admission input")
        producer_declared = set(common_producer) | set(source_paths)
        admission_declared = set(common_admission) | set(admission_paths)
        if producer_declared & admission_declared:
            raise StageInputError(
                f"{stage} producer and admission inputs must be disjoint"
            )
        all_declared = producer_declared | admission_declared
        missing_entrypoints = sorted(set(entrypoints) - producer_declared)
        if missing_entrypoints:
            raise StageInputError(
                f"{stage} entrypoint is not a declared source input: "
                + ", ".join(missing_entrypoints)
            )
        missing_control_entrypoints = sorted(
            set(control_entrypoints) - admission_declared
        )
        if missing_control_entrypoints:
            raise StageInputError(
                f"{stage} control entrypoint is not a declared admission input: "
                + ", ".join(missing_control_entrypoints)
            )
        for entrypoint in (*entrypoints, *control_entrypoints):
            discovered = _discover_executed_local_inputs(
                repository,
                entrypoint,
                all_declared,
            )
            for omitted in sorted(discovered - all_declared - set(dispatches)):
                raise StageInputError(
                    f"executed input is omitted from {stage}: {omitted}"
                )
            foreign_boundaries = {
                path
                for path in discovered & set(execution_boundary_owners)
                if stage not in execution_boundary_owners[path]
            }
            for undeclared_dispatch in sorted(foreign_boundaries - set(dispatches)):
                raise StageInputError(
                    f"cross-stage dispatch is undeclared from {stage}: "
                    f"{undeclared_dispatch}"
                )
        for lock_path in lock_paths:
            if not isinstance(lock_path, str) or not lock_path:
                raise StageInputError(f"unsafe lock path in {stage}: {lock_path!r}")
        earlier.add(stage)


def _lock_value(build_lock: dict[str, Any], path: str, mode: str) -> Any:
    components = [mode if component == "$mode" else component for component in path.split(".")]
    value: Any = build_lock
    for component in components:
        if not isinstance(value, dict) or component not in value:
            raise StageInputError(f"source lock path is missing: {path}")
        value = value[component]
    return value


def build_lock_projection(
    build_lock: dict[str, Any],
    stage: str,
    declaration: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "mode": mode,
        "inputs": {
            path: _lock_value(build_lock, path, mode)
            for path in sorted(declaration["lock_paths"])
        },
    }


def _source_records(
    repository: Path,
    paths: list[str],
    content_overrides: dict[str, bytes],
) -> list[dict[str, Any]]:
    records = []
    for relative in sorted(paths):
        actual_content, mode = _read_regular_file_beneath(
            repository,
            relative,
            "declared stage source",
        )
        content = content_overrides.get(relative, actual_content)
        records.append(
            {
                "path": relative,
                "size_bytes": len(content),
                "sha256": _file_digest(content),
                "executable_mode": mode,
            }
        )
    return records


def declared_stage_fingerprints(
    *,
    repository: Path,
    specification: dict[str, Any],
    build_lock: dict[str, Any],
    mode: str,
    content_overrides: dict[str, bytes] | None = None,
) -> dict[str, str]:
    validate_specification(repository, specification)
    if mode not in {"diagnostic", "qualification"}:
        raise StageInputError(f"unsupported build mode: {mode}")
    overrides = content_overrides or {}
    producer_paths = {
        path
        for stage in specification["stage_order"]
        for path in _producer_paths(specification, stage)
    }
    admission_paths = {
        path
        for stage in specification["stage_order"]
        for path in _admission_paths(specification, stage)
    }
    unknown_overrides = set(overrides) - producer_paths - admission_paths
    if unknown_overrides:
        raise StageInputError(
            f"content override is not a declared source: {sorted(unknown_overrides)[0]}"
        )

    fingerprints: dict[str, str] = {}
    for stage in specification["stage_order"]:
        declaration = specification["stages"][stage]
        producer_declaration = _producer_declaration(declaration)
        effective_mode = _effective_stage_mode(stage, mode)
        value = {
            "schema_version": SCHEMA_VERSION,
            "stage": stage,
            "mode": effective_mode,
            "declaration_sha256": _digest(producer_declaration),
            "source_lock": build_lock_projection(
                build_lock,
                stage,
                declaration,
                effective_mode,
            ),
            "sources": _source_records(
                repository,
                _producer_paths(specification, stage),
                overrides,
            ),
            "dependencies": {
                dependency: fingerprints[dependency]
                for dependency in declaration["depends_on"]
            },
        }
        fingerprints[stage] = _digest(value)
    return fingerprints


def declared_admission_fingerprints(
    *,
    repository: Path,
    specification: dict[str, Any],
    mode: str,
    content_overrides: dict[str, bytes] | None = None,
) -> dict[str, str]:
    validate_specification(repository, specification)
    if mode not in {"diagnostic", "qualification"}:
        raise StageInputError(f"unsupported build mode: {mode}")
    overrides = content_overrides or {}
    admission_paths = {
        path
        for stage in specification["stage_order"]
        for path in _admission_paths(specification, stage)
    }
    producer_paths = {
        path
        for stage in specification["stage_order"]
        for path in _producer_paths(specification, stage)
    }
    unknown_overrides = set(overrides) - admission_paths - producer_paths
    if unknown_overrides:
        raise StageInputError(
            f"content override is not a declared admission source: "
            f"{sorted(unknown_overrides)[0]}"
        )

    fingerprints: dict[str, str] = {}
    for stage in specification["stage_order"]:
        declaration = specification["stages"][stage]
        admission_declaration = _admission_declaration(declaration)
        effective_mode = _effective_stage_mode(stage, mode)
        value = {
            "schema_version": SCHEMA_VERSION,
            "verification_kind": "asahi-checkpoint-admission-policy",
            "stage": stage,
            "mode": effective_mode,
            "declaration_sha256": _digest(admission_declaration),
            "sources": _source_records(
                repository,
                _admission_paths(specification, stage),
                overrides,
            ),
            "dependencies": {
                dependency: fingerprints[dependency]
                for dependency in declaration["depends_on"]
            },
        }
        fingerprints[stage] = _digest(value)
    return fingerprints


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_stage_source_manifest(
    repository: Path,
    stage: str,
    paths: list[str],
    declaration: dict[str, Any],
) -> dict[str, Any]:
    records = _source_records(repository, paths, {})
    producer_declaration = _producer_declaration(declaration)
    relevant_commits = []
    for relative in sorted(paths):
        commit = _git(repository, "log", "-1", "--format=%H", "--", relative)
        relevant_commits.append(
            {"path": relative, "commit": commit or "untracked"}
        )
    status = _git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *sorted(paths),
    ).splitlines()
    identity_value = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "declaration": producer_declaration,
        "declaration_sha256": _digest(producer_declaration),
        "paths": sorted(paths),
        "entries": records,
        "relevant_commits": relevant_commits,
    }
    return identity_value | {
        "status": sorted(line for line in status if line),
        "source_identity": _digest(identity_value),
    }


def build_stage_admission_manifest(
    *,
    repository: Path,
    stage: str,
    mode: str,
    paths: list[str],
    declaration: dict[str, Any],
    dependency_policy_identities: dict[str, str],
) -> dict[str, Any]:
    admission_declaration = _admission_declaration(declaration)
    records = _source_records(repository, paths, {})
    relevant_commits = []
    for relative in sorted(paths):
        commit = _git(repository, "log", "-1", "--format=%H", "--", relative)
        relevant_commits.append(
            {"path": relative, "commit": commit or "untracked"}
        )
    status = _git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *sorted(paths),
    ).splitlines()
    identity_value = {
        "schema_version": SCHEMA_VERSION,
        "verification_kind": "asahi-checkpoint-admission-policy",
        "stage": stage,
        "mode": mode,
        "declaration_sha256": _digest(admission_declaration),
        "sources": records,
        "dependencies": dict(sorted(dependency_policy_identities.items())),
    }
    return identity_value | {
        "declaration": admission_declaration,
        "paths": sorted(paths),
        "relevant_commits": relevant_commits,
        "status": sorted(line for line in status if line),
        "admission_policy_identity": _digest(identity_value),
    }


def build_stage_runtime_manifest(
    *,
    root: Path,
    stage: str,
    declaration: dict[str, Any],
    settings: dict[str, str],
) -> dict[str, Any]:
    try:
        root_status = root.lstat()
    except FileNotFoundError as error:
        raise StageInputError(f"runtime input root is missing: {root}") from error
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise StageInputError(f"runtime input root is not a real directory: {root}")
    expected_settings = set(declaration["runtime_settings"])
    if set(settings) != expected_settings or any(
        not isinstance(value, str) for value in settings.values()
    ):
        raise StageInputError(f"runtime settings are incomplete or excessive for {stage}")

    entries: list[dict[str, Any]] = []
    for runtime_input in sorted(
        declaration["runtime_inputs"], key=lambda item: item["path"]
    ):
        relative = runtime_input["path"]
        try:
            content, executable_mode = _read_regular_file_beneath(
                root,
                relative,
                f"{stage} runtime input",
            )
        except FileNotFoundError:
            if runtime_input["required"]:
                raise StageInputError(
                    f"required {stage} runtime input is missing: {relative}"
                )
            entries.append({"path": relative, "present": False})
            continue
        entries.append(
            {
                "path": relative,
                "present": True,
                "size_bytes": len(content),
                "sha256": _file_digest(content),
                "executable_mode": executable_mode,
            }
        )
    value = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "root_role": f"{stage}-runtime",
        "entries": entries,
        "settings": dict(sorted(settings.items())),
    }
    return value | {"input_digest": _digest(value)}


PRODUCT_PROJECTIONS = {
    "configured-target": (
        "boot_backend",
        "boot_filesystem_uuid",
        "esp_volume_id",
        "kernel_package",
        "root_filesystem_uuid",
    ),
    "finalized-boot": (
        "boot_backend",
        "boot_filesystem_uuid",
        "esp_volume_id",
        "kernel_package",
        "root_filesystem_uuid",
    ),
}


def build_stage_product_manifest(
    *,
    product: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    if product.get("schema_version") != SCHEMA_VERSION:
        raise StageInputError("unsupported product schema")
    keys = PRODUCT_PROJECTIONS.get(stage)
    if keys is None:
        raise StageInputError(f"unsupported stage product projection: {stage}")
    missing = [key for key in keys if key not in product]
    if missing:
        raise StageInputError(f"product projection is missing: {missing[0]}")
    value = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "inputs": {key: product[key] for key in keys},
    }
    return value | {"input_digest": _digest(value)}


def _parse_settings(values: list[str]) -> dict[str, str]:
    settings: dict[str, str] = {}
    for value in values:
        name, separator, setting_value = value.partition("=")
        if not separator or SAFE_RUNTIME_SETTING.fullmatch(name) is None:
            raise StageInputError(f"invalid runtime setting: {value!r}")
        if name in settings:
            raise StageInputError(f"duplicate runtime setting: {name}")
        settings[name] = setting_value
    return settings


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_file_bytes(value))
    path.chmod(0o444)


def _write_json_beneath(root: Path, relative: Path, value: Any) -> None:
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() in {"", "."}
    ):
        raise StageInputError(f"unsafe generated stage-input path: {relative}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise StageInputError("safe generated stage-input writes are unsupported")

    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        try:
            root_fd = os.open(root, os.O_RDONLY | directory | nofollow)
        except OSError as error:
            raise StageInputError(
                f"unsafe generated stage-input root: {root}"
            ) from error
        directory_fds.append(root_fd)
        parent_fd = root_fd
        for component in relative.parts[:-1]:
            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                os.mkdir(component, mode=0o755, dir_fd=parent_fd)
                child_fd = os.open(
                    component,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=parent_fd,
                )
            except OSError as error:
                raise StageInputError(
                    f"unsafe generated stage-input directory: {relative.parent}"
                ) from error
            directory_fds.append(child_fd)
            parent_fd = child_fd

        try:
            file_fd = os.open(
                relative.name,
                os.O_WRONLY | os.O_CREAT | nofollow,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise StageInputError(
                f"unsafe generated stage-input file: {relative}"
            ) from error
        output_status = os.fstat(file_fd)
        if not stat.S_ISREG(output_status.st_mode) or output_status.st_nlink != 1:
            raise StageInputError(
                f"generated stage-input output must be a private file: {relative}"
            )
        os.ftruncate(file_fd, 0)
        content = _json_file_bytes(value)
        written = 0
        while written < len(content):
            count = os.write(file_fd, content[written:])
            if count <= 0:
                raise StageInputError(
                    f"generated stage-input write made no progress: {relative}"
                )
            written += count
        os.fchmod(file_fd, 0o444)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _generated_json_file_record(
    filename: str,
    value: Any,
    *,
    include_executable_mode: bool = False,
) -> dict[str, Any]:
    content = _json_file_bytes(value)
    record: dict[str, Any] = {
        "filename": filename,
        "size_bytes": len(content),
        "sha256": _file_digest(content),
    }
    if include_executable_mode:
        record["executable_mode"] = 0
    return record


def _producer_stage_materials(
    *,
    repository: Path,
    specification: dict[str, Any],
    build_lock: dict[str, Any],
    mode: str,
) -> dict[str, dict[str, Any]]:
    producer_binding_identities = declared_stage_fingerprints(
        repository=repository,
        specification=specification,
        build_lock=build_lock,
        mode=mode,
    )
    materials: dict[str, dict[str, Any]] = {}
    for stage in specification["stage_order"]:
        declaration = specification["stages"][stage]
        effective_mode = _effective_stage_mode(stage, mode)
        source_manifest = build_stage_source_manifest(
            repository,
            stage,
            _producer_paths(specification, stage),
            declaration,
        )
        source_manifest["producer_binding_identity"] = producer_binding_identities[
            stage
        ]
        source_manifest["producer_binding_mode"] = effective_mode
        materials[stage] = {
            "effective_mode": effective_mode,
            "producer_binding_identity": producer_binding_identities[stage],
            "source_manifest": source_manifest,
            "source_lock": build_lock_projection(
                build_lock,
                stage,
                declaration,
                effective_mode,
            ),
        }
    return materials


def declared_stage_identity_bindings(
    *,
    repository: Path,
    specification: dict[str, Any],
    build_lock: dict[str, Any],
    mode: str,
) -> dict[str, dict[str, Any]]:
    """Return exact producer bindings and generated-file claims for each stage."""
    materials = _producer_stage_materials(
        repository=repository,
        specification=specification,
        build_lock=build_lock,
        mode=mode,
    )
    return {
        stage: {
            "effective_mode": material["effective_mode"],
            "producer_binding_identity": material["producer_binding_identity"],
            "source_identity": material["source_manifest"]["source_identity"],
            "source_manifest": _generated_json_file_record(
                "source-manifest.json",
                material["source_manifest"],
                include_executable_mode=True,
            ),
            "source_lock": _generated_json_file_record(
                "source-lock.json",
                material["source_lock"],
            ),
        }
        for stage, material in materials.items()
    }


def generate_stage_inputs(
    *,
    repository: Path,
    specification: dict[str, Any],
    build_lock: dict[str, Any],
    mode: str,
    output_root: Path,
) -> dict[str, Any]:
    validate_specification(repository, specification)
    if output_root.is_symlink():
        raise StageInputError(f"stage input output root is a symlink: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    producer_materials = _producer_stage_materials(
        repository=repository,
        specification=specification,
        build_lock=build_lock,
        mode=mode,
    )
    index: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "mode": mode, "stages": {}}
    admission_index: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "verification_kind": "asahi-checkpoint-admission-policy-index",
        "mode": mode,
        "stages": {},
    }
    admission_policy_identities: dict[str, str] = {}
    for stage in specification["stage_order"]:
        declaration = specification["stages"][stage]
        producer_material = producer_materials[stage]
        effective_mode = producer_material["effective_mode"]
        source_manifest = producer_material["source_manifest"]
        admission_manifest = build_stage_admission_manifest(
            repository=repository,
            stage=stage,
            mode=effective_mode,
            paths=_admission_paths(specification, stage),
            declaration=declaration,
            dependency_policy_identities={
                dependency: admission_policy_identities[dependency]
                for dependency in declaration["depends_on"]
            },
        )
        admission_policy_identities[stage] = admission_manifest[
            "admission_policy_identity"
        ]
        source_lock = producer_material["source_lock"]
        _write_json_beneath(
            output_root,
            Path(stage) / "source-manifest.json",
            source_manifest,
        )
        _write_json_beneath(
            output_root,
            Path(stage) / "admission-policy.json",
            admission_manifest,
        )
        _write_json_beneath(
            output_root,
            Path(stage) / "source-lock.json",
            source_lock,
        )
        index["stages"][stage] = {
            "producer_binding_identity": producer_material[
                "producer_binding_identity"
            ],
            "producer_binding_mode": effective_mode,
            "source_identity": source_manifest["source_identity"],
            "source_lock_sha256": _file_digest(_json_file_bytes(source_lock)),
        }
        admission_index["stages"][stage] = {
            "admission_policy_identity": admission_manifest[
                "admission_policy_identity"
            ],
            "admission_policy_mode": effective_mode,
        }
    _write_json_beneath(output_root, Path("index.json"), index)
    _write_json_beneath(
        output_root,
        Path("admission-index.json"),
        admission_index,
    )
    return index


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "generate", "fingerprints"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--repo-root", type=Path, required=True)
        subparser.add_argument("--spec", type=Path, required=True)
        if command != "validate":
            subparser.add_argument("--build-lock", type=Path, required=True)
            subparser.add_argument(
                "--mode",
                choices=("diagnostic", "qualification"),
                required=True,
            )
        if command == "generate":
            subparser.add_argument("--output-root", type=Path, required=True)
    runtime = subparsers.add_parser("runtime-manifest")
    runtime.add_argument("--root", type=Path, required=True)
    runtime.add_argument("--spec", type=Path, required=True)
    runtime.add_argument("--stage", required=True)
    runtime.add_argument("--setting", action="append", default=[])
    runtime.add_argument("--output", type=Path, required=True)
    product = subparsers.add_parser("product-manifest")
    product.add_argument("--product", type=Path, required=True)
    product.add_argument("--stage", required=True)
    product.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.command == "runtime-manifest":
        specification = load_specification(arguments.spec)
        if arguments.stage not in specification.get("stages", {}):
            raise StageInputError(f"unknown runtime input stage: {arguments.stage}")
        _write_json(
            arguments.output,
            build_stage_runtime_manifest(
                root=arguments.root,
                stage=arguments.stage,
                declaration=specification["stages"][arguments.stage],
                settings=_parse_settings(arguments.setting),
            ),
        )
        return 0
    if arguments.command == "product-manifest":
        _write_json(
            arguments.output,
            build_stage_product_manifest(
                product=json.loads(arguments.product.read_text()),
                stage=arguments.stage,
            ),
        )
        return 0
    repository = arguments.repo_root.resolve()
    specification = load_specification(arguments.spec)
    validate_specification(repository, specification)
    if arguments.command == "validate":
        return 0
    build_lock = json.loads(arguments.build_lock.read_text())
    if arguments.command == "fingerprints":
        print(
            json.dumps(
                declared_stage_fingerprints(
                    repository=repository,
                    specification=specification,
                    build_lock=build_lock,
                    mode=arguments.mode,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    generate_stage_inputs(
        repository=repository,
        specification=specification,
        build_lock=build_lock,
        mode=arguments.mode,
        output_root=arguments.output_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
