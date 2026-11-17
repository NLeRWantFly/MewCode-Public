from __future__ import annotations

import copy

import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_compile import compile_plan
from mewcode.orchestration.collab_fragment import build_expansion_fragment
from mewcode.permissions.sandbox import PathSandbox

from tests.test_collaboration_mode import RUN_ID

ALLOWED_TOOLS = ("ReadFile", "WriteFile", "EditFile", "Bash")


def _sample_flow(caps=None):
    caps = caps or {"write_paths": ["work"]}
    meta = {"orchestration_mode": "collaborative", "node_plan_briefs": {
        "work": {"known_inputs": ["目标"], "outputs": ["候选"], "acceptance": ["候选符合目标"]},
        "deliver": {"known_inputs": ["候选"], "outputs": ["结果"], "acceptance": ["结果符合目标"]}}}
    meta["collaboration_spec"] = {
        "format": ledger.FORMAT, "task_id": "task-one", "goal": "交付测试结果",
        "document": {"entries": [{"id": "goal", "kind": "goal", "title": "目标",
                                  "body": "交付测试结果", "state": "active", "data": {},
                                  "sources": [{"source_id": "user_1", "quote": "交付测试结果"}]}]}}
    nodes = [{"node_id": key, "role": "executor", "label": key, "responsibility": "交付测试结果",
              "capabilities": copy.deepcopy(caps) if key == "work" else {},
              "failure_policy": {"action": "stop", "max_attempts": 1}}
             for key in ("work", "deliver")]
    return ExpertFlowSpec.from_value({"flow_id": "sample", "version": 1, "label": "样例",
        "meta": meta, "nodes": nodes,
        "edges": [{"from_node": "work", "to_node": "deliver"}], "finalizer": "deliver"})


def _core():
    return ledger.initialize({"run_id": RUN_ID}, compile_plan(_sample_flow()))


def _proposal(parent="work", version=1, first_paths=None, second_paths=None):
    first_paths = first_paths or ["work/a"]
    second_paths = second_paths or ["work/b"]
    return {
        "parent_node_id": parent,
        "expected_flow_version": version,
        "reason": "写入边界清晰，按目录拆分",
        "nodes": [
            {"node_id": "first", "label": "第一部分", "task": "撰写第一部分",
             "input_refs": ["parent"], "outputs": ["first-out"],
             "acceptance": ["第一部分完成"], "write_paths": list(first_paths)},
            {"node_id": "second", "label": "第二部分", "task": "撰写第二部分",
             "input_refs": ["parent"], "outputs": ["second-out"],
             "acceptance": ["第二部分完成"], "write_paths": list(second_paths)},
        ],
        "edges": [{"from_node": "first", "to_node": "$join"},
                  {"from_node": "second", "to_node": "$join"}],
        "join": {"label": "汇合", "acceptance": ["两部分齐备"]},
    }


def _build(value, flow_spec, tmp_path, proposal, parent="work", attempt=1):
    return build_expansion_fragment(
        value, flow_spec, parent, proposal, work_dir=tmp_path,
        sandbox=PathSandbox(str(tmp_path)), allowed_tools=ALLOWED_TOOLS, node_attempt=attempt)


