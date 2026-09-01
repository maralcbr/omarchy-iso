#!/usr/bin/env python3
"""Preview source-declaration impact without touching build checkpoints."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path, PurePosixPath
import stat
from typing import Any


SCHEMA_VERSION = 1
EARLY_STAGE_BOUNDARY = "finalized-boot"
PROFILE_TERMINALS = {
    "diagnostic": "finalized-boot",
    "qualification": "installer-metadata",
}
INTENT_BOUNDARIES = {
    "boot-only": "finalized-boot",
    "package-content": "verified-package-cache",
    "full": "builder-toolchain",
}
CRITICAL_SOURCE_ROOTS = {"archiso", "bin", "builder", "configs"}
EXECUTABLE_SUFFIXES = {".bash", ".fish", ".pl", ".py", ".rb", ".sh", ".zsh"}


class SourceImpactError(RuntimeError):
    pass


def _validate_stage_input_specification(
    repository: Path,
    specification: dict[str, Any],
) -> None:
    """Reuse the authoritative declaration validator without creating a package."""
    module_path = Path(__file__).with_name("asahi_stage_inputs.py")
    module_spec = importlib.util.spec_from_file_location(
        "_omarchy_asahi_stage_inputs",
        module_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise SourceImpactError("stage input validator is unavailable")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    try:
        module.validate_specification(repository, specification)
    except module.StageInputError as error:
        raise SourceImpactError(f"stage input specification is invalid: {error}") from error


def _ordered_downstream(
    specification: dict[str, Any], owner_stages: set[str]
) -> list[str]:
    affected = set(owner_stages)
    changed = True
    while changed:
        changed = False
        for stage in specification["stage_order"]:
            dependencies = set(specification["stages"][stage]["depends_on"])
            if dependencies & affected and stage not in affected:
                affected.add(stage)
                changed = True
    return [stage for stage in specification["stage_order"] if stage in affected]


def _estimate_cost(
    *,
    stage_order: list[str],
    invalidated: list[str],
    profile: str,
    cost_data: dict[str, Any],
) -> dict[str, Any]:
    terminal_index = stage_order.index(PROFILE_TERMINALS[profile])
    planned = [
        stage for stage in invalidated if stage_order.index(stage) <= terminal_index
    ]
    estimates: dict[str, float] = {}
    sources: dict[str, dict[str, str]] = {}
    unknown: list[str] = []
    for stage in planned:
        observation = cost_data.get("stages", {}).get(stage)
        if not isinstance(observation, dict) or not isinstance(
            observation.get("seconds"), (int, float)
        ):
            unknown.append(stage)
            continue
        estimates[stage] = float(observation["seconds"])
        sources[stage] = {
            "source": observation["source"],
            "run_id": observation["run_id"],
        }
    return {
        "metric": cost_data.get("metric"),
        "profile_terminal_stage": PROFILE_TERMINALS[profile],
        "stage_seconds": estimates,
        "total_known_seconds": sum(estimates.values()),
        "unknown_stages": unknown,
        "complete": not unknown,
        "sources": sources,
    }


def _normalise_changed_paths(changed_paths: list[str]) -> list[str]:
    normalised: set[str] = set()
    for value in changed_paths:
        if not isinstance(value, str) or not value:
            raise SourceImpactError(f"unsafe changed path: {value!r}")
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise SourceImpactError(f"unsafe changed path: {value!r}")
        relative = str(candidate)
        if relative in {"", "."}:
            raise SourceImpactError(f"unsafe changed path: {value!r}")
        normalised.add(relative)
    if not normalised:
        raise SourceImpactError("at least one changed path is required")
    return sorted(normalised)


def _looks_executable(repository: Path, relative: str) -> bool:
    candidate = PurePosixPath(relative)
    if candidate.parts[0] == "bin":
        return True
    if candidate.suffix.lower() in EXECUTABLE_SUFFIXES:
        return True
    if candidate.name.endswith("Containerfile"):
        return True
    path = repository / relative
    try:
        return bool(stat.S_IMODE(path.lstat().st_mode) & 0o111)
    except FileNotFoundError:
        return False


def preview_source_impact(
    *,
    repository: Path,
    specification: dict[str, Any],
    cost_data: dict[str, Any],
    changed_paths: list[str],
    intent: str,
    profile: str,
) -> dict[str, Any]:
    """Return declaration-derived source impact, never checkpoint state."""
    if intent not in INTENT_BOUNDARIES:
        raise SourceImpactError(f"unsupported intent: {intent}")
    if profile not in PROFILE_TERMINALS:
        raise SourceImpactError(f"unsupported profile: {profile}")
    _validate_stage_input_specification(repository, specification)
    if cost_data.get("schema_version") != SCHEMA_VERSION:
        raise SourceImpactError("unsupported source impact cost schema")

    stage_order = specification["stage_order"]
    changed = _normalise_changed_paths(changed_paths)
    ownership: dict[str, list[str]] = {}
    admission_ownership: dict[str, list[str]] = {}
    classifications: dict[str, str] = {}
    not_applicable: list[str] = []
    unknown_inputs: list[str] = []
    for path in changed:
        ownership[path] = [
            stage
            for stage in stage_order
            if path in specification["stages"][stage]["source_paths"]
            or path in specification["common_producer_inputs"]
        ]
        admission_ownership[path] = [
            stage
            for stage in stage_order
            if path in specification["stages"][stage]["admission_paths"]
            or path in specification["common_admission_inputs"]
        ]
        if ownership[path]:
            classifications[path] = "declared-stage-input"
            continue
        if admission_ownership[path]:
            classifications[path] = "declared-admission-input"
            continue
        root = PurePosixPath(path).parts[0]
        if root in CRITICAL_SOURCE_ROOTS:
            if _looks_executable(repository, path):
                classifications[path] = "unknown-executable-input"
            else:
                classifications[path] = "unknown-critical-input"
            unknown_inputs.append(path)
        else:
            classifications[path] = "not-applicable"
            not_applicable.append(path)
    owner_set = {stage for stages in ownership.values() for stage in stages}
    admission_owner_set = {
        stage for stages in admission_ownership.values() for stage in stages
    }
    invalidated = _ordered_downstream(specification, owner_set)
    admission_frontier = _ordered_downstream(specification, admission_owner_set)
    boot_index = stage_order.index(EARLY_STAGE_BOUNDARY)
    expected_early_hits = [
        stage for stage in stage_order[:boot_index] if stage not in invalidated
    ]
    boundary_index = stage_order.index(INTENT_BOUNDARIES[intent])
    intent_violations = [
        stage for stage in invalidated if stage_order.index(stage) < boundary_index
    ]
    block_reasons = [
        (
            f"unknown executable input is absent from stage declarations: {path}"
            if classifications[path] == "unknown-executable-input"
            else f"unknown critical input is absent from stage declarations: {path}"
        )
        for path in unknown_inputs
    ]
    if intent_violations:
        block_reasons.append(
            f"{intent} intent invalidates stages before {INTENT_BOUNDARIES[intent]}: "
            f"{', '.join(intent_violations)}"
        )
    blocked = bool(block_reasons)

    return {
        "schema_version": SCHEMA_VERSION,
        "verification_kind": "asahi-source-impact-preview",
        "claim_scope": "source-declarations-only",
        "checkpoint_state_inspected": False,
        "expected_hits_disclaimer": (
            "Expected hits mean only that source declarations do not invalidate the stage; "
            "checkpoint existence, integrity, runtime inputs, and admission were not inspected."
        ),
        "intent": intent,
        "profile": profile,
        "changed_paths": changed,
        "path_ownership": ownership,
        "admission_path_ownership": admission_ownership,
        "path_classifications": classifications,
        "not_applicable_paths": not_applicable,
        "owner_stages": [stage for stage in stage_order if stage in owner_set],
        "admission_owner_stages": [
            stage for stage in stage_order if stage in admission_owner_set
        ],
        "invalidation_frontier": invalidated,
        "admission_frontier": admission_frontier,
        "requires_readmission": bool(admission_frontier),
        "expected_early_stage_hits": expected_early_hits,
        "blocked": blocked,
        "ready_for_expensive_work": not blocked,
        "block_reasons": block_reasons,
        "estimated_cost": _estimate_cost(
            stage_order=stage_order,
            invalidated=invalidated,
            profile=profile,
            cost_data=cost_data,
        ),
    }


def _load_json(path: Path, role: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SourceImpactError(f"{role} is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise SourceImpactError(f"{role} must be a JSON object: {path}")
    return value


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
    parser.add_argument("--changed-path", action="append", required=True)
    parser.add_argument("--intent", choices=tuple(INTENT_BOUNDARIES), required=True)
    parser.add_argument("--profile", choices=tuple(PROFILE_TERMINALS), required=True)
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    try:
        preview = preview_source_impact(
            repository=arguments.repo_root.resolve(),
            specification=_load_json(arguments.spec, "stage input specification"),
            cost_data=_load_json(arguments.cost_data, "source impact cost data"),
            changed_paths=arguments.changed_path,
            intent=arguments.intent,
            profile=arguments.profile,
        )
    except SourceImpactError as error:
        parser.error(str(error))
    print(json.dumps(preview, indent=2, sort_keys=True))
    return 2 if preview["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
