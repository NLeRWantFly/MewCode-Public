"""Deterministic diagnosis for scheduler recovery; never grants authority."""
from __future__ import annotations

from typing import Any

POLICY_VERSION = "recovery-diagnosis-v1"
KINDS = frozenset({"capacity_shortfall", "idle_stall", "environment_fault",
                   "review_infrastructure", "protocol_conflict", "model_service_fault",
                   "state_divergence", "business_ambiguity", "insufficient_evidence",
                   "context_window_limit", "reasoning_limit", "output_window_limit"})
REVIEW_ERRORS = frozenset({"safety_review_rate_limited", "safety_review_context_limit",
                           "safety_review_unavailable", "safety_review_inconclusive"})
PROTOCOL_ERRORS = frozenset({"hash_expired", "item_revision_conflict", "version_conflict", "mutual_wait", "safety_review_format"})
MODEL_ERRORS = frozenset({"provider_interrupted", "model_stream_stalled", "provider_rate_limited",
                          "provider_temporarily_unavailable"})
BUSINESS_ERRORS = frozenset({"plan_revision_required", "criterion_mismatch", "acceptance_conflict",
                             "scope_change_required"})
CAPACITY_ERRORS = frozenset({"segment_timeout", "internal_work", "budget_exhausted"})
CONTEXT_ERRORS = frozenset({"context_limit", "model_context_limit"})
REASONING_ERRORS = frozenset({"semantic_uncertain", "reasoning_insufficient", "review_low_confidence"})


def diagnose_segment(health: dict[str, Any], *, error_kind: str = "",
                     blockers: list[dict[str, Any]] | None = None,
                     host_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return one primary kind and the exact host facts behind it.

    Old human-readable errors are intentionally absent from the authority
    path. The caller may display them, but cannot recover automatically from
    a prose-only guess.
    """
    error = error_kind or str(health.get("error_kind") or "")
    facts = host_facts or {}
    blocked = [row.get("kind") for row in blockers or []]
    evidence: list[str] = []
    kind = "insufficient_evidence"
    source = "insufficient_evidence"
    hard = bool(facts.get("hard_boundary")) or bool(set(blocked) & {
        "permission", "unknown_effect", "human_question", "safety_review", "legacy_safety_review"})
    if error in BUSINESS_ERRORS or facts.get("contract_effect") in {"acceptance", "scope", "evidence", "role_separation"}:
        kind, source, hard = "business_ambiguity", "deterministic", True
        evidence.append("error_kind:" + error if error else "host_fact:contract_effect")
    elif error == "state_divergence" or facts.get("file_fingerprint_mismatch"):
        kind, source = "state_divergence", "deterministic"
        evidence.append("error_kind:state_divergence" if error == "state_divergence" else "host_fact:file_fingerprint_mismatch")
    elif error in PROTOCOL_ERRORS:
        kind, source = "protocol_conflict", "deterministic"
        evidence.append("error_kind:" + error)
    elif error in REVIEW_ERRORS:
        kind, source = "review_infrastructure", "deterministic"
        evidence.append("error_kind:" + error)
    elif error in MODEL_ERRORS:
        kind, source = "model_service_fault", "deterministic"
        evidence.append("error_kind:" + error)
    elif error in CONTEXT_ERRORS:
        kind, source = "context_window_limit", "deterministic"
        evidence.append("error_kind:" + error)
    elif error == "output_window_limit":
        kind, source = "output_window_limit", "deterministic"
        evidence.append("error_kind:" + error)
    elif error in REASONING_ERRORS:
        kind, source = "reasoning_limit", "deterministic"
        evidence.append("error_kind:" + error)
    elif ((health.get('submission_feedback') or {}).get('status') == 'rejected'
          and (health.get('submission_feedback') or {}).get('category') in {'contract_error', 'reference_mismatch'}):
        kind, source = 'protocol_conflict', 'deterministic'
        evidence.append('tool_receipt:' + str(health['submission_feedback'].get('tool_id') or ''))
    elif (error == "environment_fault" or
          (health.get("sampling") != "partial" and
           (health.get("path_error_count", 0) > 0 or health.get("tool_misuse_count", 0) > 0))):
        kind, source = "environment_fault", "deterministic"
        evidence.append("error_kind:environment_fault" if error == "environment_fault" else "metric:typed_tool_errors")
    elif error in CAPACITY_ERRORS or health.get("terminal_status") == "budget_exhausted":
        delta = health.get("work_delta") or {}
        if delta.get("known") and any((delta.get(key) or 0) > 0 for key in (
                "review_complete", "review_recorded", "write_receipts", "submitted_items", "submitted_revisions",
                "observed_sources", "inline_outputs")):
            kind, source = "capacity_shortfall", "deterministic"
            evidence.append("metric:durable_progress")
        elif delta.get("known") and not hard and health.get("sampling") == "tool_complete" and (
                                                   health.get("repeated_reads", 0) >= 3 or
                                                   (health.get("terminal_calls", 0) >= 3 and
                                                    health.get("effective_call_ratio") is not None and
                                                    health["effective_call_ratio"] <= .2)):
            kind, source = "idle_stall", "deterministic"
            evidence.append("metric:repeated_reads_or_low_effective_ratio")
        else:
            evidence.append("error_kind:" + error if error else "status:budget_exhausted")
    else:
        evidence.append("error_kind:" + error if error else "metric:insufficient_evidence")
    if hard:
        evidence.extend("blocker:" + str(value) for value in blocked)
        if facts.get("hard_boundary"):
            evidence.append("host_fact:hard_boundary")
    from mewcode.experts.blocking_policy import policy_for
    recommended = policy_for(kind)["automatic_action"]
    if hard:
        recommended = "human_decision"
    correction = health.get('submission_feedback') or {}
    return {"schema_version": 1, "policy_version": POLICY_VERSION,
            "primary_kind": kind, "secondary_signals": [], "evidence_refs": evidence,
            "confidence_source": source, "contract_effect": "execution_only" if not hard else "human_boundary",
            "hard_boundary": hard, "recommended_action": recommended,
            "submission_correction": {key: correction.get(key) for key in ('tool', 'tool_id', 'category', 'code', 'next_action')}
                if kind == 'protocol_conflict' and correction.get('status') == 'rejected' and not hard else None}
