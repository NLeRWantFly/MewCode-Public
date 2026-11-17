"""Rolling-vs-eager planning for the collaboration compiler (tests not executed here)."""
from __future__ import annotations

import copy

import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_compile import compile_plan

BRIEFS = {
    "work": {"known_inputs": ["目标"], "outputs": ["候选"], "acceptance": ["候选符合原始目标"]},
    "deliver": {"known_inputs": ["候选"], "outputs": ["结果"], "acceptance": ["全量结果符合原始目标"]},
}
READY = [{"id": "later_ready", "kind": "review", "criteria": ["进入执行前重新编译远期范围"]}]


def entry(identity, kind, body, data=None):
    return {"id": identity, "kind": kind, "title": body, "body": body, "state": "active",
            "data": data or {}, "sources": [{"source_id": "user_1", "quote": body}]}


def goal(body="交付迭代成果"):
    return entry("goal", "goal", body)


def later_milestone(identity="m_later", body="远期迭代", **data):
    payload = {"deferred": True, "goal_ids": ["goal"], "checks": copy.deepcopy(READY)}
    payload.update(data)
    return entry(identity, "milestone", body, payload)


def flow(entries, *, planning_mode=None):
    entries = copy.deepcopy(entries)
    if planning_mode == "rolling":
        entries.append(entry("milestone_1", "milestone", "近期交付"))
    spec = {"format": ledger.FORMAT, "task_id": "task-one", "goal": "交付迭代成果",
            "document": {"entries": [copy.deepcopy(item) for item in entries]}}
    if planning_mode is not None:
        spec["planning_mode"] = planning_mode
    meta = {"orchestration_mode": "collaborative", "collaboration_spec": spec,
            "node_plan_briefs": copy.deepcopy(BRIEFS)}
    return ExpertFlowSpec.from_value({"flow_id": "rolling", "version": 1, "label": "滚动样例", "meta": meta,
        "nodes": [{"node_id": key, "role": "executor", "label": key, "responsibility": "交付迭代成果",
                   "failure_policy": {"action": "stop", "max_attempts": 1}} for key in ("work", "deliver")],
        "edges": [{"from_node": "work", "to_node": "deliver"}], "finalizer": "deliver"})


def test_rolling_compiles_recent_contracts_and_leaves_future_work_unexpanded():
    plan = compile_plan(flow([goal(), later_milestone()], planning_mode="rolling"))
    assert plan["format"] == "mewcode.collaboration.v2"
    assert plan["planning_mode"] == "rolling"
    assert set(plan["contracts"]) == {"work", "deliver"}
    later = plan["milestones"]["m_later"]
    assert later["contracts"] == [] and later["status"] == "unexpanded"
    assert "goal" in later["requirement_ids"] and later["constraint_ids"] == []
    from mewcode.kernel.contracts import checks
    assert later["checks"] == checks(READY)
    assert later["checks"][0]["version"] == 1
    assert {contract["milestone_id"] for contract in plan["contracts"].values()} == {"milestone_1"}
    assert plan["milestones"]["milestone_1"]["status"] == "planned"
    assert all(item["status"] != "accepted" for item in plan["milestones"].values())
    assert plan["final_contract_id"] == "deliver"


def test_rolling_keeps_recent_contract_shape_identical_to_eager():
    eager = compile_plan(flow([goal()]))
    rolling = compile_plan(flow([goal()], planning_mode="rolling"))
    assert rolling["contracts"] == eager["contracts"]
    assert "requirement_ids" not in eager["milestones"]["milestone_1"]
    recent = rolling["milestones"]["milestone_1"]
    assert recent["contracts"] == eager["milestones"]["milestone_1"]["contracts"]
    assert recent["status"] == eager["milestones"]["milestone_1"]["status"] == "planned"
    assert set(recent["requirement_ids"]) == {"goal"}


def test_rolling_does_not_invent_a_milestone_for_ungrouped_work():
    raw = flow([goal()], planning_mode="rolling").as_dict()
    raw["meta"]["collaboration_spec"]["document"]["entries"] = [goal()]
    with pytest.raises(ValueError, match="已声明的里程碑"):
        compile_plan(ExpertFlowSpec.from_value(raw))


def test_legacy_eager_output_is_unchanged():
    plan = compile_plan(flow([goal()]))
    assert plan["format"] == ledger.FORMAT and "planning_mode" not in plan
    assert plan["milestones"] == {"milestone_1": {"id": "milestone_1", "contracts": ["work", "deliver"],
        "goal": "", "depends_on": [], "checks": [], "status": "planned"}}
    assert plan["contracts"]["deliver"]["depends_on"] == ["work"]
    assert plan["policy"] == {"max_attempts": 3, "max_model_calls": 60, "optional_questions_per_milestone": 2,
                              "max_review_calls": 600, "max_active_milestones": 2, "context_char_budget": 64000}


def test_eager_and_illegal_planning_modes_are_rejected():
    with pytest.raises(ValueError, match="远期里程碑"):
        compile_plan(flow([goal(), later_milestone()]))
    with pytest.raises(ValueError, match="规划模式"):
        compile_plan(flow([goal()], planning_mode="adaptive"))
    with pytest.raises(ValueError, match="规划模式"):
        compile_plan(flow([goal()], planning_mode=7))


