"""Pure contract-expansion compiler for one collaborative ledger node.

Given a collaborative ledger value, the current flow and a raw proposal the
parent node produced, this module derives a candidate JSON document holding the
upgraded join contract for the parent, the child contracts and the expanded
flow.  It never calls a model or a tool, never mutates its inputs and never
publishes the graph; the host wires the candidate in later.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.node_expansion import (
    FORBIDDEN_EXPANSION_TOOLS,
    ExpansionPathSandbox,
    NodeExpansionContext,
    NodeExpansionHolder,
)
from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_compile import validate_contracts

JOIN_NODE_ID = "$join"
MAX_EXPANSION_DEPTH = 2
FORMAT_REVISION = 1
_PARENT_RUNTIME_FIELDS = (
    "held",
    "bound_dependencies",
    "iteration",
    "execution_kind",
    "join_children",
    "join_expansion_id",
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _identity(expansion_id: str) -> str:
    return f"join-{_digest(expansion_id)}"


def _overlap(first: str, second: str) -> bool:
    left, right = Path(first), Path(second)
    return left == right or right.is_relative_to(left) or left.is_relative_to(right)


def _child_scope(response, local_id: str) -> list[str]:
    return list(response.normalized_write_paths.get(local_id, ()))


def _child_contract(parent: dict, child_id: str, node, expansion_id: str,
                    write_paths: list[str], lineage: str,
                    authorization_node_id: str | None, work_dir: Path) -> dict:
    contract = copy.deepcopy(parent)
    for field in _PARENT_RUNTIME_FIELDS:
        contract.pop(field, None)
    write_scope = list(write_paths)
    contract.update({
        "id": child_id,
        "node_id": child_id,
        "version": 1,
        "goal": node.task,
        "label": node.label,
        "inputs": list(node.input_refs),
        "deliverables": list(node.outputs),
        "depends_on": list(parent.get("bound_dependencies", parent.get("depends_on", []))),
        "checks": [{
            "id": "child_" + _digest(child_id),
            "kind": "review",
            "version": 1,
            "criteria": list(node.acceptance),
        }],
        "lineage_id": lineage,
        "expanded_from": {
            "contract_id": parent["id"],
            "contract_version": parent["version"],
            "expansion_id": expansion_id,
        },
        "expansion_depth": parent.get("expansion_depth", 0) + 1,
        "authorization_node_id": authorization_node_id,
        "work_dir": str(work_dir),
        "write_paths": write_scope,
    })
    capabilities = copy.deepcopy(parent.get("capabilities") or {})
    capabilities["write_paths"] = write_scope
    contract["capabilities"] = capabilities
    contract["tools"] = list(node.tools)
    contract["deny_tools"] = list(parent.get("deny_tools", []))
    if node.requirement_ids is not None:
        required = parent.get("requirement_ids", [])
        if (len(set(node.requirement_ids)) != len(node.requirement_ids)
                or not set(node.requirement_ids) <= set(required)):
            raise ValueError("子任务需求范围必须是父合同需求的非重复子集")
        contract["requirement_ids"] = list(node.requirement_ids)
    return contract


def _child_flow_node(parent_node, child_id: str, node, contract: dict) -> dict:
    spec = parent_node.as_dict() if parent_node is not None else {}
    spec.update({
        "node_id": child_id,
        "label": node.label,
        "responsibility": node.task,
        "members": [],
        "failure_policy": {"action": "stop", "max_attempts": 1},
        "tools": list(contract["tools"]),
        "deny_tools": list(contract["deny_tools"]),
        "capabilities": copy.deepcopy(contract["capabilities"]),
    })
    return spec


def _is_plain(edge) -> bool:
    return (str(edge.condition or "always") in {"always", "pass"}
            and edge.input_when is None
            and not edge.branch_group)


def build_expansion_fragment(value, flow, parent_id, raw_proposal, *, work_dir,
                             sandbox, allowed_tools, node_attempt):
    """Validate ``raw_proposal`` and return the candidate expansion document."""
    if parent_id not in value.get("contracts", {}):
        raise ValueError(f"父合同 {parent_id} 不在当前协作账本中")
    if parent_id not in flow.node_map:
        raise ValueError(f"父节点 {parent_id} 不在当前流程图中")
    parent = value["contracts"][parent_id]
    parent_node = flow.node_map[parent_id]
    if parent_node.role != "executor" or parent_node.kind != "role":
        raise ValueError("只有 executor 角色的普通工作节点可以展开")
    if parent_node.members:
        raise ValueError("团队节点不再展开为微图")
    if parent.get("execution_kind") == "join":
        raise ValueError("汇合节点不能再次展开")
    if parent.get("held"):
        raise ValueError("父合同已由用户暂扣，不能展开")
    if any(parent_id in loop.get("node_ids", []) for loop in flow.meta.get("loops", []) or []):
        raise ValueError("循环体内部节点不能展开为微图")
    if (flow.meta.get("node_kinds") or {}).get(parent_id) == "gate":
        raise ValueError("门控节点不能展开为微图")

    capabilities = parent.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("父合同缺少能力声明，不能展开")
    depth = parent.get("expansion_depth", 0)
    if type(depth) is not int or depth < 0:
        raise ValueError("父合同扩展深度无效")
    limit = (value.get("policy") or {}).get("max_expansion_depth", MAX_EXPANSION_DEPTH)
    if type(limit) is not int or limit not in (1, 2):
        raise ValueError("协作策略的 max_expansion_depth 必须是 1 或 2")
    if depth >= limit:
        raise ValueError("已达到协作展开最大深度")

    dependency_revision = ledger.dependencies(value, parent_id)
    parent_scope = tuple(capabilities.get("write_paths", []))
    holder = NodeExpansionHolder(NodeExpansionContext(
        parent_node_id=parent_id,
        flow_version=flow.version,
        input_revision=dependency_revision,
        node_attempt=node_attempt,
        work_dir=Path(work_dir),
        sandbox=ExpansionPathSandbox(sandbox, parent_scope),
        allowed_tools=frozenset(allowed_tools),
        available_input_refs=frozenset(
            parent.get("bound_dependencies", parent.get("depends_on", []))),
    ))
    response = holder.submit(raw_proposal)

    deny = {str(tool) for tool in parent.get("deny_tools", [])}
    local_ids = [node.node_id for node in response.proposal.nodes]
    child_flow_node = f"{parent_id}::{response.expansion_id}::"
    authorization_node_id = parent.get("authorization_node_id") or parent.get("node_id")
    child_ids = {local: child_flow_node + local for local in local_ids}
    if set(child_ids.values()) & (set(value["contracts"]) | set(flow.node_map)):
        raise ValueError("子合同标识与现有节点冲突")
    lineage = parent.get("lineage_id") or parent.get("node_id")
    node_specs = {node.node_id: node for node in response.proposal.nodes}

    predecessors: dict[str, list[str]] = {local: [] for local in local_ids}
    for edge in response.proposal.edges:
        if edge.from_node != JOIN_NODE_ID and edge.to_node != JOIN_NODE_ID:
            predecessors[edge.to_node].append(edge.from_node)

    child_contracts: dict[str, dict] = {}
    normalized: dict[str, list[str]] = {}
    for local_id in local_ids:
        node = node_specs[local_id]
        write_scope = _child_scope(response, local_id)
        normalized[local_id] = write_scope
        if any(tool in FORBIDDEN_EXPANSION_TOOLS or tool in deny or "*" in deny
               for tool in node.tools):
            raise ValueError(f"子节点 {local_id} 申请了被禁止的工具")
        inherited_tools = parent.get("tools")
        available = set(allowed_tools) - FORBIDDEN_EXPANSION_TOOLS - deny
        if inherited_tools is not None and "*" not in inherited_tools:
            available &= set(inherited_tools)
        if set(node.tools) - available:
            raise ValueError(f"子节点 {local_id} 申请了父合同之外的工具")
        tools = list(node.tools) if node.tools else ([] if "*" in deny else sorted(available))
        child = _child_contract(
            parent, child_ids[local_id], node, response.expansion_id, write_scope,
            lineage, authorization_node_id, Path(work_dir))
        child["tools"] = tools
        child["allow_expansion"] = bool(parent.get("allow_expansion",
            inherited_tools is None or bool(set(inherited_tools) & {"*", "ProposeNodeExpansion"})))
        child["depends_on"] = list(dict.fromkeys(list(child["depends_on"]) + [
            child_ids[other] for other in predecessors[local_id]]))
        child_contracts[child_ids[local_id]] = child

    for index, left in enumerate(local_ids):
        for right in local_ids[index + 1:]:
            for first in normalized[left]:
                for second in normalized[right]:
                    if _overlap(first, second):
                        raise ValueError(
                            f"子节点 {left} 与 {right} 的写入范围重叠：{first} 与 {second}")

    if set(parent.get("requirement_ids", [])) != {
            key for child in child_contracts.values() for key in child["requirement_ids"]}:
        raise ValueError("子任务划分没有覆盖父合同的全部需求")

    kept = []
    for edge in flow.edges:
        spec = edge.as_dict()
        if edge.to_node == parent_id:
            if not _is_plain(edge):
                raise ValueError("当前协作展开只支持 always/pass 输入边")
            kept.extend({**spec, "to_node": child_id} for child_id in child_ids.values())
        else:
            kept.append(spec)
    mapped = {**child_ids, JOIN_NODE_ID: parent_id}
    kept.extend({"from_node": mapped[edge.from_node], "to_node": mapped[edge.to_node],
                 "condition": "always"} for edge in response.proposal.edges)

    attached: set[tuple[str, str]] = set()
    for spec in kept:
        attached.add((spec["from_node"], spec["to_node"]))
    for child_id in child_ids.values():
        if (child_id, parent_id) not in attached:
            kept.append({"from_node": child_id, "to_node": parent_id,
                         "condition": "always"})

    new_flow = flow.as_dict()
    position = next(i for i, spec in enumerate(new_flow["nodes"]) if spec["node_id"] == parent_id)
    new_flow["nodes"][position:position] = [
        _child_flow_node(parent_node, child_ids[local], node_specs[local],
                         child_contracts[child_ids[local]]) for local in local_ids]
    new_flow["edges"] = kept
    new_flow["version"] = flow.version + 1

    join_check = {
        "id": _identity(response.expansion_id),
        "kind": "review",
        "version": 1,
        "criteria": list(response.proposal.join.acceptance),
    }
    new_parent = copy.deepcopy(parent)
    for field in _PARENT_RUNTIME_FIELDS:
        new_parent.pop(field, None)
    new_parent.update({
        "version": parent["version"] + 1,
        "execution_kind": "join",
        "join_children": list(child_ids.values()),
        "join_expansion_id": response.expansion_id,
        "lineage_id": lineage,
        "depends_on": list(child_ids.values()),
        "checks": list(parent.get("checks", [])) + [join_check],
    })

    contracts = {parent_id: new_parent, **child_contracts}
    merged = {**copy.deepcopy(value["contracts"]), **copy.deepcopy(contracts)}
    candidate_flow = ExpertFlowSpec.from_value(copy.deepcopy(new_flow))
    validate_contracts(merged, value["entries"], set(candidate_flow.node_map))

    return {
        "format_revision": FORMAT_REVISION,
        "kind": "contract_expansion",
        "parent_id": parent_id,
        "expansion_id": response.expansion_id,
        "lineage_id": lineage,
        "child_ids": list(child_ids.values()),
        "base_plan_version": value.get("plan_version"),
        "base_contract_version": parent["version"],
        "basis": dependency_revision,
        "flow": candidate_flow.as_dict(),
        "contracts": contracts,
        "proposal": response.proposal.model_dump(mode="json"),
        "normalized_write_paths": normalized,
    }
