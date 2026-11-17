from __future__ import annotations

from mewcode.experts.recovery_diagnosis import diagnose_segment
from mewcode.experts.recovery_escalation import recovery_upward_report


def report(error: str, *, progress: int = 0, remaining_tokens: int | None = 20_000,
           blockers=None, window=None):
    health = {"sampling": "tool_complete", "terminal_status": "budget_exhausted",
              "work_delta": {"known": True, "review_complete": progress},
              "terminal_calls": 0, "repeated_reads": 0, "timing": {"coverage": "partial"}}
    diagnosis = diagnose_segment(health, error_kind=error, blockers=blockers)
    return recovery_upward_report(error_kind=error, diagnosis=diagnosis,
        health=health, remaining_tokens=remaining_tokens, remaining_seconds=100,
        active_blockers=blockers, token_window_receipt=window)


def test_context_and_reasoning_are_not_conflated_with_token_total():
    assert report("context_limit")["action"] == "lossless_split_or_request_larger_declared_window"
    deeper = report("semantic_uncertain")
    assert deeper["kind"] == "reasoning_capacity"
    assert deeper["automatic_route_change"] is False
    assert deeper["automatic_total_increase"] is False


def test_real_progress_on_segment_timeout_can_adjust_soft_window():
    result = report("segment_timeout", progress=1)
    assert result["kind"] == "execution_window"
    assert result["action"] == "increase_next_slice_within_parent_limit"
    assert result["requires_pause"] is False


def test_no_proof_on_timeout_requests_diagnosis_not_blind_downgrade():
    result = report("segment_timeout")
    assert result["kind"] == "needs_capacity_diagnosis"
    assert result["timing_coverage"] == "partial"


def test_token_ceiling_forecast_is_advisory_and_unknown_effect_wins():
    window = {"action": "request_total_cap", "spent_tokens": 80_000,
              "forecast_remaining_tokens": 25_000}
    cap = report("budget_exhausted", remaining_tokens=0, window=window)
    assert cap["kind"] == "total_token_ceiling"
    assert cap["suggested_total_tokens"] == 105_000
    hard = report("budget_exhausted", remaining_tokens=0, window=window,
                  blockers=[{"kind": "unknown_effect", "ref": "call"}])
    assert hard["kind"] == "hard_boundary"


def test_no_progress_window_replans_before_buying_more_tokens():
    result = report("token_window_no_progress", window={"action": "replan"})
    assert result["kind"] == "ineffective_execution"
    assert result["action"] == "replan_method_without_more_tokens"
