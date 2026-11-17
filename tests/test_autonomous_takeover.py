"""Coordinator recovery decisions on ``coordinator_unavailable`` gates."""
from __future__ import annotations

import copy

import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.collaboration_recovery import open_gate, resolve_gate, unresolved_calls
from mewcode.model_registry import ModelRef
from mewcode.orchestration.collab_compile import compile_plan

RUN_ID = "a" * 32
EXECUTOR = ModelRef("execution", "model-a", "")
REPLACEMENT = ModelRef("execution", "model-b", "")


def flow():
    meta = {"orchestration_mode": "collaborative", "node_plan_briefs": {
        "work": {"known_inputs": ["目标"], "outputs": ["候选"], "acceptance": ["候选符合原始目标"]},
        "deliver": {"known_inputs": ["候选"], "outputs": ["结果"], "acceptance": ["全量结果符合原始目标"]}}}
    meta["collaboration_spec"] = {"format": ledger.FORMAT, "task_id": "task-one", "goal": "交付测试结果",
        "document": {"entries": [{"id": "goal", "kind": "goal", "title": "目标", "body": "交付测试结果",
            "state": "active", "data": {}, "sources": [{"source_id": "user_1", "quote": "交付测试结果"}]}]}}
    return ExpertFlowSpec.from_value({"flow_id": "sample", "version": 1, "label": "样例", "meta": meta,
        "nodes": [{"node_id": key, "role": "executor", "label": key, "responsibility": "交付测试结果",
                   "failure_policy": {"action": "stop", "max_attempts": 1}} for key in ("work", "deliver")],
        "edges": [{"from_node": "work", "to_node": "deliver"}], "finalizer": "deliver"})


def make_record():
    record = {"run_id": RUN_ID, "kernel_calls": {}}
    value = ledger.initialize(record, compile_plan(flow()))
    record["collaboration"] = value
    value = ledger.state(record)
    value["coordinator"] = EXECUTOR.as_dict()
    value["coordinator_lease"] = {"id": "old", "epoch": value["coordinator_epoch"], "status": "active"}
    return record, value


def open_coordinator_gate(record):
    return open_gate(record, scope="contract:work", keys=["work"], kind="coordinator_unavailable",
                     label="协调服务", reason="回执待确认")


def resolve(record, gate, *, request_id, action, reason="确认协调回执", actor="user", model=None,
            budget_amount=None):
    return resolve_gate(record, gate_id=gate["id"], expected_version=ledger.state(record)["spec_version"],
                        request_id=request_id, action=action, reason=reason, actor=actor,
                        model=model, budget_amount=budget_amount)


def side_state(record):
    value = ledger.state(record)
    return copy.deepcopy({"calls": record.get("kernel_calls"),
                          "locked_models": record.get("locked_models"),
                          "budget": value["lineage_budget"], "semantic_calls": value["calls"],
                          "retry_generations": value.get("retry_generations")})


def test_resume_coordinator_rebinds_current_model_and_ends_old_lease():
    record, value = make_record()
    record["locked_models"] = {"work": EXECUTOR.as_dict()}
    record["kernel_calls"]["done-1"] = {"node_id": "work", "state": "succeeded"}
    value["calls"]["old-call"] = {"kind": "coordinator", "status": "dispatched"}
    gate = open_coordinator_gate(record)
    epoch, before = value["coordinator_epoch"], side_state(record)
    decision = resolve(record, gate, request_id="r1", action="resume_coordinator")
    value = ledger.state(record)
    assert decision["payload"]["action"] == "resume_coordinator"
    assert decision["coordinator_model"] == EXECUTOR.as_dict()
    assert value["coordinator"] == EXECUTOR.as_dict()
    assert value["coordinator_epoch"] == epoch + 1
    assert value["coordinator_lease"]["status"] == "superseded"
    assert record["status"] == "paused"
    assert value["recovery_gates"][gate["id"]]["status"] == "resolved"
    assert side_state(record) == before


def test_switch_coordinator_binds_strict_replacement_model():
    record, value = make_record()
    gate = open_coordinator_gate(record)
    epoch = value["coordinator_epoch"]
    target = REPLACEMENT.as_dict()
    decision = resolve(record, gate, request_id="r2", action="switch_coordinator", model=target)
    value = ledger.state(record)
    assert value["coordinator"] == target
    assert decision["coordinator_model"] == target
    assert value["coordinator_epoch"] == epoch + 1
    assert value["recovery_gates"][gate["id"]]["status"] == "resolved"


