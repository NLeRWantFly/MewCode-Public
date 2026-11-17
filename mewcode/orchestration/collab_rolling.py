"""Pure rolling-milestone expansion compiler.

A rolling collaboration ledger grows one explicitly declared milestone at a
time.  Given the ledger value, the current flow, a milestone id and the raw
proposal the parent model produced, this module derives a candidate JSON
document holding the upgraded milestone join, its child contracts and the
expanded flow.  It reuses :func:`build_expansion_fragment` for the child graph
and never calls a model, never mutates its inputs and never publishes the
graph; the host wires the candidate in later.
"""
from __future__ import annotations

import copy
from pathlib import Path

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_compile import validate_contracts
from mewcode.orchestration.collab_fragment import build_expansion_fragment

FORMAT_REVISION = 1
PARENT_PREFIX = "milestone::"
_JOIN_FORBIDDEN = ["不得修改用户目标、验收、授权或未批准的指定方法"]


def parent_id_for(milestone_id) -> str:
    return PARENT_PREFIX + str(milestone_id)


def milestone_basis(value, flow, milestone_id: str) -> str:
    """Digest the read set this milestone expansion is based on."""
    milestones = value.get("milestones") or {}
    if milestone_id not in milestones:
        raise ValueError(f"里程碑 {milestone_id} 不在协作账本中")
    milestone = milestones[milestone_id]
    entries = value.get("entries") or {}
    contracts = value.get("contracts") or {}

    selected: dict[str, object] = {}
    for ref in list(milestone.get("requirement_ids", [])) + list(milestone.get("constraint_ids", [])):
        if ref not in entries:
            raise ValueError(f"里程碑 {milestone_id} 引用了不存在的条目 {ref}")
        selected[ref] = copy.deepcopy(entries[ref])

    predecessors: dict[str, object] = {}
    certificates: dict[str, object] = {}
    for dep in milestone.get("depends_on", []):
        if dep not in milestones:
            raise ValueError(f"里程碑 {milestone_id} 的前序 {dep} 不存在")
        predecessors[dep] = copy.deepcopy(milestones[dep])
        for contract in milestones[dep].get("contracts", []):
            if contract not in contracts:
                raise ValueError(f"前序合同 {contract} 不存在")
            certificates[contract] = copy.deepcopy(value["certificates"].get(contract))

    return ledger.digest({
        "milestone": copy.deepcopy(milestone),
        "entries": selected,
        "predecessors": predecessors,
        "certificates": certificates,
        "flow_hash": ledger.digest(flow.as_dict()),
        "plan_version": value.get("plan_version"),
        "coordinator_epoch": value.get("coordinator_epoch"),
    })


def _require_ready(value, milestone_id: str) -> dict:
    if value.get("planning_mode") != "rolling":
        raise ValueError("只有 rolling 规划模式支持纯滚动里程碑展开")
    milestones = value.get("milestones") or {}
    if milestone_id not in milestones:
        raise ValueError(f"里程碑 {milestone_id} 不在协作账本中")
    milestone = milestones[milestone_id]
    if milestone.get("status") != "unexpanded":
        raise ValueError(f"里程碑 {milestone_id} 状态为 {milestone.get('status')}，不能展开")
    if milestone.get("contracts"):
        raise ValueError(f"里程碑 {milestone_id} 已安排工作合同，不能展开")
    contracts = value.get("contracts") or {}
    for dep in milestone.get("depends_on", []):
        if dep not in milestones:
            raise ValueError(f"里程碑 {milestone_id} 的前序 {dep} 不存在")
        if milestones[dep].get("status") != "accepted":
            raise ValueError(f"前序里程碑 {dep} 尚未验收")
        for contract in milestones[dep].get("contracts", []):
            if contract not in contracts or not ledger.valid_certificate(value, contract):
                raise ValueError(f"前序合同 {contract} 缺少有效证书")
    return milestone


