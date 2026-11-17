"""Stage supervision has real graph coverage without hidden model dispatches."""
import copy

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertProfile, ExpertRoleResult
from mewcode.model_registry import ModelRef
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.graph_readiness import readiness
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.orchestration.supervision import supervision_diagnostics
from mewcode.orchestration.workflow_draft import seed_document


def spec(*, edges=None, kinds=None, finalizer="finish", meta=None):
    kinds = kinds or [("plan", "plan"), ("work", "implement"),
                      ("audit", "audit"), ("finish", "reduce")]
    return OrchestrationSpec.from_value({
        "spec_id": "supervised", "label": "阶段监管", "finalizer_node_id": finalizer,
        "nodes": [{"node_id": key, "kind": kind, "label": key, "goal": key,
                   "model": {"provider_key": "configured", "model_id": "worker" if kind == "implement" else "primary"},
                   "plan_brief": {"outputs": [key + "产物"], "acceptance": [key + "符合要求"]}}
                  for key, kind in kinds],
        "edges": edges if edges is not None else [
            {"from": "plan", "to": "work"}, {"from": "work", "to": "audit"},
            {"from": "audit", "to": "finish", "condition": "pass"}],
        "meta": meta or {},
    })


def codes(value):
    return {row["code"] for row in supervision_diagnostics(value)}


def test_plain_execution_and_summary_are_not_supervised():
    value = spec(kinds=[("work", "implement"), ("finish", "reduce")],
                 edges=[{"from": "work", "to": "finish"}])
    before = copy.deepcopy(value.as_dict())
    assert codes(value) == {"missing_direction_checkpoint", "missing_result_review"}
    assert value.as_dict() == before


def test_existing_plan_and_batch_audit_cover_related_stages():
    value = spec(kinds=[("plan", "plan"), ("a", "implement"), ("b", "implement"),
                       ("audit", "audit"), ("finish", "reduce")], edges=[
        {"from": "plan", "to": "a"}, {"from": "plan", "to": "b"},
        {"from": "a", "to": "audit"}, {"from": "b", "to": "audit"},
        {"from": "audit", "to": "finish", "condition": "pass"}])
    assert codes(value) == set()
    assert len(to_expert_flow(value).nodes) == 5


@pytest.mark.parametrize("edge", [
    {"from": "work", "to": "audit", "condition": "fail"},
    {"from": "work", "to": "audit", "input_when": {"input": "review", "equals": "yes"}},
])
def test_failure_only_or_optional_review_does_not_cover_success(edge):
    value = spec(edges=[{"from": "plan", "to": "work"}, edge,
                       {"from": "work", "to": "finish"},
                       {"from": "audit", "to": "finish"}])
    # Supply the declared input required by the executable graph validator.
    if "input_when" in edge:
        raw = value.as_dict()
        raw["goal_contract"] = {"run_inputs": [{"name": "review", "description": "review", "required": True}]}
        value = OrchestrationSpec.from_value(raw)
    assert "missing_result_review" in codes(value)


def test_exhaustive_input_branches_that_join_audit_cover_success():
    value = spec(kinds=[("plan", "plan"), ("work", "implement"),
                        ("yes_path", "implement"), ("no_path", "implement"),
                        ("audit", "audit"), ("finish", "reduce")], edges=[
        {"from": "plan", "to": "work"},
        {"from": "work", "to": "yes_path", "condition": "pass",
         "input_when": {"input": "route", "equals": "yes"}, "branch_group": "routes"},
        {"from": "work", "to": "no_path", "condition": "pass",
         "input_when": {"input": "route", "equals": "no"}, "branch_group": "routes"},
        {"from": "yes_path", "to": "audit"}, {"from": "no_path", "to": "audit"},
        {"from": "audit", "to": "finish", "condition": "pass"}])
    raw = value.as_dict()
    raw["goal_contract"] = {"run_inputs": [{"name": "route", "required": True,
        "options": [{"value": "yes", "label": "是"}, {"value": "no", "label": "否"}]}]}
    value = OrchestrationSpec.from_value(raw)

    assert codes(value) == set()


def test_exhaustive_branch_with_audit_bypass_does_not_cover_success():
    value = spec(kinds=[("plan", "plan"), ("work", "implement"),
                        ("audit", "audit"), ("finish", "reduce")], edges=[
        {"from": "plan", "to": "work"},
        {"from": "work", "to": "audit", "condition": "pass",
         "input_when": {"input": "route", "equals": "review"}, "branch_group": "routes"},
        {"from": "work", "to": "finish", "condition": "pass",
         "input_when": {"input": "route", "equals": "skip"}, "branch_group": "routes"},
        {"from": "audit", "to": "finish", "condition": "pass"}])
    raw = value.as_dict()
    raw["goal_contract"] = {"run_inputs": [{"name": "route", "required": True,
        "options": [{"value": "review", "label": "审查"}, {"value": "skip", "label": "跳过"}]}]}
    value = OrchestrationSpec.from_value(raw)

    assert "missing_result_review" in codes(value)