def test_resume_requires_bound_coordinator():
    record, value = make_record()
    value.pop("coordinator", None)
    gate = open_coordinator_gate(record)
    snapshot = copy.deepcopy(record)
    with pytest.raises(ValueError, match="没有绑定可恢复"):
        resolve(record, gate, request_id="u1", action="resume_coordinator")
    assert record == snapshot


def test_non_coordinator_gate_rejects_coordinator_actions():
    record, value = make_record()
    gate = open_gate(record, scope="contract:work", keys=["work"], kind="integration_blocked",
                     label="其它关口", reason="测试")
    snapshot = copy.deepcopy(record)
    with pytest.raises(ValueError, match="当前关口不支持协调者恢复"):
        resolve(record, gate, request_id="o1", action="resume_coordinator")
    with pytest.raises(ValueError, match="当前关口不支持协调者恢复"):
        resolve(record, gate, request_id="o2", action="switch_coordinator", model=REPLACEMENT.as_dict())
    assert record == snapshot


def test_coordinator_gate_rejections_are_atomic():
    record, value = make_record()
    gate = open_coordinator_gate(record)
    snapshot = copy.deepcopy(record)
    bad = [
        {"request_id": "b1", "action": "switch_coordinator"},
        {"request_id": "b2", "action": "switch_coordinator",
         "model": {"provider_key": "execution", "model_id": "model-b"}},
        {"request_id": "b3", "action": "switch_coordinator",
         "model": {"provider_key": "", "model_id": "model-b", "effort": ""}},
        {"request_id": "b4", "action": "switch_coordinator",
         "model": {**REPLACEMENT.as_dict(), "extra": 1}},
        {"request_id": "b5", "action": "resume_coordinator", "model": REPLACEMENT.as_dict()},
        {"request_id": "b6", "action": "resume_coordinator", "budget_amount": 3},
        {"request_id": "b7", "action": "switch_model", "model": REPLACEMENT.as_dict()},
        {"request_id": "b8", "action": "retry"},
        {"request_id": "b9", "action": "resume_coordinator", "actor": "model"},
    ]
    for kwargs in bad:
        with pytest.raises(ValueError):
            resolve(record, gate, **kwargs)
        assert record == snapshot


def test_stale_gate_and_wrong_identity_are_rejected():
    record, value = make_record()
    gate = open_coordinator_gate(record)
    with pytest.raises(ValueError, match="恢复关口或规格已经变化"):
        resolve_gate(record, gate_id="missing", expected_version=value["spec_version"], request_id="s0",
                     action="resume_coordinator", reason="确认", actor="user")
    resolve(record, gate, request_id="first", action="resume_coordinator")
    snapshot = copy.deepcopy(record)
    with pytest.raises(ValueError, match="恢复关口或规格已经变化"):
        resolve(record, gate, request_id="second", action="resume_coordinator")
    assert record == snapshot


def test_unknown_business_call_anywhere_blocks_coordinator_rebind():
    record, value = make_record()
    gate = open_coordinator_gate(record)
    record["kernel_calls"]["dispatch-1"] = {"node_id": "deliver", "state": "unknown"}
    snapshot = copy.deepcopy(record)
    assert unresolved_calls(record, list(value["contracts"]))
    with pytest.raises(ValueError, match="结果不明的业务操作"):
        resolve(record, gate, request_id="k1", action="switch_coordinator", model=REPLACEMENT.as_dict())
    assert record == snapshot


def test_replay_is_idempotent_and_content_change_rejected():
    record, value = make_record()
    gate = open_coordinator_gate(record)
    target = REPLACEMENT.as_dict()
    first = resolve(record, gate, request_id="same", action="switch_coordinator", model=target)
    epoch = ledger.state(record)["coordinator_epoch"]
    replay = resolve(record, gate, request_id="same", action="switch_coordinator", model=target)
    assert replay == first
    assert ledger.state(record)["coordinator_epoch"] == epoch
    snapshot = copy.deepcopy(record)
    with pytest.raises(ValueError, match="不能改写内容"):
        resolve(record, gate, request_id="same", action="resume_coordinator")
    with pytest.raises(ValueError, match="不能改写内容"):
        resolve(record, gate, request_id="same", action="switch_coordinator", model=EXECUTOR.as_dict())
    assert record == snapshot
