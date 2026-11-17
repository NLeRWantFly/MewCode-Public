"""User-appended lineage budget gates and the observed read set on manual accept."""
from __future__ import annotations

import copy

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.kernel import collaboration_recovery as recovery
from mewcode.kernel import lineage_budget
from mewcode.orchestration.collab_compile import compile_plan

from tests.test_collaboration_mode import RUN_ID, core, flow


def record_of(value):
    return {"run_id": RUN_ID, "collaboration": value, "kernel_calls": {}}


def budget(kind="attempts", scope="root", lineage_id="root-main", requested=1, available=0):
    return {"kind": kind, "scope": scope, "lineage_id": lineage_id, "requested": requested, "available": available}


def open_budget_gate(record, **overrides):
    args = {"scope": "contract:work", "keys": ["work"], "label": "预算耗尽",
            "reason": "root attempts 预算已经提交完", "kind": "lineage_budget_exhausted", "budget": budget()}
    args.update(overrides)
    return recovery.open_gate(record, **args)


def add_budget(record, gate, request_id, amount=1, **overrides):
    args = {"gate_id": gate["id"], "expected_version": record["collaboration"]["spec_version"],
            "request_id": request_id, "action": "add_budget", "reason": "用户追加预算后继续", "actor": "user",
            "budget_amount": amount}
    args.update(overrides)
    return recovery.resolve_gate(record, **args)


def _limit(book, scope, kind):
    return book["limits"][scope][kind] + sum(row["amount"] for row in book["grants"].values()
        if row["kind"] == kind and row["lineage_id"] is None)


def _usage(book, kind, lineage_id=None):
    node = book.get("root", {}) if lineage_id is None else (book.get("lineages", {}).get(lineage_id) or {})
    return (node.get("used", {}).get(kind, 0), node.get("reserved", {}).get(kind, 0))


def test_user_add_budget_raises_root_limit_without_changing_usage():
    plan = compile_plan(flow())
    plan["policy"]["lineage_budget_limits"] = {"root": {"attempts": 1}, "lineage": {"attempts": 10}}
    value = ledger.initialize({"run_id": RUN_ID}, plan)
    lineage_budget.reserve(value, "spent", kind="attempts", lineage_id="work")
    lineage_budget.settle(value, "spent")
    with pytest.raises(lineage_budget.BudgetExhausted):
        lineage_budget.reserve(value, "next", kind="attempts", lineage_id="work")
    record = record_of(value)
    before = lineage_budget.ensure(copy.deepcopy(value))
    gate = open_budget_gate(record)
    assert gate["kind"] == "lineage_budget_exhausted" and gate["budget"] == budget()

    decision = add_budget(record, gate, "req-budget-1", amount=3)
    value = record["collaboration"]

    after = lineage_budget.ensure(copy.deepcopy(value))
    assert _limit(after, "root", "attempts") == _limit(before, "root", "attempts") + 3
    assert _usage(after, "attempts") == _usage(before, "attempts")
    assert decision["budget_grant"] == {"decision_id": decision["id"], "kind": "attempts", "amount": 3, "lineage_id": None}
    assert record["status"] == "paused"
    assert value["recovery_gates"][gate["id"]]["status"] == "resolved"
    lineage_budget.reserve(value, "next", kind="attempts", lineage_id="work")


def test_add_budget_is_idempotent_and_same_identity_cannot_change_amount():
    value = core()
    record = record_of(value)
    gate = open_budget_gate(record)
    first = add_budget(record, gate, "req-same", amount=2)
    again = add_budget(record, gate, "req-same", amount=2)
    assert again == first
    value = record["collaboration"]
    grants = lineage_budget.ensure(copy.deepcopy(value))["grants"]
    assert [row for row in grants.values() if row["lineage_id"] is None and row["kind"] == "attempts"] == [first["budget_grant"]]

    snapshot = copy.deepcopy(record)
    with pytest.raises(ValueError, match="不能改写"):
        add_budget(record, gate, "req-same", amount=5)
    assert record == snapshot


def test_add_budget_rejects_bad_requests_without_mutation():
    value = core()
    record = record_of(value)
    gate = open_budget_gate(record)
    snapshot = copy.deepcopy(record)
    with pytest.raises(ValueError):
        add_budget(record, gate, "req-actor", amount=1, actor="model")
    with pytest.raises(ValueError):
        add_budget(record, gate, "req-neg", amount=-1)
    with pytest.raises(ValueError):
        add_budget(record, gate, "req-zero", amount=0)
    with pytest.raises(ValueError):
        add_budget(record, gate, "req-bool", amount=True)
    assert record == snapshot

    other = recovery.open_gate(record, scope="contract:deliver", keys=["deliver"], label="集成失败",
                               reason="集成交付需要返工", kind="integration_fault")
    snapshot = copy.deepcopy(record)
    with pytest.raises(ValueError, match="不支持"):
        add_budget(record, other, "req-wrong-kind", amount=1)
    assert record == snapshot