@pytest.mark.parametrize("independent_delivery", [False, True])
def test_single_guard_only_covers_review_when_required_for_all_delivery(independent_delivery):
    kinds = [("plan", "plan"), ("work", "implement"), ("gate", "gate"),
             ("audit", "audit"), ("finish", "reduce")]
    edges = [{"from": "plan", "to": "work"}, {"from": "work", "to": "gate"},
             {"from": "gate", "to": "audit", "condition": "pass",
              "input_when": {"input": "permission", "equals": "allow"}},
             {"from": "audit", "to": "finish", "condition": "pass"}]
    if independent_delivery:
        kinds.append(("independent", "plan"))
        edges.append({"from": "independent", "to": "finish"})
    raw = spec(kinds=kinds, edges=edges).as_dict()
    raw["goal_contract"] = {"run_inputs": [{"name": "permission", "required": True,
        "default": None, "collect_at": "gate",
        "options": [{"value": "allow", "label": "允许"}, {"value": "deny", "label": "拒绝"}]}]}
    value = OrchestrationSpec.from_value(raw)
    before = copy.deepcopy(value.as_dict())
    assert ("missing_result_review" in codes(value)) is independent_delivery
    assert value.as_dict() == before


def test_single_guard_cannot_hide_an_unconditional_delivery_bypass():
    raw = spec(edges=[{"from": "plan", "to": "work"},
                     {"from": "work", "to": "audit", "condition": "pass",
                      "input_when": {"input": "permission", "equals": "allow"}},
                     {"from": "work", "to": "finish"},
                     {"from": "audit", "to": "finish", "condition": "pass"}]).as_dict()
    raw["goal_contract"] = {"run_inputs": [{"name": "permission", "required": True,
        "options": [{"value": "allow", "label": "允许"}, {"value": "deny", "label": "拒绝"}]}]}
    assert "missing_result_review" in codes(OrchestrationSpec.from_value(raw))


def test_unrelated_audit_and_human_gate_are_not_quality_supervision():
    value = spec(kinds=[("plan", "gate"), ("work", "implement"),
                       ("audit", "audit"), ("finish", "reduce")], edges=[
        {"from": "plan", "to": "work"}, {"from": "plan", "to": "audit"},
        {"from": "work", "to": "finish"}, {"from": "audit", "to": "finish"}])
    assert codes(value) == {"missing_direction_checkpoint", "missing_result_review"}


def test_plain_branch_decision_is_not_an_independent_result_review():
    value = spec(kinds=[("plan", "plan"), ("work", "implement"),
                       ("audit", "decision"), ("finish", "reduce")])
    assert codes(value) == {"missing_result_review"}


def test_declared_review_to_repair_loop_supplies_repair_direction():
    value = spec(kinds=[("initial", "audit"), ("work", "implement"),
                       ("audit", "decision"), ("finish", "reduce")], edges=[
        {"from": "initial", "to": "audit"}, {"from": "work", "to": "audit"},
        {"from": "audit", "to": "work", "condition": "loop"},
        {"from": "audit", "to": "finish", "condition": "pass"}], meta={"loops": [{
            "loop_id": "repair", "entry_node_id": "initial", "check_node_id": "audit",
            "node_ids": ["initial", "work", "audit"], "reviewer_node_ids": ["audit"],
            "repair_node_ids": ["work"], "requires_repair": True, "repair_dispatch": "after_decision",
            "control_mode": "team", "exit_condition": "符合冻结标准",
            "criteria": [{"criterion_id": "C1", "acceptance_test": "符合目标"}],
        }]})
    assert codes(value) == set()


def test_readiness_reports_supervision_gap_without_editing_the_graph():
    value = spec(kinds=[("plan", "plan"), ("work", "implement"), ("finish", "reduce")],
                 edges=[{"from": "plan", "to": "work"}, {"from": "work", "to": "finish"}])
    document = seed_document(value.as_dict())
    before = copy.deepcopy(document)
    result = readiness(document, value.as_dict(), conversation_id="supervision", revision=1)
    assert not result["ready"]
    assert "missing_result_review" in {row["code"] for row in result["diagnostics"]}
    assert document == before


def test_coordination_parent_is_not_an_extra_executor():
    value = spec(kinds=[("parent", "implement"), ("work", "implement"),
                       ("audit", "audit"), ("finish", "reduce")], edges=[
        {"from": "parent", "to": "work"}, {"from": "work", "to": "audit"},
        {"from": "audit", "to": "finish"}], meta={"workflow_draft": {
            "coordination_nodes": ["parent"], "parents": {"work": "parent"}}})
    assert codes(value) == set()


