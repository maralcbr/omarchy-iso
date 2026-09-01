#!/usr/bin/env python3
"""Plan checkpoint reuse from metadata without hashing or materializing outputs."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any


BUILDER_ROOT = Path(__file__).resolve().parent
if str(BUILDER_ROOT) not in sys.path:
    sys.path.insert(0, str(BUILDER_ROOT))

from asahi_source_impact import (
    INTENT_BOUNDARIES,
    PROFILE_TERMINALS,
    SourceImpactError,
    preview_source_impact,
)
from asahi_stage_inputs import (
    StageInputError,
    build_lock_projection,
    declared_admission_fingerprints,
    declared_stage_identity_bindings,
    validate_specification,
)
import asahi_toolchain_metadata as toolchain_metadata


SCHEMA_VERSION = 1
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IDENTITY_KEYS = {
    "schema_version",
    "stage",
    "mode",
    "source_lock",
    "source_commits",
    "inputs",
    "input_digest",
    "checkpoint_identity",
}
# The three closed shapes the checkpoint library emits. Mirrored rather than
# imported: this planner is contracted to reason from metadata alone, and
# importing the store/restore library would put its hashing and materializing
# code on the planner's dependency surface. The emitted-versus-accepted parity
# test drives real records through both modules so the mirror cannot drift.
RUN_RECORD_KEYS = IDENTITY_KEYS | {
    "outputs",
    "validation",
    "completed_at",
    "elapsed_seconds",
    "cache_hit",
    "checkpoint_manifest",
    "bytes_read",
    "bytes_written",
    "verification_timing",
}
REPRODUCIBILITY_RUN_RECORD_KEYS = RUN_RECORD_KEYS | {"reproducibility_match"}
CACHE_HIT_RUN_RECORD_KEYS = RUN_RECORD_KEYS | {"cache_hit_timing"}
VERIFICATION_TIMING_KEYS = {
    "checkpoint_verification_seconds",
    "content_readback_seconds",
    "transfer_seconds",
}
CACHE_HIT_TIMING_KEYS = {
    "lookup_and_verification_seconds",
    "materialization_and_readback_seconds",
}
MANIFEST_KEYS = IDENTITY_KEYS | {
    "outputs",
    "validation",
    "completed_at",
    "elapsed_seconds",
    "cache_hit",
    "immutable",
}
MIGRATED_MANIFEST_KEYS = MANIFEST_KEYS | {"migration"}
FILE_OUTPUT_KEYS = {
    "kind",
    "size_bytes",
    "sha256",
    "restore_mode",
    "name",
    "storage",
}
DIRECTORY_OUTPUT_KEYS = FILE_OUTPUT_KEYS | {"entries"}
TOOLCHAIN_STAGE = "builder-toolchain"
TOOLCHAIN_MODE = "shared"
# Output kinds a future executor would restore through the destination-set
# contract. A container image is reused by identity instead.
RESTORABLE_KINDS = frozenset({"file", "directory"})
TOOLCHAIN_IDENTITY_KEYS = {
    "schema_version",
    "stage",
    "mode",
    "input_digest",
    "checkpoint_identity",
}
TOOLCHAIN_RUN_KEYS = TOOLCHAIN_IDENTITY_KEYS | {
    "validation",
    "completed_at",
    "elapsed_seconds",
    "cache_hit",
    "output",
}
TOOLCHAIN_MANIFEST_KEYS = {
    "schema_version",
    "stage",
    "mode",
    "declared_inputs",
    "declared_input_digest",
    "actual_inputs",
    "checkpoint_identity",
    "output",
    "validation",
    "completed_at",
    "elapsed_seconds",
    "cache_hit",
    "immutable",
    "environment",
}
TOOLCHAIN_DECLARED_INPUT_KEYS = {
    "base_image",
    "source_lock_sha256",
    "containerfile_sha256",
    "script_sha256",
    "source",
    "toolchain_packages",
}
TOOLCHAIN_ACTUAL_INPUT_KEYS = TOOLCHAIN_DECLARED_INPUT_KEYS | {
    "package_inventory_sha256",
    "package_inventory",
    "synchronized_database_digests",
}
TOOLCHAIN_OUTPUT_KEYS = {
    "image_id",
    "size_bytes",
    "package_inventory_sha256",
}
TOOLCHAIN_COMPATIBILITY_KEYS = {
    "schema_version",
    "reason",
    "source_checkpoint_identity",
    "source_lock_sha256",
    "target_lock_sha256",
}


class CheckpointPlanError(RuntimeError):
    pass


class ProducerBindingMismatch(CheckpointPlanError):
    """A self-consistent checkpoint is stale against current producer inputs."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _bind_plan_digest(plan: dict[str, Any]) -> dict[str, Any]:
    if "plan_digest" in plan:
        raise CheckpointPlanError("plan digest cannot bind an already bound plan")
    return plan | {"plan_digest": _digest(plan)}


def validate_plan_digest(plan: dict[str, Any]) -> None:
    """Reject any plan whose canonical content changed after planning."""
    if not isinstance(plan, dict):
        raise CheckpointPlanError("checkpoint plan must be a JSON object")
    unsigned = dict(plan)
    claimed = unsigned.pop("plan_digest", None)
    if (
        not isinstance(claimed, str)
        or SHA256.fullmatch(claimed) is None
        or not hmac.compare_digest(claimed, _digest(unsigned))
    ):
        raise CheckpointPlanError("checkpoint plan digest is missing or mismatched")


def validate_advisory_selection(
    plan: dict[str, Any],
    *,
    repository: Path,
    specification: dict[str, Any],
    build_lock: dict[str, Any],
    cost_data: dict[str, Any],
    identity_root: Path,
    cache_root: Path,
    expected_profile: str,
) -> dict[str, Any]:
    """Re-plan from current inputs before accepting a metadata-only selection."""

    validate_plan_digest(plan)
    if expected_profile not in PROFILE_TERMINALS:
        raise CheckpointPlanError("advisory selection profile is unsupported")
    source_preview = plan.get("source_preview")
    if not isinstance(source_preview, dict) or source_preview.get("profile") != expected_profile:
        raise CheckpointPlanError("advisory selection profile is mismatched")
    if (
        plan.get("ready_for_execution") is not False
        or plan.get("ready_for_authoritative_execution") is not False
    ):
        raise CheckpointPlanError("metadata-only plan must not authorize execution")
    changed_paths = source_preview.get("changed_paths")
    intent = source_preview.get("intent")
    if not isinstance(changed_paths, list) or not isinstance(intent, str):
        raise CheckpointPlanError("advisory selection source context is invalid")
    current = plan_checkpoint_execution(
        repository=repository,
        specification=specification,
        build_lock=build_lock,
        cost_data=cost_data,
        changed_paths=changed_paths,
        intent=intent,
        profile=expected_profile,
        identity_root=identity_root,
        cache_root=cache_root,
    )
    if not hmac.compare_digest(plan["plan_digest"], current["plan_digest"]):
        raise CheckpointPlanError(
            "advisory selection drifted from current producer, policy, or cache metadata"
        )
    if plan.get("blocked") is not False or plan.get("advisory_selection_ready") is not True:
        raise CheckpointPlanError("blocked checkpoint plan has no advisory selection")
    selection = plan.get("execution_selection")
    if not isinstance(selection, dict):
        raise CheckpointPlanError("advisory selection is invalid")
    return selection


