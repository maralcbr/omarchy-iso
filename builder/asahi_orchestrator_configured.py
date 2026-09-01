"""Configured-target phase profile for the checkpoint runner."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any, Callable


STAGE = "configured"
PHASE_MODULE = "orchestrator.configured_phases"
PHASE_SOURCE = (
    "configs/airootfs/usr/share/omarchy-iso/orchestrator/configured_phases.py"
)
PHASES = (
    ("Preparing live environment", "prepare_live"),
    ("Preparing install target", "prepare_install_target"),
    ("Installing Arch + Omarchy", "arch_install_system"),
    ("Configuring hibernation", "configure_hibernation"),
    ("Configuring system", "run_system_finalizer"),
    ("Staging provisioning", "stage_provisioning_state"),
)


def prepare_stage(ctx) -> None:
    return None


def _run_evidence_path(ctx) -> Path:
    value = os.environ.get("OMARCHY_ASAHI_ORCHESTRATOR_RUN_EVIDENCE", "")
    path = Path(value)
    if not value or not path.is_absolute():
        raise RuntimeError("Configured-stage run evidence path is missing or unsafe")
    for artifact_root in (ctx.state_dir, ctx.target):
        if path == artifact_root or artifact_root in path.parents:
            raise RuntimeError("Configured-stage run evidence must be outside artifact paths")
    return path


def _semantic_checkpoint_state(state: dict[str, Any]) -> dict[str, Any]:
    phase_names = [name for name, _function in PHASES]
    phases = state.get("phases")
    if (
        state.get("total_phases") != len(phase_names)
        or state.get("current_index") != len(phase_names) - 1
        or state.get("current_phase") != "Installation complete"
        or not isinstance(phases, list)
        or [(phase.get("name"), phase.get("status")) for phase in phases]
        != [(name, "ok") for name in phase_names]
    ):
        raise RuntimeError("Configured-stage state is incomplete or invalid")
    installed = state.get("installed_packages")
    expected = state.get("expected_packages")
    if (
        not isinstance(installed, int)
        or isinstance(installed, bool)
        or installed < 0
        or not isinstance(expected, int)
        or isinstance(expected, bool)
        or expected < 0
    ):
        raise RuntimeError("Configured-stage package counts are invalid")
    return {
        "schema_version": 1,
        "verification_kind": "asahi-configured-stage-state-v1",
        "stage": "configured-target",
        "total_phases": len(phase_names),
        "current_index": len(phase_names) - 1,
        "current_phase": "Installation complete",
        "phases": [{"name": name, "status": "ok"} for name in phase_names],
        "installed_packages": installed,
        "expected_packages": expected,
        "validation": {"result": "passed"},
    }


def _remove_image_run_evidence(ctx) -> None:
    for relative in (
        "var/log/omarchy-install.log",
        "var/log/omarchy-install-timing.json",
    ):
        path = ctx.target / relative
        if path.is_dir() and not path.is_symlink():
            raise RuntimeError(f"Installed run-evidence path is an unsafe directory: {path}")
        path.unlink(missing_ok=True)


def _replace_state_directory(
    ctx,
    stable_state: dict[str, Any],
    atomic_json: Callable[[Path, dict[str, Any]], None],
) -> None:
    state_path = ctx.state_dir / "state.json"
    for path in ctx.state_dir.iterdir():
        if path == state_path:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    atomic_json(state_path, stable_state)


def persist_stage_timing(
    ctx,
    state: dict[str, Any],
    prepared: None,
    atomic_json: Callable[[Path, dict[str, Any]], None],
) -> None:
    del prepared
    stable_state = _semantic_checkpoint_state(state)
    evidence = {
        "schema_version": 1,
        "verification_kind": "asahi-orchestrator-run-evidence-v1",
        "stage": "configured-target",
        "timing": state,
    }
    _remove_image_run_evidence(ctx)
    _replace_state_directory(ctx, stable_state, atomic_json)
    atomic_json(_run_evidence_path(ctx), evidence)