def test_expansion_upgrades_parent_and_adds_children(tmp_path):
    value = _core()
    flow_spec = _sample_flow()
    before = copy.deepcopy(value)
    result = _build(value, flow_spec, tmp_path, _proposal())

    assert value == before, "编译不得修改协作账本"
    assert result["kind"] == "contract_expansion"
    assert result["parent_id"] == "work"
    assert result["basis"] == ledger.dependencies(value, "work")
    assert result["base_plan_version"] == value["plan_version"]
    assert result["base_contract_version"] == 1
    assert result["expansion_id"].startswith("mx-")
    child_ids = [f"work::{result['expansion_id']}::{local}" for local in ("first", "second")]
    assert result["child_ids"] == child_ids

    parent = result["contracts"]["work"]
    assert parent["version"] == 2
    assert parent["execution_kind"] == "join"
    assert parent["join_children"] == child_ids
    assert parent["depends_on"] == child_ids
    assert "bound_dependencies" not in parent and "held" not in parent
    checks = value["contracts"]["work"]["checks"]
    assert parent["checks"][:len(checks)] == checks
    assert parent["checks"][-1]["criteria"] == ["两部分齐备"]

    first = result["contracts"][child_ids[0]]
    assert first["id"] == child_ids[0] and first["node_id"] == child_ids[0]
    assert first["version"] == 1
    assert first["goal"] == "撰写第一部分"
    assert first["inputs"] == ["parent"] and first["deliverables"] == ["first-out"]
    assert first["lineage_id"] == "work"
    assert first["expanded_from"] == {"contract_id": "work", "contract_version": 1,
                                      "expansion_id": result["expansion_id"]}
    assert first["expansion_depth"] == 1
    assert first["authorization_node_id"] == "work"
    assert first["requirement_ids"] == value["contracts"]["work"]["requirement_ids"]
    assert first["checks"][0]["criteria"] == ["第一部分完成"]
    assert first["tools"] == sorted(set(ALLOWED_TOOLS))
    assert first["depends_on"] == []
    second = result["contracts"][child_ids[1]]
    assert second["depends_on"] == []

    expanded = ExpertFlowSpec.from_value(result["flow"])
    assert expanded.version == 2
    assert expanded.node_map["work"].role == flow_spec.node_map["work"].role
    assert expanded.node_map["work"].members == flow_spec.node_map["work"].members
    for child_id in child_ids:
        node = expanded.node_map[child_id]
        assert node.members == () and node.kind == "role"
        assert node.failure_policy.action == "stop" and node.failure_policy.max_attempts == 1
        assert "::" in node.node_id
    nodes = [node["node_id"] for node in result["flow"]["nodes"]]
    assert nodes.index(child_ids[0]) < nodes.index("work") < nodes.index("deliver")
    edges = [(edge["from_node"], edge["to_node"]) for edge in result["flow"]["edges"]]
    assert ("work", "deliver") in edges, "下游出口必须保留"
    assert all(("deliver", child_id) not in edges for child_id in child_ids)
    for child_id in child_ids:
        assert (child_id, "work") in edges
    assert ("work", child_ids[0]) not in edges
    assert result["normalized_write_paths"]["first"] == list(first["write_paths"])


def test_nested_expansion_inherits_lineage_and_stops_at_depth_two(tmp_path):
    value = _core()
    first = _build(value, _sample_flow(), tmp_path, _proposal())
    child_ids = first["child_ids"]
    root = child_ids[0]
    value["contracts"].update(copy.deepcopy(first["contracts"]))
    flow_spec = ExpertFlowSpec.from_value(copy.deepcopy(first["flow"]))

    nested = _build(value, flow_spec, tmp_path,
                    _proposal(parent=root, version=2, first_paths=["work/a/x"],
                              second_paths=["work/a/y"]), parent=root)
    assert nested["lineage_id"] == "work"
    grand = nested["child_ids"]
    assert all(grand_id.startswith(root + "::") for grand_id in grand)
    assert nested["contracts"][root]["version"] == 2
    assert nested["contracts"][grand[0]]["authorization_node_id"] == "work"

    value["contracts"].update(copy.deepcopy(nested["contracts"]))
    flow_spec = ExpertFlowSpec.from_value(copy.deepcopy(nested["flow"]))
    with pytest.raises(ValueError):
        _build(value, flow_spec, tmp_path,
               _proposal(parent=grand[0], version=3, first_paths=["work/a/x/p"],
                         second_paths=["work/a/x/q"]), parent=grand[0])


def test_rejects_overlapping_sibling_write_scopes(tmp_path):
    with pytest.raises(Exception) as caught:
        _build(_core(), _sample_flow(), tmp_path, _proposal(second_paths=["work/a/b"]))
    assert "重叠" in str(caught.value)


def test_rejects_write_scope_outside_parent_capability(tmp_path):
    with pytest.raises(Exception) as caught:
        _build(_core(), _sample_flow(), tmp_path, _proposal(first_paths=["outside"]))
    assert "outside" in str(caught.value) or "范围" in str(caught.value)


def test_rejects_forbidden_and_unauthorized_tools(tmp_path):
    proposal = _proposal()
    proposal["nodes"][0]["tools"] = ["ProposeNodeExpansion"]
    with pytest.raises(Exception) as caught:
        _build(_core(), _sample_flow(), tmp_path, proposal)
    assert "ProposeNodeExpansion" in str(caught.value)

    proposal = _proposal()
    proposal["nodes"][0]["tools"] = ["UnknownTool"]
    with pytest.raises(Exception) as caught:
        _build(_core(), _sample_flow(), tmp_path, proposal)
    assert "UnknownTool" in str(caught.value)


def test_rejects_unknown_cycle_and_join_targets(tmp_path):
    with pytest.raises(ValueError):
        _build(_core(), _sample_flow(), tmp_path, _proposal(parent="ghost"), parent="ghost")

    cycle = _proposal()
    cycle["edges"] = [{"from_node": "first", "to_node": "second"},
                      {"from_node": "second", "to_node": "first"}]
    with pytest.raises(Exception):
        _build(_core(), _sample_flow(), tmp_path, cycle)

    joined = _core()
    joined["contracts"]["work"]["execution_kind"] = "join"
    with pytest.raises(ValueError):
        _build(joined, _sample_flow(), tmp_path, _proposal())


