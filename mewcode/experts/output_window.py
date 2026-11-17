"""Bounded response-output escalation for an expert model call."""
from __future__ import annotations


def next_output_limit(current: int, *, remaining_budget: int | None,
                      configured_cap: int, safety_cap: int = 64_000) -> int | None:
    """Return one at-most-2x increase, or None when chunking is required."""
    if (isinstance(current, bool) or not isinstance(current, int) or current < 1
            or isinstance(configured_cap, bool) or not isinstance(configured_cap, int)
            or configured_cap < 1 or safety_cap < 1
            or (remaining_budget is not None and
                (isinstance(remaining_budget, bool) or not isinstance(remaining_budget, int)
                 or remaining_budget < 0))):
        raise ValueError("invalid output window")
    cap = min(configured_cap, safety_cap,
              remaining_budget if remaining_budget is not None else safety_cap)
    candidate = min(cap, current * 2)
    return candidate if candidate > current else None