def test_empty_milestone_without_deferred_flag_is_rejected_in_both_modes():
    empty = entry("m_empty", "milestone", "尚未定义的阶段")
    for planning_mode in (None, "rolling"):
        with pytest.raises(ValueError, match="尚未安排工作合同"):
            compile_plan(flow([goal(), empty], planning_mode=planning_mode))


def test_deferred_flag_must_be_bool_and_future_milestone_cannot_hold_contracts():
    for value in ("yes", 1, None, []):
        with pytest.raises(ValueError, match="布尔"):
            compile_plan(flow([goal(), later_milestone(deferred=value)], planning_mode="rolling"))
    attached = entry("work", "node", "交付迭代成果", {"contract": {"milestone_id": "m_later"}})
    with pytest.raises(ValueError, match="远期里程碑不能包含当前执行合同"):
        compile_plan(flow([goal(), later_milestone(), attached], planning_mode="rolling"))


def test_future_milestone_reference_declarations_are_validated():
    def rejected(entries, match):
        with pytest.raises(ValueError, match=match):
            compile_plan(flow([goal(), *entries], planning_mode="rolling"))
    rejected([later_milestone(goal_ids=[])], "非空 goal_ids")
    rejected([later_milestone(goal_ids="goal")], "goal_ids")
    rejected([later_milestone(requirement_ids=5)], "requirement_ids")
    rejected([entry("artifact_1", "artifact", "候选"), later_milestone(goal_ids=["artifact_1"])], "原始目标")
    rejected([later_milestone(requirement_ids=["ghost"])], "不存在")
    rejected([entry("limit_1", "constraint", "不得访问网络", {"strength": "hard"}),
              later_milestone(requirement_ids=["limit_1"])], "不能引用")
    rejected([entry("artifact_1", "artifact", "候选"), later_milestone(constraint_ids=["artifact_1"])], "只能引用约束")
    rejected([later_milestone(checks={})], "验收检查")
    rejected([later_milestone(checks=None)], "验收检查")
    rejected([later_milestone(checks=[{"id": "ready", "kind": "guess"}])], "验收检查类型无效")


def test_future_milestone_requires_explicit_goal():
    blank = later_milestone(body="   ")
    blank["title"] = "远期迭代"
    blank["sources"] = [{"source_id": "user_1", "quote": "远期迭代"}]
    with pytest.raises(ValueError, match="目标描述"):
        compile_plan(flow([goal(), blank], planning_mode="rolling"))


def test_milestone_scoped_requirements_are_covered_by_the_future_milestone():
    artifact = entry("artifact_1", "artifact", "候选集", {"scope": {"milestone_id": "m_later"}})
    limit = entry("limit_1", "constraint", "不得访问网络", {"scope": {"milestone_id": "m_later"}, "strength": "hard"})
    plan = compile_plan(flow([goal(), artifact, limit, later_milestone()], planning_mode="rolling"))
    later = plan["milestones"]["m_later"]
    assert set(later["requirement_ids"]) == {"goal", "artifact_1"}
    assert later["constraint_ids"] == ["limit_1"]
    assert all("artifact_1" not in contract["requirement_ids"] for contract in plan["contracts"].values())
    assert all("limit_1" not in contract["constraint_ids"] for contract in plan["contracts"].values())


def test_unknown_node_scope_and_unplanned_requirements_still_fail():
    ghost = entry("artifact_ghost", "artifact", "幽灵产出", {"scope": {"node_id": "ghost"}})
    for planning_mode in (None, "rolling"):
        with pytest.raises(ValueError, match="尚未安排完成位置"):
            compile_plan(flow([goal(), ghost], planning_mode=planning_mode))


def test_future_milestone_dependencies_reject_cycles_and_unknown_targets():
    cyclic = [later_milestone("m_a", "阶段甲", depends_on=["m_b"]),
              later_milestone("m_b", "阶段乙", depends_on=["m_a"])]
    with pytest.raises(ValueError, match="成环"):
        compile_plan(flow([goal(), *cyclic], planning_mode="rolling"))
    with pytest.raises(ValueError, match="不存在"):
        compile_plan(flow([goal(), later_milestone("m_c", "阶段丙", depends_on=["m_missing"])], planning_mode="rolling"))
    with pytest.raises(ValueError, match="depends_on"):
        compile_plan(flow([goal(), later_milestone(depends_on={"milestone_1": True})], planning_mode="rolling"))


def test_plan_is_a_deep_copy_and_never_touches_the_flow():
    spec = flow([goal(), later_milestone()], planning_mode="rolling")
    before = copy.deepcopy(spec.as_dict())
    plan = compile_plan(spec)
    plan["contracts"]["work"]["requirement_ids"].append("tampered")
    plan["milestones"]["m_later"]["contracts"].append("fake")
    plan["milestones"]["m_later"]["goal_ids"].append("tampered")
    plan["entries"]["goal"]["body"] = "改写"
    assert spec.as_dict() == before
    again = compile_plan(spec)
    assert "tampered" not in again["contracts"]["work"]["requirement_ids"]
    assert again["milestones"]["m_later"]["contracts"] == []
    assert again["milestones"]["m_later"]["goal_ids"] == ["goal"]
    assert again["entries"]["goal"]["body"] == "交付迭代成果"
