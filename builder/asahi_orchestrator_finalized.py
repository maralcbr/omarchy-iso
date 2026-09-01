"""Finalized-boot phase profile for the checkpoint runner."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable


STAGE = "finalized"
PHASE_MODULE = "orchestrator.finalized_phases"
PHASE_SOURCE = (
    "configs/airootfs/usr/share/omarchy-iso/orchestrator/finalized_phases.py"
)
PHASES = (
    ("Preparing install target", "prepare_install_target"),
    ("Finalizing boot", "finalize_boot"),
    ("Installing vendor firmware", "install_vendor_firmware"),
    ("Enabling platform services", "enable_apple_platform_services"),
    ("Finalizing user", "run_chroot_finalizer"),
    ("Configuring login", "configure_login"),
    ("Configuring SSH access", "configure_ssh_access"),
    ("Configuring Tailscale", "configure_tailscale"),
    ("Configuring DNS resolver", "configure_dns_resolver"),
    ("Configuring package repository", "configure_arm_package_repository"),
    ("Validating boot setup", "validate_boot"),
    ("Creating factory snapshot", "create_factory_snapshot"),
)


def merge_timing(configured: dict[str, Any], finalized: dict[str, Any]) -> dict[str, Any]:
    phases = [*configured.get("phases", []), *finalized.get("phases", [])]
    merged = dict(finalized)
    merged.update(
        {
            "started_at": configured.get("started_at", finalized.get("started_at")),
            "finished_at": finalized.get("finished_at", configured.get("finished_at")),
            "total_phases": len(phases),
            "current_index": max(len(phases) - 1, 0),
            "current_phase": "Installation complete",
            "phases": phases,
            "checkpointed_stages": ["configured-target", "finalized-boot"],
        }
    )
    return merged


def _external_evidence_path(ctx, environment_name: str, description: str) -> Path:
    value = os.environ.get(environment_name, "")
    path = Path(value)
    if not value or not path.is_absolute():
        raise RuntimeError(f"{description} path is missing or unsafe")
    for artifact_root in (ctx.state_dir, ctx.target):
        if path == artifact_root or artifact_root in path.parents:
            raise RuntimeError(f"{description} must be outside artifact paths")
    return path


def _remove_image_run_evidence(ctx) -> None:
    for relative in (
        "var/log/omarchy-install.log",
        "var/log/omarchy-install-timing.json",
    ):
        path = ctx.target / relative
        if path.is_dir() and not path.is_symlink():
            raise RuntimeError(f"Installed run-evidence path is an unsafe directory: {path}")
        path.unlink(missing_ok=True)


def prepare_stage(ctx) -> dict[str, Any]:
    environment_name = "OMARCHY_ASAHI_CONFIGURED_TIMING_EVIDENCE"
    if environment_name not in os.environ:
        return {}
    configured_timing_path = _external_evidence_path(
        ctx,
        environment_name,
        "Configured-stage timing evidence",
    )
    try:
        value = json.loads(configured_timing_path.read_text())
    except (OSError, json.JSONDecodeError) as exception:
        raise RuntimeError(
            f"Configured-stage timing is missing or invalid: {exception}"
        ) from exception
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("verification_kind")
        != "asahi-orchestrator-run-evidence-v1"
        or value.get("stage") != "configured-target"
        or not isinstance(value.get("timing"), dict)
    ):
        raise RuntimeError(
            "Configured-stage timing is missing or invalid: expected bound evidence"
        )
    return value["timing"]


def persist_stage_timing(
    ctx,
    state: dict[str, Any],
    prepared: dict[str, Any],
    atomic_json: Callable[[Path, dict[str, Any]], None],
) -> None:
    evidence_path = _external_evidence_path(
        ctx,
        "OMARCHY_ASAHI_ORCHESTRATOR_RUN_EVIDENCE",
        "Finalized-stage run evidence",
    )
    evidence = {
        "schema_version": 1,
        "verification_kind": "asahi-orchestrator-run-evidence-v1",
        "stage": "finalized-boot",
        "timing": merge_timing(prepared, state),
    }
    _remove_image_run_evidence(ctx)
    atomic_json(evidence_path, evidence)
