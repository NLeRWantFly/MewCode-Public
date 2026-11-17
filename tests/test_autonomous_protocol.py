"""A10: collaboration design v1 must initialize as a v2 runtime ledger."""
from __future__ import annotations

import copy
import json

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.events import digest
from mewcode.orchestration.collab_compile import compile_plan
from mewcode.experts.flow import ExpertFlowSpec

try:
    from tests.test_collaboration_mode import core, flow, accept
except ImportError:  # tests imported as a top-level module
    from test_collaboration_mode import core, flow, accept

RUN_ID = "a" * 32


def plan_v2_rolling():
    plan = copy.deepcopy(compile_plan(flow()))
    plan["format"] = ledger.RUNTIME_FORMAT
    plan["planning_mode"] = "rolling"
    return plan


def test_initialize_builds_v2_runtime_ledger():
    value = core()
    assert value["format"] == ledger.RUNTIME_FORMAT
    assert {"lineage_budget", "context_projection"} <= set(value["required_capabilities"])
    assert [row["kind"] for row in value["timeline"]].count("protocol_upgraded") == 1

    rolling = ledger.initialize({"run_id": RUN_ID}, plan_v2_rolling())
    assert "rolling_plan" in rolling["required_capabilities"]
    assert [row["kind"] for row in rolling["timeline"]] == ["initialized"]


def test_reinitialize_upgrades_v1_ledger_keeping_history_and_stays_idempotent():
    plan = compile_plan(flow())
    record = {"run_id": RUN_ID}
    first = ledger.initialize(record, plan)
    attempt, _ = accept(first)
    legacy = copy.deepcopy(first)
    legacy["format"] = ledger.FORMAT
    legacy.pop("lineage_budget")
    legacy.pop("required_capabilities")
    legacy["timeline"] = [{"sequence": 1, "kind": "initialized"}]
    old_certificate = copy.deepcopy(legacy["certificates"])
    record["collaboration"] = legacy

    upgraded = ledger.initialize(record, plan)
    assert upgraded["format"] == ledger.RUNTIME_FORMAT
    assert upgraded["attempts"][attempt["id"]]["status"] == "accepted"
    assert upgraded["certificates"] == old_certificate
    assert ledger.valid_certificate(upgraded, "work")
    assert upgraded["source_snapshot"] == plan["source_snapshot"]
    assert upgraded["lineage_budget"]["root"]["used"]["attempts"] == 1
    assert upgraded["initial_plan_hash"] == digest(plan)
    assert [row["kind"] for row in upgraded["timeline"]].count("protocol_upgraded") == 1

    again = ledger.initialize(record, plan)
    assert [row["kind"] for row in again["timeline"]].count("protocol_upgraded") == 1
    assert again["attempts"][attempt["id"]]["status"] == "accepted"
    assert again["lineage_budget"] == upgraded["lineage_budget"]


def test_reinitialize_preserves_capabilities_added_by_published_runtime_plan():
    plan = compile_plan(flow())
    record = {"run_id": RUN_ID}
    current = ledger.initialize(record, plan)
    current["required_capabilities"].append("rolling_plan")
    ledger.initialize(record, plan)
    assert "rolling_plan" in ledger.state(record)["required_capabilities"]


@pytest.mark.parametrize("capabilities", [None, "lineage_budget", ["lineage_budget", 1]])
def test_malformed_runtime_capability_declaration_is_rejected(capabilities):
    value = core()
    value["required_capabilities"] = capabilities
    with pytest.raises(ValueError):
        ledger.state({"collaboration": value})


def test_initialize_rejects_unknown_format_and_bad_capabilities():
    record = {"run_id": RUN_ID}
    with pytest.raises(ValueError):
        ledger.initialize(record, {**compile_plan(flow()), "format": "mewcode.collaboration.v9"})
    with pytest.raises(ValueError):
        ledger.initialize(record, {**compile_plan(flow()),
                                   "required_capabilities": ["lineage_budget", "telepathy"]})
    with pytest.raises(ValueError):
        ledger.initialize(record, {**compile_plan(flow()),
                                   "required_capabilities": ["lineage_budget", "lineage_budget"]})
    assert record.get("collaboration") is None


