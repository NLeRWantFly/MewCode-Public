"""Shared orchestration error type.

Lives in its own module so leaf helpers (kinds, loops, interventions) can raise
it without importing ``spec`` back (which would be a cycle). ``spec`` re-exports
the name, so every existing ``from ...spec import OrchestrationError`` keeps
working unchanged.
"""

from __future__ import annotations


class OrchestrationError(ValueError):
    """Invalid orchestration specification."""


__all__ = ["OrchestrationError"]
