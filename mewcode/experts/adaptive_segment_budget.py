"""Bounded, deterministic per-segment budgeting.

Only the execution slice changes. The frozen contract, token ceiling and
remaining parent-loop wall clock are never raised by this module.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

POLICY_VERSION = "adaptive-segment-budget-v1"
REDUCED_PROFILE_VERSION = "reduced-execution-slice-v1"
MAX_HISTORY = 8
MAX_ITERATIONS = 200


def choose_segment_budget(*, base_iterations: int, base_seconds: float,
                          remaining_total_seconds: float | None,
                          history: list[dict[str, Any]] | None = None,
                          next_batch_items: int = 1) -> dict[str, Any]:
    """Use measured productive slices; unknown/nonproductive data never buys more.

    `remaining_total_seconds` is the parent's hard budget, not a proposed new
    allowance. `base_seconds` is the existing node slice. The result is a
    versioned host receipt suitable for checkpoint persistence.
    """
    if base_iterations < 1 or base_seconds <= 0 or not math.isfinite(base_seconds):
        raise ValueError("positive base segment limits required")
    if remaining_total_seconds is not None and (not math.isfinite(remaining_total_seconds)
                                                or remaining_total_seconds < 0):
        raise ValueError("invalid parent remaining time")
    if next_batch_items < 1:
        raise ValueError("next_batch_items must be positive")
    rows = [row for row in (history or [])[-MAX_HISTORY:]
            if isinstance(row, dict) and row.get("progressed") is True
            and isinstance(row.get("iterations"), int) and row["iterations"] > 0
            and isinstance(row.get("wall_seconds"), (int, float))
            and math.isfinite(row["wall_seconds"]) and row["wall_seconds"] > 0]
    per_iteration = [row["wall_seconds"] / row["iterations"] for row in rows]
    enough = len(rows) >= 2
    target_iterations = base_iterations
    target_seconds = base_seconds
    reason = "insufficient_productive_samples"
    if enough:
        median_turn = statistics.median(per_iteration)
        productive_turns = statistics.median(row["iterations"] for row in rows)
        estimated_batch_turns = math.ceil(productive_turns * min(next_batch_items, 4) * 1.25)
        target_iterations = min(MAX_ITERATIONS, max(base_iterations, estimated_batch_turns))
        # Even with no parent time limit, a single slice may grow only 2x.
        target_seconds = min(base_seconds * 2, max(base_seconds,
            math.ceil(median_turn * target_iterations * 1.25)))
        reason = "productive_sliding_window"
    if remaining_total_seconds is not None:
        target_seconds = min(target_seconds, remaining_total_seconds)
    # The first warning leaves room for a model/tool turn; the soft stop leaves
    # a second reserve for durable checkpointing before the hard wait_for cap.
    grace = min(60., max(5., target_seconds * .15)) if target_seconds > 0 else 0.
    if target_seconds <= grace:
        grace = max(0., target_seconds / 3)
    split_due_to_parent = False
    if enough:
        fit = max(1, math.floor(max(0., target_seconds - grace) / statistics.median(per_iteration)))
        if fit < target_iterations:
            target_iterations = fit
            split_due_to_parent = True
    return {"schema_version": 1, "policy_version": POLICY_VERSION,
            "reason": reason, "sample_count": len(rows),
            "base_iterations": base_iterations, "base_seconds": base_seconds,
            "max_iterations": target_iterations,
            "timeout_seconds": round(target_seconds, 3),
            "soft_grace_seconds": round(grace, 3),
            "split_due_to_parent": split_due_to_parent,
            "parent_remaining_seconds": remaining_total_seconds,
            "increases_total_budget": False,
            "changes_contract": False}


def observed_segment(*, iterations: int, wall_seconds: float, progressed: bool) -> dict[str, Any]:
    """Persist only bounded control facts, never messages or business evidence."""
    return {"iterations": max(0, int(iterations)),
            "wall_seconds": round(max(0., float(wall_seconds)), 3),
            "progressed": bool(progressed)}


def reduce_execution_slice(budget: dict[str, Any]) -> dict[str, Any]:
    """One bounded smaller slice on the same parent budget and contract."""
    iterations = int(budget["max_iterations"])
    seconds = float(budget["timeout_seconds"])
    if iterations < 1 or seconds <= 0 or not math.isfinite(seconds):
        raise ValueError("invalid segment budget for reduced profile")
    reduced_iterations = max(1, min(iterations, max(4, math.ceil(iterations / 2))))
    reduced_seconds = min(seconds, max(30.0, seconds / 2))
    grace = min(float(budget.get("soft_grace_seconds") or 0), max(0.0, reduced_seconds / 3))
    return {**budget, "max_iterations": reduced_iterations,
            "timeout_seconds": round(reduced_seconds, 3),
            "soft_grace_seconds": round(grace, 3),
            "reason": "recovery_reduced_execution_slice",
            "reduced_profile_version": REDUCED_PROFILE_VERSION,
            "original_iterations": iterations, "original_seconds": seconds,
            "increases_total_budget": False, "changes_contract": False}
