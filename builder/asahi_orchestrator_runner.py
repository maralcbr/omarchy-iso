"""Shared runner for one statically selected Asahi orchestrator profile."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import ModuleType
from typing import Any


def stage_phase_names(profile: ModuleType) -> list[str]:
    return [name for name, _ in profile.PHASES]


def _phase_functions(profile: ModuleType, implementation: ModuleType):
    return [
        (name, getattr(implementation, function))
        for name, function in profile.PHASES
    ]


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
    )
    temporary.replace(path)


def run_stage(profile: ModuleType, implementation: ModuleType) -> int:
    from orchestrator.configured_phases import (
        boost_cpu_governor,
        cleanup_bind_mounts,
        cleanup_protected_state,
        cleanup_target_hook_masks,
        restore_cpu_governors,
    )
    from orchestrator.context import InstallContext
    from orchestrator.phases import PhaseError, run
    from orchestrator.ui import error, info

    stage = profile.STAGE
    try:
        ctx = InstallContext.from_env()
    except RuntimeError as exception:
        error(f"Configuration error: {exception}")
        return 2

    try:
        prepared = profile.prepare_stage(ctx)
    except RuntimeError as exception:
        error(str(exception))
        return 2

    info(f"Running checkpointed Asahi stage: {stage}")
    governors = boost_cpu_governor()
    success = False
    try:
        try:
            run(ctx, _phase_functions(profile, implementation))
            success = True
        except PhaseError:
            error(f"Checkpointed Asahi stage halted: {stage}")
            return 1
        except KeyboardInterrupt:
            error(f"Checkpointed Asahi stage interrupted: {stage}")
            return 130

        state_path = ctx.state_dir / "state.json"
        state = json.loads(state_path.read_text())
        profile.persist_stage_timing(ctx, state, prepared, _atomic_json)
        info(f"Checkpointed Asahi stage complete: {stage}")
        return 0
    finally:
        restore_cpu_governors(governors)
        cleanup_bind_mounts(ctx)
        cleanup_target_hook_masks(ctx)
        if not success:
            cleanup_protected_state(ctx)


def main(profile: ModuleType, implementation: ModuleType) -> int:
    if len(sys.argv) != 1:
        print(f"Usage: {Path(sys.argv[0]).name}", file=sys.stderr)
        return 64
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    return run_stage(profile, implementation)