def validate_execution_selection(*_args: Any, **_kwargs: Any) -> None:
    """Metadata-only plans can never authorize an executor."""

    raise CheckpointPlanError(
        "metadata-only checkpoint plan cannot authorize execution; "
        "current admission receipts are required"
    )


def _real_path(path: Path, role: str, *, kind: str) -> os.stat_result:
    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise CheckpointPlanError(f"missing {role}: {path}") from error
    if stat.S_ISLNK(status.st_mode):
        raise CheckpointPlanError(f"symlink is forbidden for {role}: {path}")
    expected = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if not expected(status.st_mode):
        raise CheckpointPlanError(f"{role} is not a real {kind}: {path}")
    return status


def _toolchain_directory_status(
    path: Path,
    role: str,
    *,
    immutable: bool,
) -> os.stat_result:
    status = _real_path(path, role, kind="directory")
    mode = stat.S_IMODE(status.st_mode)
    if status.st_uid != os.geteuid():
        raise CheckpointPlanError(f"{role} is not owned by the current user")
    if mode & 0o022:
        raise CheckpointPlanError(f"{role} is group/world writable")
    if immutable and mode & 0o222:
        raise CheckpointPlanError(f"{role} is writable")
    return status


def _require_real_ancestors(root: Path, path: Path, role: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise CheckpointPlanError(f"{role} escapes its trusted root: {path}") from error
    current = root
    for component in relative.parts[:-1]:
        current /= component
        _real_path(current, f"{role} ancestor", kind="directory")


def _read_json(path: Path, role: str) -> dict[str, Any]:
    _real_path(path, role, kind="file")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CheckpointPlanError(f"{role} is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise CheckpointPlanError(f"{role} must be a JSON object: {path}")
    return value


def _source_file_digest(repository: Path, relative: str, role: str) -> str:
    path = repository / relative
    _require_real_ancestors(repository, path, role)
    _real_path(path, role, kind="file")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise CheckpointPlanError(f"could not read {role}: {path}") from error


def _directory_names(path: Path, role: str) -> set[str]:
    try:
        with os.scandir(path) as entries:
            return {entry.name for entry in entries}
    except OSError as error:
        raise CheckpointPlanError(f"could not inspect {role}: {path}") from error


def _audit_immutable_tree(root: Path, role: str) -> dict[str, dict[str, Any]]:
    """Recursively inspect node metadata only; never open regular-file content."""
    root_status = _real_path(root, role, kind="directory")
    if root_status.st_mode & 0o222:
        raise CheckpointPlanError(f"{role} is writable: {root}")
    pending = [root]
    metadata: dict[str, dict[str, Any]] = {}
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError as error:
            raise CheckpointPlanError(
                f"could not inspect {role}: {directory}"
            ) from error
        for child in children:
            try:
                status = child.stat(follow_symlinks=False)
            except OSError as error:
                raise CheckpointPlanError(
                    f"could not inspect {role} entry: {child.path}"
                ) from error
            child_path = Path(child.path)
            if stat.S_ISLNK(status.st_mode):
                raise CheckpointPlanError(
                    f"symlink is forbidden in {role}: {child_path}"
                )
            if status.st_mode & 0o222:
                raise CheckpointPlanError(
                    f"writable node is forbidden in {role}: {child_path}"
                )
            relative = child_path.relative_to(root).as_posix()
            if stat.S_ISDIR(status.st_mode):
                metadata[relative] = {"kind": "directory"}
                pending.append(child_path)
            elif stat.S_ISREG(status.st_mode):
                metadata[relative] = {
                    "kind": "file",
                    "size_bytes": status.st_size,
                }
            else:
                raise CheckpointPlanError(
                    f"special node is forbidden in {role}: {child_path}"
                )
    return metadata


def _inline_manifest_metadata(
    output: dict[str, Any], stage: str
) -> dict[str, dict[str, Any]]:
    entries = output.get("entries")
    if not isinstance(entries, list):
        raise CheckpointPlanError(f"inline checkpoint entries are invalid: {stage}")
    metadata: dict[str, dict[str, Any]] = {}
    content_entries: list[dict[str, Any]] = []
    total_size = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise CheckpointPlanError(f"inline checkpoint entry is invalid: {stage}")
        relative = entry.get("path")
        if not isinstance(relative, str):
            raise CheckpointPlanError(
                f"inline checkpoint entry path is invalid: {stage}"
            )
        candidate = PurePosixPath(relative)
        if (
            candidate.is_absolute()
            or relative in {"", "."}
            or ".." in candidate.parts
            or relative in metadata
        ):
            raise CheckpointPlanError(
                f"inline checkpoint entry path is unsafe: {stage}"
            )
        kind = entry.get("kind")
        if kind == "directory":
            if set(entry) != {"kind", "path", "restore_mode"}:
                raise CheckpointPlanError(
                    f"inline directory metadata is invalid: {stage}"
                )
            metadata[relative] = {"kind": "directory"}
        elif kind == "file":
            if set(entry) != {
                "kind",
                "path",
                "size_bytes",
                "sha256",
                "restore_mode",
            }:
                raise CheckpointPlanError(f"inline file metadata is invalid: {stage}")
            size = entry.get("size_bytes")
            digest = entry.get("sha256")
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(digest, str)
                or SHA256.fullmatch(digest) is None
            ):
                raise CheckpointPlanError(f"inline file metadata is invalid: {stage}")
            metadata[relative] = {"kind": "file", "size_bytes": size}
            total_size += size
        else:
            raise CheckpointPlanError(
                f"inline checkpoint entry kind is invalid: {stage}"
            )
        restore_mode = entry.get("restore_mode")
        if (
            not isinstance(restore_mode, int)
            or isinstance(restore_mode, bool)
            or not 0 <= restore_mode <= 0o7777
        ):
            raise CheckpointPlanError(f"inline restore mode is invalid: {stage}")
        content_entries.append(
            {key: value for key, value in entry.items() if key != "restore_mode"}
        )
    if total_size != output.get("size_bytes") or _digest(content_entries) != output.get(
        "sha256"
    ):
        raise CheckpointPlanError(
            f"inline checkpoint aggregate metadata is invalid: {stage}"
        )
    return metadata


def _validate_identity(identity: dict[str, Any], *, stage: str, mode: str) -> None:
    if set(identity) != IDENTITY_KEYS:
        raise CheckpointPlanError(f"checkpoint identity fields are invalid: {stage}")
    if identity.get("schema_version") != SCHEMA_VERSION:
        raise CheckpointPlanError(f"checkpoint identity schema is unsupported: {stage}")
    if identity.get("stage") != stage or identity.get("mode") != mode:
        raise CheckpointPlanError(
            f"checkpoint identity stage or mode is mismatched: {stage}"
        )
    unsigned = {
        key: value
        for key, value in identity.items()
        if key not in {"input_digest", "checkpoint_identity"}
    }
    if identity.get("input_digest") != _digest(unsigned):
        raise CheckpointPlanError(f"checkpoint input digest is mismatched: {stage}")
    with_input = unsigned | {"input_digest": identity["input_digest"]}
    if identity.get("checkpoint_identity") != _digest(with_input):
        raise CheckpointPlanError(f"checkpoint identity digest is mismatched: {stage}")


def _validate_timing_split(
    value: Any, expected_keys: set[str], stage: str
) -> None:
    """Check one timing split closed against its exact key set."""
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise CheckpointPlanError(
            f"retained checkpoint timing split is invalid: {stage}"
        )
    for seconds in value.values():
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or seconds < 0
        ):
            raise CheckpointPlanError(
                f"retained checkpoint timing split is invalid: {stage}"
            )


