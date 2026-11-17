from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertProfile, ExpertRoleResult
from mewcode.model_registry import ModelRef
from mewcode.orchestration.convert import from_expert_flow, to_expert_flow
from mewcode.orchestration.extract import repair_extracted_graph_cycles
from mewcode.orchestration.loops import LoopContract, LoopWatchdog, compile_loops
from mewcode.orchestration.pipeline_bridge import from_pipeline_graph, to_pipeline_graph
from mewcode.orchestration.plan_text import compact_plan_text
from mewcode.orchestration.spec import OrchestrationError, OrchestrationSpec
from mewcode.orchestration.validate import validate_spec


def spec_for(topic="数据清洗"):
    return OrchestrationSpec.from_value({
        "spec_id": "generic", "label": topic, "finalizer_node_id": "done",
        "nodes": [{"node_id": nid, "kind": kind, "label": nid, "goal": topic + "：" + nid}
                  for nid, kind in [("prepare", "plan"), ("work", "implement"), ("check", "decision"), ("done", "reduce")]],
        "edges": [{"from": "prepare", "to": "work"}, {"from": "work", "to": "check"},
                  {"from": "check", "to": "work", "condition": "loop"},
                  {"from": "check", "to": "done", "condition": "pass"}],
        "meta": {"loops": [{"loop_id": "improve", "entry_node_id": "work", "check_node_id": "check",
                            "node_ids": ["work", "check"], "exit_condition": "验证通过",
                            "criteria": [{"criterion_id": "REQ-01", "acceptance_test": "结果通过验证"}]}]},
    })


def report(context, state="PASS"):
    return {"contract_hash": context["contract_hash"],
            "decision": "EXIT" if state == "PASS" else ("BLOCKED" if state == "UNKNOWN" else "CONTINUE"),
            "checks": [{"criterion_id": "REQ-01", "state": state, "evidence": "结果核验记录", "location": "test:record"}]}


@pytest.mark.parametrize("topic", ["代码测试修复", "数据清洗", "设计评审", "论文修改"])
def test_generic_roundtrip(topic):
    spec = spec_for(topic)
    validate_spec(spec)
    flow = to_expert_flow(spec)
    assert len(flow.nodes) == 4 and len(flow.edges) == 3
    assert flow.meta["loops"][0]["max_iterations"] == 4
    assert flow.node_map["work"].failure_policy.max_attempts == 1
    for restored in [from_expert_flow(flow), from_pipeline_graph(to_pipeline_graph(spec))]:
        validate_spec(restored)
        assert restored.node_map()["check"].kind == "decision"
        assert any(edge.condition == "loop" for edge in restored.edges)
        assert to_expert_flow(restored).meta["loops"] == flow.meta["loops"]


