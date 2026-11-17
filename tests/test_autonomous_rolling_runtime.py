"""Explicit future work is published only after accepted predecessor delivery."""
import copy

import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger, collaboration_planning as planning
from mewcode.kernel.collaboration_graph import effective_flow
from mewcode.orchestration.collab_rolling import milestone_basis
from tests.test_autonomous_context_runtime import retry_store, orchestrate, run_flow
from tests.test_autonomous_fragment import _sample_flow, ALLOWED_TOOLS
from tests.test_autonomous_rolling_plan import entry
from tests.test_autonomous_rolling_fragment import _proposal, _build
from tests.test_collaboration_mode import RUN_ID, delivery, accept
from mewcode.orchestration.collab_compile import compile_plan


def rolling_flow():
    raw = _sample_flow().as_dict()
    spec = raw["meta"]["collaboration_spec"]
    spec["planning_mode"] = "rolling"
    spec["document"]["entries"].extend([
        entry("m1", "milestone", "准备初步交付"),
        entry("work", "node", "完成工作", {"contract": {"milestone_id": "m1"}}),
        entry("deliver", "node", "验收初步交付", {"contract": {"milestone_id": "m1"}}),
        entry("m2", "milestone", "撰写后续报告", {
            "deferred": True, "depends_on": ["m1"], "goal_ids": ["goal"],
            "checks": [{"id": "m2-review", "kind": "review", "criteria": ["后续报告符合目标"]}],
            "capabilities": {"write_paths": ["work2"]}, "tools": list(ALLOWED_TOOLS),
            "outputs": ["后续报告"]}),
    ])
    return ExpertFlowSpec.from_value(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_rolling_real_scheduler_waits_publishes_and_replays(tmp_path, approved):
    flow = rolling_flow()
    store = retry_store(tmp_path, flow)
    calls, decisions = [], []

    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id in {"work", "deliver"}:
            assert not decisions
            assert store.load(RUN_ID)["collaboration"]["milestones"]["m2"]["status"] == "unexpanded"
        else:
            assert decisions == ["plan", "review"]
        return delivery("已完成：" + node.node_id)

    async def review(*args):
        return {"passed": True, "evidence": ["交付符合原始合同"]}

    async def coordinate(model, payload, system):
        value = ledger.state(store.load(RUN_ID))
        assert value["milestones"]["m1"]["status"] == "accepted"
        assert all(ledger.valid_certificate(value, key) for key in ("work", "deliver"))
        decisions.append("review" if "fragment" in payload else "plan")
        if "fragment" in payload:
            return {"passed": approved, "reason": "保持原始目标" if approved else "交付范围不完整", "evidence": ["需求逐项核对"]}
        return _proposal()

    result = await run_flow(orchestrate(store, tmp_path, runner, review, coordinate), flow, store)
    assert result.status == ("completed" if approved else "blocked"), result.final_summary
    value = ledger.state(store.load(RUN_ID))
    assert decisions == ["plan", "review"]
    assert len(calls) == (4 if approved else 2)
    assert value["milestones"]["m2"]["status"] == ("accepted" if approved else "unexpanded")
    assert store.load(RUN_ID)["locked_flow"] == flow.as_dict()
    assert value["lineage_budget"]["root"]["used"]["expansions"] == int(approved)
    assert value["lineage_budget"]["root"]["reserved"]["expansions"] == 0
    before = (calls[:], decisions[:])
    replay = await run_flow(orchestrate(store, tmp_path, runner, review, coordinate), flow, store)
    assert replay.status == result.status
    assert (calls, decisions) == before


def prepared(tmp_path):
    flow = rolling_flow()
    record = {"run_id": RUN_ID, "locked_flow": flow.as_dict()}
    value = ledger.initialize(record, compile_plan(flow))
    for key in ("work", "deliver"):
        accept(value, key)
    value["milestones"]["m1"]["status"] = "accepted"
    basis = milestone_basis(value, flow, "m2")
    planning.reserve(record, "m2", planning_id="plan-two", basis=basis)
    fragment = _build(ledger.state(record), flow, tmp_path, _proposal())
    return record, fragment


def publish(record, fragment):
    return planning.publish(record, "plan-two", fragment,
        review={"passed": True, "evidence": ["需求保持"]}, proposer_vendor="alpha", reviewer_vendor="beta")


@pytest.mark.parametrize("change", ["basis", "certificate", "epoch", "edge", "contract", "scope", "dependency"])
def test_milestone_publication_rejects_stale_or_tampered_candidate_atomically(tmp_path, change):
    record, fragment = prepared(tmp_path)
    value = ledger.state(record)
    if change == "basis":
        value["milestones"]["m2"]["goal"] += "新需求"
    elif change == "certificate":
        ledger.invalidate(value, {"work"}, "上游证据变化")
    elif change == "epoch":
        value["coordinator_epoch"] += 1
    elif change == "edge":
        fragment["flow"]["edges"] = [e for e in fragment["flow"]["edges"] if e["from_node"] != "work"]
    elif change == "contract":
        fragment["contracts"]["milestone::m2"]["checks"] = []
    elif change == "scope":
        fragment["contracts"][fragment["child_ids"][0]]["capabilities"] = {"write_paths": ["."]}
    elif change == "dependency":
        fragment["contracts"][fragment["child_ids"][0]]["depends_on"] = []
    before = copy.deepcopy(record)
    with pytest.raises(ValueError):
        publish(record, fragment)
    assert record == before


def test_milestone_publication_is_durable_idempotent_and_keeps_original_graph(tmp_path):
    record, fragment = prepared(tmp_path)
    old = copy.deepcopy(record["locked_flow"])
    assert publish(record, fragment)
    before = copy.deepcopy(record)
    assert not publish(record, fragment)
    assert record == before and record["locked_flow"] == old
    assert effective_flow(record).finalizer_node_id == "milestone::m2"
    assert ledger.state(record)["milestones"]["m2"]["contracts"] == fragment["contract_ids"]
