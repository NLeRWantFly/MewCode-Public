"""Regression coverage for the T3 blocked-node fallback projection."""
from __future__ import annotations

import pytest

from mewcode.experts.flow import ExpertFailurePolicy, ExpertFlowError, ExpertFlowSpec
from mewcode.orchestration.convert import from_expert_flow, to_expert_flow
from mewcode.orchestration.spec import OrchestrationSpec


def _spec():
    return OrchestrationSpec.from_value({
        "spec_id": "fallback-policy",
        "label": "Fallback policy",
        "finalizer_node_id": "reduce",
        "nodes": [
            {"node_id": "work", "kind": "implement", "on_blocked": "fallback",
             "fallback_node_ids": ["alternate-a", "alternate-b"]},
            {"node_id": "alternate-a", "kind": "implement"},
            {"node_id": "alternate-b", "kind": "implement"},
            {"node_id": "default-work", "kind": "implement"},
            {"node_id": "plan", "kind": "plan"},
            {"node_id": "reduce", "kind": "reduce"},
            {"node_id": "gate", "kind": "gate"},
        ],
        "edges": [],
    })


def test_declared_fallback_compiles_ordered_targets_and_roundtrips():
    flow = to_expert_flow(_spec())

    policy = flow.node_map["work"].failure_policy
    assert policy.action == "fallback"
    assert policy.target_node_id == "alternate-a"
    assert policy.fallback_node_ids == ("alternate-a", "alternate-b")

    restored_spec = from_expert_flow(ExpertFlowSpec.from_value(flow.as_dict()))
    restored_node = restored_spec.node_map()["work"]
    assert restored_node.on_blocked == "fallback"
    assert restored_node.fallback_node_ids == ("alternate-a", "alternate-b")
    assert to_expert_flow(restored_spec).node_map["work"].failure_policy.fallback_node_ids == (
        "alternate-a", "alternate-b"
    )


def test_default_failure_policies_are_unchanged():
    flow = to_expert_flow(_spec())

    assert flow.node_map["default-work"].failure_policy.action == "retry"
    assert flow.node_map["default-work"].failure_policy.max_attempts == 3
    for node_id in ("plan", "reduce", "gate"):
        assert flow.node_map[node_id].failure_policy.action == "stop"
        assert flow.node_map[node_id].failure_policy.max_attempts == 1


def test_failure_policy_serialization_keeps_fallback_list_and_legacy_shape():
    policy = ExpertFailurePolicy.from_value({
        "action": "fallback",
        "target_node_id": "alternate-a",
        "fallback_node_ids": ["alternate-a", "alternate-b"],
    })
    assert policy.as_dict() == {
        "action": "fallback",
        "max_attempts": 1,
        "target_node_id": "alternate-a",
        "fallback_node_ids": ["alternate-a", "alternate-b"],
    }

    legacy = ExpertFailurePolicy.from_value({"action": "retry", "max_attempts": 3})
    assert legacy.as_dict() == {"action": "retry", "max_attempts": 3, "target_node_id": ""}


@pytest.mark.parametrize("policy", [
    {"action": "fallback", "fallback_node_ids": ["alternate"]},
    {"action": "fallback", "target_node_id": "alternate"},
])
def test_flow_validation_accepts_ordered_or_legacy_fallback_target(policy):
    flow = ExpertFlowSpec.from_value({
        "flow_id": "valid-fallback",
        "version": 1,
        "label": "valid-fallback",
        "nodes": [
            {"node_id": "work", "role": "executor", "label": "Work",
             "failure_policy": policy},
            {"node_id": "alternate", "role": "executor", "label": "Alternate"},
            {"node_id": "final", "role": "manager", "label": "Final"},
        ],
        "edges": [{"from": "work", "to": "final"}],
        "finalizer_node_id": "final",
    })
    assert flow.node_map["work"].failure_policy


@pytest.mark.parametrize("targets", [[], ["missing"], ["work"]])
def test_flow_validation_rejects_empty_unknown_or_self_fallback_targets(targets):
    with pytest.raises(ExpertFlowError, match=r"fall\s?back"):
        ExpertFlowSpec.from_value({
            "flow_id": "invalid-fallback",
            "version": 1,
            "label": "invalid-fallback",
            "nodes": [
                {"node_id": "work", "role": "executor", "label": "Work",
                 "failure_policy": {"action": "fallback", "fallback_node_ids": targets}},
                {"node_id": "alternate", "role": "executor", "label": "Alternate"},
                {"node_id": "final", "role": "manager", "label": "Final"},
            ],
            "edges": [{"from": "work", "to": "final"}],
            "finalizer_node_id": "final",
        })
