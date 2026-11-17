"""Host graph publication transactions inside the existing run journal.

Preparing a candidate holds affected work. Only an independent review and
reconciled effect receipts let the host publish it. The initial locked design
is preserved; runtime_flow and flow_versions describe adopted graph versions.
Call these functions inside ExpertRunStore.mutate for persistence and locking.
"""
from __future__ import annotations

import copy
from pathlib import Path

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger, lineage_budget
from mewcode.kernel.collaboration_work_budget import (
    reserve_expansion, verify_expansion_reservations, release_unclaimed, release_obsolete,
)
from mewcode.kernel.collaboration_recovery import unresolved_calls
from mewcode.orchestration.collab_compile import validate_contracts


def effective_flow(record):
    locked = copy.deepcopy(record["locked_flow"])
    if ledger.flow_mode(locked) != "collaborative":
        return ExpertFlowSpec.from_value(locked)
    value = ledger.state(record)
    if "runtime_flow" not in value:
        return ExpertFlowSpec.from_value(locked)
    candidate = value["runtime_flow"]
    if not isinstance(candidate, dict) or ledger.digest(candidate) != value.get("runtime_flow_hash"):
        raise ValueError("已发布运行图与摘要不一致")
    return ExpertFlowSpec.from_value(copy.deepcopy(candidate))


def _affected(value, parent):
    affected = {parent}
    while True:
        more = {key for key, contract in value["contracts"].items()
                if set(contract.get("depends_on", [])) & affected}
        if more <= affected:
            return sorted(affected)
        affected |= more


def _basis(record, parent):
    value = ledger.state(record)
    return {"plan_version": value["plan_version"], "epoch": value["coordinator_epoch"],
            "parent_dependencies": ledger.dependencies(value, parent),
            "flow_hash": ledger.digest(effective_flow(record).as_dict())}


def validate_children(parent, delta, children, flow, predecessors):
    """Check the executable graph and the narrower contracts agree at commit."""
    from mewcode.experts.node_expansion import FORBIDDEN_EXPANSION_TOOLS
    scopes = []
    for key in children:
        child, node = delta[key], flow.node_map[key]
        caps = child.get("capabilities")
        paths = child.get("write_paths")
        tools = child.get("tools")
        original_caps = parent.get("capabilities") or {}
        if (not isinstance(caps, dict) or not isinstance(paths, list) or caps.get("write_paths") != paths
                or {k: v for k, v in caps.items() if k != "write_paths"}
                != {k: v for k, v in original_caps.items() if k != "write_paths"}
                or not isinstance(tools, list) or any(not isinstance(tool, str) for tool in tools)
                or set(tools) & FORBIDDEN_EXPANSION_TOOLS or "*" in tools
                or child.get("deny_tools") != parent.get("deny_tools", [])
                or set(tools) & set(parent.get("deny_tools", []))
                or ("*" in parent.get("deny_tools", []) and tools)
                or (parent.get("tools") is not None and "*" not in parent["tools"] and not set(tools) <= set(parent["tools"]))
                or child.get("authorization_node_id") != (parent.get("authorization_node_id") or parent.get("node_id"))
                or node.capabilities != caps or list(node.tools or []) != tools
                or list(node.deny_tools) != child["deny_tools"]):
            raise ValueError("子合同与运行节点的权限范围不一致")
        work_dir = child.get("work_dir")
        if not isinstance(work_dir, str) or not Path(work_dir).is_absolute():
            raise ValueError("子合同缺少宿主工作目录")
        roots = [(Path(work_dir) / path).resolve() for path in original_caps.get("write_paths", [])]
        normalized = [(Path(work_dir) / path).resolve() for path in paths]
        if any(not any(path.is_relative_to(root) for root in roots) for path in normalized):
            raise ValueError("子合同扩大了父级写入范围")
        if any(left.is_relative_to(right) or right.is_relative_to(left)
               for left in normalized for right in scopes):
            raise ValueError("子合同写入范围相互重叠")
        scopes.extend(normalized)
        dependencies = set(child.get("depends_on", []))
        incoming = {edge.from_node for edge in flow.edges if edge.to_node == key and edge.condition in {"always", "pass"}}
        if not set(predecessors) <= dependencies or dependencies != incoming:
            raise ValueError("子合同与运行图的前序依赖不一致")


