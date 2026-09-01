#!/usr/bin/env python3
"""Apply one exact diagnostic legacy checkpoint compatibility transition."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import stat

import asahi_checkpoint as checkpoint


PLAN_KEYS = {
    "schema_version",
    "stage",
    "mode",
    "source_identity_kind",
    "source_checkpoint_identity",
    "target_checkpoint_identity",
    "target_source_manifest_identity",
    "target_producer_binding_identity",
    "target_source_lock_sha256",
    "equivalent_inputs",
    "projected_equivalent_inputs",
    "repository_manifest_transition",
    "configured_target_transition",
    "legacy_immutable_admission",
    "allowed_added_inputs",
    "allowed_removed_inputs",
    "allow_source_lock_change",
    "allow_source_commits_change",
    "expected_outputs",
    "reason",
}

REKEY_PLAN_SCHEMA_VERSION = 2
REKEY_REASON = "stage-input-granularity-v1"
LEGACY_MONOLITHIC_KIND = "legacy-monolithic-v0"
STAGE_PREBINDING_KIND = "stage-specific-prebinding-v1"

CONFIGURED_TRANSITION_KEYS = {
    "kind",
    "source_build_implementation_sha256",
    "source_configured_source_sha256",
}
CONFIGURED_PROOF_KEYS = {
    "schema_version",
    "verification_kind",
    "validator_sha256",
    "source_checkpoint_identity",
    "checkpoint_outputs",
    "repository_identity",
    "runtime_input_digest",
    "product_input_digest",
    "filesystems",
    "installed_packages",
    "package_inventory_sha256",
    "stage_state",
    "staged_node",
    "validation",
    "proof_digest",
}


def _load_repository_module():
    module_path = Path(__file__).with_name("capture-asahi-offline-repository.py")
    specification = importlib.util.spec_from_file_location(
        "asahi_offline_repository_transition", module_path
    )
    if specification is None or specification.loader is None:
        raise checkpoint.CheckpointError("repository transition verifier is unavailable")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _load_checkpoint_object_json(
    cache_root: Path,
    record: dict,
    role: str,
) -> dict:
    if record.get("kind") != "file":
        raise checkpoint.CheckpointError(f"{role} is not a file")
    object_record = record | {
        "storage": {"kind": "sha256-object", "sha256": record.get("sha256")}
    }
    path = checkpoint._verify_object(cache_root, object_record)
    return _load_real_json(path, role)


def _checkpoint_input_object_path(
    cache_root: Path,
    record: dict,
    role: str,
) -> Path:
    if record.get("kind") != "file":
        raise checkpoint.CheckpointError(f"{role} is not a file")
    object_record = record | {
        "storage": {"kind": "sha256-object", "sha256": record.get("sha256")}
    }
    return checkpoint._verify_object(cache_root, object_record)


def _load_real_json(path: Path, role: str) -> dict:
    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise checkpoint.CheckpointError(f"missing {role}: {path}") from error
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise checkpoint.CheckpointError(f"{role} must be a real file: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise checkpoint.CheckpointError(f"{role} must be an object")
    return value


def _verify_identity_file(
    *,
    identity: dict,
    name: str,
    path: Path,
    role: str,
) -> dict:
    records = checkpoint._input_records_by_name(identity)
    expected = records.get(name)
    if expected is None:
        raise checkpoint.CheckpointError(f"{role} is absent from target identity")
    actual = checkpoint._path_record(
        path,
        include_restore_modes=False,
        include_executable_modes=True,
    )
    expected_content = {
        key: value for key, value in expected.items() if key not in {"name", "path"}
    }
    if actual != expected_content:
        raise checkpoint.CheckpointError(f"{role} differs from target identity")
    return expected


def _is_digest(value: object, lengths: tuple[int, ...] = (64,)) -> bool:
    return isinstance(value, str) and len(value) in lengths and re.fullmatch(
        r"[0-9a-f]+", value
    ) is not None


def _source_date_epoch_role_is_valid(source_commits: dict) -> bool:
    value = source_commits.get("source_date_epoch")
    return value is None or value in {"unknown", "unset"} or (
        isinstance(value, str) and value.isdecimal()
    )


def _classify_legacy_source_identity(identity: dict) -> str:
    source_commits = identity.get("source_commits")
    if not isinstance(source_commits, dict):
        raise checkpoint.CheckpointError(
            "source identity is not an exact supported legacy shape"
        )
    records = checkpoint._input_records_by_name(identity)
    roles = set(source_commits)
    legacy_roles = {"archiso", "omarchy_iso"}
    prebinding_roles = {"omarchy_iso_stage"}
    without_epoch = roles - {"source_date_epoch"}
    if (
        without_epoch == legacy_roles
        and "source-manifest" not in records
        and _is_digest(source_commits.get("archiso"), (40,))
        and _is_digest(source_commits.get("omarchy_iso"), (40, 64))
        and _source_date_epoch_role_is_valid(source_commits)
    ):
        return LEGACY_MONOLITHIC_KIND
    source_manifest = records.get("source-manifest")
    if (
        without_epoch == prebinding_roles
        and source_manifest is not None
        and source_manifest.get("kind") == "file"
        and _is_digest(source_commits.get("omarchy_iso_stage"))
        and _source_date_epoch_role_is_valid(source_commits)
    ):
        return STAGE_PREBINDING_KIND
    raise checkpoint.CheckpointError(
        "source identity is not an exact supported legacy shape"
    )


def validate_rekey_contract(
    *,
    plan: dict,
    source_manifest: dict,
    source_identity: dict,
    target_identity: dict,
) -> None:
    """Reject unbound transitions after validating their claimed shape."""
    if (
        plan.get("mode") != "diagnostic"
        or source_identity.get("mode") != "diagnostic"
        or target_identity.get("mode") != "diagnostic"
    ):
        raise checkpoint.CheckpointError("checkpoint rekey is diagnostic-only")
    if plan.get("reason") != REKEY_REASON:
        raise checkpoint.CheckpointError("checkpoint rekey reason is not authorized")
    if source_manifest.get("migration") is not None:
        raise checkpoint.CheckpointError("checkpoint migration-of-migration is forbidden")

    source_kind = _classify_legacy_source_identity(source_identity)
    if plan.get("source_identity_kind") != source_kind:
        raise checkpoint.CheckpointError("checkpoint rekey source identity kind is stale")

    target_commits = target_identity.get("source_commits")
    if not isinstance(target_commits, dict):
        raise checkpoint.CheckpointError("target source commit roles are invalid")
    roles = set(target_commits)
    if roles - {"source_date_epoch"} != {
        "omarchy_iso_stage",
        "omarchy_iso_producer",
    } or not _source_date_epoch_role_is_valid(target_commits):
        raise checkpoint.CheckpointError("target source commit roles are invalid")
    source_manifest_identity = target_commits.get("omarchy_iso_stage")
    producer_binding_identity = target_commits.get("omarchy_iso_producer")
    if not _is_digest(source_manifest_identity) or not _is_digest(
        producer_binding_identity
    ):
        raise checkpoint.CheckpointError("target source commit roles are invalid")
    target_records = checkpoint._input_records_by_name(target_identity)
    source_manifest_record = target_records.get("source-manifest")
    if source_manifest_record is None or source_manifest_record.get("kind") != "file":
        raise checkpoint.CheckpointError("target identity has no stage source manifest")
    if plan.get("target_source_manifest_identity") != source_manifest_identity:
        raise checkpoint.CheckpointError(
            "checkpoint rekey target source-manifest identity is stale"
        )
    if plan.get("target_producer_binding_identity") != producer_binding_identity:
        raise checkpoint.CheckpointError(
            "checkpoint rekey target producer binding identity is stale"
        )
    if producer_binding_identity == source_manifest_identity:
        raise checkpoint.CheckpointError(
            "producer binding must differ from source-manifest provenance"
        )

    physical_digests = {
        record.get("sha256")
        for record in target_identity.get("inputs", [])
        if isinstance(record, dict)
    }
    source_lock = target_identity.get("source_lock")
    if isinstance(source_lock, dict):
        physical_digests.add(source_lock.get("sha256"))
    expected_outputs = plan.get("expected_outputs")
    if isinstance(expected_outputs, dict):
        physical_digests.update(
            expectation.get("sha256")
            for expectation in expected_outputs.values()
            if isinstance(expectation, dict)
        )
    if producer_binding_identity in physical_digests:
        raise checkpoint.CheckpointError(
            "producer binding collides with a physical artifact digest"
        )
    raise checkpoint.CheckpointError(
        "generic checkpoint rekey has no current exact stage-declaration authority"
    )


def _validate_rekey_preflight(
    *,
    cache_root: Path,
    source_identity: dict,
    target_identity: dict,
    source_manifest: dict,
    equivalent_inputs: object,
    projected_equivalent_inputs: object,
    projection_verifier,
    allowed_added_inputs: object,
    allowed_removed_inputs: object,
    allow_source_lock_change: object,
    allow_source_commits_change: object,
    expected_outputs: object,
    legacy_seal_planned: bool,
) -> None:
    """Complete all rekey-contract checks before either mutation primitive."""
    if not isinstance(equivalent_inputs, dict) or any(
        not isinstance(source, str) or not isinstance(target, str)
        for source, target in equivalent_inputs.items()
    ):
        raise checkpoint.CheckpointError("equivalent checkpoint inputs are invalid")
    if not isinstance(projected_equivalent_inputs, dict) or any(
        not isinstance(source, str) or not isinstance(target, str)
        for source, target in projected_equivalent_inputs.items()
    ):
        raise checkpoint.CheckpointError("projected checkpoint inputs are invalid")
    if (
        not isinstance(allowed_added_inputs, list)
        or not isinstance(allowed_removed_inputs, list)
        or any(not isinstance(name, str) for name in allowed_added_inputs)
        or any(not isinstance(name, str) for name in allowed_removed_inputs)
        or len(set(allowed_added_inputs)) != len(allowed_added_inputs)
        or len(set(allowed_removed_inputs)) != len(allowed_removed_inputs)
    ):
        raise checkpoint.CheckpointError("checkpoint input allowlists are invalid")
    if not isinstance(allow_source_lock_change, bool) or not isinstance(
        allow_source_commits_change, bool
    ):
        raise checkpoint.CheckpointError("checkpoint source transition flags are invalid")
    if not isinstance(expected_outputs, dict) or any(
        not isinstance(name, str)
        or not isinstance(expectation, dict)
        or set(expectation) != {"sha256", "size_bytes"}
        or not _is_digest(expectation.get("sha256"))
        or not isinstance(expectation.get("size_bytes"), int)
        or expectation["size_bytes"] < 0
        for name, expectation in expected_outputs.items()
    ):
        raise checkpoint.CheckpointError("checkpoint rekey expected outputs are invalid")

    source_lock_changed = source_identity["source_lock"] != target_identity["source_lock"]
    if source_lock_changed and not allow_source_lock_change:
        raise checkpoint.CheckpointError("source lock transition is not allowed")
    commits_changed = (
        source_identity["source_commits"] != target_identity["source_commits"]
    )
    if commits_changed and not allow_source_commits_change:
        raise checkpoint.CheckpointError("source commit transition is not allowed")

    source_inputs = checkpoint._input_records_by_name(source_identity)
    target_inputs = checkpoint._input_records_by_name(target_identity)
    if set(equivalent_inputs) & set(projected_equivalent_inputs):
        raise checkpoint.CheckpointError(
            "input cannot use exact and projected equivalence"
        )
    exact_targets = set(equivalent_inputs.values())
    projected_targets = set(projected_equivalent_inputs.values())
    if exact_targets & projected_targets:
        raise checkpoint.CheckpointError(
            "target input cannot use exact and projected equivalence"
        )
    if set(equivalent_inputs) - set(source_inputs):
        raise checkpoint.CheckpointError("equivalent source input is missing")
    if len(exact_targets) != len(equivalent_inputs):
        raise checkpoint.CheckpointError("equivalent target inputs are duplicated")
    if exact_targets - set(target_inputs):
        raise checkpoint.CheckpointError("equivalent target input is missing")
    for source_name, target_name in sorted(equivalent_inputs.items()):
        if checkpoint._comparable_input(
            source_inputs[source_name]
        ) != checkpoint._comparable_input(target_inputs[target_name]):
            raise checkpoint.CheckpointError(
                f"equivalent input differs: {source_name} -> {target_name}"
            )
    if set(projected_equivalent_inputs) - set(source_inputs):
        raise checkpoint.CheckpointError("projected source input is missing")
    if len(projected_targets) != len(projected_equivalent_inputs):
        raise checkpoint.CheckpointError("projected target inputs are duplicated")
    if projected_targets - set(target_inputs):
        raise checkpoint.CheckpointError("projected target input is missing")
    if projected_equivalent_inputs and projection_verifier is None:
        raise checkpoint.CheckpointError(
            "projected input equivalence requires an executed verifier"
        )
    if not projected_equivalent_inputs and projection_verifier is not None:
        raise checkpoint.CheckpointError("projected input verifier has no declared inputs")
    if projection_verifier is not None:
        for source_name, target_name in sorted(projected_equivalent_inputs.items()):
            proof = projection_verifier(
                source_inputs[source_name], target_inputs[target_name]
            )
            if (
                not isinstance(proof, dict)
                or set(proof) != {"kind", "proof_digest"}
                or not isinstance(proof.get("kind"), str)
                or checkpoint.SAFE_NAME.fullmatch(proof["kind"]) is None
                or not _is_digest(proof.get("proof_digest"))
            ):
                raise checkpoint.CheckpointError(
                    "projected input verifier returned an invalid proof"
                )

    covered_source = set(equivalent_inputs) | set(projected_equivalent_inputs)
    covered_target = exact_targets | projected_targets
    if set(source_inputs) - covered_source != set(allowed_removed_inputs):
        raise checkpoint.CheckpointError(
            "removed input allowlist is incomplete or excessive"
        )
    if set(target_inputs) - covered_target != set(allowed_added_inputs):
        raise checkpoint.CheckpointError(
            "added input allowlist is incomplete or excessive"
        )

    output_records = {
        record.get("name"): record for record in source_manifest.get("outputs", [])
        if isinstance(record, dict)
    }
    if None in output_records or set(output_records) != set(expected_outputs):
        raise checkpoint.CheckpointError(
            "expected output set does not match source checkpoint"
        )
    for name, expectation in expected_outputs.items():
        if (
            output_records[name].get("sha256") != expectation["sha256"]
            or output_records[name].get("size_bytes") != expectation["size_bytes"]
        ):
            raise checkpoint.CheckpointError(
                f"expected output digest or size mismatch: {name}"
            )

    if not legacy_seal_planned:
        checkpoint.verify_checkpoint(cache_root, source_identity)
    target_checkpoint = checkpoint._checkpoint_directory(cache_root, target_identity)
    if target_checkpoint.exists() or target_checkpoint.is_symlink():
        checkpoint.verify_checkpoint(cache_root, target_identity)


def verify_configured_target_transition(
    *,
    source_identity: dict,
    target_identity: dict,
    transition: dict,
    expected_outputs: dict,
    configured_contract_proof: Path | None,
    configured_runtime_manifest: Path | None,
    configured_product_manifest: Path | None,
    configured_repository_manifest: Path | None,
    configured_node_runtime: Path | None,
    configured_validator: Path | None,
) -> dict:
    """Prove configured-byte equivalence against exact installed state."""
    required_paths = (
        configured_contract_proof,
        configured_runtime_manifest,
        configured_product_manifest,
        configured_repository_manifest,
        configured_node_runtime,
        configured_validator,
    )
    if any(path is None for path in required_paths):
        raise checkpoint.CheckpointError("configured transition inputs are missing")
    assert configured_contract_proof is not None
    assert configured_runtime_manifest is not None
    assert configured_product_manifest is not None
    assert configured_repository_manifest is not None
    assert configured_node_runtime is not None
    assert configured_validator is not None
    if (
        source_identity.get("stage") != "configured-target"
        or target_identity.get("stage") != "configured-target"
    ):
        raise checkpoint.CheckpointError("configured transition belongs to another stage")
    if (
        not isinstance(transition, dict)
        or set(transition) != CONFIGURED_TRANSITION_KEYS
        or transition.get("kind") != "configured-target-installed-contract-v1"
    ):
        raise checkpoint.CheckpointError("configured target transition is invalid")

    source_records = checkpoint._input_records_by_name(source_identity)
    for source_name, transition_name in (
        ("build-implementation", "source_build_implementation_sha256"),
        ("configured-source", "source_configured_source_sha256"),
    ):
        record = source_records.get(source_name)
        declared_digest = transition.get(transition_name)
        if (
            record is None
            or record.get("kind") != "file"
            or re.fullmatch(r"[0-9a-f]{64}", declared_digest or "") is None
            or record.get("sha256") != declared_digest
        ):
            raise checkpoint.CheckpointError(
                f"configured legacy source declaration differs: {source_name}"
            )

    for name, path, role in (
        (
            "configured-contract-proof",
            configured_contract_proof,
            "configured contract proof",
        ),
        ("configured-runtime", configured_runtime_manifest, "configured runtime"),
        ("configured-product", configured_product_manifest, "configured product"),
        (
            "offline-repository",
            configured_repository_manifest,
            "configured repository manifest",
        ),
        ("node-runtime", configured_node_runtime, "configured Node runtime"),
    ):
        _verify_identity_file(
            identity=target_identity,
            name=name,
            path=path,
            role=role,
        )

    proof = _load_real_json(configured_contract_proof, "configured contract proof")
    if (
        set(proof) != CONFIGURED_PROOF_KEYS
        or proof.get("schema_version") != 1
        or proof.get("verification_kind")
        != "configured-target-installed-contract-v1"
        or proof.get("validation") != {"result": "passed"}
    ):
        raise checkpoint.CheckpointError("configured contract proof schema is invalid")
    unsigned_proof = {
        key: value for key, value in proof.items() if key != "proof_digest"
    }
    if proof.get("proof_digest") != checkpoint._json_digest(unsigned_proof):
        raise checkpoint.CheckpointError("configured contract proof digest is invalid")
    if proof.get("source_checkpoint_identity") != source_identity.get(
        "checkpoint_identity"
    ):
        raise checkpoint.CheckpointError("configured contract source differs")
    if proof.get("checkpoint_outputs") != expected_outputs:
        raise checkpoint.CheckpointError("configured contract outputs differ")
    if proof.get("validator_sha256") != checkpoint.sha256_file(configured_validator):
        raise checkpoint.CheckpointError("configured contract validator differs")

    runtime = _load_real_json(configured_runtime_manifest, "configured runtime manifest")
    product = _load_real_json(configured_product_manifest, "configured product manifest")
    repository = _load_real_json(
        configured_repository_manifest,
        "configured repository manifest",
    )
    if (
        runtime.get("schema_version") != 1
        or runtime.get("stage") != "configured-target"
        or proof.get("runtime_input_digest") != runtime.get("input_digest")
    ):
        raise checkpoint.CheckpointError("configured runtime contract differs")
    if (
        product.get("schema_version") != 1
        or product.get("stage") != "configured-target"
        or proof.get("product_input_digest") != product.get("input_digest")
    ):
        raise checkpoint.CheckpointError("configured product contract differs")
    if (
        repository.get("schema_version") != 1
        or repository.get("validation")
        != {"result": "passed", "signatures": "required"}
        or proof.get("repository_identity") != repository.get("identity")
    ):
        raise checkpoint.CheckpointError("configured repository contract differs")
    expected_node = {
        "filename": configured_node_runtime.name,
        "sha256": checkpoint.sha256_file(configured_node_runtime),
        "size_bytes": configured_node_runtime.stat().st_size,
    }
    if proof.get("staged_node") != expected_node:
        raise checkpoint.CheckpointError("configured Node runtime contract differs")
    if (
        not isinstance(proof.get("filesystems"), dict)
        or not isinstance(proof.get("stage_state"), dict)
        or not isinstance(proof.get("installed_packages"), int)
        or proof["installed_packages"] <= 0
        or re.fullmatch(r"[0-9a-f]{64}", proof.get("package_inventory_sha256", ""))
        is None
    ):
        raise checkpoint.CheckpointError("configured installed-state proof is invalid")
    raise checkpoint.CheckpointError(
        "configured target rekey requires an executed authoritative validator"
    )


def apply_plan(
    *,
    cache_root: Path,
    target_identity_path: Path,
    plan_path: Path,
    legacy_build_lock: Path | None = None,
    package_source_lock: Path | None = None,
    configured_contract_proof: Path | None = None,
    configured_runtime_manifest: Path | None = None,
    configured_product_manifest: Path | None = None,
    configured_node_runtime: Path | None = None,
) -> dict:
    plan = _load_real_json(plan_path, "checkpoint rekey plan")
    if (
        set(plan) != PLAN_KEYS
        or plan.get("schema_version") != REKEY_PLAN_SCHEMA_VERSION
    ):
        raise checkpoint.CheckpointError("checkpoint rekey plan schema is invalid")
    target_identity = checkpoint._load_identity(target_identity_path)
    stage = target_identity["stage"]
    if plan["stage"] != stage or plan["mode"] != target_identity["mode"]:
        raise checkpoint.CheckpointError("checkpoint rekey plan stage or mode is stale")
    source_checkpoint_identity = plan["source_checkpoint_identity"]
    if re.fullmatch(r"[0-9a-f]{64}", source_checkpoint_identity) is None:
        raise checkpoint.CheckpointError("checkpoint rekey source identity is invalid")
    if plan["target_checkpoint_identity"] != target_identity["checkpoint_identity"]:
        raise checkpoint.CheckpointError("checkpoint rekey target identity is stale")
    source_manifest_path = (
        cache_root
        / "checkpoints"
        / stage
        / source_checkpoint_identity
        / "manifest.json"
    )
    source_manifest = _load_real_json(source_manifest_path, "source checkpoint manifest")
    source_identity = checkpoint._manifest_identity(source_manifest)
    checkpoint._assert_identity(source_identity)
    if source_identity["checkpoint_identity"] != source_checkpoint_identity:
        raise checkpoint.CheckpointError("source checkpoint path and identity mismatch")
    validate_rekey_contract(
        plan=plan,
        source_manifest=source_manifest,
        source_identity=source_identity,
        target_identity=target_identity,
    )
    if plan["target_source_lock_sha256"] != target_identity["source_lock"].get(
        "sha256"
    ):
        raise checkpoint.CheckpointError("checkpoint rekey target source lock is stale")

    expected_outputs = plan["expected_outputs"]
    if not isinstance(expected_outputs, dict):
        raise checkpoint.CheckpointError("checkpoint rekey expected outputs are invalid")
    configured_transition = plan["configured_target_transition"]
    configured_arguments = (
        configured_contract_proof,
        configured_runtime_manifest,
        configured_product_manifest,
        configured_node_runtime,
    )
    if configured_transition is not None:
        target_records = checkpoint._input_records_by_name(target_identity)
        repository_record = target_records.get("offline-repository")
        if repository_record is None:
            raise checkpoint.CheckpointError(
                "configured target identity has no repository manifest"
            )
        repository_path = _checkpoint_input_object_path(
            cache_root,
            repository_record,
            "configured repository manifest object",
        )
        verify_configured_target_transition(
            source_identity=source_identity,
            target_identity=target_identity,
            transition=configured_transition,
            expected_outputs=expected_outputs,
            configured_contract_proof=configured_contract_proof,
            configured_runtime_manifest=configured_runtime_manifest,
            configured_product_manifest=configured_product_manifest,
            configured_repository_manifest=repository_path,
            configured_node_runtime=configured_node_runtime,
            configured_validator=Path(__file__).with_name(
                "capture-asahi-configured-target.py"
            ),
        )
    elif any(value is not None for value in configured_arguments):
        raise checkpoint.CheckpointError("undeclared configured transition inputs")
    legacy_admission = plan["legacy_immutable_admission"]
    if legacy_admission is not None:
        if not isinstance(legacy_admission, dict) or set(legacy_admission) != {
            "kind",
            "manifest_sha256",
            "manifest_size_bytes",
        }:
            raise checkpoint.CheckpointError("legacy immutable admission is invalid")
        if legacy_admission["kind"] != "legacy-checkpoint-immutable-admission-v1":
            raise checkpoint.CheckpointError("legacy immutable admission kind is invalid")
        if not _is_digest(legacy_admission.get("manifest_sha256")) or not isinstance(
            legacy_admission.get("manifest_size_bytes"), int
        ) or legacy_admission["manifest_size_bytes"] <= 0:
            raise checkpoint.CheckpointError("legacy immutable admission is invalid")
    projected_inputs = plan["projected_equivalent_inputs"]
    transition = plan["repository_manifest_transition"]
    if not isinstance(projected_inputs, dict):
        raise checkpoint.CheckpointError("projected checkpoint inputs are invalid")
    projection_verifier = None
    if projected_inputs:
        if len(projected_inputs) != 1:
            raise checkpoint.CheckpointError("unsupported projected checkpoint inputs")
        source_repository_name, target_repository_name = next(
            iter(projected_inputs.items())
        )
        if (
            source_repository_name != target_repository_name
            or source_repository_name
            not in {"repository-manifest", "offline-repository"}
        ):
            raise checkpoint.CheckpointError("unsupported projected checkpoint inputs")
        if (
            not isinstance(transition, dict)
            or transition.get("kind") != "repository-database-manifest-v1"
            or set(transition) - {"kind", "ownertrust_transition"}
        ):
            raise checkpoint.CheckpointError("repository manifest transition is invalid")
        if legacy_build_lock is None or package_source_lock is None:
            raise checkpoint.CheckpointError("repository transition lock inputs are missing")
        source_records = checkpoint._input_records_by_name(source_identity)
        target_records = checkpoint._input_records_by_name(target_identity)
        source_repository = _load_checkpoint_object_json(
            cache_root,
            source_records[source_repository_name],
            "source repository manifest object",
        )
        target_repository = _load_checkpoint_object_json(
            cache_root,
            target_records[target_repository_name],
            "target repository manifest object",
        )
        repository_module = _load_repository_module()
        verified_projection = None

        def verify_repository_projection(source_record: dict, target_record: dict) -> dict:
            nonlocal verified_projection
            if source_record != source_records[source_repository_name]:
                raise checkpoint.CheckpointError("repository source input changed during rekey")
            if target_record != target_records[target_repository_name]:
                raise checkpoint.CheckpointError("repository target input changed during rekey")
            if verified_projection is not None:
                return dict(verified_projection)
            try:
                verified_projection = repository_module.verify_repository_database_transition(
                    source_manifest=source_repository,
                    target_manifest=target_repository,
                    legacy_build_lock=legacy_build_lock,
                    package_source_lock=package_source_lock,
                    mode=target_identity["mode"],
                    ownertrust_transition=transition.get("ownertrust_transition"),
                )
            except repository_module.RepositoryCaptureError as error:
                raise checkpoint.CheckpointError(
                    f"repository manifest transition failed: {error}"
                ) from error
            return dict(verified_projection)

        projection_verifier = verify_repository_projection
    elif transition is not None or legacy_build_lock is not None or package_source_lock is not None:
        raise checkpoint.CheckpointError("undeclared repository transition inputs")
    _validate_rekey_preflight(
        cache_root=cache_root,
        source_identity=source_identity,
        target_identity=target_identity,
        source_manifest=source_manifest,
        equivalent_inputs=plan["equivalent_inputs"],
        projected_equivalent_inputs=projected_inputs,
        projection_verifier=projection_verifier,
        allowed_added_inputs=plan["allowed_added_inputs"],
        allowed_removed_inputs=plan["allowed_removed_inputs"],
        allow_source_lock_change=plan["allow_source_lock_change"],
        allow_source_commits_change=plan["allow_source_commits_change"],
        expected_outputs=expected_outputs,
        legacy_seal_planned=legacy_admission is not None,
    )
    if legacy_admission is not None:
        checkpoint.seal_legacy_checkpoint(
            cache_root=cache_root,
            identity=source_identity,
            expected_manifest={
                "sha256": legacy_admission["manifest_sha256"],
                "size_bytes": legacy_admission["manifest_size_bytes"],
            },
            expected_outputs=expected_outputs,
            reason=legacy_admission["kind"],
        )
    return checkpoint.rekey_checkpoint(
        cache_root=cache_root,
        source_identity=source_identity,
        target_identity=target_identity,
        equivalent_inputs=plan["equivalent_inputs"],
        projected_equivalent_inputs=projected_inputs,
        projected_equivalence_verifier=projection_verifier,
        allowed_added_inputs=set(plan["allowed_added_inputs"]),
        allowed_removed_inputs=set(plan["allowed_removed_inputs"]),
        allow_source_lock_change=plan["allow_source_lock_change"],
        allow_source_commits_change=plan["allow_source_commits_change"],
        expected_outputs=expected_outputs,
        reason=plan["reason"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--target-identity", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--legacy-build-lock", type=Path)
    parser.add_argument("--package-source-lock", type=Path)
    parser.add_argument("--configured-contract-proof", type=Path)
    parser.add_argument("--configured-runtime-manifest", type=Path)
    parser.add_argument("--configured-product-manifest", type=Path)
    parser.add_argument("--configured-node-runtime", type=Path)
    arguments = parser.parse_args()
    result = apply_plan(
        cache_root=arguments.cache_root,
        target_identity_path=arguments.target_identity,
        plan_path=arguments.plan,
        legacy_build_lock=arguments.legacy_build_lock,
        package_source_lock=arguments.package_source_lock,
        configured_contract_proof=arguments.configured_contract_proof,
        configured_runtime_manifest=arguments.configured_runtime_manifest,
        configured_product_manifest=arguments.configured_product_manifest,
        configured_node_runtime=arguments.configured_node_runtime,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (checkpoint.CheckpointError, json.JSONDecodeError, OSError) as error:
        raise SystemExit(f"asahi-checkpoint-rekey: {error}") from error
