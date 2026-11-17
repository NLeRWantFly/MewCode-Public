from __future__ import annotations

import copy

import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_rolling import (
    build_milestone_fragment,
    milestone_basis,
    parent_id_for,
)
from mewcode.permissions.sandbox import PathSandbox

from tests.test_autonomous_fragment import ALLOWED_TOOLS, _sample_flow
from tests.test_collaboration_mode import accept, core
from mewcode.orchestration.collab_compile import compile_plan

PARENT = parent_id_for("m2")


def _accept_all(value):
    value["planning_mode"] = "rolling"
    for contract in ("work", "deliver"):
        if contract in value["contracts"]:
            accept(value, contract)
    for milestone in value["milestones"].values():
        for contract in milestone["contracts"]:
            if contract not in ("work", "deliver") and contract in value["contracts"]:
                accept(value, contract)
        milestone["status"] = "accepted"
    return value


def _predecessor(value):
    for mid, milestone in value["milestones"].items():
        if "work" in milestone["contracts"]:
            return mid
    return next(iter(value["milestones"]))


def _rolling_value():
    value = _accept_all(ledger.initialize({"run_id": "a" * 32}, compile_plan(_sample_flow())))
    value["milestones"]["m2"] = {
        "id": "m2",
        "status": "unexpanded",
        "contracts": [],
        "depends_on": [_predecessor(value)],
        "goal": "撰写后续报告",
        "goal_ids": [],
        "requirement_ids": ["goal"],
        "constraint_ids": [],
        "checks": [{"id": "m2-review", "kind": "review", "version": 1,
                    "criteria": ["后续报告符合目标"]}],
        "capabilities": {"write_paths": ["work2"]},
        "tools": list(ALLOWED_TOOLS),
        "deny_tools": [],
        "inputs": [],
        "outputs": ["后续报告"],
    }
    return value


def _proposal(version=1, first_paths=None, second_paths=None, tools=None):
    first_paths = first_paths or ["work2/a"]
    second_paths = second_paths or ["work2/b"]
    return {
        "parent_node_id": PARENT,
        "expected_flow_version": version,
        "reason": "写入边界清晰，按目录拆分",
        "nodes": [
            {"node_id": "alpha", "label": "第一部分", "task": "撰写第一部分",
             "input_refs": ["parent"], "outputs": ["alpha-out"],
             "acceptance": ["第一部分完成"], "write_paths": list(first_paths),
             **({"tools": tools} if tools else {})},
            {"node_id": "beta", "label": "第二部分", "task": "撰写第二部分",
             "input_refs": ["parent"], "outputs": ["beta-out"],
             "acceptance": ["第二部分完成"], "write_paths": list(second_paths)},
        ],
        "edges": [{"from_node": "alpha", "to_node": "$join"},
                  {"from_node": "beta", "to_node": "$join"}],
        "join": {"label": "汇合", "acceptance": ["两部分齐备"]},
    }


def _build(value, flow_spec, tmp_path, proposal, milestone="m2"):
    return build_milestone_fragment(
        value, flow_spec, milestone, proposal, work_dir=tmp_path,
        sandbox=PathSandbox(str(tmp_path)), allowed_tools=ALLOWED_TOOLS)


def test_plan_expands_declared_milestone_purely(tmp_path):
    value = _rolling_value()
    flow_spec = _sample_flow()
    before_value = copy.deepcopy(value)
    before_flow = copy.deepcopy(flow_spec.as_dict())
    result = _build(value, flow_spec, tmp_path, _proposal())

    assert value == before_value, "编译不得修改协作账本"
    assert flow_spec.as_dict() == before_flow, "编译不得修改流程图"

    assert result["kind"] == "milestone_plan"
    assert result["format_revision"] == 1
    assert result["milestone_id"] == "m2"
    assert result["parent_id"] == PARENT and result["lineage_id"] == PARENT
    assert result["base_plan_version"] == value["plan_version"]
    assert "base_contract_version" not in result
    assert result["contract_ids"] == [*result["child_ids"], PARENT]
    assert result["basis"] == milestone_basis(value, flow_spec, "m2")

    parent = result["contracts"][PARENT]
    assert parent["version"] == 1
    assert parent["execution_kind"] == "join"
    assert parent["join_children"] == result["child_ids"]
    assert parent["milestone_id"] == "m2"
    milestone_checks = value["milestones"]["m2"]["checks"]
    assert parent["checks"][:len(milestone_checks)] == milestone_checks
    assert parent["checks"][-1]["criteria"] == ["两部分齐备"]
    assert parent["requirement_ids"] == ["goal"]

    for child_id in result["child_ids"]:
        child = result["contracts"][child_id]
        assert child["authorization_node_id"] == PARENT
        assert child["lineage_id"] == PARENT
        assert child["milestone_id"] == "m2"
        assert child["requirement_ids"] == ["goal"]
        assert child["expanded_from"]["contract_version"] == 1
        assert child["expanded_from"]["contract_id"] == PARENT