def _validate_fragment(record, fragment):
    value = ledger.state(record)
    if not isinstance(fragment, dict) or fragment.get("format_revision") != 1 or fragment.get("kind") != "contract_expansion":
        raise ValueError("不支持的子合同发布候选")
    parent = fragment.get("parent_id")
    if not isinstance(parent, str) or parent not in value["contracts"]:
        raise ValueError("父合同不存在")
    old_parent = value["contracts"][parent]
    children = fragment.get("child_ids")
    if (not isinstance(children, list) or not 2 <= len(children) <= 6
            or any(not isinstance(key, str) or not key for key in children)
            or len(set(children)) != len(children) or set(children) & set(value["contracts"])):
        raise ValueError("子合同标识无效或与现有合同冲突")
    delta = fragment.get("contracts")
    if not isinstance(delta, dict) or set(delta) != {parent, *children} or any(not isinstance(item, dict) for item in delta.values()):
        raise ValueError("候选合同范围不符")
    if (fragment.get("base_plan_version") != value["plan_version"]
            or fragment.get("base_contract_version") != old_parent["version"]
            or fragment.get("basis") != ledger.dependencies(value, parent)):
        raise ValueError("子合同候选基准已经变化")
    lineage = old_parent.get("lineage_id") or parent
    if fragment.get("lineage_id") != lineage:
        raise ValueError("子合同不能更换谱系")
    expansion_id = fragment.get("expansion_id")
    if not isinstance(expansion_id, str) or not expansion_id:
        raise ValueError("展开标识无效")
    joined = delta[parent]
    if (joined.get("id") != parent or joined.get("node_id") != parent
            or joined.get("version") != old_parent["version"] + 1
            or joined.get("execution_kind") != "join"
            or joined.get("join_children") != children or joined.get("depends_on") != children
            or joined.get("lineage_id") != lineage):
        raise ValueError("父合同必须保留身份并等待全部子合同")
    for field in ("goal", "requirement_ids", "constraint_ids", "method", "method_decision", "forbidden_changes", "milestone_id"):
        if joined.get(field) != old_parent.get(field):
            raise ValueError("展开不得修改父合同要求")
    checks = old_parent.get("checks", [])
    if joined.get("checks", [])[:len(checks)] != checks:
        raise ValueError("展开不得移除原验收检查")
    for key in children:
        child = delta[key]
        if (child.get("id") != key or child.get("node_id") != key
                or type(child.get("version")) is not int or child["version"] != 1
                or child.get("lineage_id") != lineage
                or child.get("expanded_from") != {"contract_id": parent, "contract_version": old_parent["version"], "expansion_id": expansion_id}):
            raise ValueError("子合同身份或来源无效")
        requirements = child.get("requirement_ids")
        if (not isinstance(requirements, list) or not requirements
                or any(not isinstance(key, str) for key in requirements)
                or len(set(requirements)) != len(requirements)
                or not set(requirements) <= set(old_parent.get("requirement_ids", []))):
            raise ValueError("子合同需求范围必须属于父合同")
        for field in ("constraint_ids", "method", "method_decision", "forbidden_changes", "milestone_id"):
            if child.get(field) != old_parent.get(field):
                raise ValueError("子合同必须继承父合同要求")
    if set(old_parent.get("requirement_ids", [])) != {
            key for child_id in children for key in delta[child_id]["requirement_ids"]}:
        raise ValueError("子合同必须合计覆盖全部父合同需求")
    old_flow = effective_flow(record)
    new_flow = ExpertFlowSpec.from_value(copy.deepcopy(fragment.get("flow")))
    if (new_flow.flow_id != old_flow.flow_id or new_flow.version != old_flow.version + 1
            or new_flow.finalizer_node_id != old_flow.finalizer_node_id
            or new_flow.finalizer_plugin != old_flow.finalizer_plugin or new_flow.meta != old_flow.meta
            or set(new_flow.node_map) != set(old_flow.node_map) | set(children)
            or any(new_flow.node_map[key].as_dict() != node.as_dict() for key, node in old_flow.node_map.items())):
        raise ValueError("候选图改动了既有节点、边界或版本")
    merged = {**copy.deepcopy(value["contracts"]), **copy.deepcopy(delta)}
    preserved_edges = [edge.as_dict() for edge in old_flow.edges if edge.to_node != parent]
    actual_preserved = [edge.as_dict() for edge in new_flow.edges
                        if edge.from_node in old_flow.node_map and edge.to_node in old_flow.node_map]
    if sorted(preserved_edges, key=ledger.digest) != sorted(actual_preserved, key=ledger.digest):
        raise ValueError("展开不得修改原图其它连接")
    validate_children(old_parent, delta, children, new_flow,
                      old_parent.get("bound_dependencies", old_parent.get("depends_on", [])))
    validate_contracts(merged, value["entries"], set(new_flow.node_map))
    return parent, children, lineage


