"""Atomic publication of a declared future milestone after its predecessors."""
from __future__ import annotations

import copy

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger, lineage_budget
from mewcode.kernel.collaboration_graph import effective_flow, validate_children
from mewcode.kernel.collaboration_recovery import unresolved_calls
from mewcode.orchestration.collab_compile import validate_contracts


def reserve(record, milestone_id, *, planning_id, basis):
    from mewcode.orchestration.collab_rolling import milestone_basis
    value = ledger.state(record)
    old = value.get("milestone_plans", {}).get(planning_id)
    if old:
        if old["milestone_id"] != milestone_id or old["basis"] != basis:
            raise ValueError("同一规划身份不能替换内容")
        return copy.deepcopy(old)
    if milestone_basis(value, effective_flow(record), milestone_id) != basis:
        raise ValueError("里程碑规划依据已经变化")
    candidate = copy.deepcopy(record)
    current = ledger.state(candidate)
    reservation_id = "milestone:" + planning_id
    lineage_budget.reserve(current, reservation_id, lineage_id="milestone::" + milestone_id, kind="expansions")
    item = {"id": planning_id, "milestone_id": milestone_id, "basis": basis,
            "status": "planning", "reservation_id": reservation_id}
    current.setdefault("milestone_plans", {})[planning_id] = item
    current["required_capabilities"] = sorted(set(current.get("required_capabilities", [])) | {"runtime_graph"})
    ledger.event(current, "milestone_planning", milestone_id=milestone_id, planning_id=planning_id)
    record.clear()
    record.update(candidate)
    return copy.deepcopy(item)