def test_rejects_conditional_incoming_edges(tmp_path):
    base = _sample_flow()
    spec = base.as_dict()
    spec["nodes"] = [{"node_id": "intake", "role": "executor", "label": "intake",
                      "responsibility": "收拢输入", "capabilities": {},
                      "failure_policy": {"action": "stop", "max_attempts": 1}}] + spec["nodes"]
    spec["meta"]["node_plan_briefs"]["intake"] = {
        "known_inputs": ["目标"], "outputs": ["候选"], "acceptance": ["候选符合目标"]}
    spec["edges"] = [{"from_node": "intake", "to_node": "work", "condition": "always"},
                     {"from_node": "work", "to_node": "deliver", "condition": "always"}]
    flow_spec = ExpertFlowSpec.from_value(spec)
    value = ledger.initialize({"run_id": RUN_ID}, compile_plan(flow_spec))

    spec["edges"][0] = {"from_node": "intake", "to_node": "work", "condition": "success"}
    conditional = ExpertFlowSpec.from_value(copy.deepcopy(spec))
    with pytest.raises(ValueError):
        _build(value, conditional, tmp_path, _proposal())


def test_children_share_the_parent_upstream_inputs(tmp_path):
    base = _sample_flow()
    spec = base.as_dict()
    spec["nodes"] = [{"node_id": "intake", "role": "executor", "label": "intake",
                      "responsibility": "收拢输入", "capabilities": {},
                      "failure_policy": {"action": "stop", "max_attempts": 1}}] + spec["nodes"]
    spec["meta"]["node_plan_briefs"]["intake"] = {
        "known_inputs": ["目标"], "outputs": ["候选"], "acceptance": ["候选符合目标"]}
    spec["edges"] = [{"from_node": "intake", "to_node": "work", "condition": "always"},
                     {"from_node": "work", "to_node": "deliver", "condition": "always"}]
    flow_spec = ExpertFlowSpec.from_value(spec)
    value = ledger.initialize({"run_id": RUN_ID}, compile_plan(flow_spec))
    result = _build(value, flow_spec, tmp_path, _proposal())

    edges = [(edge["from_node"], edge["to_node"]) for edge in result["flow"]["edges"]]
    assert ("intake", "work") not in edges, "父的旧输入边必须重定向到子节点"
    for child_id in result["child_ids"]:
        assert ("intake", child_id) in edges
        assert (child_id, "work") in edges
    assert ("work", "deliver") in edges
    assert all(result["contracts"][key]["depends_on"] == ["intake"] for key in result["child_ids"])


def test_sequential_children_preserve_internal_dependencies_and_stable_ids(tmp_path):
    proposal = _proposal()
    proposal["edges"] = [{"from_node": "first", "to_node": "second"},
                         {"from_node": "second", "to_node": "$join"}]
    value = _core()
    result = _build(value, _sample_flow(), tmp_path, proposal)
    assert result == _build(value, _sample_flow(), tmp_path, proposal)
    first, second = result["child_ids"]
    assert result["contracts"][first]["depends_on"] == []
    assert result["contracts"][second]["depends_on"] == [first]
    assert (first, second) in [(e["from_node"], e["to_node"]) for e in result["flow"]["edges"]]
    proposal["nodes"][1]["write_paths"] = ["work/a/nested"]
    with pytest.raises(ValueError, match="重叠"):
        _build(value, _sample_flow(), tmp_path, proposal)


def test_denied_tools_and_user_constraints_are_inherited(tmp_path):
    value = _core()
    parent = value["contracts"]["work"]
    parent["deny_tools"] = ["*"]
    parent["forbidden_changes"] = ["不得修改发布配置"]
    parent["method"] = "依照用户指定的验证方法"
    result = _build(value, _sample_flow(), tmp_path, _proposal())
    for key in result["child_ids"]:
        assert result["contracts"][key]["tools"] == []
        assert result["contracts"][key]["deny_tools"] == ["*"]
        assert result["contracts"][key]["forbidden_changes"] == parent["forbidden_changes"]
        assert result["contracts"][key]["method"] == parent["method"]
    proposal = _proposal()
    proposal["nodes"][0]["tools"] = ["ReadFile"]
    with pytest.raises(ValueError):
        _build(value, _sample_flow(), tmp_path, proposal)
