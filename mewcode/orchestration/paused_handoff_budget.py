"""Explicit, source-bound allowance for additional handoff review calls."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable


GRANT_VERSION = "paused-handoff-review-grant-v1"
MAX_REVIEW_TOKENS = 2_000_000
FINAL_DECISION_RESERVE_TOKENS = 35_000
_SOURCE_ID = re.compile(r"^[a-f0-9]{64}$")


def observed_review_token_ratio(events: Iterable[dict[str, Any]]) -> tuple[int, int, int]:
    """Peak actual/base-reservation ratio from completed real review calls.

    A failed or in-flight request supplies no safe sample. Its reservation is
    accounted separately and execution remains held for reconciliation.
    """
    numerator, denominator, samples = 1, 1, 0
    pending: dict[tuple[str, str], int] = {}
    targets = {"started": "classified", "review_started": "reviewed",
               "omission_started": "omission_reviewed"}
    for event in events:
        phase = event.get("phase")
        fragment_id = str(event.get("fragment_id") or "")
        if phase in targets:
            base = event.get("base_reserved_tokens", event.get("reserved_tokens"))
            if isinstance(base, int) and not isinstance(base, bool) and base > 0:
                pending[(fragment_id, targets[phase])] = base
        elif phase in {"classified", "reviewed", "omission_reviewed"}:
            base = pending.pop((fragment_id, phase), None)
            if base is None:
                continue
            usage = event.get("usage") or {}
            actual = max(0, int(usage.get("input_tokens") or 0)) + max(
                0, int(usage.get("output_tokens") or 0)
            )
            if actual <= 0:
                continue
            samples += 1
            if actual * denominator > numerator * base:
                numerator, denominator = actual, base
    return numerator, denominator, samples


def calibrated_review_reservation(base_tokens: int, *,
                                  ratio_numerator: int,
                                  ratio_denominator: int) -> int:
    if (base_tokens < 0 or ratio_numerator < 1 or ratio_denominator < 1):
        raise ValueError("invalid review reservation calibration")
    return (base_tokens * ratio_numerator + ratio_denominator - 1) // ratio_denominator


def suggest_handoff_review_budget(budget: dict[str, Any], *,
                                  spent_tokens: int,
                                  unknown_reserved_tokens: int,
                                  approved_total_tokens: int | None = None) -> dict[str, Any]:
    """Generate a finite *proposal*, never an authorization or a grant update.

    The preflight estimate is for work still to do. Actual usage and an
    unresolved remote call's reservation are added once across the complete
    source lineage. The displayed number rounds upward, never downward.
    """
    remaining = budget.get("estimated_semantic_tokens_upper")
    values = (remaining, spent_tokens, unknown_reserved_tokens)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in values):
        raise ValueError("invalid handoff review budget accounting")
    if (approved_total_tokens is not None
            and (isinstance(approved_total_tokens, bool)
                 or not isinstance(approved_total_tokens, int)
                 or approved_total_tokens < 1)):
        raise ValueError("invalid approved handoff review budget")
    required = remaining + spent_tokens + unknown_reserved_tokens
    suggested = max(1_000, ((required + 999) // 1_000) * 1_000)
    supported = suggested <= MAX_REVIEW_TOKENS
    return {
        "required_total_tokens_upper": required,
        "suggested_total_tokens": suggested if supported else None,
        "exceeds_supported_max": not supported,
        "spent_tokens": spent_tokens,
        "unknown_reserved_tokens": unknown_reserved_tokens,
        "estimated_remaining_tokens_upper": remaining,
        "approved_total_tokens": approved_total_tokens,
        "additional_tokens_needed": (
            (max(0, suggested - approved_total_tokens)
             if required > approved_total_tokens else 0)
            if supported and approved_total_tokens is not None else None
        ),
        "requires_explicit_authorization": (
            approved_total_tokens is None or required > approved_total_tokens
        ),
        "basis": "actual_receipts_plus_recalibrated_remaining_v1",
    }


def diagnose_handoff_review_blocker(status: dict[str, Any],
                                    recommendation: dict[str, Any], *,
                                    has_semantic_route: bool,
                                    hard_hold_records: int = 0) -> dict[str, Any]:
    """Distinguish a negotiable capacity limit from evidence that must stay held.

    This is a host decision about *what to ask next*, never a permission to
    spend more, switch model, repeat an unknown call, or resume the run.
    """
    code = str(status.get("status") or "")
    reference = str(status.get("fragment_id") or status.get("record_id") or "")
    result = {"status": code, "source_reference": reference,
              "can_auto_resume": False, "can_auto_increase_total": False}
    if code in {"outcome_unknown", "actual_budget_overrun"}:
        return {**result, "kind": "accounting_or_effect_unknown",
                "action": "reconcile_before_more_calls"}
    if code == "hard_reconciliation_required":
        return {**result, "kind": "evidence_provenance_gap",
                "action": "reconcile_hard_holds_before_paid_review",
                "hard_hold_records": hard_hold_records,
                "reason": "source_bound_effect_receipts_missing"}
    if code == "context_window_insufficient":
        return {**result, "kind": "execution_window_limit",
                "action": "request_larger_context_or_lossless_split",
                "context_sizes": status.get("context_sizes") or {}}
    if code in {"insufficient_budget", "budget_unknown", "semantic_route_unavailable"}:
        if hard_hold_records > 0:
            return {**result, "kind": "evidence_and_capacity",
                    "action": "reconcile_hard_holds_before_paid_review",
                    "hard_hold_records": hard_hold_records,
                    "suggested_total_tokens": recommendation.get("suggested_total_tokens")}
        if code == "semantic_route_unavailable" and not has_semantic_route:
            return {**result, "kind": "route_unavailable",
                    "action": "request_declared_typed_route"}
        if recommendation.get("exceeds_supported_max"):
            return {**result, "kind": "capacity_limit",
                    "action": "split_review_or_request_policy_decision"}
        return {**result, "kind": "capacity_limit",
                "action": "request_explicit_total_cap",
                "suggested_total_tokens": recommendation.get("suggested_total_tokens"),
                "additional_tokens_needed": recommendation.get("additional_tokens_needed")}
    if code in {"review_hold", "omission_hold"}:
        reason = str(status.get("reason") or "")
        if reason == "semantic_uncertain":
            return {**result, "kind": "reasoning_limit",
                    "action": "request_declared_deeper_review", "reason": reason}
        return {**result, "kind": "evidence_or_checkpoint_hold",
                "action": "inspect_source_and_reconcile", "reason": reason}
    return {**result, "kind": "none", "action": "continue_existing_gate"}


def authorize_handoff_review(snapshot: dict[str, Any], *,
                             source_id: str, provider_key: str,
                             model_id: str, max_tokens: int) -> dict[str, Any]:
    """Create/raise a *total* cap; never change route or reset spent usage."""
    if snapshot.get("status") not in {"paused", "blocked", "needs_attention"}:
        raise ValueError("run is not paused")
    pending = snapshot.get("pending_dialogue_handoff") or {}
    progress = pending.get("handoff_review_progress") if isinstance(pending, dict) else None
    if (not isinstance(progress, dict) or progress.get("source_id") != source_id
            or not _SOURCE_ID.fullmatch(source_id)):
        raise ValueError("review grant source is not current")
    if (isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
            or not 1 <= max_tokens <= MAX_REVIEW_TOKENS):
        raise ValueError("invalid review token cap")
    if not provider_key or not model_id:
        raise ValueError("review route must be explicit")
    old = snapshot.get("handoff_review_grant") or {}
    if old:
        lineage = progress.get("source_lineage") or [source_id]
        if (old.get("source_id") not in lineage
                or old.get("provider_key") != provider_key
                or old.get("model_id") != model_id):
            raise ValueError("changing source or route needs a separate review decision")
        if max_tokens <= int(old.get("max_tokens") or 0):
            raise ValueError("new total cap must exceed the existing cap")
    return {"policy_version": GRANT_VERSION,
            "grant_id": str(old.get("grant_id") or uuid.uuid4().hex),
            "source_id": source_id,
            "provider_key": provider_key, "model_id": model_id,
            "max_tokens": max_tokens,
            **({"escalation_route": old["escalation_route"]}
               if isinstance(old.get("escalation_route"), dict) else {}),
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approved_conversation_id": snapshot.get("conversation_id"),
            "basis": "explicit_user_review_token_cap"}


def authorize_handoff_review_escalation(
    snapshot: dict[str, Any], *, source_id: str,
    provider_key: str, model_id: str,
) -> dict[str, Any]:
    """Declare one distinct secondary typed route inside the existing total cap.

    This changes neither the approved token ceiling nor a previous judgment.
    A model switch is never inferred from a low-confidence answer alone.
    """
    if snapshot.get("status") not in {"paused", "blocked", "needs_attention"}:
        raise ValueError("run is not paused")
    progress = ((snapshot.get("pending_dialogue_handoff") or {})
                .get("handoff_review_progress") or {})
    grant = snapshot.get("handoff_review_grant") or {}
    lineage = progress.get("source_lineage") or [source_id]
    if (progress.get("source_id") != source_id
            or not _SOURCE_ID.fullmatch(source_id)
            or grant.get("policy_version") != GRANT_VERSION
            or grant.get("source_id") not in lineage
            or grant.get("approved_conversation_id") != snapshot.get("conversation_id")
            or isinstance(grant.get("max_tokens"), bool)
            or not isinstance(grant.get("max_tokens"), int)
            or grant["max_tokens"] <= 0
            or not provider_key or not model_id
            or (provider_key, model_id)
            == (grant.get("provider_key"), grant.get("model_id"))
            or grant.get("escalation_route")):
        raise ValueError("secondary review route is not an available source-bound upgrade")
    return {**grant, "escalation_route": {
        "provider_key": provider_key, "model_id": model_id,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "basis": "explicit_user_secondary_review_route",
    }}