def reject(record, planning_id, *, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("拒绝规划需要明确原因")
    item = ledger.state(record)["milestone_plans"][planning_id]
    if item["status"] == "rejected":
        return False
    if item["status"] == "published":
        raise ValueError("已发布规划不能被撤回为未发布")
    candidate = copy.deepcopy(record)
    value = ledger.state(candidate)
    target = value["milestone_plans"][planning_id]
    target.update(status="rejected", reason=reason)
    lineage_budget.release(value, target["reservation_id"])
    ledger.event(value, "milestone_plan_rejected", planning_id=planning_id, reason=reason)
    record.clear()
    record.update(candidate)
    return True


def publish(record, planning_id, fragment, *, review, proposer_vendor, reviewer_vendor):
    from mewcode.orchestration.collab_rolling import milestone_basis
    value = ledger.state(record)
    plan = value["milestone_plans"][planning_id]
    if plan["status"] == "published":
        return False
    if plan["status"] != "planning":
        raise ValueError("里程碑规划已经结束")
    if (not isinstance(review, dict) or review.get("passed") is not True or not review.get("evidence")
            or not isinstance(proposer_vendor, str) or not proposer_vendor.strip()
            or not isinstance(reviewer_vendor, str) or not reviewer_vendor.strip()
            or proposer_vendor.strip().casefold() == reviewer_vendor.strip().casefold()):
        raise ValueError("里程碑发布需要独立复核通过")
    milestone_id = plan["milestone_id"]
    milestone = value["milestones"][milestone_id]
    before_flow = effective_flow(record)
    basis = milestone_basis(value, before_flow, milestone_id)
    if (plan["basis"] != basis or not isinstance(fragment, dict) or fragment.get("basis") != basis
            or fragment.get("kind") != "milestone_plan" or fragment.get("format_revision") != 1
            or fragment.get("base_plan_version") != value["plan_version"]
            or fragment.get("milestone_id") != milestone_id
            or milestone["status"] != "unexpanded" or milestone["contracts"]):
        raise ValueError("里程碑候选基准已经变化")
    predecessors = [key for previous in milestone["depends_on"] for key in value["milestones"][previous]["contracts"]]
    if (any(value["milestones"][key]["status"] != "accepted" for key in milestone["depends_on"])
            or any(not ledger.valid_certificate(value, key) for key in predecessors)
            or unresolved_calls(record, predecessors)):
        raise ValueError("里程碑前序交付尚未全部确认")
    parent = "milestone::" + milestone_id
    children = fragment.get("child_ids")
    delta = fragment.get("contracts")
    if (fragment.get("parent_id") != parent or fragment.get("lineage_id") != parent
            or not isinstance(children, list) or not 2 <= len(children) <= 6
            or any(not isinstance(key, str) or not key for key in children)
            or len(set(children)) != len(children) or parent in children
            or not isinstance(delta, dict) or set(delta) != {parent, *children}
            or set(delta) & set(value["contracts"]) or fragment.get("contract_ids") != [*children, parent]):
        raise ValueError("里程碑新增合同范围无效")
    for key, contract in delta.items():
        if (contract.get("id") != key or contract.get("node_id") != key or contract.get("version") != 1
                or contract.get("lineage_id") != parent or contract.get("milestone_id") != milestone_id
                or contract.get("requirement_ids") != milestone["requirement_ids"]
                or contract.get("constraint_ids") != milestone["constraint_ids"]):
            raise ValueError("里程碑合同遗漏了原始范围或改变身份")
    join = delta[parent]
    if (join.get("execution_kind") != "join" or join.get("join_children") != children
            or join.get("depends_on") != children or join.get("goal") != milestone["goal"]
            or join.get("checks", [])[:len(milestone["checks"])] != milestone["checks"]):
        raise ValueError("里程碑必须保留完整汇合验收")
    new_flow = ExpertFlowSpec.from_value(copy.deepcopy(fragment["flow"]))
    if (new_flow.flow_id != before_flow.flow_id or new_flow.version != before_flow.version + 1
            or new_flow.meta != before_flow.meta or new_flow.finalizer_node_id != parent
            or new_flow.finalizer_plugin != before_flow.finalizer_plugin
            or set(new_flow.node_map) != set(before_flow.node_map) | set(delta)
            or any(new_flow.node_map[key].as_dict() != node.as_dict() for key, node in before_flow.node_map.items())):
        raise ValueError("里程碑候选改变了既有运行图")
    old_edges = [edge.as_dict() for edge in before_flow.edges]
    kept = [edge.as_dict() for edge in new_flow.edges if edge.from_node in before_flow.node_map and edge.to_node in before_flow.node_map]
    if sorted(old_edges, key=ledger.digest) != sorted(kept, key=ledger.digest):
        raise ValueError("里程碑候选改变了既有依赖")
    validate_children({**milestone, "node_id": parent}, delta, children, new_flow, predecessors)
    validate_contracts({**value["contracts"], **delta}, value["entries"], set(new_flow.node_map))
    candidate = copy.deepcopy(record)
    current = ledger.state(candidate)
    current["contracts"].update(copy.deepcopy(delta))
    current["milestones"][milestone_id].update(contracts=[*children, parent], status="planned")
    previous = current["plan_version"]
    current["plan_version"] += 1
    current["final_contract_id"] = parent
    current["runtime_flow"] = new_flow.as_dict()
    current["runtime_flow_hash"] = ledger.digest(current["runtime_flow"])
    versions = current.setdefault("flow_versions", [])
    if not versions:
        versions.append({"plan_version": previous, "flow": before_flow.as_dict(),
            "flow_hash": ledger.digest(before_flow.as_dict()), "publication_id": None})
    versions.append({"plan_version": current["plan_version"], "flow": copy.deepcopy(current["runtime_flow"]),
                     "flow_hash": current["runtime_flow_hash"], "publication_id": planning_id})
    current.setdefault("graph_publications", {})[planning_id] = {
        "id": planning_id, "status": "published", "kind": "milestone_plan", "fragment": copy.deepcopy(fragment),
        "new_plan_version": current["plan_version"], "review": copy.deepcopy(review),
        "proposer_vendor": proposer_vendor, "reviewer_vendor": reviewer_vendor}
    current["milestone_plans"][planning_id].update(status="published", fragment=copy.deepcopy(fragment),
                                                  new_plan_version=current["plan_version"])
    lineage_budget.settle(current, plan["reservation_id"])
    ledger.event(current, "graph_published", publication_id=planning_id, parent_id=parent, milestone_id=milestone_id,
                 children=children, plan_version=current["plan_version"])
    record.clear()
    record.update(candidate)
    return True