def test_review_coordinator_without_child_review_cannot_approve_work():
    value = spec(meta={"workflow_draft": {
        "coordination_nodes": ["audit"], "parents": {"work": "audit"}}})
    assert codes(value) == {"missing_result_review"}


@pytest.mark.parametrize("verdict,expected", [
    ({"verdict": "PASS", "issues": [], "evidence": []}, "completed"),
    ({"verdict": "NEEDS_ATTENTION", "issues": [{"issue_id": "I1", "description": "wrong result"}]}, "needs_attention"),
    (None, "needs_attention"),
])
@pytest.mark.asyncio
async def test_terminal_auditor_requires_verdict_and_reuses_existing_dispatch(tmp_path, verdict, expected):
    value = spec(kinds=[("plan", "plan"), ("work", "implement"), ("audit", "audit")],
                 edges=[{"from": "plan", "to": "work"}, {"from": "work", "to": "audit"}],
                 finalizer="audit")
    calls, events = [], []

    async def runner(node, model, prompt, policy, member):
        calls.append((node.node_id, model.model_id, prompt))
        return ExpertRoleResult(text="stage result", verdict=verdict if node.role == "auditor" else None)

    async def fallback(*_args):
        raise AssertionError("must use explicit node route")

    model = ModelRef("configured", "profile-default")
    orchestrator = ExpertGroupOrchestrator(runner=fallback, node_runner=runner,
                                          event_sink=events.append,
                                          archive_root=tmp_path / "archives")
    result = await orchestrator.run("implement", ExpertProfile(model, model, model, model),
                                    {"providers": []}, strategy="stable", flow=to_expert_flow(value))
    assert result.status == expected
    assert [(key, model_id) for key, model_id, _ in calls] == [
        ("plan", "primary"), ("work", "worker"), ("audit", "primary")]
    assert "输入范围、保留范围、交付物和验证方法" in calls[0][2]
    assert "候选成果由独立审查者验收" in calls[1][2]
    assert "SubmitVerdict" in calls[2][2]
    assert "复审聚焦变化集" in calls[2][2]
    completion = next(event for event in events if event.get("type") == "expert_node_completed"
                      and event.get("node_id") == "audit")
    assert completion["passed"] is (expected == "completed")


@pytest.mark.asyncio
async def test_resume_does_not_restore_terminal_completion_without_verdict(tmp_path):
    value = spec(kinds=[("plan", "plan"), ("work", "implement"), ("audit", "audit")],
                 edges=[{"from": "plan", "to": "work"}, {"from": "work", "to": "audit"}],
                 finalizer="audit")
    calls = []

    async def runner(node, model, prompt, policy, member):
        calls.append(node.node_id)
        return ExpertRoleResult(text="reviewed", verdict={"verdict": "PASS", "issues": []})

    async def fallback(*_args):
        raise AssertionError("unexpected route")

    checkpoint = {"node_states": {key: {"status": "completed", "text": key}
                                   for key in ("plan", "work", "audit")}}
    model = ModelRef("configured", "primary")
    orchestrator = ExpertGroupOrchestrator(runner=fallback, node_runner=runner,
                                          archive_root=tmp_path / "archives")
    result = await orchestrator.run("implement", ExpertProfile(model, model, model, model),
                                    {"providers": []}, strategy="stable", flow=to_expert_flow(value),
                                    checkpoint=checkpoint)
    assert result.status == "completed"
    assert calls == ["audit"]


@pytest.mark.asyncio
async def test_plugin_finalizer_cannot_bypass_audit_acceptance(tmp_path):
    from mewcode.experts.flow import ExpertFlowSpec

    flow = ExpertFlowSpec.from_value({
        "flow_id": "plugin-audit", "label": "Plugin audit", "version": 1,
        "nodes": [{"node_id": "audit", "label": "Audit", "role": "auditor"}],
        "edges": [], "finalizer_node_id": "audit", "finalizer_plugin": "custom",
    })
    events = []

    async def forbidden(*_args):
        raise AssertionError("plugin owns the finalizer")

    model = ModelRef("configured", "primary")
    orchestrator = ExpertGroupOrchestrator(runner=forbidden, event_sink=events.append,
                                          archive_root=tmp_path / "archives")
    result = await orchestrator.run("review", ExpertProfile(model, model, model, model),
        {"providers": []}, strategy="stable", flow=flow,
        finalizers={"custom": lambda **_kwargs: {"text": "done", "status": "completed"}})
    assert result.status == "needs_attention"
    assert next(event for event in events if event.get("type") == "expert_node_completed")["passed"] is False
