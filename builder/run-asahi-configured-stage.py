#!/usr/bin/env python3
"""Run only the configured-target orchestrator profile."""

from __future__ import annotations

import asahi_orchestrator_configured as profile
from asahi_orchestrator_runner import main
from orchestrator import configured_phases as implementation


if __name__ == "__main__":
    raise SystemExit(main(profile, implementation))
