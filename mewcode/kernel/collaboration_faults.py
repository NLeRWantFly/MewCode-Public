"""Host fault categories kept separate from a substantive acceptance rejection."""
MODEL_UNAVAILABLE = frozenset({
    "provider_error", "provider_interrupted", "provider_rate_limited", "provider_temporarily_unavailable",
    "model_stream_stalled", "acceptance_unavailable", "safety_review_unavailable", "provider_cooldown",
})
TRANSIENT = (MODEL_UNAVAILABLE - {"provider_cooldown"}) | frozenset({
    "environment_fault", "acceptance_format", "capability_missing", "readonly_effect_unknown", "not_started",
})
REQUIRES_CLEARANCE = frozenset({
    "permission_denied", "permission_required", "permission", "effect_unknown", "unknown_effect",
    "missing_authorization", "safety_review", "safety_review_inconclusive",
    "human_question", "user_input", "awaiting_input", "branch_wait", "human_gate_waiting", "run_deadline", "user_paused",
    "provider_cooldown",
})
# Model availability is an execution prerequisite even for an advisory review.
# A quality waiver cannot restore the selected model or replace host clearance.
ACCEPTANCE_BLOCKERS = REQUIRES_CLEARANCE | MODEL_UNAVAILABLE
ASSISTANCE = frozenset({'assistance_requested'})
SEGMENT_LIMITS = frozenset({'soft_deadline', 'max_iterations'})


def scheduled_yield(result):
    """Only an explicit host slice boundary belongs to the owning scheduler."""
    handoff = result.handoff or {}
    return (result.status == 'budget_exhausted' and result.error_kind in SEGMENT_LIMITS
            and handoff.get('blocker_kind') not in TRANSIENT | REQUIRES_CLEARANCE | ASSISTANCE
            and not any(handoff.get(key) for key in ('blockers', 'question', 'file_requests', 'command_requests')))


def execution_fault(result):
    if scheduled_yield(result):
        return ''
    handoff_kind = (result.handoff or {}).get('blocker_kind', '')
    if handoff_kind in REQUIRES_CLEARANCE:
        return handoff_kind
    kind = result.error_kind or (result.handoff or {}).get("blocker_kind", "")
    if kind in TRANSIENT | REQUIRES_CLEARANCE | ASSISTANCE:
        return kind
    if result.status in {"budget_exhausted", "review_pending", "paused"}:
        return "run_deadline" if result.status == "budget_exhausted" else "user_input"
    return ""


def host_blocker(kind):
    if kind.startswith("provider_") or kind in {"model_stream_stalled", "acceptance_format", "acceptance_unavailable",
                                             "safety_review_unavailable"}:
        return "provider_error"
    from mewcode.kernel.blockers import canonical_blocker
    try:
        return str(canonical_blocker(kind))
    except KeyError:
        return "environment_fault"


def continuation_fault(blockers, original=""):
    """Keep typed host blockers intact when a loop projects work progress."""
    from mewcode.kernel.blockers import BLOCKER_CLASS, canonical_blocker
    kinds = set()
    for item in blockers:
        try:
            kinds.add(str(canonical_blocker(item.get("kind", ""))))
        except KeyError:
            continue
    # A technical fault must not hide a real authority or user-input gate.
    for kind in ("unknown_effect", "permission", "missing_authorization",
                 "safety_review", "policy_violation", "run_deadline", "user_input"):
        if kind in kinds:
            return {"unknown_effect": "effect_unknown", "permission": "permission_denied",
                    "user_input": "awaiting_input"}.get(kind, kind)
    business = sorted(kind for kind in kinds if BLOCKER_CLASS.get(kind) == "business")
    if business:
        return business[0]
    if original and host_blocker(original) in kinds:
        return original
    return next(iter(sorted(kinds - {""})), original or "node_failure")


def command_fault(observation):
    """Interpret host receipts, never error wording supplied by a command."""
    receipt = observation.get("command_execution") or {}
    condition = receipt.get("execution_condition") or observation.get("execution_condition") or {}
    if condition.get("kind") == "environment_unavailable" or receipt.get("status") == "spawn_failed":
        return "environment_fault"
    if receipt.get("status") in {"unknown", "timed_out", "outcome_unknown"}:
        return "effect_unknown"
    if receipt.get("status") == "denied" or condition.get("kind") == "permission_denied":
        return "permission_denied"
    if observation.get("safety_review") or observation.get("review_pending"):
        return "safety_review"
    return ""


def report_fault(report):
    """A required host clearance dominates any transient check failure."""
    faults = [report.get("execution_error", "")]
    faults.extend((check.get("evidence") or {}).get("execution_error", "") for check in report.get("checks", []))
    return next((kind for kind in faults if kind in REQUIRES_CLEARANCE),
                next((kind for kind in faults if kind in ACCEPTANCE_BLOCKERS),
                     next((kind for kind in faults if kind), "")))