def prepare(record, fragment, *, publication_id, attempt_id):
    if not isinstance(publication_id, str) or not publication_id.strip() or len(publication_id) > 120:
        raise ValueError("发布标识无效")
    value = ledger.state(record)
    old = value.get("graph_publications", {}).get(publication_id)
    if old:
        if old["fragment"] != fragment or old["attempt_id"] != attempt_id:
            raise ValueError("同一发布标识不能更换内容")
        return copy.deepcopy(old)
    parent, children, lineage = _validate_fragment(record, fragment)
    attempt = ledger.check_attempt(value, attempt_id)
    if attempt["contract_id"] != parent or attempt["status"] not in {"dispatched", "checking"}:
        raise ValueError("发布必须来自父合同当前执行尝试")
    affected = _affected(value, parent)
    if value["contracts"][parent].get("held") or any(value.get("plan_holds", {}).get(key) for key in affected):
        raise ValueError("受影响工作已有待处理修订或子图发布")
    if any(row.get("status") == "prepared" and set(row["affected"]) & set(affected)
           for row in value.get("graph_publications", {}).values()):
        raise ValueError("另一个发布正在修改相同工作")
    candidate = copy.deepcopy(record)
    current = ledger.state(candidate)
    reservation = "expansion:" + publication_id
    lineage_budget.reserve(current, reservation, lineage_id=lineage, kind="expansions")
    attempts = reserve_expansion(current, fragment, publication_id)
    publication = {"id": publication_id, "status": "prepared", "fragment": copy.deepcopy(fragment),
                   "attempt_id": attempt_id, "affected": affected, "basis": _basis(record, parent),
                   "reservation_id": reservation, "attempt_reservations": attempts}
    current.setdefault("graph_publications", {})[publication_id] = publication
    current["required_capabilities"] = sorted(set(current.get("required_capabilities", [])) | {"runtime_graph"})
    if any(fragment["contracts"][key]["requirement_ids"] != value["contracts"][parent]["requirement_ids"]
           for key in children):
        current["required_capabilities"] = sorted(set(current["required_capabilities"]) | {"scoped_contracts"})
    for key in affected:
        current.setdefault("plan_holds", {})[key] = publication_id
    ledger.event(current, "graph_prepared", publication_id=publication_id, parent_id=parent, affected=affected)
    record.clear()
    record.update(candidate)
    return copy.deepcopy(publication)


