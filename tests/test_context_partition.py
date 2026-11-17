import copy
import json

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_compile import compile_plan
from mewcode.orchestration.context_partition import context_packets, encoded_size, planning_proposal
from mewcode.orchestration.task_context import build_task_context, ContextBudgetExceeded
from tests.test_autonomous_fragment import _sample_flow, _proposal, _build
from tests.test_autonomous_context_runtime import retry_store, orchestrate, run_flow
from tests.test_collaboration_mode import RUN_ID, EXECUTOR, REVIEWER, delivery


def large_flow():
    flow = _sample_flow()
    entries = flow.meta["collaboration_spec"]["document"]["entries"]
    for index in range(4):
        entries.append({"id": f"req{index}", "kind": "artifact", "title": f"第{index}部分",
            "body": f"精确原文{index}" * 1600, "state": "active", "data": {"scope": "work"},
            "sources": [{"source_id": f"user_{index}", "quote": f"原始要求{index}"}]})
    entries.append({"id": "hard", "kind": "constraint", "title": "公共约束", "body": "不得更换方法或扩大权限",
                    "scope": "global", "state": "active", "data": {},
                    "sources": [{"source_id": "user_hard", "quote": "不得更换方法或扩大权限"}]})
    return flow


def value_for(flow):
    value = ledger.initialize({"run_id": RUN_ID}, compile_plan(flow))
    value["policy"]["context_char_budget"] = 28000
    return value


def test_packets_preserve_whole_source_entries_and_global_constraints_without_mutation():
    value = value_for(large_flow())
    before = copy.deepcopy(value)
    with pytest.raises(ContextBudgetExceeded):
        build_task_context(value, "work", char_budget=28000)
    packets = context_packets(value, "work", budget=28000)
    assert len(packets) > 1 and all(encoded_size(row) <= 28000 for row in packets)
    covered = []
    for row in packets:
        assert row["scope"]["parent_basis"] == ledger.dependencies(value, "work")
        # These simple fixtures fit without source sharing.
        assert not row["context"].get("context_encoding")
        assert row["context"]["objective"]["constraints"] == [value["entries"]["hard"]]
        for entry in row["context"]["objective"]["requirements"]:
            assert entry == value["entries"][entry["id"]]
            covered.append(entry["id"])
    assert covered == value["contracts"]["work"]["requirement_ids"]
    assert value == before


def test_indivisible_requirement_returns_specific_failure_without_truncation():
    value = value_for(large_flow())
    value["entries"]["req0"]["body"] = "不可截断" * 20000
    with pytest.raises(ContextBudgetExceeded, match="req0.*未丢弃原文"):
        context_packets(value, "work", budget=28000)


def split_proposal():
    proposal = _proposal()
    proposal["nodes"][0]["requirement_ids"] = ["goal", "req0", "req1"]
    proposal["nodes"][1]["requirement_ids"] = ["goal", "req2", "req3"]
    return proposal


def test_scoped_fragment_requires_complete_coverage_and_publication_rechecks_it(tmp_path):
    from mewcode.kernel import collaboration_graph as graph
    flow = large_flow()
    value = value_for(flow)
    proposal = split_proposal()
    fragment = _build(value, flow, tmp_path, proposal)
    assert fragment["contracts"]["work"]["requirement_ids"] == value["contracts"]["work"]["requirement_ids"]
    record = {"run_id": RUN_ID, "locked_flow": flow.as_dict(), "collaboration": value}
    attempt = ledger.dispatch(value, "work")
    broken = copy.deepcopy(fragment)
    broken["contracts"][broken["child_ids"][1]]["requirement_ids"].remove("req3")
    with pytest.raises(ValueError, match="全部父合同需求"):
        graph.prepare(record, broken, publication_id="missing", attempt_id=attempt["id"])
    proposal["nodes"][1]["requirement_ids"] = ["absent"]
    with pytest.raises(ValueError, match="父合同需求"):
        _build(value, flow, tmp_path, proposal)


