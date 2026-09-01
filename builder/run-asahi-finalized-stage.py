#!/usr/bin/env python3
"""Run only the finalized-boot orchestrator profile."""

from __future__ import annotations

import asahi_orchestrator_finalized as profile
from asahi_orchestrator_runner import main
from orchestrator import finalized_phases as implementation


if __name__ == "__main__":
    raise SystemExit(main(profile, implementation))
