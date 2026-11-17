"""Five business stages; decision-owned retries, not ordinary graph cycles."""
import copy
import asyncio

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.orchestration.convert import from_expert_flow, to_expert_flow
from mewcode.orchestration.loops import LoopContract
from mewcode.orchestration.pipeline_bridge import from_pipeline_graph, to_pipeline_graph
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.orchestration.validate import validate_spec
from tests.test_generic_loops import report, spec_for


def five_stage_spec():
    return OrchestrationSpec.from_value({
        "spec_id": "five", "label": "原子 OCR", "finalizer_node_id": "done",
        "nodes": [{"node_id": n, "kind": k, "label": n, "goal": "保留原内容并逐项复核"} for n, k in (
            ("first", "implement"), ("second", "implement"), ("audit", "audit"),
            ("repair", "implement"), ("decide", "decision"), ("done", "reduce"))],
        "edges": [{"from": a, "to": b, "condition": c} for a, b, c in (
            ("first", "second", "always"), ("second", "audit", "always"), ("repair", "audit", "always"),
            ("audit", "decide", "pass"), ("audit", "decide", "fail"),
            ("decide", "first", "loop"), ("decide", "repair", "loop"), ("decide", "done", "pass"))],
        "meta": {"loops": [{"loop_id": "atomic", "entry_node_id": "first", "check_node_id": "decide",
            "node_ids": ["first", "second", "audit", "repair", "decide"], "reviewer_node_ids": ["audit", "decide"],
            "exit_condition": "逐项通过，UNKNOWN 不通过", "criteria": [{"criterion_id": "REQ-01", "acceptance_test": "逐项核验"}],
            "requires_repair": True, "repair_node_ids": ["repair"], "repair_dispatch": "after_decision"}]},
    })


def test_five_node_conversion_roundtrips_and_legacy_hash():
    spec = five_stage_spec()
    validate_spec(spec)
    for value in (from_expert_flow(to_expert_flow(spec)), from_pipeline_graph(to_pipeline_graph(spec))):
        validate_spec(value)
        contract = value.meta["loops"][0]
        assert contract["repair_dispatch"] == "after_decision" and len(contract["node_ids"]) == 5
        assert {(e.from_node, e.to_node) for e in value.edges if e.condition == "loop"} == {("decide", "first"), ("decide", "repair")}
    old = LoopContract.from_value(spec_for().meta["loops"][0])
    assert "repair_dispatch" not in old.as_dict()
    assert old.contract_hash == LoopContract.from_value(old.as_dict()).contract_hash


@pytest.mark.parametrize("fault", ["fail_edge", "ordinary_cycle", "no_declaration", "reviewer", "first_is_repair", "lost_check", "repair_to_first"])
def test_invalid_directed_graph_fails(fault):
    raw = five_stage_spec().as_dict()
    loop = raw["meta"]["loops"][0]
    if fault == "fail_edge":
        next(e for e in raw["edges"] if e["from_node"] == "decide" and e["to_node"] == "repair")["condition"] = "fail"
    elif fault == "ordinary_cycle":
        loop.pop("repair_dispatch")
        next(e for e in raw["edges"] if e["from_node"] == "decide" and e["to_node"] == "repair")["condition"] = "fail"
    elif fault == "no_declaration": loop["repair_node_ids"] = []
    elif fault == "reviewer": loop["repair_node_ids"] = ["audit"]
    elif fault == "first_is_repair": loop["repair_node_ids"] = ["first"]
    elif fault == "lost_check": raw["edges"] = [e for e in raw["edges"] if e["from_node"] != "audit"]
    else: raw["edges"].append({"from_node": "repair", "to_node": "first", "condition": "always"})
    with pytest.raises(ValueError): validate_spec(OrchestrationSpec.from_value(raw))


async def execute_case(raw, decide):
    flow = to_expert_flow(raw)
    contract = LoopContract.from_value(flow.meta["loops"][0])
    states, calls, events = {}, [], []
    async def emit(event): events.append(copy.deepcopy(event))
    async def runner(node, incoming, policy, prompt):
        context = policy.loop_context
        calls.append((context["round"], node.node_id))
        decision = decide(context, node.node_id) if node.node_id in {"audit", "decide"} else None
        return ExpertRoleResult(text="有证据", input_tokens=10, loop_decision=decision)
    result = await run_bounded_loop(contract=contract, flow=flow, saved=states, run_node=runner, emit=emit,
        policy_for=lambda _: ExpertExecutionPolicy("stable", False, 0), incoming=[])
    return result, states, calls, events


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["strict", "team"])
async def test_decision_owns_repair_then_review_then_decision(mode):
    raw = five_stage_spec(); raw.meta["loops"][0]["control_mode"] = mode
    def decide(ctx, node):
        value = report(ctx, "FAIL" if ctx["round"] == 1 else "PASS")
        if ctx["round"] == 1: value["repair_node_ids"] = ["repair"] if node == "decide" else ["first"]
        return value
    (_, result), states, calls, _ = await execute_case(raw, decide)
    assert result.status == "completed"
    assert calls == [(1, "first"), (1, "second"), (1, "audit"), (1, "decide"),
                     (2, "repair"), (2, "audit"), (2, "decide")]
    assert states["atomic"]["tokens"] == 70