def test_untyped_cycles_are_not_silently_removed():
    with pytest.raises(OrchestrationError, match="未声明"):
        repair_extracted_graph_cycles({"nodes": [{"node_id": "a"}, {"node_id": "b"}],
                                       "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]})
    raw = spec_for().as_dict()
    assert repair_extracted_graph_cycles(raw)[1] == []


def test_loop_body_heals_missing_structural_members():
    """Models often list only worker nodes in node_ids; entry/check/reviewers
    are in the loop body by definition and must be merged, not rejected."""
    raw = spec_for().as_dict()
    raw["meta"]["loops"][0]["node_ids"] = ["work"]
    spec = OrchestrationSpec.from_value(raw)
    validate_spec(spec)
    _, loops = compile_loops(spec.nodes, spec.edges, spec.meta.get("loops"))
    assert {"work", "check"} <= set(loops[0].node_ids)
    assert "check" in loops[0].reviewer_node_ids


def test_loop_body_heals_when_node_ids_omitted():
    raw = spec_for().as_dict()
    del raw["meta"]["loops"][0]["node_ids"]
    spec = OrchestrationSpec.from_value(raw)
    validate_spec(spec)
    _, loops = compile_loops(spec.nodes, spec.edges, spec.meta.get("loops"))
    assert {"work", "check"} <= set(loops[0].node_ids)


def test_loop_explicit_reviewers_gain_check_node():
    raw = spec_for().as_dict()
    raw["meta"]["loops"][0]["reviewer_node_ids"] = ["work"]
    spec = OrchestrationSpec.from_value(raw)
    _, loops = compile_loops(spec.nodes, spec.edges, spec.meta.get("loops"))
    assert "check" in loops[0].reviewer_node_ids
    assert "work" in loops[0].node_ids


def test_loop_body_unknown_node_still_fails():
    raw = spec_for().as_dict()
    raw["meta"]["loops"][0]["node_ids"].append("missing")
    with pytest.raises(OrchestrationError, match="不存在的节点"):
        validate_spec(OrchestrationSpec.from_value(raw))


def test_loop_healed_membership_roundtrips_through_canvas():
    raw = spec_for().as_dict()
    raw["meta"]["loops"][0]["node_ids"] = ["work"]
    spec = OrchestrationSpec.from_value(raw)
    restored = from_pipeline_graph(to_pipeline_graph(spec))
    healed = restored.meta["loops"][0]
    assert {"work", "check"} <= set(healed["node_ids"])


@pytest.mark.parametrize("case", ["no_exit", "outside_entry", "bad_id", "bad_budget", "finalizer_inside"])
def test_invalid_loops_fail_before_execution(case):
    raw = spec_for().as_dict()
    if case == "no_exit":
        raw["edges"] = [edge for edge in raw["edges"] if edge["to_node"] != "done"]
    elif case == "outside_entry":
        raw["edges"].append({"from": "prepare", "to": "check"})
    elif case == "bad_id":
        raw["meta"]["loops"][0]["node_ids"].append("missing")
    elif case == "bad_budget":
        raw["meta"]["loops"][0]["max_iterations"] = 0
    else:
        raw["finalizer_node_id"] = "check"
    with pytest.raises(OrchestrationError):
        validate_spec(OrchestrationSpec.from_value(raw))


def test_plan_compaction_is_exact_and_roundtrips_new_fields():
    assert compact_plan_text("1. 验证结果。\n2. 验证结果。\n3. 保留证据。") == "1. 验证结果。\n3. 保留证据。"
    assert "2026.2" in compact_plan_text("2026.1 数据\n2026.2 数据")
    raw = spec_for().as_dict()
    raw["nodes"][1]["plan_brief"] = {"summary": "验证。验证。", "steps": ["检查", "检查"],
                                    "outputs": ["报告"], "acceptance": ["测试通过"]}
    spec = OrchestrationSpec.from_value(raw)
    for _ in range(3):
        spec = from_pipeline_graph(to_pipeline_graph(from_expert_flow(to_expert_flow(spec))))
    brief = spec.node_map()["work"].plan_brief
    assert brief.steps == ("检查",) and brief.outputs == ("报告",) and brief.acceptance == ("测试通过",)
    assert to_expert_flow(spec).node_map["work"].responsibility.count("验收：") == 1


@pytest.mark.parametrize("states,expected,completed", [
    (["PASS"], 1, True), (["FAIL", "PARTIAL", "PASS"], 3, True),
    (["FAIL", "FAIL", "FAIL"], 3, False), (["PARTIAL", "FAIL"], 2, False), (["UNKNOWN"], 1, False),
])
@pytest.mark.asyncio
async def test_runtime_convergence_and_no_retry_multiplication(tmp_path, states, expected, completed):
    calls, events = [], []
    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "check":
            decision = report(policy.loop_context, states[calls.count("check") - 1])
            assert not policy.allow_delegation and policy.token_budget
            return ExpertRoleResult(text="核验", loop_decision=decision,
                                    verdict={"verdict": "PASS" if decision["decision"] == "EXIT" else "NEEDS_ATTENTION"})
        return ExpertRoleResult(text=node.node_id, input_tokens=10, output_tokens=10)
    ref = ModelRef("test", "test", "")
    orchestrator = ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path,
                                         event_sink=lambda event: events.append(copy.deepcopy(event)))
    result = await orchestrator.run("task", ExpertProfile(ref, ref, ref), {}, strategy="stable", flow=to_expert_flow(spec_for()))
    assert calls.count("work") == expected and calls.count("prepare") == 1
    assert (result.status == "completed") is completed
    assert ("done" in calls) is completed
    assert any(event.get("loop_state", {}).get("round") == expected for event in events)


@pytest.mark.asyncio
async def test_resume_keeps_completed_outer_and_inner_nodes_and_budget(tmp_path):
    calls, events = [], []
    interrupted = True
    async def runner(node, model, prompt, policy, member=None):
        nonlocal interrupted
        calls.append(node.node_id)
        if node.node_id == "check":
            if interrupted:
                interrupted = False
                await policy.usage_callback(100)
                raise asyncio.CancelledError()
            assert policy.resume_messages
            return ExpertRoleResult(text="核验", verdict={"verdict": "PASS"}, loop_decision=report(policy.loop_context))
        return ExpertRoleResult(text=node.node_id, input_tokens=10)
    ref = ModelRef("test", "test", "")
    orchestrator = ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path,
                                         event_sink=lambda event: events.append(copy.deepcopy(event)))
    kwargs = dict(task="task", profile=ExpertProfile(ref, ref, ref), catalog={}, strategy="stable", flow=to_expert_flow(spec_for()))
    with pytest.raises(asyncio.CancelledError):
        await orchestrator.run(**kwargs)
    saved = {event["loop_state_key"]: event["loop_state"] for event in events if event.get("loop_state")}
    assert saved["improve"]["tokens"] == 110
    result = await orchestrator.run(**kwargs, checkpoint={"node_states": {"prepare": {"status": "completed", "text": "saved"}}, "loop_states": saved})
    assert result.status == "completed"
    assert calls == ["prepare", "work", "check", "check", "done"]