def publish(record, publication_id, *, review, proposer_vendor, reviewer_vendor):
    value = ledger.state(record)
    item = value.get("graph_publications", {}).get(publication_id)
    if not item:
        raise ValueError("发布记录不存在")
    if item["status"] == "published":
        return False
    if item["status"] != "prepared":
        raise ValueError("发布记录不处于待审核状态")
    if (not isinstance(review, dict) or review.get("passed") is not True or not review.get("evidence")
            or not isinstance(proposer_vendor, str) or not proposer_vendor.strip()
            or not isinstance(reviewer_vendor, str) or not reviewer_vendor.strip()
            or proposer_vendor.strip().casefold() == reviewer_vendor.strip().casefold()):
        raise ValueError("发布需要不同厂商的独立复核及通过证据")
    parent, children, _ = _validate_fragment(record, item["fragment"])
    if item["basis"] != _basis(record, parent):
        raise ValueError("发布基准已经变化")
    source = ledger.check_attempt(value, item["attempt_id"])
    if source["status"] not in {"dispatched", "checking"}:
        raise ValueError("发布来源尝试已结束")
    affected = item["affected"]
    if (unresolved_calls(record, affected) or any(
            attempt["id"] != source["id"] and attempt["contract_id"] in affected
            and attempt["status"] in {"dispatched", "checking", "unknown"}
            for attempt in value["attempts"].values())):
        raise ValueError("受影响工作仍有在途尝试或结果不明的操作")
    if any(value.get("plan_holds", {}).get(key) != publication_id for key in affected):
        raise ValueError("发布保留范围已经变化")
    candidate = copy.deepcopy(record)
    current = ledger.state(candidate)
    target = current["graph_publications"][publication_id]
    # A prepared candidate saved by an older host must also fund its whole
    # first execution before it becomes visible. This is still transactional.
    if 'attempt_reservations' not in target:
        target['attempt_reservations'] = reserve_expansion(current, target['fragment'], publication_id)
    verify_expansion_reservations(current, target)
    old_flow = effective_flow(record).as_dict()
    old_contracts = {key: copy.deepcopy(current["contracts"][key]) for key in affected}
    current["attempts"][source["id"]].update(status="expanded", publication_id=publication_id)
    ledger.invalidate(current, affected, "已发布子合同计划")
    current["contracts"].update(copy.deepcopy(target["fragment"]["contracts"]))
    release_obsolete(current)
    # A user-selected per-node route belongs to this lineage, including its
    # children; never fall back to an obsolete role default during expansion.
    binding = (candidate.get("locked_models") or {}).get(parent)
    override = current.get("acceptance_model_overrides", {}).get(parent)
    for key in children:
        if binding:
            candidate.setdefault("locked_models", {})[key] = copy.deepcopy(binding)
        if override:
            current.setdefault("acceptance_model_overrides", {})[key] = copy.deepcopy(override)
    milestone = current["milestones"][current["contracts"][parent]["milestone_id"]]
    index = milestone["contracts"].index(parent)
    milestone["contracts"][index:index] = children
    previous_version = current["plan_version"]
    current["plan_version"] += 1
    versions = current.setdefault("flow_versions", [])
    if not versions:
        versions.append({"plan_version": previous_version, "flow": old_flow,
                         "flow_hash": ledger.digest(old_flow), "publication_id": None})
    current["runtime_flow"] = copy.deepcopy(target["fragment"]["flow"])
    current["runtime_flow_hash"] = ledger.digest(current["runtime_flow"])
    versions.append({"plan_version": current["plan_version"], "flow": copy.deepcopy(current["runtime_flow"]),
                     "flow_hash": current["runtime_flow_hash"], "publication_id": publication_id})
    target.update(status="published", review=copy.deepcopy(review), proposer_vendor=proposer_vendor,
                  reviewer_vendor=reviewer_vendor, new_plan_version=current["plan_version"], old_contracts=old_contracts)
    lineage_budget.settle(current, target["reservation_id"])
    for key in affected:
        if current["plan_holds"].get(key) == publication_id:
            del current["plan_holds"][key]
    ledger.event(current, "graph_published", publication_id=publication_id, parent_id=parent,
                 children=children, plan_version=current["plan_version"])
    record.clear()
    record.update(candidate)
    return True


def reject(record, publication_id, *, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("拒绝发布需要明确原因")
    item = ledger.state(record).get("graph_publications", {}).get(publication_id)
    if not item:
        raise ValueError("发布记录不存在")
    if item["status"] == "rejected" and item["reason"] == reason:
        return False
    if item["status"] != "prepared":
        raise ValueError("只能拒绝尚未发布的候选")
    candidate = copy.deepcopy(record)
    value = ledger.state(candidate)
    target = value["graph_publications"][publication_id]
    target.update(status="rejected", reason=reason)
    release_unclaimed(value, target)
    lineage_budget.release(value, target["reservation_id"])
    for key in target["affected"]:
        if value.get("plan_holds", {}).get(key) == publication_id:
            del value["plan_holds"][key]
    ledger.event(value, "graph_rejected", publication_id=publication_id, reason=reason)
    record.clear()
    record.update(candidate)
    return True
