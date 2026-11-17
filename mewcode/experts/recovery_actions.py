"""Host-owned recovery capabilities and bounded, inspectable action receipts.

This registry describes execution methods, not authority. An unavailable
executor is never represented as a successful recovery action.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from mewcode.experts.recovery_state import advanced

POLICY_VERSION = "recovery-actions-v1"
ACTION_ORDER = (
    "retry_same_checkpoint", "retry_review_after_recheck",
    "gather_read_only_facts", "reduce_execution_profile",
    "switch_declared_route", "replan_execution_method",
    "reconcile_protocol", "reconcile_state", "human_decision",
)
ACTION_LIMITS = {
    # One dispatch per identical diagnosis/input/cursor fingerprint. A newly
    # persisted work cursor is a new generation and may be considered again.
    "retry_same_checkpoint": 1,
    "retry_review_after_recheck": 1,
    "gather_read_only_facts": 1,
    "reduce_execution_profile": 1,
    "switch_declared_route": 1,
    "replan_execution_method": 1,
    "reconcile_protocol": 1,
    "reconcile_state": 1,
    "human_decision": 1,
}
LADDERS = {
    "capacity_shortfall": ("retry_same_checkpoint", "reduce_execution_profile", "human_decision"),
    "idle_stall": ("reduce_execution_profile", "switch_declared_route", "gather_read_only_facts", "replan_execution_method", "human_decision"),
    "environment_fault": ("gather_read_only_facts", "replan_execution_method", "human_decision"),
    "review_infrastructure": ("retry_review_after_recheck", "human_decision"),
    "protocol_conflict": ("reconcile_protocol", "human_decision"),
    "model_service_fault": ("retry_same_checkpoint", "reduce_execution_profile", "switch_declared_route", "human_decision"),
    "state_divergence": ("reconcile_state", "human_decision"),
    "business_ambiguity": ("human_decision",),
    "context_window_limit": ("replan_execution_method", "human_decision"),
    "output_window_limit": ("replan_execution_method", "human_decision"),
    "reasoning_limit": ("switch_declared_route", "replan_execution_method", "human_decision"),
    "insufficient_evidence": ("gather_read_only_facts", "retry_same_checkpoint", "human_decision"),
}


def action_key(*, diagnosis_kind: str, action: str, input_revision: str | None,
               marker: dict[str, Any], policy_version: str = POLICY_VERSION) -> str:
    """A restart-stable key for one attempted action on one input generation."""
    payload = [policy_version, diagnosis_kind, action, input_revision, marker]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def plan_recovery_actions(*, diagnosis: dict[str, Any], allowed: list[str],
                          checkpoint_present: bool, hard_boundary: bool,
                          review_route_present: bool = False,
                          adaptive_methods: bool = False,
                          adaptive_eligible: bool = False,
                          supervisor_replan: bool = False,
                          declared_backup_present: bool = False,
                          prior_attempts: list[dict[str, Any]] | None = None,
                          input_revision: str | None = None,
                          marker: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return current ladder availability without running any action.

    ``allowed`` is the already budget- and permission-gated legacy dispatcher
    set. The registry may narrow it, never create a new permission or budget.
    """
    kind = str(diagnosis.get("primary_kind") or "insufficient_evidence")
    ladder = LADDERS.get(kind, LADDERS["insufficient_evidence"])
    if supervisor_replan and kind in {"capacity_shortfall", "insufficient_evidence"}:
        ladder = ("replan_execution_method",) + ladder
    saved = [entry for entry in prior_attempts or [] if isinstance(entry, dict)]
    cursor = marker or {}
    rows = []
    for action in ladder:
        executor = None
        reason = ""
        if action == "human_decision":
            status = "available"
        elif hard_boundary:
            status, reason = "blocked", "hard_boundary"
        elif action == "retry_same_checkpoint":
            executor = "continue_checkpoint"
            status = "available" if executor in allowed and checkpoint_present else "unavailable"
            reason = "" if status == "available" else "checkpoint_or_budget_missing"
        elif action == "retry_review_after_recheck":
            executor = "continue_checkpoint"
            status = "available" if executor in allowed and checkpoint_present and review_route_present else "unavailable"
            reason = "" if status == "available" else "review_receipt_or_budget_missing"
        elif action == "gather_read_only_facts":
            executor = "diagnose_path"
            status = "available" if executor in allowed else "unavailable"
            reason = "" if status == "available" else "diagnostic_executor_missing"
        elif action == "reduce_execution_profile":
            executor = "reduce_execution_profile"
            status = ("available" if executor in allowed and checkpoint_present
                      else "unavailable")
            reason = "" if status == "available" else "bounded_profile_executor_or_checkpoint_missing"
        elif action == "replan_execution_method":
            executor = "diagnose_path" if adaptive_methods and adaptive_eligible else None
            status = "available" if executor in allowed else "unavailable"
            reason = "" if status == "available" else "adaptive_executor_unavailable"
        elif action == "switch_declared_route":
            executor = "switch_declared_route" if declared_backup_present else None
            status = "available" if executor in allowed else "unavailable"
            reason = "" if status == "available" else "declared_backup_unavailable"
        elif action == 'reconcile_protocol' and (diagnosis.get('submission_correction') or {}).get('category') in {
                'contract_error', 'reference_mismatch'}:
            executor = 'continue_checkpoint'
            status = 'available' if executor in allowed and checkpoint_present else 'unavailable'
            reason = '' if status == 'available' else 'checkpoint_or_budget_missing'
        else:
            status, reason = "unavailable", "executor_not_implemented"
        key = action_key(diagnosis_kind=kind, action=action, input_revision=input_revision,
                         marker=cursor)
        count = sum(entry.get("id") == key for entry in saved)
        if action != "human_decision" and status == "available" and count >= ACTION_LIMITS[action]:
            status, reason = "blocked", "attempt_limit_reached"
        rows.append({"action": action, "legacy_action": executor,
                     "status": status, "reason": reason,
                     "max_attempts": ACTION_LIMITS[action], "attempts": count,
                     "idempotency_key": key})
    return {"schema_version": 1, "policy_version": POLICY_VERSION,
            "diagnosis_kind": kind, "actions": rows}


def verify_attempt(attempt: dict[str, Any], *, marker: dict[str, Any],
                   terminal_status: str, error_kind: str = "") -> dict[str, Any]:
    """Verify the next segment; this never certifies business acceptance."""
    receipt = dict(attempt)
    progressed = advanced(receipt.get("progress_before"), marker)
    review_cleared = (receipt.get("diagnosis_kind") == "review_infrastructure"
                      and terminal_status == "completed" and not error_kind)
    receipt["progress_after"] = marker
    receipt["verification"] = ("durable_progress" if progressed else
                                "infrastructure_cleared" if review_cleared else
                                "no_verified_progress")
    receipt["status"] = "verified" if progressed or review_cleared else "no_verified_progress"
    return receipt