def test_disagreement_uses_worst_evidence_and_stops_without_claiming_pass():
    contract = LoopContract.from_value(spec_for().meta["loops"][0])
    watchdog = LoopWatchdog(contract)
    context = {"contract_hash": contract.contract_hash}
    for _ in range(3):
        status, reason = watchdog.observe([report(context, "PASS"), report(context, "FAIL")])
    assert status == "blocked" and watchdog.state["disputes"] == ["REQ-01"]


def test_restart_requires_explicit_outside_anchor_and_archives_old_budget(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    ref = ModelRef("test", "test", "")
    store = ExpertRunStore(tmp_path)
    run_id = "a" * 32
    store.create(run_id=run_id, conversation_id="test", task="task", attachments=[],
                 profile=ExpertProfile(ref, ref, ref).as_dict(), strategy="stable", flow=to_expert_flow(spec_for()))
    old = {"status": "blocked", "tokens": 50000, "round": 4, "round_results": {"work": {"text": "private", "status": "completed"}}}
    store.mutate(run_id, lambda value: value.update(status="paused", loop_states={"improve": old},
        node_states={"prepare": {"status": "completed", "text": "keep"}, "work": {"status": "completed"}}))
    with pytest.raises(ValueError, match="未启动新调用"):
        store.resume(run_id, strategy="stable")
    resumed = store.load(run_id)
    assert resumed["loop_states"]["improve"]["tokens"] == 50000
    store.mutate(run_id, lambda value: value.update(status="paused"))
    with pytest.raises(ValueError, match="循环体中途"):
        store.resume(run_id, strategy="stable", resume_after_node="work")
    restarted = store.resume(run_id, strategy="stable", resume_after_node="prepare")
    assert restarted["loop_states"] == {}
    assert restarted["loop_restarts"][0]["previous"]["tokens"] == 50000
    assert restarted["node_states"]["prepare"]["text"] == "keep"
    assert "loop_restarts" not in store.public(run_id)


def test_control_plan_uses_loop_budget_not_individual_retries():
    from mewcode.orchestration.control_plan import build_control_plan, ControlPlan
    plan = build_control_plan(spec_for())
    assert plan.loops and not any(rule.node_id == "work" for rule in plan.retries)
    assert ControlPlan.from_value(plan.as_dict()).loops == plan.loops


@pytest.mark.asyncio
async def test_structured_decision_tool_rejects_drift_unknown_and_duplicate_submissions():
    from mewcode.tools.submit_loop_decision import SubmitLoopDecisionTool, SubmitLoopDecisionParams
    from mewcode.tools.submit_verdict import VerdictHolder
    contract = LoopContract.from_value(spec_for().meta["loops"][0])
    context = {**contract.as_dict(), "contract_hash": contract.contract_hash}
    holder = VerdictHolder()
    tool = SubmitLoopDecisionTool(holder, context)
    wrong = report(context)
    wrong["contract_hash"] = "drift"
    assert (await tool.execute(SubmitLoopDecisionParams(**wrong))).is_error
    unknown = report(context, "UNKNOWN")
    unknown["decision"] = "EXIT"
    assert (await tool.execute(SubmitLoopDecisionParams(**unknown))).is_error
    assert not (await tool.execute(SubmitLoopDecisionParams(**report(context)))).is_error
    assert (await tool.execute(SubmitLoopDecisionParams(**report(context)))).is_error


@pytest.mark.asyncio
async def test_plan_generation_watchdog_keeps_original_graph(monkeypatch):
    from mewcode.orchestration import extract
    from mewcode.tools.base import TextDelta
    closed = []
    async def stream(*args, **kwargs):
        try:
            for _ in range(20):
                yield TextDelta("重复计划。" * 20)
        finally:
            closed.append(True)
    monkeypatch.setattr(extract, "scheduled_client_stream", stream)
    with pytest.raises(OrchestrationError, match="原图和已有进度保持不变"):
        await extract.collect_model_text(object(), system="plan", user_text="task", max_generated_chars=150)
    assert closed


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_branch_join_and_nested_loop_run_the_declared_paths(tmp_path, nested):
    raw = spec_for().as_dict()
    raw["nodes"].extend([{"node_id": nid, "kind": kind, "label": nid, "goal": nid} for nid, kind in
                         [("choose", "decision"), ("left", "implement"), ("right", "implement")]])
    raw["edges"] = [edge for edge in raw["edges"] if not (edge["from_node"] == "work" and edge["to_node"] == "check")]
    raw["edges"].extend([{"from": "work", "to": "choose"}, {"from": "choose", "to": "left", "condition": "pass"},
                         {"from": "choose", "to": "right", "condition": "fail"}, {"from": "left", "to": "check"},
                         {"from": "right", "to": "check"}])
    raw["meta"]["loops"][0]["node_ids"] += ["choose", "left", "right"]
    if nested:
        raw["nodes"].append({"node_id": "inner_check", "kind": "decision", "label": "inner", "goal": "inner"})
        raw["edges"] = [edge for edge in raw["edges"] if (edge.get("from"), edge.get("to")) != ("left", "check")]
        raw["edges"] += [{"from": "left", "to": "inner_check"}, {"from": "inner_check", "to": "check", "condition": "pass"},
                         {"from": "inner_check", "to": "left", "condition": "loop"}]
        raw["meta"]["loops"][0]["node_ids"].append("inner_check")
        raw["meta"]["loops"].append({**raw["meta"]["loops"][0], "loop_id": "inner",
                                     "entry_node_id": "left", "check_node_id": "inner_check", "node_ids": ["left", "inner_check"]})
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    called = []
    async def runner(node, _model, _prompt, policy, member=None):
        called.append(node.node_id)
        if policy.loop_context and policy.loop_context.get("reviewer"):
            return ExpertRoleResult(text="evidence", input_tokens=10, loop_decision=report(policy.loop_context))
        return ExpertRoleResult(text=node.node_id, input_tokens=10, verdict={"verdict": "PASS"} if node.role == "auditor" else None)
    ref = ModelRef("test", "test", "")
    saved = {}
    def sink(event):
        if event.get("loop_state"):
            saved[event["loop_state_key"]] = copy.deepcopy(event["loop_state"])
    orchestrator = ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path, event_sink=sink)
    result = await orchestrator.run("task", ExpertProfile(ref, ref, ref), {}, strategy="stable", flow=flow)
    assert result.status == "completed" and "right" not in called
    assert called == ["prepare", "work", "choose", "left", *(["inner_check"] if nested else []), "check", "done"]
    assert saved["improve"]["tokens"] == (50 if nested else 40)


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["tokens", "time"])
async def test_budget_exhaustion_prevents_another_model_call(tmp_path, budget):
    raw = spec_for().as_dict()
    raw["meta"]["loops"][0]["token_budget"] = 2048
    raw["meta"]["loops"][0]["timeout_seconds"] = 1
    called = []
    async def runner(node, _model, _prompt, policy, member=None):
        called.append(node.node_id)
        if node.node_id == "work":
            if budget == "time":
                await asyncio.sleep(1.1)
            else:
                await policy.usage_callback(2048)
            return ExpertRoleResult(text="work", input_tokens=2048 if budget == "tokens" else 0)
        return ExpertRoleResult(text=node.node_id)
    ref = ModelRef("test", "test", "")
    orchestrator = ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path)
    result = await orchestrator.run("task", ExpertProfile(ref, ref, ref), {}, strategy="stable", flow=to_expert_flow(OrchestrationSpec.from_value(raw)))
    assert result.status != "completed" and called == ["prepare", "work"]