def test_candidate_flow_keeps_history_and_joins(tmp_path):
    value = _rolling_value()
    flow_spec = _sample_flow()
    result = _build(value, flow_spec, tmp_path, _proposal())
    candidate = ExpertFlowSpec.from_value(result["flow"])

    assert candidate.version == flow_spec.version + 1
    for old in ("work", "deliver"):
        assert old in candidate.node_map
    assert candidate.finalizer_node_id == PARENT
    nodes = [node["node_id"] for node in result["flow"]["nodes"]]
    for child_id in result["child_ids"]:
        assert child_id in nodes
    assert nodes.index("work") < nodes.index(PARENT)

    edges = [(edge["from_node"], edge["to_node"]) for edge in result["flow"]["edges"]]
    assert ("work", "deliver") in edges, "原有边必须保留"
    for child_id in result["child_ids"]:
        assert (child_id, PARENT) in edges
        assert (PARENT, child_id) not in edges
    for fro, to in edges:
        if to == PARENT:
            assert fro in result["child_ids"], "汇合节点只接受子节点输入"


def test_progress_milestone_without_extra_review_can_expand(tmp_path):
    from tests.test_autonomous_rolling_runtime import rolling_flow
    raw = rolling_flow().as_dict()
    milestone = next(row for row in raw['meta']['collaboration_spec']['document']['entries'] if row['id'] == 'm2')
    milestone['data'].pop('checks')
    compiled = compile_plan(ExpertFlowSpec.from_value(raw))
    assert compiled['milestones']['m2']['checks'] == []
    value = _rolling_value()
    value['milestones']['m2']['checks'] = []
    result = _build(value, _sample_flow(), tmp_path, _proposal())
    assert value['milestones']['m2']['checks'] == []
    assert len(result['contracts'][PARENT]['checks']) == 1
    assert result['contracts'][PARENT]['checks'][0]['criteria'] == ['两部分齐备']


def test_rejects_non_rolling_or_consumed_milestone(tmp_path):
    value = _rolling_value()
    value["planning_mode"] = "classic"
    with pytest.raises(ValueError):
        _build(value, _sample_flow(), tmp_path, _proposal())

    value = _rolling_value()
    value["milestones"]["m2"]["status"] = "accepted"
    with pytest.raises(ValueError):
        _build(value, _sample_flow(), tmp_path, _proposal())

    value = _rolling_value()
    value["milestones"]["m2"]["contracts"] = ["work"]
    with pytest.raises(ValueError):
        _build(value, _sample_flow(), tmp_path, _proposal())

    value = _rolling_value()
    with pytest.raises(ValueError):
        _build(value, _sample_flow(), tmp_path, _proposal(), milestone="ghost")


def test_rejects_missing_predecessor_and_stale_certificate(tmp_path):
    value = _rolling_value()
    value["milestones"]["m2"]["depends_on"] = ["ghost"]
    with pytest.raises(ValueError):
        _build(value, _sample_flow(), tmp_path, _proposal())

    value = _rolling_value()
    pred = _predecessor(value)
    value["milestones"][pred]["status"] = "unexpanded"
    with pytest.raises(ValueError):
        _build(value, _sample_flow(), tmp_path, _proposal())

    value = _rolling_value()
    pred = _predecessor(value)
    for contract in value["milestones"][pred]["contracts"]:
        value["certificates"][contract]["invalidated"] = "实际产物已变化"
    with pytest.raises(ValueError):
        _build(value, _sample_flow(), tmp_path, _proposal())


def test_rejects_out_of_scope_writes_and_forbidden_tools(tmp_path):
    with pytest.raises(Exception):
        _build(_rolling_value(), _sample_flow(), tmp_path,
               _proposal(first_paths=["outside"]))

    with pytest.raises(Exception):
        _build(_rolling_value(), _sample_flow(), tmp_path,
               _proposal(second_paths=["work2/a/b"]))

    with pytest.raises(Exception):
        _build(_rolling_value(), _sample_flow(), tmp_path,
               _proposal(tools=["ProposeNodeExpansion"]))


def test_candidate_cannot_forge_predecessor_certificate(tmp_path):
    value = _rolling_value()
    pred = _predecessor(value)
    contract = value["milestones"][pred]["contracts"][0]
    certificate = copy.deepcopy(value["certificates"][contract])
    _build(value, _sample_flow(), tmp_path, _proposal())
    assert value["certificates"][contract] == certificate
    assert ledger.valid_certificate(value, contract)


def test_basis_binds_actual_certificates_and_custom_finalizer(tmp_path):
    value = _rolling_value()
    flow = _sample_flow()
    original = milestone_basis(value, flow, "m2")
    value["certificates"]["deliver"]["report_hash"] = "different-fixed-evidence"
    assert milestone_basis(value, flow, "m2") != original
    raw = flow.as_dict()
    raw["finalizer_plugin"] = "custom"
    flow = ExpertFlowSpec.from_value(raw)
    fragment = _build(value, flow, tmp_path, _proposal())
    assert fragment["flow"]["finalizer_plugin"] == "custom"
