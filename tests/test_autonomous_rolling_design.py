import copy

import pytest

from mewcode.orchestration.collab_compile import attach_design, compile_plan
from mewcode.orchestration.workflow_draft import DraftPatchError, entry_contract, validate_entry
from tests.test_autonomous_rolling_plan import entry, flow, goal, later_milestone


def test_deferred_design_survives_host_schema_and_enters_rolling_compiler():
    later = later_milestone(capabilities={"write_paths": ["later/"]}, tools=["WriteFile"],
                           deny_tools=["Bash"], inputs=["阶段一交付"], outputs=["阶段二交付"])
    validated = validate_entry(later, orchestration_mode="collaborative")
    assert validated["data"] == later["data"]
    assert "deferred" in entry_contract("collaborative")["data_fields"]["milestone"]
    spec = flow([goal()])
    document = {"entries": [goal(), entry("milestone_1", "milestone", "近期交付"), validated]}
    draft = {"candidate": {"document": document}}
    original = copy.deepcopy(draft)
    attach_design(spec, {"orchestration_mode": "collaborative", "task_id": "task-one", "goal": "交付迭代成果"}, draft)
    assert draft == original
    plan = compile_plan(spec)
    milestone = plan["milestones"]["m_later"]
    assert plan["planning_mode"] == "rolling"
    assert milestone["status"] == "unexpanded" and milestone["contracts"] == []
    assert milestone["capabilities"]["write_paths"] == ["later/"]
    assert milestone["tools"] == ["WriteFile"] and milestone["deny_tools"] == ["Bash"]
    assert milestone["inputs"] == ["阶段一交付"] and milestone["outputs"] == ["阶段二交付"]


@pytest.mark.parametrize("patch", [{"deferred": 1}, {"requirement_ids": ["bad.id"]},
                                   {"constraint_ids": ["same", "same"]}, {"tools": "WriteFile"},
                                   {"capabilities": {"command_execution": "yes"}}])
def test_design_rejects_invalid_future_fields(patch):
    row = later_milestone()
    row["data"].update(patch)
    with pytest.raises(DraftPatchError):
        validate_entry(row, orchestration_mode="collaborative")


def test_future_work_inherits_global_constraints_and_requirements():
    required = entry("artifact", "artifact", "所有阶段保留来源")
    global_rule = entry("rule", "constraint", "禁止访问网络", {"scope": "global"})
    other = entry("local", "constraint", "仅约束近期节点", {"scope": {"node_id": "work"}})
    plan = compile_plan(flow([goal(), required, global_rule, other, later_milestone()], planning_mode="rolling"))
    milestone = plan["milestones"]["m_later"]
    assert {"goal", "artifact"} <= set(milestone["requirement_ids"])
    assert milestone["constraint_ids"] == ["rule"]


def test_ordinary_design_keeps_legacy_eager_protocol():
    spec = flow([goal()])
    attach_design(spec, {"orchestration_mode": "collaborative", "task_id": "task-one"},
                  {"document": {"entries": [goal()]}})
    assert "planning_mode" not in spec.meta["collaboration_spec"]
    assert "planning_mode" not in compile_plan(spec)
    with pytest.raises(DraftPatchError):
        validate_entry(later_milestone(), orchestration_mode="classic")
