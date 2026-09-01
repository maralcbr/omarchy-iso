"""Compatibility facade for the split orchestrator phase modules.

Checkpointed builds import :mod:`configured_phases` or :mod:`finalized_phases`
directly. This facade remains for existing full-install callers and downstream
imports, and is intentionally outside checkpoint producer identities.
"""

from __future__ import annotations

from . import configured_phases as _configured_phases
from . import finalized_phases as _finalized_phases


def _reexport(module) -> None:
    for name, value in vars(module).items():
        if not name.startswith("__"):
            globals()[name] = value


_reexport(_configured_phases)
_reexport(_finalized_phases)
del _reexport
