from mewcode.experts.blocking_policy import (
    BLOCKER_CLEARANCE, DIAGNOSTIC_POLICY, clearance_for_blocker, policy_for,
)
from mewcode.experts.recovery_diagnosis import KINDS, diagnose_segment
from mewcode.experts.recovery_state import HARD_KINDS, blockers


def test_every_blocking_kind_has_a_clearance_condition():
    assert set(DIAGNOSTIC_POLICY) == set(KINDS)
    assert HARD_KINDS <= set(BLOCKER_CLEARANCE)
    assert all(all(policy_for(kind).values()) for kind in KINDS)
    stage = {"tools": {"write": {"name": "WriteFile", "status": "outcome_unknown"}}}
    assert blockers(stage)[0]["clearance"] == "independent_effect_receipt"
    assert blockers(stage)[0]["clearance_satisfied"] is False


def test_missing_clearance_is_not_silently_accepted():
    try:
        clearance_for_blocker("unregistered_blocker")
    except KeyError:
        pass
    else:
        raise AssertionError("unregistered blocker was silently allowed")


def test_review_context_limit_is_infrastructure_not_hard_boundary():
    assert "safety_review_context_limit" not in HARD_KINDS
    assert blockers({}, error_kind="safety_review_context_limit") == []
    diagnosis = diagnose_segment({}, error_kind="safety_review_context_limit")
    assert diagnosis["primary_kind"] == "review_infrastructure"
    assert diagnosis["hard_boundary"] is False
