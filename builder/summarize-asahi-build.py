#!/usr/bin/env python3
"""Produce deterministic acceleration evidence for one Asahi package build."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_phases(root: Path) -> list[dict]:
    phases = []
    for path in sorted(root.glob("*.json")):
        if path.name in {"build-report.json", "retention.json"}:
            continue
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or "stage" not in value or "cache_hit" not in value:
            continue
        invalidation = root / f"{value['stage']}.invalidation"
        reason = invalidation.read_text().strip() if invalidation.is_file() else None
        phases.append(
            {
                "stage": value["stage"],
                "checkpoint_identity": value.get("checkpoint_identity"),
                "elapsed_seconds": value.get("elapsed_seconds", 0),
                "cache_hit": value["cache_hit"],
                "reproducibility_match": value.get("reproducibility_match", False),
                "invalidation_cause": reason,
                "outputs": value.get("outputs", value.get("output", {})),
                "validation": value.get("validation"),
            }
        )
    return sorted(phases, key=lambda value: value["stage"])


def _output_names(phase: dict) -> set[str]:
    outputs = phase.get("outputs", [])
    if isinstance(outputs, list):
        return {
            output["name"]
            for output in outputs
            if isinstance(output, dict) and isinstance(output.get("name"), str)
        }
    if isinstance(outputs, dict):
        return {name for name in outputs if isinstance(name, str)}
    return set()


def catalog_admission(
    *,
    mode: str,
    phases: list[dict],
    package: Path | None,
    source_date_epoch: int | None,
) -> dict:
    reasons: list[str] = []
    if mode != "qualification":
        reasons.append("diagnostic mode is never catalog eligible")
        return {"result": "failed", "reasons": reasons}
    if package is None:
        reasons.append("sealed release package is missing")
    if (
        source_date_epoch is None
        or isinstance(source_date_epoch, bool)
        or source_date_epoch < 0
    ):
        reasons.append("SOURCE_DATE_EPOCH was not bound")

    by_stage = {phase["stage"]: phase for phase in phases}
    required_outputs = {
        "verified-package-cache": {"repository-manifest"},
        "offline-repository-database": {"repository-db", "repository-files"},
        "base-images": {"root-image", "boot-image", "esp-image"},
        "configured-target": {"installed-contract"},
        "finalized-boot": {"installed-contents"},
        "sealed-release-package": {"release-package"},
        "installer-metadata": {"package-evidence", "installer-data"},
    }
    missing_messages = {
        "verified-package-cache": "verified package-cache receipt is missing",
        "offline-repository-database": "verified offline-repository receipt is missing",
        "base-images": "verified base-image receipt is missing",
        "configured-target": "configured installed-content contract is missing",
        "finalized-boot": "finalized installed-content evidence is missing",
        "sealed-release-package": "sealed release checkpoint evidence is missing",
        "installer-metadata": "verified installer metadata is missing",
    }
    for stage, expected_outputs in required_outputs.items():
        phase = by_stage.get(stage)
        if phase is None or not expected_outputs.issubset(_output_names(phase)):
            reasons.append(missing_messages[stage])
            continue
        if phase.get("validation") != {"result": "passed"}:
            reasons.append(f"{stage} validation did not pass")

    sealed = by_stage.get("sealed-release-package")
    if sealed is not None:
        if sealed.get("cache_hit") is True:
            reasons.append("sealed release was restored instead of independently rebuilt")
        if sealed.get("reproducibility_match") is not True:
            reasons.append(
                "sealed release was not independently rebuilt with identical bytes"
            )
    return {"result": "passed" if not reasons else "failed", "reasons": reasons}


def build_report(
    *,
    mode: str,
    run_id: str,
    evidence_root: Path,
    package: Path | None,
    source_date_epoch: int | None,
) -> dict:
    if mode not in {"diagnostic", "qualification"}:
        raise ValueError(f"unsupported mode: {mode}")
    phases = load_phases(evidence_root)
    dominant = sorted(
        (
            {"stage": phase["stage"], "elapsed_seconds": phase["elapsed_seconds"]}
            for phase in phases
        ),
        key=lambda value: (-value["elapsed_seconds"], value["stage"]),
    )
    misses = [phase for phase in phases if not phase["cache_hit"]]
    next_optimization = (
        f"Measure and narrow inputs for {dominant[0]['stage']} without weakening verification."
        if dominant
        else "No checkpoint phase evidence was available; repair instrumentation before optimizing."
    )
    retention_path = evidence_root / "retention.json"
    retention = (
        json.loads(retention_path.read_text())
        if retention_path.is_file()
        else {"evicted": [], "reclaimed_bytes": 0, "result": "not-run"}
    )
    admission = catalog_admission(
        mode=mode,
        phases=phases,
        package=package,
        source_date_epoch=source_date_epoch,
    )
    report = {
        "schema_version": 1,
        "verification_kind": "asahi-build-acceleration-report",
        "run_id": run_id,
        "mode": mode,
        "catalog_eligible": admission["result"] == "passed",
        "catalog_admission": admission,
        "source_date_epoch": source_date_epoch,
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "phase_elapsed_seconds": {
            phase["stage"]: phase["elapsed_seconds"] for phase in phases
        },
        "cache_hits": [phase["stage"] for phase in phases if phase["cache_hit"]],
        "cache_misses": [phase["stage"] for phase in misses],
        "invalidation_causes": {
            phase["stage"]: phase["invalidation_cause"]
            for phase in misses
            if phase["invalidation_cause"]
        },
        "phases": phases,
        "dominant_phases": dominant,
        "retention": retention,
        "next_safe_optimization": next_optimization,
    }
    if package is not None:
        status = package.stat()
        report["sealed_release_package"] = {
            "filename": package.name,
            "size_bytes": status.st_size,
            "sha256": sha256_file(package),
        }
    return report


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"output is a symlink: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--source-date-epoch", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    atomic_json(
        args.output,
        build_report(
            mode=args.mode,
            run_id=args.run_id,
            evidence_root=args.evidence_root,
            package=args.package,
            source_date_epoch=args.source_date_epoch,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