@pytest.mark.asyncio
async def test_pass_never_dispatches_repair():
    (_, result), _, calls, _ = await execute_case(five_stage_spec(), lambda ctx, _: report(ctx))
    assert result.status == "completed"
    assert [n for _, n in calls] == ["first", "second", "audit", "decide"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unknown", "blocked", "budget", "missing_target", "mixed_target", "unknown_target", "audit_only"])
async def test_no_repair_without_valid_final_decision(fault):
    raw = five_stage_spec()
    if fault == "budget": raw.meta["loops"][0]["max_iterations"] = 1
    def decide(ctx, node):
        value = report(ctx, "UNKNOWN" if fault == "unknown" else "FAIL")
        value["repair_node_ids"] = ["repair"]
        if fault == "blocked": value["decision"] = "BLOCKED"
        if node == "decide":
            if fault == "missing_target": value["repair_node_ids"] = []
            if fault == "mixed_target": value["repair_node_ids"] = ["first", "repair"]
            if fault == "unknown_target": value["repair_node_ids"] = ["outside"]
            if fault == "audit_only": value = report(ctx)
        return value
    (_, result), _, calls, _ = await execute_case(raw, decide)
    assert result.status == "needs_attention"
    assert calls == [(1, "first"), (1, "second"), (1, "audit"), (1, "decide")]


@pytest.mark.asyncio
async def test_restart_after_decision_preserves_dispatch_and_does_not_repeat_reads():
    flow = to_expert_flow(five_stage_spec())
    contract = LoopContract.from_value(flow.meta["loops"][0])
    states, calls = {}, []
    crash = True
    async def emit(event):
        if crash and event.get("loop_state", {}).get("status") == "continue":
            assert event["loop_state"]["dispatch_roots"] == ["repair"]
            assert set(event["loop_state"]["carry_results"]) == {"first", "second"}
            raise asyncio.CancelledError()
    async def runner(node, incoming, policy, prompt):
        ctx = policy.loop_context
        calls.append((ctx["round"], node.node_id))
        assert "after_decision" in prompt and "两者不能混选" in prompt
        decision = report(ctx, "FAIL" if ctx["round"] == 1 else "PASS")
        decision["repair_node_ids"] = ["repair"]
        return ExpertRoleResult(text="done", loop_decision=decision if node.node_id in {"audit", "decide"} else None)
    async def run(saved):
        return await run_bounded_loop(contract=contract, flow=flow, saved=saved, run_node=runner, emit=emit,
            policy_for=lambda _: ExpertExecutionPolicy("stable", False, 0), incoming=[])
    with pytest.raises(asyncio.CancelledError): await run(states)
    assert calls == [(1, "first"), (1, "second"), (1, "audit"), (1, "decide")]
    crash = False
    _, result = await run(copy.deepcopy(states))
    assert result.status == "completed"
    assert calls[4:] == [(2, "repair"), (2, "audit"), (2, "decide")]


@pytest.mark.asyncio
async def test_new_item_explicitly_restarts_reads_not_previous_repair():
    def decide(ctx, node):
        value = report(ctx, "FAIL" if ctx["round"] < 3 else "PASS")
        if ctx["round"] == 1: value["repair_node_ids"] = ["repair"]
        elif ctx["round"] == 2: value["repair_node_ids"] = ["first"]
        return value
    (_, result), _, calls, _ = await execute_case(five_stage_spec(), decide)
    assert result.status == "completed"
    assert [n for r, n in calls if r == 3] == ["first", "second", "audit", "decide"]


@pytest.mark.asyncio
@pytest.mark.parametrize("targets,allowed", [(["repair"], True), (["first"], True), ([], False), (["first", "repair"], False), (["second"], False), (["repair", "repair"], False)])
async def test_real_decision_tool_checks_directed_dispatch(targets, allowed):
    from mewcode.tools.submit_loop_decision import SubmitLoopDecisionTool, SubmitLoopDecisionParams
    from mewcode.tools.submit_verdict import VerdictHolder
    contract = LoopContract.from_value(five_stage_spec().meta["loops"][0])
    context = {**contract.as_dict(), "contract_hash": contract.contract_hash, "acting_node_id": "decide"}
    holder = VerdictHolder()
    tool = SubmitLoopDecisionTool(holder, context)
    value = {**report(context, "FAIL"), "repair_node_ids": targets}
    result = await tool.execute(SubmitLoopDecisionParams(**value))
    assert result.is_error != allowed
    assert (holder.read() is not None) == allowed