def _validate_run_record_accounting(evidence: dict[str, Any], stage: str) -> None:
    """Check the byte and timing accounting a retained run record carries.

    The closed key set proves the fields are present; this proves they mean
    something. Retained evidence is identity evidence, so nonsense accounting
    -- negative bytes, a timing split with unexpected members -- is rejected
    rather than carried forward.
    """
    for key in ("bytes_read", "bytes_written"):
        value = evidence[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CheckpointPlanError(
                f"retained checkpoint transfer accounting is invalid: {stage}"
            )
    _validate_timing_split(
        evidence["verification_timing"], VERIFICATION_TIMING_KEYS, stage
    )
    if "cache_hit_timing" in evidence:
        _validate_timing_split(
            evidence["cache_hit_timing"], CACHE_HIT_TIMING_KEYS, stage
        )


def _load_identity_evidence(
    identity_root: Path, stage: str
) -> tuple[dict[str, Any], Path, str]:
    standalone = identity_root / f"{stage}.identity.json"
    retained_run = identity_root / f"{stage}.json"
    if standalone.exists() or standalone.is_symlink():
        path = standalone
        evidence_kind = "standalone-identity"
    else:
        path = retained_run
        evidence_kind = "retained-run-record"
    evidence = _read_json(path, "checkpoint identity evidence")
    if not IDENTITY_KEYS.issubset(evidence):
        raise CheckpointPlanError(f"checkpoint identity fields are invalid: {stage}")
    if evidence_kind == "standalone-identity" and set(evidence) != IDENTITY_KEYS:
        raise CheckpointPlanError(
            f"standalone checkpoint identity has unknown fields: {stage}"
        )
    if evidence_kind == "retained-run-record":
        fields = set(evidence)
        if fields not in (
            RUN_RECORD_KEYS,
            REPRODUCIBILITY_RUN_RECORD_KEYS,
            CACHE_HIT_RUN_RECORD_KEYS,
        ):
            raise CheckpointPlanError(
                f"retained checkpoint run record fields are invalid: {stage}"
            )
        _validate_run_record_accounting(evidence, stage)
        if "reproducibility_match" in evidence and (
            evidence["reproducibility_match"] is not True
            or evidence.get("cache_hit") is not False
        ):
            raise CheckpointPlanError(
                f"retained checkpoint reproducibility match metadata is invalid: {stage}"
            )
        if "cache_hit_timing" in evidence and evidence.get("cache_hit") is not True:
            raise CheckpointPlanError(
                f"retained checkpoint cache hit metadata is invalid: {stage}"
            )
    identity = {key: evidence[key] for key in IDENTITY_KEYS}
    return identity, path, evidence_kind


def _validate_completed_at(value: Any, stage: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CheckpointPlanError(
            f"checkpoint completion timestamp is invalid: {stage}"
        )
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise CheckpointPlanError(
            f"checkpoint completion timestamp is invalid: {stage}"
        ) from error


def _expected_toolchain_declared_inputs(
    *,
    repository: Path,
    specification: dict[str, Any],
    build_lock: dict[str, Any],
    expected_binding: dict[str, Any],
) -> dict[str, Any]:
    declaration = specification["stages"].get(TOOLCHAIN_STAGE)
    if not isinstance(declaration, dict):
        raise CheckpointPlanError("builder-toolchain declaration is missing")
    if expected_binding.get("effective_mode") != TOOLCHAIN_MODE:
        raise CheckpointPlanError(
            "builder-toolchain current binding does not use shared mode"
        )
    script_path = "builder/ensure-asahi-toolchain-image.sh"
    containerfile_path = "builder/asahi-toolchain.Containerfile"
    if declaration.get("entrypoints") != [script_path]:
        raise CheckpointPlanError(
            "builder-toolchain producer entrypoint contract is unsupported"
        )
    if containerfile_path not in declaration.get("source_paths", []):
        raise CheckpointPlanError(
            "builder-toolchain Containerfile is not a declared producer input"
        )
    try:
        source_lock = build_lock_projection(
            build_lock,
            TOOLCHAIN_STAGE,
            declaration,
            TOOLCHAIN_MODE,
        )
        builder = source_lock["inputs"]["builder"]
        base_image = builder["base_image"]
        toolchain_packages = builder["toolchain_packages"]
        source_lock_record = expected_binding["source_lock"]
        source_manifest_record = expected_binding["source_manifest"]
        source_identity = expected_binding["source_identity"]
        producer_binding_identity = expected_binding[
            "producer_binding_identity"
        ]
    except (KeyError, TypeError, StageInputError) as error:
        raise CheckpointPlanError(
            "builder-toolchain current producer binding is incomplete"
        ) from error
    if (
        not isinstance(base_image, str)
        or not base_image
        or not isinstance(toolchain_packages, list)
        or not toolchain_packages
        or any(
            not isinstance(package, str) or not package
            for package in toolchain_packages
        )
        or not isinstance(source_lock_record, dict)
        or source_lock_record.get("filename") != "source-lock.json"
        or not isinstance(source_manifest_record, dict)
        or source_manifest_record.get("filename") != "source-manifest.json"
        or not isinstance(source_identity, str)
        or SHA256.fullmatch(source_identity) is None
        or not isinstance(producer_binding_identity, str)
        or SHA256.fullmatch(producer_binding_identity) is None
    ):
        raise CheckpointPlanError(
            "builder-toolchain current producer binding is invalid"
        )
    source_lock_sha256 = source_lock_record.get("sha256")
    source_manifest_sha256 = source_manifest_record.get("sha256")
    if (
        not isinstance(source_lock_sha256, str)
        or SHA256.fullmatch(source_lock_sha256) is None
        or not isinstance(source_manifest_sha256, str)
        or SHA256.fullmatch(source_manifest_sha256) is None
    ):
        raise CheckpointPlanError(
            "builder-toolchain current source metadata is invalid"
        )
    return {
        "base_image": base_image,
        "source_lock_sha256": source_lock_sha256,
        "containerfile_sha256": _source_file_digest(
            repository,
            containerfile_path,
            "builder-toolchain Containerfile",
        ),
        "script_sha256": _source_file_digest(
            repository,
            script_path,
            "builder-toolchain producer",
        ),
        "source": {
            "omarchy_iso_stage": source_identity,
            "omarchy_iso_producer": producer_binding_identity,
            "manifest_sha256": source_manifest_sha256,
        },
        "toolchain_packages": toolchain_packages,
    }


def _canonical(call, *args, **keywords):
    """Delegate to the canonical metadata implementation.

    builder/asahi_toolchain_metadata.py owns every schema-2 rule the planner,
    the producer, and the projection gate share. Its errors are re-raised as
    planner errors so plan classification is unchanged.
    """
    try:
        return call(*args, **keywords)
    except toolchain_metadata.ToolchainMetadataError as error:
        raise CheckpointPlanError(str(error)) from error


def _validate_toolchain_output(value: Any, role: str) -> dict[str, Any]:
    return _canonical(toolchain_metadata.validate_output, value, role)


def _validate_toolchain_compatibility(
    value: Any,
    *,
    expected_target_lock: str,
    expected_source_lock: str | None = None,
    role: str,
) -> dict[str, Any]:
    return _canonical(
        toolchain_metadata.validate_compatibility,
        value,
        expected_target_lock=expected_target_lock,
        expected_source_lock=expected_source_lock,
        role=role,
    )


def _load_toolchain_run_record(
    identity_root: Path,
) -> tuple[dict[str, Any], Path]:
    evidence_path = identity_root / f"{TOOLCHAIN_STAGE}.json"
    run_record = _read_json(evidence_path, "builder-toolchain run record")
    _canonical(toolchain_metadata.validate_run_record, run_record)
    return run_record, evidence_path


def _load_toolchain_manifest(
    cache_root: Path,
    checkpoint_identity: str,
) -> tuple[dict[str, Any], Path, Path]:
    _toolchain_directory_status(
        cache_root,
        "builder-toolchain cache root",
        immutable=False,
    )
    stage_root = cache_root / TOOLCHAIN_STAGE
    _toolchain_directory_status(
        stage_root,
        "builder-toolchain stage root",
        immutable=False,
    )
    checkpoint = stage_root / checkpoint_identity
    _require_real_ancestors(
        cache_root,
        checkpoint,
        "builder-toolchain checkpoint path",
    )
    _toolchain_directory_status(
        checkpoint,
        "builder-toolchain checkpoint directory",
        immutable=True,
    )
    manifest_path = checkpoint / "manifest.json"
    manifest_status = _real_path(
        manifest_path,
        "builder-toolchain checkpoint manifest",
        kind="file",
    )
    if (
        manifest_status.st_uid != os.geteuid()
        or manifest_status.st_nlink != 1
        or stat.S_IMODE(manifest_status.st_mode) & 0o222
        or manifest_status.st_size <= 0
        or manifest_status.st_size > 4 * 1024 * 1024
    ):
        raise CheckpointPlanError(
            "builder-toolchain manifest is writable, mutable, or unsafe"
        )
    manifest = _read_json(manifest_path, "builder-toolchain checkpoint manifest")
    # Every content rule the producer also applies lives in the canonical
    # implementation. Directory identity, permissions, and immutability are
    # checked above and below, because they are the planner's own concern.
    _canonical(toolchain_metadata.validate_checkpoint_manifest, manifest)
    return manifest, manifest_path, checkpoint


def _validate_toolchain_manifest_contents(
    manifest: dict[str, Any],
    *,
    checkpoint: Path,
    checkpoint_identity: str,
) -> dict[str, Any]:
    # Document-internal consistency -- inventory shape, the inventory digest,
    # the output shape, and the identity binding the actual inputs -- is
    # canonical and was already applied by _load_toolchain_manifest. What
    # remains is this checkpoint directory's own binding and shape, which only
    # the planner cares about.
    output = _validate_toolchain_output(
        manifest.get("output"),
        "builder-toolchain manifest",
    )
    actual_identity = manifest.get("checkpoint_identity")
    if actual_identity != checkpoint_identity or checkpoint.name != actual_identity:
        raise CheckpointPlanError(
            "builder-toolchain checkpoint identity or directory binding is mismatched"
        )
    if _directory_names(checkpoint, "builder-toolchain checkpoint directory") != {
        "manifest.json"
    }:
        raise CheckpointPlanError(
            "builder-toolchain checkpoint directory entries are invalid"
        )
    _audit_immutable_tree(checkpoint, "builder-toolchain checkpoint tree")
    return output


def _toolchain_metadata_candidate(
    *,
    repository: Path,
    specification: dict[str, Any],
    build_lock: dict[str, Any],
    expected_binding: dict[str, Any],
    identity_root: Path,
    cache_root: Path,
) -> tuple[dict[str, Any], Path, str, dict[str, Any]]:
    """Validate schema-2/shared toolchain metadata without inspecting Docker."""
    expected_declared = _expected_toolchain_declared_inputs(
        repository=repository,
        specification=specification,
        build_lock=build_lock,
        expected_binding=expected_binding,
    )
    run_record, evidence_path = _load_toolchain_run_record(identity_root)
    checkpoint_identity = run_record["checkpoint_identity"]
    manifest, manifest_path, checkpoint = _load_toolchain_manifest(
        cache_root,
        checkpoint_identity,
    )
    output = _validate_toolchain_manifest_contents(
        manifest,
        checkpoint=checkpoint,
        checkpoint_identity=checkpoint_identity,
    )
    if (
        run_record["input_digest"] != manifest["declared_input_digest"]
        or run_record["output"] != output
        or run_record["checkpoint_identity"]
        != manifest["checkpoint_identity"]
    ):
        raise CheckpointPlanError(
            "builder-toolchain run record does not bind the checkpoint manifest"
        )
    manifest_compatibility = manifest.get("compatibility")
    run_compatibility = run_record.get("compatibility")
    if manifest_compatibility is not None:
        # The producer always bound compatibility.source_lock_sha256 to the
        # digest of the legacy build lock on disk; the planner only checked its
        # shape, so a block naming a source lock that never existed was accepted
        # here and refused there. Both surfaces now bind it.
        legacy_lock = repository / "builder/asahi-build-lock.json"
        if not legacy_lock.is_file() or legacy_lock.is_symlink():
            raise CheckpointPlanError(
                "builder-toolchain manifest compatibility metadata is invalid"
            )
        _validate_toolchain_compatibility(
            manifest_compatibility,
            expected_target_lock=expected_declared["source_lock_sha256"],
            expected_source_lock=toolchain_metadata.file_digest(legacy_lock),
            role="builder-toolchain manifest",
        )
    if run_compatibility != manifest_compatibility:
        raise CheckpointPlanError(
            "builder-toolchain compatibility evidence is mismatched"
        )
    if manifest["declared_inputs"] != expected_declared:
        raise ProducerBindingMismatch(
            "builder-toolchain producer or source lock does not match current inputs"
        )

    identity = {
        "schema_version": 2,
        "stage": TOOLCHAIN_STAGE,
        "mode": TOOLCHAIN_MODE,
        "input_digest": run_record["input_digest"],
        "checkpoint_identity": checkpoint_identity,
    }
    return (
        identity,
        evidence_path,
        "builder-toolchain-schema-2-run-record",
        {
            "manifest_path": str(manifest_path),
            "checkpoint_identity": checkpoint_identity,
            "artifact_set_identity": _digest(
                {
                    "schema_version": 2,
                    "verification_kind": "asahi-builder-toolchain-artifact-set",
                    "stage": TOOLCHAIN_STAGE,
                    "mode": TOOLCHAIN_MODE,
                    "checkpoint_identity": checkpoint_identity,
                    "output": output,
                }
            ),
            "referenced_bytes": output["size_bytes"],
            "output_count": 1,
            "completed_at": manifest["completed_at"],
            # Normalized into the same handle shape the other stages use. This
            # stage's artifact is a container image rather than stored objects,
            # so it is not restored through the destination-set contract; the
            # handle records that distinction rather than hiding it.
            "outputs": [
                {
                    "name": "builder-toolchain-image",
                    "kind": "container-image",
                    "storage": {"kind": "container-image"},
                    "sha256": output["image_id"].removeprefix("sha256:"),
                    "size_bytes": output["size_bytes"],
                    "restore_mode": None,
                }
            ],
        },
    )


def _metadata_candidate(
    *, cache_root: Path, identity: dict[str, Any], stage: str
) -> dict[str, Any]:
    checkpoint_digest = identity["checkpoint_identity"]
    checkpoint = cache_root / "checkpoints" / stage / checkpoint_digest
    _require_real_ancestors(cache_root, checkpoint, "checkpoint path")
    checkpoint_status = _real_path(
        checkpoint, "checkpoint identity directory", kind="directory"
    )
    if checkpoint_status.st_mode & 0o222:
        raise CheckpointPlanError(f"checkpoint identity directory is writable: {stage}")
    manifest_path = checkpoint / "manifest.json"
    manifest_status = _real_path(manifest_path, "checkpoint manifest", kind="file")
    if manifest_status.st_mode & 0o222:
        raise CheckpointPlanError(f"checkpoint manifest is writable: {stage}")
    manifest = _read_json(manifest_path, "checkpoint manifest")
    if frozenset(manifest) not in {
        frozenset(MANIFEST_KEYS),
        frozenset(MIGRATED_MANIFEST_KEYS),
    }:
        raise CheckpointPlanError(f"checkpoint manifest fields are invalid: {stage}")
    manifest_identity = {key: manifest[key] for key in IDENTITY_KEYS}
    if manifest_identity != identity:
        raise CheckpointPlanError(
            f"checkpoint manifest identity is mismatched: {stage}"
        )
    if "migration" in manifest:
        migration = manifest["migration"]
        if not isinstance(migration, dict) or set(migration) != {
            "source_checkpoint_identity",
            "reason",
            "transition_digest",
        }:
            raise CheckpointPlanError(
                f"checkpoint migration metadata is invalid: {stage}"
            )
        if (
            not isinstance(migration["source_checkpoint_identity"], str)
            or SHA256.fullmatch(migration["source_checkpoint_identity"]) is None
            or not isinstance(migration["transition_digest"], str)
            or SHA256.fullmatch(migration["transition_digest"]) is None
            or not isinstance(migration["reason"], str)
            or SAFE_NAME.fullmatch(migration["reason"]) is None
        ):
            raise CheckpointPlanError(
                f"checkpoint migration metadata is invalid: {stage}"
            )
    if manifest.get("validation") != {"result": "passed"}:
        raise CheckpointPlanError(
            f"checkpoint validation result is not passed: {stage}"
        )
    if manifest.get("cache_hit") is not False or manifest.get("immutable") is not True:
        raise CheckpointPlanError(
            f"checkpoint immutability metadata is invalid: {stage}"
        )
    elapsed = manifest.get("elapsed_seconds")
    if (
        not isinstance(elapsed, (int, float))
        or isinstance(elapsed, bool)
        or elapsed < 0
    ):
        raise CheckpointPlanError(f"checkpoint elapsed time is invalid: {stage}")
    _validate_completed_at(manifest.get("completed_at"), stage)

    outputs = manifest.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise CheckpointPlanError(f"checkpoint outputs are missing or invalid: {stage}")
    names: set[str] = set()
    inline_names: set[str] = set()
    referenced_bytes = 0
    for output in outputs:
        if not isinstance(output, dict):
            raise CheckpointPlanError(f"checkpoint output metadata is invalid: {stage}")
        output_kind = output.get("kind")
        expected_output_keys = (
            FILE_OUTPUT_KEYS
            if output_kind == "file"
            else DIRECTORY_OUTPUT_KEYS
            if output_kind == "directory"
            else None
        )
        if expected_output_keys is None:
            raise CheckpointPlanError(f"checkpoint output kind is invalid: {stage}")
        if set(output) != expected_output_keys:
            raise CheckpointPlanError(f"checkpoint output fields are invalid: {stage}")
        restore_mode = output.get("restore_mode")
        if (
            not isinstance(restore_mode, int)
            or isinstance(restore_mode, bool)
            or not 0 <= restore_mode <= 0o7777
        ):
            raise CheckpointPlanError(
                f"checkpoint output restore mode is invalid: {stage}"
            )
        name = output.get("name")
        size = output.get("size_bytes")
        digest = output.get("sha256")
        if (
            not isinstance(name, str)
            or SAFE_NAME.fullmatch(name) is None
            or name in names
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or SHA256.fullmatch(digest) is None
        ):
            raise CheckpointPlanError(f"checkpoint output metadata is invalid: {stage}")
        names.add(name)
        referenced_bytes += size
        if output_kind == "file":
            if output.get("storage") != {"kind": "sha256-object", "sha256": digest}:
                raise CheckpointPlanError(
                    f"checkpoint object reference is invalid: {stage}"
                )
            object_path = cache_root / "objects" / "sha256" / digest[:2] / digest
            _require_real_ancestors(cache_root, object_path, "checkpoint object path")
            object_status = _real_path(object_path, "checkpoint object", kind="file")
            if object_status.st_mode & 0o222 or object_status.st_size != size:
                raise CheckpointPlanError(
                    f"checkpoint object metadata is mismatched: {stage}"
                )
        elif output_kind == "directory":
            if output.get("storage") != {"kind": "inline-directory"}:
                raise CheckpointPlanError(
                    f"inline checkpoint reference is invalid: {stage}"
                )
            inline = checkpoint / "outputs" / name
            _require_real_ancestors(checkpoint, inline, "inline checkpoint path")
            inline_status = _real_path(
                inline, "inline checkpoint output", kind="directory"
            )
            if inline_status.st_mode & 0o222:
                raise CheckpointPlanError(
                    f"inline checkpoint output is writable: {stage}"
                )
            expected_metadata = _inline_manifest_metadata(output, stage)
            actual_metadata = _audit_immutable_tree(
                inline, f"inline checkpoint output for {stage}"
            )
            if actual_metadata != expected_metadata:
                raise CheckpointPlanError(
                    f"inline checkpoint tree metadata is mismatched: {stage}"
                )
            inline_names.add(name)

    if _directory_names(checkpoint, "checkpoint identity directory") != {
        "manifest.json",
        "outputs",
    }:
        raise CheckpointPlanError(
            f"checkpoint identity directory entries are invalid: {stage}"
        )
    outputs_directory = checkpoint / "outputs"
    _real_path(outputs_directory, "checkpoint outputs directory", kind="directory")
    if (
        _directory_names(outputs_directory, "checkpoint outputs directory")
        != inline_names
    ):
        raise CheckpointPlanError(
            f"checkpoint inline output set is mismatched: {stage}"
        )
    _audit_immutable_tree(checkpoint, f"checkpoint tree for {stage}")

    return {
        "manifest_path": str(manifest_path),
        "checkpoint_identity": checkpoint_digest,
        "artifact_set_identity": _digest(
            {
                "schema_version": SCHEMA_VERSION,
                "verification_kind": "asahi-checkpoint-artifact-set",
                "stage": stage,
                "mode": manifest["mode"],
                "checkpoint_identity": checkpoint_digest,
                "outputs": outputs,
            }
        ),
        "referenced_bytes": referenced_bytes,
        "output_count": len(outputs),
        "completed_at": manifest["completed_at"],
        # The validated output records themselves. Carried so the resume context
        # can be built from data this function has already checked, never from a
        # second read of the manifest.
        "outputs": outputs,
    }


def plan_checkpoint_execution(
    *,
    repository: Path,
    specification: dict[str, Any],
    build_lock: dict[str, Any],
    cost_data: dict[str, Any],
    changed_paths: list[str],
    intent: str,
    profile: str,
    identity_root: Path,
    cache_root: Path,
    expected_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a metadata-only execution plan; object bytes are never read."""
    _real_path(repository, "source repository", kind="directory")
    validate_specification(repository, specification)
    _real_path(identity_root, "checkpoint identity root", kind="directory")
    _real_path(cache_root, "checkpoint cache root", kind="directory")
    preview = preview_source_impact(
        repository=repository,
        specification=specification,
        cost_data=cost_data,
        changed_paths=changed_paths,
        intent=intent,
        profile=profile,
    )
    if expected_preview is not None and _digest(expected_preview) != _digest(preview):
        raise CheckpointPlanError("source preview drifted before checkpoint planning")
    terminal = PROFILE_TERMINALS[profile]
    stage_order = specification["stage_order"]
    planned_order = stage_order[: stage_order.index(terminal) + 1]
    all_producer_bindings = declared_stage_identity_bindings(
        repository=repository,
        specification=specification,
        build_lock=build_lock,
        mode=profile,
    )
    producer_binding_identities = {
        stage: all_producer_bindings[stage]["producer_binding_identity"]
        for stage in planned_order
    }
    all_admission_policy_identities = declared_admission_fingerprints(
        repository=repository,
        specification=specification,
        mode=profile,
    )
    admission_policy_identities = {
        stage: all_admission_policy_identities[stage] for stage in planned_order
    }
    invalidated = set(preview["invalidation_frontier"])
    stages: list[dict[str, Any]] = []
    candidates_by_stage: dict[str, dict[str, Any]] = {}
    outputs_by_stage: dict[str, list[dict[str, Any]]] = {}
    unsafe_or_malformed: list[tuple[str, str]] = []
    producer_binding_rejections: list[tuple[str, str]] = []
    for stage in planned_order:
        if stage in invalidated:
            stages.append(
                {
                    "stage": stage,
                    "classification": "declared-invalidated",
                    "classifications": ["declared-invalidated"],
                    "invalidation_reason": "source-declaration-frontier",
                    "content_verification": "not-applicable",
                }
            )
            continue
        try:
            expected_binding = all_producer_bindings[stage]
            if stage == TOOLCHAIN_STAGE:
                (
                    identity,
                    identity_path,
                    identity_evidence_kind,
                    candidate,
                ) = _toolchain_metadata_candidate(
                    repository=repository,
                    specification=specification,
                    build_lock=build_lock,
                    expected_binding=expected_binding,
                    identity_root=identity_root,
                    cache_root=cache_root,
                )
                producer_binding_matches = True
            else:
                (
                    identity,
                    identity_path,
                    identity_evidence_kind,
                ) = _load_identity_evidence(identity_root, stage)
                _validate_identity(identity, stage=stage, mode=profile)
            source_commits = identity.get("source_commits")
            producer_claim = (
                source_commits.get("omarchy_iso_producer")
                if isinstance(source_commits, dict)
                else None
            )
            source_claim = (
                source_commits.get("omarchy_iso_stage")
                if isinstance(source_commits, dict)
                else None
            )
            expected_producer_binding = expected_binding[
                "producer_binding_identity"
            ]
            source_manifest_inputs = [
                record
                for record in identity.get("inputs", [])
                if isinstance(record, dict) and record.get("name") == "source-manifest"
            ]
            expected_source_manifest = {
                "kind": "file",
                "name": "source-manifest",
                "path": "source-manifest",
                "size_bytes": expected_binding["source_manifest"]["size_bytes"],
                "sha256": expected_binding["source_manifest"]["sha256"],
                "executable_mode": expected_binding["source_manifest"][
                    "executable_mode"
                ],
            }
            if stage != TOOLCHAIN_STAGE:
                producer_binding_matches = (
                    producer_claim == expected_producer_binding
                    and source_claim == expected_binding["source_identity"]
                    and identity.get("source_lock") == expected_binding["source_lock"]
                    and source_manifest_inputs == [expected_source_manifest]
                )
            if not producer_binding_matches:
                state = (
                    "legacy-unbound"
                    if not isinstance(producer_claim, str)
                    else "mismatched-current-inputs"
                )
                reason = (
                    "checkpoint identity has no current producer binding"
                    if state == "legacy-unbound"
                    else (
                        "checkpoint producer, provenance, source lock, or "
                        "source manifest does not match current inputs"
                    )
                )
                producer_binding_rejections.append((stage, reason))
                stages.append(
                    {
                        "stage": stage,
                        "classification": "producer-binding-rejected",
                        "classifications": ["producer-binding-rejected"],
                        "invalidation_reason": reason,
                        "content_verification": "not-applicable",
                        "producer_binding_state": state,
                        "current_producer_binding_identity": expected_producer_binding,
                    }
                )
                continue
            if stage != TOOLCHAIN_STAGE:
                candidate = _metadata_candidate(
                    cache_root=cache_root,
                    identity=identity,
                    stage=stage,
                )
        except ProducerBindingMismatch as error:
            reason = str(error)
            producer_binding_rejections.append((stage, reason))
            stages.append(
                {
                    "stage": stage,
                    "classification": "producer-binding-rejected",
                    "classifications": ["producer-binding-rejected"],
                    "invalidation_reason": reason,
                    "content_verification": "not-applicable",
                    "producer_binding_state": "mismatched-current-inputs",
                    "current_producer_binding_identity": all_producer_bindings[
                        stage
                    ]["producer_binding_identity"],
                }
            )
            continue
        except CheckpointPlanError as error:
            reason = str(error)
            if not reason.startswith("missing "):
                unsafe_or_malformed.append((stage, reason))
            stages.append(
                {
                    "stage": stage,
                    "classification": "missing/rejected",
                    "classifications": ["missing/rejected"],
                    "invalidation_reason": reason,
                    "content_verification": "not-applicable",
                }
            )
            continue
        # Held aside rather than emitted per stage: the handles belong in the
        # resume context, where a blocked plan cannot reach them.
        outputs_by_stage[stage] = candidate.pop("outputs")
        record = {
            "stage": stage,
            "classification": "manifest-candidate-hit",
            "classifications": [
                "manifest-candidate-hit",
                "deferred-content-verification",
            ],
            "invalidation_reason": None,
            "content_verification": "deferred-content-verification",
            "admission_state": "current-policy-admission-required",
            "admission_policy_identity": admission_policy_identities[stage],
            "producer_binding_state": "identity-claim-matched-current",
            "current_producer_binding_identity": producer_binding_identities[stage],
            "identity": identity,
            "identity_evidence_path": str(identity_path),
            "identity_evidence_kind": identity_evidence_kind,
            **candidate,
        }
        stages.append(record)
        candidates_by_stage[stage] = record

    expected_reusable = [
        stage for stage in planned_order if stage not in invalidated
    ]
    unexpected_expensive_misses = [
        stage for stage in expected_reusable if stage not in candidates_by_stage
    ]
    block_reasons = list(preview["block_reasons"])
    if unexpected_expensive_misses:
        block_reasons.append(
            "unexpected expensive miss outside declared invalidation frontier: "
            + ", ".join(unexpected_expensive_misses)
        )
    block_reasons.extend(
        f"producer binding rejected for {stage}: {reason}"
        for stage, reason in producer_binding_rejections
    )
    block_reasons.extend(
        f"unsafe or malformed checkpoint metadata for {stage}: {reason}"
        for stage, reason in unsafe_or_malformed
    )
    ready = (
        not preview["blocked"]
        and not unexpected_expensive_misses
        and not unsafe_or_malformed
    )

    planned_execution_stages = [
        stage for stage in planned_order if stage in invalidated
    ]
    planned_execution_skipped = [
        stage for stage in planned_order if stage not in invalidated
    ]
    execution_stages = planned_execution_stages if ready else []
    execution_skipped = planned_execution_skipped if ready else []
    execution_stage_set = set(execution_stages)
    restore_stage_names = [
        stage
        for stage in planned_order
        if stage
        in {
            dependency
            for runnable in execution_stages
            for dependency in specification["stages"][runnable]["depends_on"]
            if dependency in planned_order and dependency not in execution_stage_set
        }
    ]
    admission_stage_names = [
        stage
        for stage in planned_order
        if stage in preview["admission_frontier"] and stage not in execution_stage_set
    ]

    def resume_context(stage: str) -> dict[str, Any]:
        """Everything a future executor would need to resume this stage.

        Modelling only. Nothing here restores, verifies content, or authorizes
        reuse; the execution blockers still refuse all of that. Every value is
        taken from metadata already validated while the candidate was built, so
        a tampered manifest is refused long before it can reach this structure.

        Per-output consumption -- which of a parent's outputs this stage
        actually reads -- is deliberately not modelled. The whole parent
        artifact set is named instead, which over-states the dependency rather
        than under-stating it.
        """
        outputs = sorted(outputs_by_stage[stage], key=lambda record: record["name"])
        parents = []
        for parent in specification["stages"][stage]["depends_on"]:
            resolved = candidates_by_stage.get(parent)
            parents.append(
                {
                    "stage": parent,
                    "candidate_available": resolved is not None,
                    "checkpoint_identity": (
                        resolved["checkpoint_identity"] if resolved else None
                    ),
                    "artifact_set_identity": (
                        resolved["artifact_set_identity"] if resolved else None
                    ),
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "verification_kind": "asahi-checkpoint-resume-context",
            "claim_scope": "checkpoint-metadata-only",
            "stage": stage,
            "output_handles": [
                {
                    "name": output["name"],
                    "kind": output["kind"],
                    "storage_kind": output["storage"]["kind"],
                    "sha256": output["sha256"],
                    "size_bytes": output["size_bytes"],
                    "restore_mode": output["restore_mode"],
                    "restorable_via_destinations": output["kind"] in RESTORABLE_KINDS,
                }
                for output in outputs
            ],
            # Restore refuses unless the destination set equals the output set
            # exactly, so the required names are stated here and an executor
            # never has to consult a stage script to learn them.
            "required_destination_names": [
                output["name"] for output in outputs if output["kind"] in RESTORABLE_KINDS
            ],
            "destination_set_contract": (
                "restore destinations must equal the output name set exactly"
            ),
            "parents": parents,
            "parent_count": len(parents),
            "unresolved_parents": [
                parent["stage"] for parent in parents if not parent["candidate_available"]
            ],
        }

    def selection_record(stage: str) -> dict[str, Any]:
        candidate = candidates_by_stage[stage]
        return {
            "stage": stage,
            "identity": candidate["identity"],
            "checkpoint_identity": candidate["checkpoint_identity"],
            "artifact_set_identity": candidate["artifact_set_identity"],
            "producer_binding_identity": candidate[
                "current_producer_binding_identity"
            ],
            "admission_policy_identity": candidate["admission_policy_identity"],
            "referenced_bytes": candidate["referenced_bytes"],
            "resume_context": resume_context(stage),
        }

    if ready:
        restore_frontier = [
            selection_record(stage) for stage in restore_stage_names
        ]
        admission_frontier = [
            selection_record(stage) for stage in admission_stage_names
        ]
    else:
        restore_frontier = []
        admission_frontier = []
    if not ready:
        checkpoint_state = "blocked-no-execution-selection"
    elif admission_frontier and not execution_stages:
        checkpoint_state = "current-policy-admission-required"
    elif restore_frontier:
        checkpoint_state = "content-verification-and-admission-required"
    elif execution_stages:
        checkpoint_state = "producer-build-required"
    else:
        checkpoint_state = "no-producer-work-planned"
    execution_selection = {
        "restore_frontier": restore_frontier,
        "admission_frontier": admission_frontier,
        "first_stage_to_run": execution_stages[0] if execution_stages else None,
        "skipped_stages": execution_skipped,
        "stages_to_run": execution_stages,
        "checkpoint_state": checkpoint_state,
        "authority": "advisory-metadata-only",
        "producer_input_cross_check": (
            "identity-claim-matched-current"
            if restore_frontier or admission_frontier
            else "not-applicable"
        ),
    }
    materialization_bytes = sum(
        record["referenced_bytes"] for record in restore_frontier
    )
    admission_bytes = sum(
        record["referenced_bytes"] for record in admission_frontier
    )
    return _bind_plan_digest(
        {
            "schema_version": SCHEMA_VERSION,
            "verification_kind": "asahi-checkpoint-execution-plan",
            "claim_scope": "checkpoint-metadata-only",
            "checkpoint_content_verified": False,
            "current_producer_inputs_verified": False,
            "ready_for_authoritative_execution": False,
            "authoritative_execution_blockers": [
                # "complete-resume-context-not-modeled" was removed 2026-08-30.
                # Every selected stage now carries a resume context naming its
                # typed output handles, its full parent set, and the exact
                # destination-name set restore requires. The blocker is no
                # longer true, so it no longer belongs here. Nothing else in
                # this list changed, and modelling authorizes nothing.
                "checkpoint-content-unverified",
                "full-producer-descriptor-not-recomputed",
                "read-only-admission-adapter-unimplemented",
                "current-admission-receipts-missing",
                (
                    "qualification-receipt-authority-unavailable"
                    if profile == "qualification"
                    else "diagnostic-artifacts-are-qualification-ineligible"
                ),
            ],
            "content_verification_disclaimer": (
                "Manifest candidate hits are not cryptographic runtime hits. Execution must "
                "verify exact object content while streaming before restore or reuse."
            ),
            "source_preview_digest": _digest(preview),
            "source_preview": preview,
            "profile_terminal_stage": terminal,
            "producer_binding_identities": producer_binding_identities,
            "admission_policy_identities": admission_policy_identities,
            "stages": stages,
            "materialization_forecast": {
                "stages": restore_stage_names if ready else [],
                "referenced_bytes": materialization_bytes,
                "basis": "checkpoint-manifest-output-sizes",
                "content_verification_required": bool(restore_frontier),
            },
            "admission_forecast": {
                "stages": admission_stage_names if ready else [],
                "referenced_bytes": admission_bytes,
                "basis": "checkpoint-manifest-output-sizes",
                "content_verification_required": bool(admission_frontier),
            },
            "execution_selection": execution_selection,
            "unexpected_expensive_misses": unexpected_expensive_misses,
            "producer_binding_rejected_stages": [
                stage for stage, _ in producer_binding_rejections
            ],
            "unsafe_or_malformed_stages": [stage for stage, _ in unsafe_or_malformed],
            "blocked": not ready,
            "advisory_selection_ready": ready,
            "ready_for_execution": False,
            "block_reasons": block_reasons,
        }
    )


def _parser() -> argparse.ArgumentParser:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repository)
    parser.add_argument(
        "--spec",
        type=Path,
        default=repository / "builder/asahi-stage-inputs.json",
    )
    parser.add_argument(
        "--cost-data",
        type=Path,
        default=repository / "builder/asahi-source-impact-costs.json",
    )
    parser.add_argument(
        "--build-lock",
        type=Path,
        default=repository / "builder/asahi-build-lock.json",
    )
    parser.add_argument("--identity-root", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--changed-path", action="append", required=True)
    parser.add_argument("--intent", choices=tuple(INTENT_BOUNDARIES), required=True)
    parser.add_argument("--profile", choices=tuple(PROFILE_TERMINALS), required=True)
    parser.add_argument("--expected-preview", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        expected_preview = (
            _read_json(arguments.expected_preview, "expected source preview")
            if arguments.expected_preview is not None
            else None
        )
        plan = plan_checkpoint_execution(
            repository=arguments.repo_root,
            specification=_read_json(arguments.spec, "stage input specification"),
            build_lock=_read_json(arguments.build_lock, "stage build lock"),
            cost_data=_read_json(arguments.cost_data, "source impact cost data"),
            changed_paths=arguments.changed_path,
            intent=arguments.intent,
            profile=arguments.profile,
            identity_root=arguments.identity_root,
            cache_root=arguments.cache_root,
            expected_preview=expected_preview,
        )
    except (
        CheckpointPlanError,
        SourceImpactError,
        StageInputError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        blocked_plan = _bind_plan_digest(
            {
                "schema_version": SCHEMA_VERSION,
                "verification_kind": "asahi-checkpoint-execution-plan",
                "claim_scope": "checkpoint-metadata-only",
                "checkpoint_content_verified": False,
                "current_producer_inputs_verified": False,
                "ready_for_authoritative_execution": False,
                "blocked": True,
                "advisory_selection_ready": False,
                "ready_for_execution": False,
                "block_reasons": [str(error)],
            }
        )
        print(
            json.dumps(
                blocked_plan,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 2 if plan["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
