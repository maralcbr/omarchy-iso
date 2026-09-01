#!/usr/bin/env python3
"""Canonical builder-toolchain metadata: schema-2 validation and schema-1 projection.

Added 2026-08-30 (plan Phase C1). Before this module three surfaces validated the
same schema-2 documents independently and disagreed about what was valid:

  - the producer, builder/ensure-asahi-toolchain-image.sh, in jq;
  - the checkpoint planner, in python;
  - the projection gate in bin/omarchy-iso-make, in jq.

Note on the planner: it imports this module, not the reverse. Its path is left
unwritten above on purpose. The stage-input validator discovers executed inputs
by scanning file text, so naming it here would make this module appear to
execute the planner and would drag the planner's own inputs into
builder-toolchain's identity.

This module is the single source of truth for all of it. Each surface now
delegates its metadata decisions here and keeps only what is genuinely its own:
the producer keeps its docker-state probes, the planner stays metadata-only, and
bin/omarchy-iso-make keeps only the call.

THE BYTE-STABILITY INVARIANT
----------------------------
`projection_bytes` reproduces, byte for byte, what `jq -S` produced from the
projection filter that shipped before this module existed. Those bytes feed the
verified-package-cache checkpoint identity, so any drift silently rekeys a
downstream stage. test/unit/test_asahi_toolchain_projection_golden.py asserts
byte equality against goldens generated with the preserved jq program from the
Phase A rollback checkpoint.

Two emitter details are load bearing:

  - Projection uses ensure_ascii=False because jq emits raw UTF-8. Every
    validated string is ASCII, so the two agree today; this keeps them agreeing
    if that ever stops being true.
  - Digests use ensure_ascii=True and compact separators, matching both the
    planner's existing _digest and `jq -ceS`. Changing it would rekey every
    existing identity.

Integer magnitude is bounded at 2**53 for the same reason: jq carries numbers as
doubles and silently mangles integers above that, so a manifest that would
project differently under jq than under this module is rejected outright rather
than projected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


SCHEMA_VERSION = 2
PROJECTION_SCHEMA_VERSION = 1
STAGE = "builder-toolchain"
MODE = "shared"

SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")

# jq represents numbers as IEEE doubles. Anything at or above this loses
# integer precision when projected through jq, so it can never be byte-stable.
MAXIMUM_EXACT_INTEGER = 2**53

COMPATIBILITY_REASON = "stage-input-granularity-v1"

IDENTITY_KEYS = {
    "schema_version",
    "stage",
    "mode",
    "input_digest",
    "checkpoint_identity",
}
RUN_KEYS = IDENTITY_KEYS | {
    "validation",
    "completed_at",
    "elapsed_seconds",
    "cache_hit",
    "output",
}
MANIFEST_KEYS = {
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
OUTPUT_KEYS = {"image_id", "size_bytes", "package_inventory_sha256"}
COMPATIBILITY_KEYS = {
    "schema_version",
    "reason",
    "source_checkpoint_identity",
    "source_lock_sha256",
    "target_lock_sha256",
}
DECLARED_INPUT_KEYS = {
    "base_image",
    "source_lock_sha256",
    "containerfile_sha256",
    "script_sha256",
    "source",
    "toolchain_packages",
}
ACTUAL_INPUT_KEYS = DECLARED_INPUT_KEYS | {
    "package_inventory_sha256",
    "package_inventory",
    "synchronized_database_digests",
}
SOURCE_KEYS = {"omarchy_iso_stage", "omarchy_iso_producer", "manifest_sha256"}

TOOLCHAIN_ENVIRONMENT = "OMARCHY_ASAHI_TOOLCHAIN_PREPARED=1"

# The schema-1 projection. This tuple, and nothing else, defines the projected
# shape; the gate below closes over exactly these keys.
PROJECTION_KEYS = (
    "checkpoint_identity",
    "compatibility",
    "input_digest",
    "mode",
    "output",
    "schema_version",
    "stage",
    "validation",
)


class ToolchainMetadataError(Exception):
    """A schema-2 document or its schema-1 projection is not valid."""


def canonical_bytes(value: Any) -> bytes:
    """Digest input form. Matches the planner's _digest and `jq -ceS`."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _is_exact_integer(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value < MAXIMUM_EXACT_INTEGER
    )


def _validate_completed_at(value: Any, role: str) -> None:
    from datetime import datetime

    if not isinstance(value, str) or not value.endswith("Z"):
        raise ToolchainMetadataError(
            f"checkpoint completion timestamp is invalid: {role}"
        )
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ToolchainMetadataError(
            f"checkpoint completion timestamp is invalid: {role}"
        ) from error