def _temporary_parent(value, milestone: dict, parent_id: str) -> dict:
    checks = milestone.get("checks")
    if not isinstance(checks, list):
        raise ValueError("里程碑检查声明类型无效")
    capabilities = milestone.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("里程碑缺少能力声明，不能展开")
    tools = milestone.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise ValueError("里程碑 tools 声明类型无效")
    depends_on = sorted({contract for dep in milestone.get("depends_on", [])
                         for contract in (value["milestones"][dep].get("contracts") or [])})
    return {
        "id": parent_id,
        "node_id": parent_id,
        "version": 1,
        "label": milestone["goal"],
        "goal": milestone["goal"],
        "milestone_id": milestone["id"],
        "requirement_ids": copy.deepcopy(milestone.get("requirement_ids", [])),
        "constraint_ids": copy.deepcopy(milestone.get("constraint_ids", [])),
        "checks": copy.deepcopy(checks),
        "capabilities": copy.deepcopy(capabilities),
        "tools": None if tools is None else list(tools),
        "deny_tools": list(milestone.get("deny_tools", [])),
        "inputs": list(milestone.get("inputs", [])),
        "deliverables": list(milestone.get("outputs", [])),
        "method": None,
        "method_decision": None,
        "forbidden_changes": list(_JOIN_FORBIDDEN),
        "depends_on": depends_on,
        "lineage_id": parent_id,
        "authorization_node_id": parent_id,
    }


def _template_node_spec(flow, parent_id: str, milestone: dict, contract: dict) -> dict:
    for node in flow.nodes:
        if node.role == "executor" and node.kind == "role" and not node.members:
            spec = node.as_dict()
            spec.update({
                "node_id": parent_id,
                "label": milestone["goal"],
                "responsibility": milestone["goal"],
                "capabilities": copy.deepcopy(contract["capabilities"]),
                "tools": None if contract["tools"] is None else list(contract["tools"]),
                "deny_tools": list(contract["deny_tools"]),
                "members": [],
                "failure_policy": {"action": "stop", "max_attempts": 1},
            })
            return spec
    raise ValueError("流程中没有可用的 executor 模板节点")


def _temporary_flow(flow, parent_id: str, node_spec: dict, depends_on: list[str]) -> ExpertFlowSpec:
    data = copy.deepcopy(flow.as_dict())
    data["nodes"] = [*data["nodes"], node_spec]
    edges = [*data.get("edges", [])]
    edges.extend({"from_node": contract, "to_node": parent_id, "condition": "always"}
                 for contract in depends_on)
    data["edges"] = edges
    data["finalizer_node_id"] = parent_id
    data.pop("finalizer", None)
    return ExpertFlowSpec.from_value(data)


def build_milestone_fragment(value, flow, milestone_id, raw_proposal, *, work_dir,
                             sandbox, allowed_tools) -> dict:
    """Return the candidate milestone-plan document for ``milestone_id``."""
    milestone = _require_ready(value, milestone_id)
    parent_id = parent_id_for(milestone_id)
    if parent_id in (value.get("contracts") or {}) or parent_id in flow.node_map:
        raise ValueError(f"里程碑节点 {parent_id} 与现有合同或节点冲突")

    parent_contract = _temporary_parent(value, milestone, parent_id)
    node_spec = _template_node_spec(flow, parent_id, milestone, parent_contract)

    temp_value = copy.deepcopy(value)
    temp_value["contracts"][parent_id] = copy.deepcopy(parent_contract)
    temp_flow = _temporary_flow(flow, parent_id, node_spec, parent_contract["depends_on"])

    fragment = build_expansion_fragment(
        temp_value, temp_flow, parent_id, raw_proposal, work_dir=work_dir,
        sandbox=sandbox, allowed_tools=allowed_tools, node_attempt=1)

    contracts = fragment["contracts"]
    contracts[parent_id]["version"] = 1
    child_ids = list(fragment["child_ids"])

    merged = {**copy.deepcopy(value["contracts"]), **copy.deepcopy(contracts)}
    candidate_flow = ExpertFlowSpec.from_value(copy.deepcopy(fragment["flow"]))
    validate_contracts(merged, value["entries"], set(candidate_flow.node_map))

    return {
        "format_revision": FORMAT_REVISION,
        "kind": "milestone_plan",
        "milestone_id": milestone_id,
        "parent_id": parent_id,
        "lineage_id": parent_id,
        "child_ids": child_ids,
        "contract_ids": [*child_ids, parent_id],
        "base_plan_version": value.get("plan_version"),
        "basis": milestone_basis(value, flow, milestone_id),
        "flow": candidate_flow.as_dict(),
        "contracts": contracts,
        "proposal": fragment["proposal"],
        "normalized_write_paths": fragment["normalized_write_paths"],
    }