def test_add_budget_cannot_close_unknown_receipts():
    value = core()
    record = record_of(value)
    gate = open_budget_gate(record)
    record["kernel_calls"] = {"call-1": {"state": "unknown", "node_id": "work"}}
    snapshot = copy.deepcopy(record)
    with pytest.raises(ValueError, match="结果不明"):
        add_budget(record, gate, "req-unknown", amount=1)
    assert record == snapshot


def test_add_budget_gate_is_stale_after_external_scope_grant():
    value = core()
    record = record_of(value)
    gate = open_budget_gate(record)
    lineage_budget.grant(value, "external-grant", kind="attempts", amount=5, lineage_id=None)
    snapshot = copy.deepcopy(record)
    with pytest.raises(ValueError, match="变化"):
        add_budget(record, gate, "req-stale", amount=1)
    assert record == snapshot


def test_open_gate_rejects_budget_metadata_for_other_kinds():
    value = core()
    record = record_of(value)
    with pytest.raises(ValueError, match="仅预算耗尽"):
        recovery.open_gate(record, scope="contract:work", keys=["work"], label="x", reason="y",
                           kind="integration_fault", budget=budget())
    for broken in (None, {"kind": "attempts"}, budget(kind="other"), budget(scope="branch"),
                   budget(lineage_id=""), budget(requested=0), budget(requested=True), budget(available=-1)):
        with pytest.raises(ValueError):
            recovery.open_gate(record, scope="contract:work", keys=["work"], label="x", reason="y",
                               kind="lineage_budget_exhausted", budget=broken)


def test_plain_retry_does_not_grant_budget():
    value = core()
    record = record_of(value)
    before = lineage_budget.ensure(copy.deepcopy(value))
    gate = recovery.open_gate(record, scope="contract:work", keys=["work"], label="验收失败",
                              reason="自动验收未通过需要用户决定", kind="acceptance_failed")
    recovery.resolve_gate(record, gate_id=gate["id"], expected_version=value["spec_version"], request_id="req-retry",
                          action="retry", reason="用户要求重试", actor="user")
    value = record["collaboration"]
    after = lineage_budget.ensure(copy.deepcopy(value))
    assert after.get("grants") == before.get("grants")
    assert _limit(after, "root", "attempts") == _limit(before, "root", "attempts")
    assert value["retry_generations"]["work"] == 1


def test_manual_accept_certificate_keeps_host_read_set():
    value = core()
    record = record_of(value)
    attempt = ledger.dispatch(value, "work")
    checks = [{"id": check["id"], "version": check["version"], "passed": False,
               "evidence": {"host": "observed"}} for check in value["contracts"]["work"]["checks"]]
    report = {"passed": False, "checks": checks, "artifacts": [{"name": "out", "path": "out.txt"}],
              "read_set": {"files": ["claimed-by-delivery.txt"]}}
    ledger.complete(value, attempt["id"], report)
    observed = {"files": ["evidence.txt"]}
    attempt = value["attempts"][attempt["id"]]
    attempt["read_set"] = copy.deepcopy(observed)
    attempt["delivery"] = {"status": "completed", "read_set": {"files": ["claimed-by-delivery.txt"]}}
    if attempt["status"] == "checking":
        attempt["acceptance_fault"] = {**report, "artifacts": list(report["artifacts"])}
    else:
        attempt["report"] = {**report, "artifacts": list(report["artifacts"])}

    gate = recovery.open_gate(record, scope="contract:work", keys=["work"], label="人工验收",
                              reason="自动验收未通过，用户人工验收", kind="acceptance_failed")
    recovery.resolve_gate(record, gate_id=gate["id"], expected_version=value["spec_version"], request_id="req-accept",
                          action="accept", reason="用户人工验收固定交付", actor="user")

    value = record["collaboration"]
    certificate = value["certificates"]["work"]
    assert certificate["read_set"] == observed
    assert "claimed-by-delivery" not in str(certificate["read_set"])


def test_nested_effect_receipts_are_scoped_to_the_whole_child_subtree():
    child = "work::mx-one::first"
    record = {"kernel_calls": {
        "nested": {"node_id": child + "::mx-two::leaf", "state": "unknown"},
        "review": {"node_id": child + "__acceptance_123", "state": "dispatched"},
        "sibling": {"node_id": "work::mx-one::second", "state": "unknown"},
    }}
    assert recovery.unresolved_calls(record, [child]) == [record["kernel_calls"]["nested"], record["kernel_calls"]["review"]]