def test_initialize_rejects_conflicting_plan_without_touching_record():
    plan = compile_plan(flow())
    record = {"run_id": RUN_ID}
    original = ledger.initialize(record, plan)
    snapshot = copy.deepcopy(original)
    other = copy.deepcopy(plan)
    other["task_id"] = "task-two"
    with pytest.raises(ValueError, match="另一版协作规格"):
        ledger.initialize(record, other)
    assert record["collaboration"] == snapshot
    assert record["collaboration"] is original


def provenance_flow():
    raw = flow().as_dict()
    source = raw["meta"]["collaboration_spec"]
    source["sources"] = [{"id": "user_1", "role": "user"}]
    source["field_sources"] = {"goal": {
        field: [{"source_id": "user_1", "quote": "交付测试结果"}]
        for field in ("title", "state", "body")}}
    return ExpertFlowSpec.from_value(raw)


def test_compilation_identity_survives_canonical_json_reload():
    spec = provenance_flow()
    before = compile_plan(spec)
    after = compile_plan(ExpertFlowSpec.from_value(json.loads(json.dumps(spec.as_dict(), sort_keys=True))))
    assert before["entries"]["goal"]["adopted_fields"] == ["body", "state", "title"]
    assert before == after
    record = {"run_id": RUN_ID}
    ledger.initialize(record, before)
    reloaded = json.loads(json.dumps(record, sort_keys=True))
    snapshot = copy.deepcopy(reloaded)
    ledger.initialize(reloaded, after)
    assert reloaded == snapshot


def legacy_order_record():
    canonical = compile_plan(provenance_flow())
    old = copy.deepcopy(canonical)
    old["entries"]["goal"]["adopted_fields"] = ["title", "state", "body"]
    record = {"run_id": RUN_ID}
    ledger.initialize(record, old)
    return record, canonical


def test_legacy_order_recovery_preserves_runtime_and_initial_hash():
    record, plan = legacy_order_record()
    current = record["collaboration"]
    accept(current)
    before = copy.deepcopy(current)
    result = ledger.initialize(record, plan)
    assert result["canonical_initial_plan_hash"] == digest(plan)
    assert result["initial_plan_hash"] == before["initial_plan_hash"]
    for key in ("entries", "contracts", "source_snapshot", "attempts", "certificates", "lineage_budget"):
        assert result[key] == before[key]
    assert ledger.valid_certificate(result, "work")
    assert result["timeline"][:-1] == before["timeline"]
    assert result["timeline"][-1]["kind"] == "initial_plan_order_normalized"
    # An adopted runtime edit must neither block resuming the same initial plan
    # nor be overwritten by that plan after the proven normalization.
    result["entries"]["goal"]["body"] = "新的运行期目标"
    result["entries"]["goal"]["adopted_fields"] = ["body"]
    expected = copy.deepcopy(record)
    ledger.initialize(record, plan)
    assert record == expected


@pytest.mark.parametrize("change", ["body", "source", "fields", "contract", "policy"])
def test_legacy_order_does_not_accept_real_plan_change(change):
    record, plan = legacy_order_record()
    before = copy.deepcopy(record)
    if change == "body":
        plan["entries"]["goal"]["body"] += "不同"
    elif change == "source":
        plan["source_snapshot"]["field_sources"]["goal"]["body"][0]["quote"] += "不同"
    elif change == "fields":
        plan["entries"]["goal"]["adopted_fields"].append("data.extra")
    elif change == "contract":
        plan["contracts"]["work"]["forbidden_changes"] = []
    else:
        plan["policy"]["max_attempts"] += 1
    with pytest.raises(ValueError, match="另一版协作规格"):
        ledger.initialize(record, plan)
    assert record == before
