from __future__ import annotations

import pytest

from mewcode.experts import (
    DECISION_APPROVE,
    DECISION_DEFER,
    DECISION_DENY,
    ESCALATION_LEVELS,
    OUTCOME_GRANTED,
    OUTCOME_REJECTED,
    EscalationChain,
    EscalationError,
)


def test_levels_are_the_fixed_chain() -> None:
    assert ESCALATION_LEVELS == ("subagent", "executor", "planner", "manager")


def test_approve_grants() -> None:
    chain = EscalationChain()
    chain.decide("executor", DECISION_APPROVE, by="executor-agent")
    assert chain.outcome == OUTCOME_GRANTED
    assert chain.is_terminated is True


def test_deny_blocks_even_after_defers() -> None:
    chain = EscalationChain()
    chain.decide("subagent", DECISION_DEFER)
    chain.decide("executor", DECISION_DEFER)
    chain.decide("planner", DECISION_DENY, reason="越界写")
    assert chain.outcome == OUTCOME_REJECTED
    assert chain.any_denied is True


def test_defer_keeps_chain_open_and_escalates() -> None:
    chain = EscalationChain()
    chain.decide("subagent", DECISION_DEFER)
    assert chain.outcome is None
    assert chain.is_terminated is False
    assert chain.next_level("subagent") == "executor"
    assert chain.next_level("executor") == "planner"
    assert chain.next_level("planner") == "manager"


def test_depth_cap_manager_has_no_next() -> None:
    chain = EscalationChain()
    assert chain.next_level("manager") is None


def test_manager_cannot_defer() -> None:
    chain = EscalationChain()
    with pytest.raises(EscalationError, match="最终裁决者"):
        chain.decide("manager", DECISION_DEFER)


def test_decision_after_termination_is_rejected() -> None:
    chain = EscalationChain()
    chain.decide("executor", DECISION_APPROVE)
    with pytest.raises(EscalationError, match="已终结"):
        chain.decide("planner", DECISION_DENY)


def test_unknown_level_is_rejected() -> None:
    chain = EscalationChain()
    with pytest.raises(EscalationError, match="未知层级"):
        chain.decide("intern", DECISION_APPROVE)


def test_invalid_outcome_is_rejected() -> None:
    chain = EscalationChain()
    with pytest.raises(EscalationError, match="非法决策"):
        chain.decide("executor", "maybe")


def test_every_decision_is_traceable() -> None:
    chain = EscalationChain()
    chain.decide("subagent", DECISION_DEFER, reason="不确定", by="sub-a")
    chain.decide("executor", DECISION_APPROVE, reason="低风险", by="exec-b")
    trace = chain.trace
    assert len(trace) == 2
    assert trace[0] == {
        "level": "subagent",
        "outcome": "defer",
        "reason": "不确定",
        "by": "sub-a",
    }
    assert trace[1] == {
        "level": "executor",
        "outcome": "approve",
        "reason": "低风险",
        "by": "exec-b",
    }


def test_custom_chain_depth_is_respected() -> None:
    chain = EscalationChain(levels=("worker", "lead"))
    chain.decide("worker", DECISION_DEFER)
    assert chain.next_level("worker") == "lead"
    assert chain.next_level("lead") is None
    with pytest.raises(EscalationError, match="最终裁决者"):
        chain.decide("lead", DECISION_DEFER)