def _validate_elapsed(value: Any, role: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ToolchainMetadataError(f"{role} elapsed time is invalid")


def validate_output(value: Any, role: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != OUTPUT_KEYS:
        raise ToolchainMetadataError(f"{role} output metadata is invalid")
    if (
        not isinstance(value.get("image_id"), str)
        or IMAGE_ID.fullmatch(value["image_id"]) is None
        or not _is_exact_integer(value.get("size_bytes"))
        or not _is_sha256(value.get("package_inventory_sha256"))
    ):
        raise ToolchainMetadataError(f"{role} output metadata is invalid")
    return value


def validate_compatibility(
    value: Any,
    *,
    expected_target_lock: str | None = None,
    expected_source_lock: str | None = None,
    role: str,
) -> dict[str, Any]:
    """Validate a rekey compatibility block.

    Shape, schema version, the reason literal, and digest formats are always
    checked. The two bindings are checked when the caller can supply them:

      - `expected_target_lock` -- the stage's current source lock. Available to
        the producer and the planner, which both hold the declared inputs.
      - `expected_source_lock` -- the digest of the legacy
        builder/asahi-build-lock.json on disk. The producer always bound it; the
        planner did not, so a block naming a source lock that never existed used
        to be accepted there. Every surface that can reach the file now binds it.
    """
    if not isinstance(value, dict) or set(value) != COMPATIBILITY_KEYS:
        raise ToolchainMetadataError(f"{role} compatibility metadata is invalid")
    if (
        value.get("schema_version") != 1
        or value.get("reason") != COMPATIBILITY_REASON
        or not _is_sha256(value.get("source_checkpoint_identity"))
        or not _is_sha256(value.get("source_lock_sha256"))
        or not _is_sha256(value.get("target_lock_sha256"))
    ):
        raise ToolchainMetadataError(f"{role} compatibility metadata is invalid")
    if (
        expected_target_lock is not None
        and value["target_lock_sha256"] != expected_target_lock
    ):
        raise ToolchainMetadataError(f"{role} compatibility metadata is invalid")
    if (
        expected_source_lock is not None
        and value["source_lock_sha256"] != expected_source_lock
    ):
        raise ToolchainMetadataError(f"{role} compatibility metadata is invalid")
    return value


def validate_run_record(record: Any, *, role: str = STAGE) -> dict[str, Any]:
    """Validate a schema-2 run manifest (the producer's run record)."""
    if not isinstance(record, dict):
        raise ToolchainMetadataError(f"{role} run record fields are invalid")
    allowed = {
        frozenset(RUN_KEYS),
        frozenset(RUN_KEYS | {"compatibility"}),
        frozenset(RUN_KEYS | {"compatibility", "rekeyed"}),
    }
    if frozenset(record) not in allowed:
        raise ToolchainMetadataError(f"{role} run record fields are invalid")
    if (
        record.get("schema_version") != SCHEMA_VERSION
        or record.get("stage") != STAGE
        or record.get("mode") != MODE
        or record.get("validation") != {"result": "passed"}
        or not isinstance(record.get("cache_hit"), bool)
    ):
        raise ToolchainMetadataError(f"{role} run record is invalid")
    if "rekeyed" in record and (
        record["rekeyed"] is not True or record.get("cache_hit") is not False
    ):
        raise ToolchainMetadataError(f"{role} rekey claim is invalid")
    _validate_completed_at(record.get("completed_at"), role)
    _validate_elapsed(record.get("elapsed_seconds"), f"{role} run")
    if not _is_sha256(record.get("input_digest")) or not _is_sha256(
        record.get("checkpoint_identity")
    ):
        raise ToolchainMetadataError(f"{role} run identity metadata is invalid")
    validate_output(record.get("output"), f"{role} run")
    compatibility = record.get("compatibility")
    if compatibility is not None:
        # Shape, reason, and digest formats are checkable here without any
        # further context. The lock bindings need files, so callers that can
        # reach them pass them in.
        validate_compatibility(compatibility, role=f"{role} run")
    return record


def validate_checkpoint_manifest(
    manifest: Any,
    *,
    role: str = STAGE,
    expected_declared_input_digest: str | None = None,
    expected_source_lock_sha256: str | None = None,
    expected_source_identity: str | None = None,
    expected_producer_binding_identity: str | None = None,
) -> dict[str, Any]:
    """Validate a schema-2 checkpoint manifest's metadata.

    Content that only the container runtime can confirm -- that the image exists,
    that it embeds the expected lock and inventory -- is deliberately not checked
    here. That stays with the producer.
    """
    if not isinstance(manifest, dict):
        raise ToolchainMetadataError(f"{role} manifest fields are invalid")
    allowed = {frozenset(MANIFEST_KEYS), frozenset(MANIFEST_KEYS | {"compatibility"})}
    if frozenset(manifest) not in allowed:
        raise ToolchainMetadataError(f"{role} manifest fields are invalid")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("stage") != STAGE
        or manifest.get("mode") != MODE
        or manifest.get("validation") != {"result": "passed"}
        or manifest.get("cache_hit") is not False
        or manifest.get("immutable") is not True
        or manifest.get("environment") != TOOLCHAIN_ENVIRONMENT
    ):
        raise ToolchainMetadataError(f"{role} manifest is invalid")
    _validate_completed_at(manifest.get("completed_at"), role)
    _validate_elapsed(manifest.get("elapsed_seconds"), f"{role} manifest")

    declared_inputs = manifest.get("declared_inputs")
    actual_inputs = manifest.get("actual_inputs")
    if (
        not isinstance(declared_inputs, dict)
        or set(declared_inputs) != DECLARED_INPUT_KEYS
        or not isinstance(declared_inputs.get("source"), dict)
        or set(declared_inputs["source"]) != SOURCE_KEYS
        or not isinstance(actual_inputs, dict)
        or set(actual_inputs) != ACTUAL_INPUT_KEYS
    ):
        raise ToolchainMetadataError(f"{role} input metadata fields are invalid")

    declared_digest = manifest.get("declared_input_digest")
    if not _is_sha256(declared_digest) or declared_digest != digest(declared_inputs):
        raise ToolchainMetadataError(f"{role} declared input digest is mismatched")
    if {key: actual_inputs[key] for key in DECLARED_INPUT_KEYS} != declared_inputs:
        raise ToolchainMetadataError(f"{role} actual inputs do not bind declared inputs")

    inventory = actual_inputs.get("package_inventory")
    synchronized = actual_inputs.get("synchronized_database_digests")
    for records in (inventory, synchronized):
        if (
            not isinstance(records, list)
            or not records
            or any(
                not isinstance(record, str)
                or not record
                or "\n" in record
                or "\0" in record
                for record in records
            )
        ):
            raise ToolchainMetadataError(f"{role} actual inventory metadata is invalid")

    inventory_digest = hashlib.sha256(
        ("\n".join(inventory) + "\n").encode("utf-8")
    ).hexdigest()
    output = validate_output(manifest.get("output"), f"{role} manifest")
    if (
        actual_inputs.get("package_inventory_sha256") != inventory_digest
        or output["package_inventory_sha256"] != inventory_digest
    ):
        raise ToolchainMetadataError(f"{role} package inventory digest is mismatched")

    identity = manifest.get("checkpoint_identity")
    if not _is_sha256(identity) or identity != digest(actual_inputs):
        raise ToolchainMetadataError(
            f"{role} checkpoint identity or directory binding is mismatched"
        )

    if (
        expected_declared_input_digest is not None
        and declared_digest != expected_declared_input_digest
    ) or (
        expected_source_lock_sha256 is not None
        and declared_inputs.get("source_lock_sha256") != expected_source_lock_sha256
    ):
        raise ToolchainMetadataError(f"{role} declared inputs do not match current inputs")
    source = declared_inputs["source"]
    if (
        expected_source_identity is not None
        and source.get("omarchy_iso_stage") != expected_source_identity
    ) or (
        expected_producer_binding_identity is not None
        and source.get("omarchy_iso_producer") != expected_producer_binding_identity
    ):
        raise ToolchainMetadataError(f"{role} producer binding does not match current inputs")
    return manifest


def project(run_record: dict[str, Any]) -> dict[str, Any]:
    """Project a validated schema-2 run manifest to the schema-1 identity shape."""
    projected: dict[str, Any] = {}
    for key in PROJECTION_KEYS:
        if key == "schema_version":
            projected[key] = PROJECTION_SCHEMA_VERSION
        elif key == "compatibility":
            projected[key] = run_record.get("compatibility") or None
        else:
            projected[key] = run_record.get(key)
    return projected


def projection_bytes(run_record: dict[str, Any]) -> bytes:
    """Byte-stable schema-1 projection. Must equal the retired `jq -S` output."""
    return (
        json.dumps(project(run_record), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def validate_projection(projected: Any, *, role: str = "schema-1 projection") -> dict:
    """The gate that used to live inline in bin/omarchy-iso-make."""
    if not isinstance(projected, dict) or sorted(projected) != sorted(PROJECTION_KEYS):
        raise ToolchainMetadataError(f"{role} fields are invalid")
    validation = projected.get("validation")
    output = projected.get("output")
    if (
        projected.get("schema_version") != PROJECTION_SCHEMA_VERSION
        or projected.get("stage") != STAGE
        or not isinstance(validation, dict)
        or validation.get("result") != "passed"
        or not _is_sha256(projected.get("checkpoint_identity"))
        or not _is_sha256(projected.get("input_digest"))
        or not isinstance(output, dict)
        or not isinstance(output.get("image_id"), str)
        or IMAGE_ID.fullmatch(output["image_id"]) is None
    ):
        raise ToolchainMetadataError(f"{role} is invalid")
    return projected


def read_json(path: Path, role: str) -> Any:
    """Read a JSON document. `-` reads stdin.

    The producer verifies an immutable snapshot it already read, rather than the
    file on disk, so that a manifest swapped underneath it cannot change the
    decision. Passing that snapshot through stdin preserves the property.
    """
    try:
        text = sys.stdin.read() if str(path) == "-" else path.read_text()
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as error:
        raise ToolchainMetadataError(f"{role} is unreadable: {path}") from error


def _command_project(arguments: argparse.Namespace) -> int:
    record = read_json(arguments.run_manifest, "builder-toolchain run manifest")
    validate_run_record(record)
    compatibility = record.get("compatibility")
    if compatibility is not None and arguments.legacy_lock is not None:
        if not arguments.legacy_lock.is_file() or arguments.legacy_lock.is_symlink():
            raise ToolchainMetadataError(
                "builder-toolchain run compatibility metadata is invalid"
            )
        validate_compatibility(
            compatibility,
            expected_source_lock=file_digest(arguments.legacy_lock),
            role="builder-toolchain run",
        )
    payload = projection_bytes(record)
    validate_projection(json.loads(payload))
    arguments.output.write_bytes(payload)
    return 0


def _command_validate_run_manifest(arguments: argparse.Namespace) -> int:
    validate_run_record(read_json(arguments.manifest, "builder-toolchain run manifest"))
    return 0


def _command_validate_checkpoint_manifest(arguments: argparse.Namespace) -> int:
    manifest = read_json(arguments.manifest, "builder-toolchain checkpoint manifest")
    validate_checkpoint_manifest(
        manifest,
        expected_declared_input_digest=arguments.expected_declared_input_digest,
        expected_source_lock_sha256=arguments.expected_source_lock_sha256,
        expected_source_identity=arguments.expected_source_identity,
        expected_producer_binding_identity=arguments.expected_producer_binding_identity,
    )
    compatibility = manifest.get("compatibility")
    if compatibility is not None:
        if arguments.legacy_lock is None:
            raise ToolchainMetadataError(
                "builder-toolchain compatibility metadata is invalid"
            )
        if not arguments.legacy_lock.is_file() or arguments.legacy_lock.is_symlink():
            raise ToolchainMetadataError(
                "builder-toolchain compatibility metadata is invalid"
            )
        validate_compatibility(
            compatibility,
            expected_target_lock=manifest["declared_inputs"]["source_lock_sha256"],
            expected_source_lock=file_digest(arguments.legacy_lock),
            role="builder-toolchain manifest",
        )
    elif arguments.require_compatibility:
        raise ToolchainMetadataError(
            "builder-toolchain compatibility metadata is invalid"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    projector = commands.add_parser(
        "project", help="validate a schema-2 run manifest and write its schema-1 projection"
    )
    projector.add_argument("--run-manifest", type=Path, required=True)
    projector.add_argument("--output", type=Path, required=True)
    projector.add_argument("--legacy-lock", type=Path)
    projector.set_defaults(handler=_command_project)

    runner = commands.add_parser("validate-run-manifest")
    runner.add_argument("--manifest", type=Path, required=True)
    runner.set_defaults(handler=_command_validate_run_manifest)

    checkpoint = commands.add_parser("validate-checkpoint-manifest")
    checkpoint.add_argument("--manifest", type=Path, required=True)
    checkpoint.add_argument("--expected-declared-input-digest")
    checkpoint.add_argument("--expected-source-lock-sha256")
    checkpoint.add_argument("--expected-source-identity")
    checkpoint.add_argument("--expected-producer-binding-identity")
    checkpoint.add_argument("--legacy-lock", type=Path)
    checkpoint.add_argument("--require-compatibility", action="store_true")
    checkpoint.set_defaults(handler=_command_validate_checkpoint_manifest)

    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except ToolchainMetadataError as error:
        print(f"{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