@pytest.mark.asyncio
@pytest.mark.parametrize("scalar_acceptance", [False, True])
async def test_real_scheduler_overflow_splits_once_and_preserves_full_acceptance_and_routes(tmp_path, scalar_acceptance):
    flow = large_flow()
    store = retry_store(tmp_path, flow)
    # Initialize before selecting a deliberately small *test* policy limit.
    store.mutate(RUN_ID, lambda record: ledger.initialize(record, compile_plan(flow)), durable=True)
    def setup(record):
        value = ledger.state(record)
        value["policy"]["context_char_budget"] = 28000
        value.setdefault("acceptance_model_overrides", {})["work"] = REVIEWER.as_dict()
        record["locked_models"] = {"work": EXECUTOR.as_dict()}
    store.mutate(RUN_ID, setup, durable=True)
    before = copy.deepcopy(ledger.state(store.load(RUN_ID)))
    invoked, page_scopes, graph_scopes, accepted_scopes = [], [], [], []

    async def runner(node, model, prompt, policy, member=None):
        invoked.append(node.node_id)
        assert node.node_id != "work", "oversized parent must not enter business execution"
        assert len(prompt) <= 28000
        if node.node_id.startswith("work::"):
            assert model == EXECUTOR
            contract = ledger.state(store.load(RUN_ID))["contracts"][node.node_id]
            assert len(contract["requirement_ids"]) == 3
        return delivery(node.node_id)

    async def coordinate(model, payload, system):
        assert encoded_size(payload) <= 28000
        if payload.get("purpose") == "context_overflow_partition":
            assert model == EXECUTOR
            page_scopes.extend(payload["scope"]["included_requirement_ids"])
            return {"notes": "把 req0/req1 与 req2/req3 分开，分别只写 work/a 和 work/b。共同保留 goal 与全部约束。"}
        if payload.get("purpose") == "context_overflow_plan":
            proposal = split_proposal()
            if scalar_acceptance:
                for row in [*proposal["nodes"], proposal["join"]]:
                    row["acceptance"] = "\n".join(row["acceptance"])
            return proposal
        if payload.get("purpose") == "context_partition_review":
            assert model == REVIEWER
            graph_scopes.extend(payload["scope"]["included_requirement_ids"])
            return {"passed": True, "evidence": ["本页需求覆盖且写入范围独立"]}
        return {"action": "wait", "reason": "unexpected coordination"}

    async def review(model, payload, context):
        if payload["contract"]["id"] == "work":
            accepted_scopes.append(set(payload["requirements"]))
        return {"passed": True, "evidence": ["实际交付符合本合同全部检查"]}

    host = orchestrate(store, tmp_path, runner, review, coordinate)
    result = await run_flow(host, flow, store)
    assert result.status == "completed", result.final_summary
    record = store.load(RUN_ID)
    value = ledger.state(record)
    full = set(before["contracts"]["work"]["requirement_ids"])
    assert set(page_scopes) == full and set(graph_scopes) == full
    assert accepted_scopes and all(scope == full for scope in accepted_scopes)
    assert len(invoked) == 3 and len(set(invoked)) == 3
    assert value["entries"] == before["entries"] and record["locked_flow"] == flow.as_dict()
    assert value["lineage_budget"]["limits"] == before["lineage_budget"]["limits"]
    assert value["lineage_budget"]["lineages"]["work"]["used"]["attempts"] == 4
    assert value["lineage_budget"]["lineages"]["work"]["used"]["expansions"] == 1
    for child in value["contracts"]["work"]["join_children"]:
        assert record["locked_models"][child] == EXECUTOR.as_dict()
        assert value["acceptance_model_overrides"][child] == REVIEWER.as_dict()
    count = len(page_scopes), len(graph_scopes), len(invoked)
    replay = await run_flow(host, flow, store)
    assert replay.status == "completed"
    assert (len(page_scopes), len(graph_scopes), len(invoked)) == count


def test_planning_scalar_criterion_is_preserved_verbatim_and_raw_result_is_unchanged():
    raw = split_proposal()
    raw["nodes"][0]["acceptance"] = "第一项；第二项\n未知保持未知，不宣称通过。"
    raw["join"]["acceptance"] = "父合同全部要求仍须完整验收。"
    before = copy.deepcopy(raw)
    normalized = planning_proposal(raw)
    assert raw == before
    assert normalized["nodes"][0]["acceptance"] == [raw["nodes"][0]["acceptance"]]
    assert normalized["join"]["acceptance"] == [raw["join"]["acceptance"]]
    normalized["nodes"][0]["acceptance"] = raw["nodes"][0]["acceptance"]
    normalized["join"]["acceptance"] = raw["join"]["acceptance"]
    assert normalized == raw


@pytest.mark.parametrize("invalid", ["", "  ", None, 1, {"passed": True}])
def test_planning_invalid_acceptance_remains_rejected(tmp_path, invalid):
    proposal = split_proposal()
    proposal["nodes"][0]["acceptance"] = invalid
    with pytest.raises(ValueError, match="acceptance"):
        _build(value_for(large_flow()), large_flow(), tmp_path, planning_proposal(proposal))


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["review_rejected", "insufficient_budget", "expansion_forbidden"])
async def test_unsafe_or_unfunded_split_never_runs_workers(tmp_path, condition):
    flow = large_flow()
    store = retry_store(tmp_path, flow)
    store.mutate(RUN_ID, lambda record: ledger.initialize(record, compile_plan(flow)), durable=True)
    def setup(record):
        value = ledger.state(record)
        value["policy"]["context_char_budget"] = 28000
        if condition == "insufficient_budget":
            # New runs share root capacity in elastic mode. Limit the actual
            # enforced pool, not the now-advisory local allocation.
            value["lineage_budget"]["limits"]["root"]["attempts"] = 3
        if condition == "expansion_forbidden":
            value["contracts"]["work"]["allow_expansion"] = False
    store.mutate(RUN_ID, setup, durable=True)
    calls, invoked = [], []
    async def runner(*args):
        invoked.append(True)
        return delivery("must not execute")
    async def coordinate(model, payload, system):
        calls.append(payload.get("purpose"))
        if payload.get("purpose") == "context_overflow_partition":
            return {"notes": "分成两个独立输出"}
        if payload.get("purpose") == "context_overflow_plan":
            return split_proposal()
        return {"passed": False, "reason": "分工没有满足语义约束", "evidence": ["独立复核拒绝"]}
    result = await run_flow(orchestrate(store, tmp_path, runner, None, coordinate), flow, store)
    value = ledger.state(store.load(RUN_ID))
    assert result.status == "blocked" and not invoked
    assert set(value["contracts"]) == {"work", "deliver"}
    assert not value.get("plan_holds")
    assert not value["lineage_budget"]["grants"]
    assert value["lineage_budget"]["root"]["used"]["expansions"] == 0
    if condition != "review_rejected":
        assert not calls, "preflight must fail before spending model calls"
