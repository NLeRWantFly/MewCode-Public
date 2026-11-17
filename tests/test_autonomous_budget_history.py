"""History migration for adopted revisions and structured exhaustion errors.

These tests build plain ledger dictionaries exactly like
``tests/test_autonomous_budget.py`` and never rely on other module APIs.
"""
from __future__ import annotations

import json

import pytest

from mewcode.kernel import lineage_budget


def core(attempts=None, contracts=None, proposals=None, limits=None):
    value = {"run_id": "r" * 32, "policy": {}, "attempts": attempts or {},
             "contracts": contracts or {}, "proposals": proposals or {}}
    if limits is not None:
        value["policy"]["lineage_budget_limits"] = limits
    return value


def adopted(**extra):
    row = {"status": "adopted"}
    row.update(extra)
    return row


def snapshot(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


# --------------------------------------------------------------------------- adopted revisions


def test_adopted_proposals_seed_one_revision_per_unique_lineage():
    proposals = {
        "p1": adopted(affected_lineages=["lin-a", "lin-b"]),
        "p2": adopted(affected_lineages=["lin-a"]),
        "p3": {"status": "rejected", "affected_lineages": ["lin-b", "ghost"]},
        "p4": {"status": "proposed", "affected_lineages": ["lin-b"]},
    }
    value = core(proposals=proposals)
    budget = lineage_budget.ensure(value)
    assert budget["root"]["used"]["revisions"] == 3
    assert budget["root"]["reserved"]["revisions"] == 0
    assert budget["lineages"]["lin-a"]["used"]["revisions"] == 2
    assert budget["lineages"]["lin-b"]["used"]["revisions"] == 1
    assert "ghost" not in budget["lineages"]
    assert "revision:p3:lin-b" not in budget["reservations"]
    assert "revision:p4:lin-b" not in budget["reservations"]
    seeded = budget["reservations"]["revision:p1:lin-a"]
    assert seeded["kind"] == "revisions" and seeded["amount"] == 1
    assert seeded["status"] == "settled" and seeded["actual"] == 1
    assert seeded["migrated"] is True and seeded["lineage_id"] == "lin-a"


def test_history_counts_attempts_and_adopted_revisions_together():
    attempts = {"a1": {"id": "a1", "contract_id": "work", "status": "accepted"}}
    value = core(attempts=attempts, contracts={"work": {"lineage_id": "lin"}},
                 proposals={"p1": adopted(affected_lineages=["lin"])})
    budget = lineage_budget.ensure(value)
    assert budget["root"]["used"] == {"attempts": 1, "revisions": 1, "expansions": 0}
    assert budget["lineages"]["lin"]["used"]["attempts"] == 1
    assert budget["lineages"]["lin"]["used"]["revisions"] == 1
    restored = json.loads(json.dumps(value))
    assert lineage_budget.ensure(restored) == budget


def test_empty_affected_lineages_marks_no_history_without_fallback():
    value = core(contracts={"c": {"requirement_ids": ["r1"]}},
                 proposals={"p": adopted(affected_lineages=[],
                                         payload={"changes": {"r1": {}}})})
    budget = lineage_budget.ensure(value)
    assert budget["root"]["used"]["revisions"] == 0
    assert budget["lineages"] == {}


def test_explicit_affected_lineages_win_and_history_is_never_reseeded():
    proposals = {"p1": adopted(affected_lineages=["chosen"],
                               payload={"changes": {"req-1": True}})}
    contracts = {"work": {"lineage_id": "other", "requirement_ids": ["req-1"]}}
    value = core(contracts=contracts, proposals=proposals)
    budget = lineage_budget.ensure(value)
    assert budget["lineages"]["chosen"]["used"]["revisions"] == 1
    assert "other" not in budget["lineages"]

    # A renamed contract and a new limit policy must not reseed a committed budget.
    value["contracts"]["work"] = {"lineage_id": "renamed", "requirement_ids": ["req-1"]}
    value["policy"]["lineage_budget_limits"] = {"root": {"revisions": 1}}
    assert lineage_budget.ensure(value) == budget
    assert lineage_budget.ensure(value)["limits"]["root"]["revisions"] == 120


# --------------------------------------------------------------------------- payload fallback


def test_changes_fallback_maps_to_contract_lineage_or_key():
    contracts = {
        "c-req": {"lineage_id": "lin-req", "requirement_ids": ["r1", "r2"]},
        "c-con": {"constraint_ids": ["k1"]},
        "c-node": {"node_id": "nodeK"},
    }
    proposals = {"p": adopted(payload={"changes": {"r2": {}, "k1": {}, "nodeK": {}, "unknown": {}}})}
    value = core(contracts=contracts, proposals=proposals)
    budget = lineage_budget.ensure(value)
    assert budget["lineages"]["lin-req"]["used"]["revisions"] == 1
    assert budget["lineages"]["c-con"]["used"]["revisions"] == 1
    assert budget["lineages"]["c-node"]["used"]["revisions"] == 1
    assert "unknown" not in budget["lineages"]
    assert budget["root"]["used"]["revisions"] == 3


def test_fallback_deduplicates_lineages_shared_by_several_contracts():
    contracts = {
        "c1": {"lineage_id": "shared", "requirement_ids": ["r1"]},
        "c2": {"lineage_id": "shared", "constraint_ids": ["r1"]},
    }
    value = core(contracts=contracts,
                 proposals={"p": adopted(payload={"changes": {"r1": {}}})})
    budget = lineage_budget.ensure(value)
    assert budget["lineages"]["shared"]["used"]["revisions"] == 1
    assert budget["root"]["used"]["revisions"] == 1
    assert budget["reservations"]["revision:p:shared"]["actual"] == 1


# --------------------------------------------------------------------------- over-limit history


def test_over_limit_history_migrates_fully_and_blocks_only_new_reserve():
    proposals = {"p1": adopted(affected_lineages=["lin"]),
                 "p2": adopted(affected_lineages=["lin"])}
    value = core(proposals=proposals, limits={"lineage": {"revisions": 1}})
    budget = lineage_budget.ensure(value)
    assert budget["lineages"]["lin"]["used"]["revisions"] == 2
    assert budget["root"]["used"]["revisions"] == 2

    before = snapshot(value)
    with pytest.raises(lineage_budget.BudgetExhausted, match="谱系") as excinfo:
        lineage_budget.reserve(value, "r", lineage_id="lin", kind="revisions")
    assert snapshot(value) == before
    assert lineage_budget.ensure(value) == budget
    assert excinfo.value.scope == "lineage" and excinfo.value.kind == "revisions"
    assert excinfo.value.lineage_id == "lin"
    assert excinfo.value.requested == 1 and excinfo.value.available == 0


# --------------------------------------------------------------------------- invalid history


INVALID_HISTORY = [
    ("duplicate-lineages", {"p": adopted(affected_lineages=["a", "a"])}, {}),
    ("affected-not-list", {"p": adopted(affected_lineages="a")}, {}),
    ("empty-lineage-name", {"p": adopted(affected_lineages=[""])}, {}),
    ("missing-affected-and-payload", {"p": adopted()}, {}),
    ("changes-not-dict", {"p": adopted(payload={"changes": ["x"]})}, {}),
    ("proposal-not-dict", {"p": "adopted"}, {}),
    ("non-text-proposal-id", {1: adopted(affected_lineages=["a"])}, {}),
    ("contract-ids-not-list",
     {"p": adopted(payload={"changes": {"r1": {}}})},
     {"c": {"requirement_ids": "r1"}}),
    ("contract-not-dict", {"p": adopted(payload={"changes": {"r1": {}}})}, {"c": ["nope"]}),
    ("contract-id-not-text", {"p": adopted(payload={"changes": {"r1": {}}})}, {1: {}}),
    ("node-id-not-text", {"p": adopted(payload={"changes": {"r1": {}}})}, {"c": {"node_id": ["bad"]}}),
]


@pytest.mark.parametrize("proposals,contracts", [case[1:] for case in INVALID_HISTORY],
                         ids=[case[0] for case in INVALID_HISTORY])
def test_invalid_proposal_history_never_writes_a_budget(proposals, contracts):
    value = core(contracts=contracts, proposals=proposals)
    before = snapshot(value)
    with pytest.raises(ValueError):
        lineage_budget.ensure(value)
    assert "lineage_budget" not in value
    assert snapshot(value) == before


# --------------------------------------------------------------------------- structured errors


def test_budget_exhausted_exposes_scope_and_context_without_partial_commit():
    value = core(limits={"root": {"attempts": 3}, "lineage": {"attempts": 2}})
    lineage_budget.reserve(value, "r1", lineage_id="lin", kind="attempts")
    lineage_budget.reserve(value, "r2", lineage_id="lin", kind="attempts")
    before = snapshot(value)
    with pytest.raises(lineage_budget.BudgetExhausted, match="谱系") as excinfo:
        lineage_budget.reserve(value, "r3", lineage_id="lin", kind="attempts")
    error = excinfo.value
    assert error.scope == "lineage" and error.kind == "attempts"
    assert error.lineage_id == "lin"
    assert error.requested == 1 and error.available == 0
    assert snapshot(value) == before

    lineage_budget.reserve(value, "r4", lineage_id="other", kind="attempts")
    before = snapshot(value)
    with pytest.raises(lineage_budget.BudgetExhausted, match="根") as excinfo:
        lineage_budget.reserve(value, "r5", lineage_id="other-two", kind="attempts")
    error = excinfo.value
    assert error.scope == "root" and error.kind == "attempts"
    assert error.lineage_id == "other-two"
    assert error.requested == 1 and error.available == 0
    assert snapshot(value) == before
    assert "other-two" not in lineage_budget.ensure(value)["lineages"]


def test_budget_exhausted_reports_remaining_allowance_for_larger_request():
    value = core(limits={"root": {"attempts": 2}})
    before = snapshot(value)
    with pytest.raises(lineage_budget.BudgetExhausted, match="根") as excinfo:
        lineage_budget.reserve(value, "r", lineage_id="lin", kind="attempts", amount=5)
    assert excinfo.value.requested == 5 and excinfo.value.available == 2
    assert snapshot(value) == before


def test_budget_exhausted_defaults_and_value_error_compatibility():
    error = lineage_budget.BudgetExhausted("boom")
    assert isinstance(error, ValueError)
    assert (error.kind, error.scope, error.lineage_id,
            error.requested, error.available) == (None, None, None, None, None)
    assert str(error) == "boom"
