"""Convert OrchestrationSpec ↔ ExpertFlowSpec for the existing executor."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

from mewcode.experts.flow import ExpertFlowSpec, normalize_edge_condition
from mewcode.model_registry import ModelRef
from mewcode.orchestration.loops import compile_loops
from mewcode.orchestration.plan_text import compact_plan_text
from mewcode.orchestration.kinds import kind_roles
from mewcode.orchestration.spec import (
    GoalContract,
    ModelBinding,
    OrchestrationEdge,
    OrchestrationError,
    OrchestrationNode,
    OrchestrationSpec,
    PlanBrief,
    parse_review_channels,
)

_ROLE_TO_KIND = {
    "planner": "plan",
    "executor": "implement",
    "auditor": "audit",
    "manager": "reduce",
}


def _binding_to_ref(binding: ModelBinding | None) -> ModelRef | None:
    if binding is None or not binding.is_complete:
        return None
    return ModelRef(
        provider_key=binding.provider_key,
        model_id=binding.model_id,
        effort=binding.effort,
    )


def _ref_to_binding(ref: ModelRef | None) -> ModelBinding | None:
    if ref is None:
        return None
    return ModelBinding(
        provider_key=ref.provider_key,
        model_id=ref.model_id,
        effort=ref.effort,
    )


_CONSTRAINT_PIPELINE_HINT = (
    "约束管线说明：下列子节点不单独调度；默认由本阶段一次完成。"
    "仅当某条约束需要独立深入时，再按需创建直属子 agent；"
    "成员不得递归委派；你负责整合结果。"
)


def clean_generated_responsibility(value: str) -> str:
    """Remove text appended by earlier spec↔flow conversions.

    Canvas round-trips used to feed the generated Plan/known-input/policy tail
    back into ``goal`` and append it again on every save.  Keep the authored
    goal only; the generated sections are rebuilt once below.
    """
    text = str(value or "").strip()
    markers = (
        "\nPlan：",
        "\n已知输入：",
        "\n待澄清（开跑前应已问清）：",
        "\n决策指南（执行期必须遵守）：",
        "\n约束管线（子阶段）：",
        "\n步骤：",
        "\n产物：",
        "\n验收：",
    )
    cuts = [text.find(marker) for marker in markers if text.find(marker) >= 0]
    return text[:min(cuts)].rstrip() if cuts else text


def _canvas_groups_of(spec: OrchestrationSpec) -> dict[str, dict[str, Any]]:
    raw = (spec.meta or {}).get("canvas_groups")
    if not isinstance(raw, dict):
        return {}
    groups: dict[str, dict[str, Any]] = {}
    for group_id, info in raw.items():
        if not isinstance(group_id, str) or not isinstance(info, dict):
            continue
        children = info.get("children") if isinstance(info.get("children"), list) else []
        groups[group_id] = {
            **info,
            "children": [str(item).strip() for item in children if str(item).strip()],
        }
    return groups


def looks_like_refine_child_label(label: str) -> bool:
    """Canvas inserts unnamed children as exactly 「子阶段」."""
    text = str(label or "").strip()
    return text == "子阶段" or text.startswith("子阶段 ")


def infer_refine_child_groups(spec: OrchestrationSpec) -> dict[str, dict[str, Any]]:
    """Recover parent→child refine groups from sequential 「子阶段」 chains.

    When ``canvas_groups`` was dropped, nested checkpoints stay in the DAG as
    ordinary stages (parent → child → child → next). Infer the chain so the
    board can nest them and ``to_expert_flow`` can fold them into constraints.
    """
    live_ids = {node.node_id for node in spec.nodes}
    child_ids = {
        node.node_id
        for node in spec.nodes
        if looks_like_refine_child_label(node.label)
    }
    if not child_ids:
        return {}
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in live_ids}
    for edge in spec.edges:
        if edge.from_node in live_ids and edge.to_node in live_ids:
            outgoing[edge.from_node].append(edge.to_node)

    declared = _canvas_groups_of(spec)
    claimed = {
        child_id
        for info in declared.values()
        for child_id in (info.get("children") or [])
    }
    groups: dict[str, dict[str, Any]] = {}
    for node in spec.nodes:
        if node.node_id in child_ids:
            continue
        chain: list[str] = []
        cursor = node.node_id
        seen: set[str] = set()
        while True:
            child_next = [
                item
                for item in outgoing.get(cursor) or []
                if item in child_ids and item not in claimed and item not in seen
            ]
            if len(child_next) != 1:
                break
            nxt = child_next[0]
            chain.append(nxt)
            seen.add(nxt)
            cursor = nxt
        if not chain:
            continue
        groups[node.node_id] = {
            "expanded": True,
            "children": chain,
            "label": node.label or node.node_id,
        }
        claimed.update(chain)
    return groups


def resolve_canvas_groups(spec: OrchestrationSpec) -> dict[str, dict[str, Any]]:
    """A declared group snapshot (including empty) wins over legacy inference."""
    declared = _canvas_groups_of(spec)
    if isinstance((spec.meta or {}).get("canvas_groups"), dict):
        live = set(spec.node_map())
        return {gid: {**info, "children": [child for child in info.get("children", [])
                                         if child in live and child != gid]}
                for gid, info in declared.items() if gid in live}
    inferred = infer_refine_child_groups(spec)
    merged = dict(inferred)
    merged.update(declared)
    return merged


def _child_parent_map(
    groups: dict[str, dict[str, Any]],
    live_ids: set[str],
) -> dict[str, str]:
    """Map schedule-skipped child id → parent id (one nesting level)."""
    mapping: dict[str, str] = {}
    for parent_id, info in groups.items():
        if parent_id not in live_ids:
            continue
        for child_id in info.get("children") or []:
            if child_id in live_ids and child_id != parent_id:
                mapping[child_id] = parent_id
    return mapping


def _brief_responsibility(node: OrchestrationNode) -> str:
    responsibility = compact_plan_text(clean_generated_responsibility(node.goal)) or node.label
    if node.plan_brief is None:
        return responsibility
    parts = [responsibility]
    if node.plan_brief.summary and node.plan_brief.summary != responsibility:
        parts.append("Plan：" + node.plan_brief.summary)
    if node.plan_brief.known_inputs:
        parts.append("已知输入：" + "；".join(node.plan_brief.known_inputs))
    if node.plan_brief.open_questions:
        parts.append(
            "待澄清（开跑前应已问清）：" + "；".join(node.plan_brief.open_questions)
        )
    for label, values in (("步骤", node.plan_brief.steps), ("产物", node.plan_brief.outputs),
                          ("验收", node.plan_brief.acceptance)):
        if values:
            parts.append(label + "：" + "；".join(values))
    return "\n".join(part for part in parts if part)


def _coordination_contracts(
    spec: OrchestrationSpec,
    draft_meta: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate and describe explicitly marked coordination boundaries.

    A coordination marker is deliberately not enough to turn an arbitrary
    node into an entry.  The canvas must contain an explicit edge from the
    marked node to at least one declared child (or from at least one declared
    child into the marked node for an exit).  The declared children remain the
    complete coordination scope; global graph validation owns topology and
    loop semantics.
    """
    marked = draft_meta.get("coordination_nodes") or []
    if not isinstance(marked, (list, tuple, set)):
        raise OrchestrationError("协调节点标记必须是节点 ID 列表")
    marked_ids = {str(node_id).strip() for node_id in marked if str(node_id).strip()}
    if not marked_ids:
        return {}
    node_map = spec.node_map()
    missing = sorted(marked_ids - set(node_map))
    if missing:
        raise OrchestrationError("协调节点不存在：" + "、".join(missing))
    parents = draft_meta.get("parents") or {}
    if not isinstance(parents, dict):
        raise OrchestrationError("协调节点的父子映射无效")

    contracts: dict[str, dict[str, Any]] = {}
    for node_id in marked_ids:
        node = node_map[node_id]
        if node.kind == "gate":
            raise OrchestrationError("协调汇总节点不能是人工门禁")
        declared = [
            child_id for child_id, parent_id in parents.items()
            if parent_id == node_id and child_id in node_map and child_id != node_id
        ]
        declared_set = set(declared)
        # Conditions such as fail/loop describe execution semantics, not the
        # direction of the coordination anchor.  The global topology checker
        # validates those semantics; here we only need a direct declared-child
        # edge to identify the boundary.
        entry_children = list(dict.fromkeys(
            edge.to_node for edge in spec.edges
            if edge.from_node == node_id and edge.to_node in declared_set
        )) if declared else []
        exit_children = list(dict.fromkeys(
            edge.from_node for edge in spec.edges
            if edge.to_node == node_id and edge.from_node in declared_set
        )) if declared else []
        if entry_children and exit_children:
            raise OrchestrationError(
                f"协调节点 {node_id} 的入口/出口子边结构有歧义"
            )
        if entry_children:
            mode = "entry"
            children = declared
            boundary = {node_id, *children}
        elif exit_children:
            mode = "exit"
            children = declared
            boundary = {node_id, *children}
        else:
            raise OrchestrationError(
                f"协调节点 {node_id} 必须有明确的入口或出口子边"
            )
        child_edges = [edge for edge in spec.edges
                       if edge.from_node in boundary and edge.to_node in boundary]
        child_nodes = [node_map[child_id] for child_id in children]
        declarations = []
        for item in (spec.goal_contract.run_inputs if spec.goal_contract else ()):
            value = dict(item)
            declarations.append({
                "name": value.get("name", ""),
                "label": value.get("label") or value.get("name", ""),
                "collect_at": value.get("collect_at", "start"),
            })
        contracts[node_id] = {
            "mode": mode,
            "child_node_ids": children,
            "child_edges": [
                {
                    "from_node": edge.from_node,
                    "to_node": edge.to_node,
                    "condition": normalize_edge_condition(edge.condition or "always"),
                }
                for edge in child_edges
                if (edge.to_node if mode == "entry" else edge.from_node) in children
            ],
            "known_inputs": list(node.plan_brief.known_inputs) if node.plan_brief else [],
            "original_outputs": list(node.plan_brief.outputs) if node.plan_brief else [],
            "original_acceptance": list(node.plan_brief.acceptance) if node.plan_brief else [],
            "run_input_collect_at": declarations,
        }
    return contracts


def _coordination_responsibility(
    node: OrchestrationNode,
    contract: dict[str, Any],
    node_map: dict[str, OrchestrationNode],
) -> str:
    children = [node_map[child_id] for child_id in contract["child_node_ids"]]
    child_text = "；".join(
        f"{child.label or child.node_id}（{child.node_id}）"
        for child in children
    )
    known = contract.get("known_inputs") or []
    known_text = "；".join(known) if known else "沿用已绑定的当前输入，不猜测外部授权或凭据"
    collect = contract.get("run_input_collect_at") or []
    collect_text = "；".join(
        f"{item.get('label') or item.get('name')}→{item.get('collect_at', 'start')}"
        for item in collect if item.get("name")
    ) or "无新增运行参数声明"
    if contract.get("mode") == "entry":
        return (
            "[COORDINATE] ENTRY：本节点只做前向协调交接，不执行下游制作或写入。"
            f"\n已知范围：{known_text}。"
            f"\n下游任务：{child_text}。"
            f"\n参数收集阶段：{collect_text}。当前入口只收集本节点已到期的字段；未来字段到对应节点再收集。"
            "\n明确分派与交接即可继续，不要求未来子结果、全书产物或下游业务验收；"
            "原业务合同、产物和验收仍由下游节点及最终独立审查承担。"
        )
    return (
        "[COORDINATE] EXIT：本节点只做后向协调汇总，不把汇总动作当作业务完成。"
        f"\n待核对子阶段：{child_text}。"
        f"\n参数收集阶段：{collect_text}。"
        "\n必须逐项核对子结果、版本、证据与未通过项；原业务产物和验收合同保持不变。"
        + ("\n" + _brief_responsibility(node) if _brief_responsibility(node) else "")
    )


def _format_constraint_pipeline(
    children: list[OrchestrationNode],
) -> str:
    lines = ["约束管线（子阶段）：", _CONSTRAINT_PIPELINE_HINT]
    for index, child in enumerate(children, start=1):
        title = child.label or child.node_id
        body = _brief_responsibility(child).strip() or title
        lines.append(f"{index}. [{title}] {body}")
    return "\n".join(lines)


def _remap_edges_through_parents(
    edges: tuple[OrchestrationEdge, ...],
    *,
    child_to_parent: dict[str, str],
    scheduled_ids: set[str],
) -> list[dict[str, str]]:
    def resolve(node_id: str) -> str:
        if node_id in scheduled_ids:
            return node_id
        return child_to_parent.get(node_id, node_id)

    remapped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for edge in edges:
        frm = resolve(edge.from_node)
        to = resolve(edge.to_node)
        if frm not in scheduled_ids or to not in scheduled_ids:
            continue
        if frm == to:
            continue
        # Folding a skipped sibling through the parent must not create
        # child → parent, which would cycle with the group entry edge.
        if child_to_parent.get(frm) == to:
            continue
        condition = normalize_edge_condition(edge.condition or "always")
        key = (frm, to, condition)
        if key in seen:
            continue
        seen.add(key)
        remapped.append(
            {
                "from_node": frm,
                "to_node": to,
                "condition": condition,
                **({"input_when": dict(edge.input_when)} if edge.input_when is not None else {}),
                **({"branch_group": edge.branch_group} if edge.branch_group else {}),
            }
        )
    return remapped


def _skipped_constraint_child_ids(
    child_to_parent: dict[str, str],
    branched_ids: set[str],
) -> set[str]:
    """Fold unnamed always-only children; keep a whole group if any child branches."""
    grouped: dict[str, set[str]] = {}
    for child_id, parent_id in child_to_parent.items():
        grouped.setdefault(parent_id, set()).add(child_id)
    keep_groups = {
        parent_id
        for parent_id, children in grouped.items()
        if children & branched_ids
    }
    return {
        child_id
        for child_id, parent_id in child_to_parent.items()
        if child_id not in branched_ids and parent_id not in keep_groups
    }


def to_expert_flow(spec: OrchestrationSpec) -> ExpertFlowSpec:
    if spec.meta.get('workflow_core'):
        from mewcode.workflow_core.web import envelope
        from mewcode.workflow_core.schema import normalize
        original = spec.as_dict()
        original['meta']['run_input_contract'] = list(spec.goal_contract.run_inputs) if spec.goal_contract else []
        original['meta']['goal_contract'] = spec.goal_contract.as_dict() if spec.goal_contract else {}
        original['flow_id'] = spec.spec_id
        return envelope(normalize(spec.meta['workflow_core']), original)
    """Map product orchestration graph onto ExpertFlowSpec for _run_graph.

    Refine-group children are constraint pipelines on the parent: they are
    removed from the schedulable node list, folded into the parent
    responsibility text, and edges are remapped through the parent.
    """
    if spec.meta.get("canvas_mode") == "chat_activity":
        raise OrchestrationError("对话进度是只读活动视图，不能作为执行方案运行")
    live_ids = {node.node_id for node in spec.nodes}
    forward_edges, loops = compile_loops(spec.nodes, spec.edges, spec.meta.get("loops"))
    loop_node_ids = {node_id for loop in loops for node_id in loop.node_ids}
    groups = resolve_canvas_groups(spec)
    child_to_parent = _child_parent_map(groups, live_ids)
    branched_ids = {
        node_id
        for edge in spec.edges
        for node_id in (edge.from_node, edge.to_node)
        if normalize_edge_condition(edge.condition) in {"pass", "fail"}
    }
    branched_ids.update(loop_node_ids)
    # Execution is explicit; collapsing a container is a visual action only.
    for group in groups.values():
        if group.get("mode") == "executable":
            branched_ids.update(group.get("children") or [])
    # Nested checkpoints that already have 正确/错误分流 must stay schedulable.
    # Folding them into the parent is how extra 「修订实验」 boxes vanished from
    # 总进度 after the canvas closed.
    skipped_ids = _skipped_constraint_child_ids(child_to_parent, branched_ids)
    node_map = spec.node_map()
    constraints_by_parent: dict[str, list[OrchestrationNode]] = {}
    for child_id, parent_id in child_to_parent.items():
        child = node_map.get(child_id)
        if child is None:
            continue
        constraints_by_parent.setdefault(parent_id, []).append(child)

    # Preserve canvas_groups child order when present.
    for parent_id, children in list(constraints_by_parent.items()):
        order = {
            child_id: index
            for index, child_id in enumerate(
                (groups.get(parent_id) or {}).get("children") or []
            )
        }
        children.sort(key=lambda node: order.get(node.node_id, 10**9))
        constraints_by_parent[parent_id] = children

    draft_meta = spec.meta.get("workflow_draft") or {}
    coordination_contracts = _coordination_contracts(spec, draft_meta)
    for contract in coordination_contracts.values():
        for item in contract.get("run_input_collect_at", []):
            target = item.get("collect_at", "start")
            if target in skipped_ids:
                item["collect_at"] = child_to_parent[target]
    coordination_ids = set(coordination_contracts)
    nodes: list[dict[str, Any]] = []
    for node in spec.nodes:
        if node.node_id in skipped_ids:
            continue
        role = kind_roles().get(node.kind)
        if role is None:
            raise OrchestrationError(f"cannot map kind {node.kind}")
        responsibility = _brief_responsibility(node)
        if node.node_id in coordination_ids:
            responsibility = _coordination_responsibility(
                node, coordination_contracts[node.node_id], node_map
            )
            if node.kind != "audit":
                role = "planner"
        pipeline_children = [
            child
            for child in (constraints_by_parent.get(node.node_id) or [])
            if child.node_id in skipped_ids
        ]
        if pipeline_children:
            block = _format_constraint_pipeline(pipeline_children)
            responsibility = (
                f"{responsibility.rstrip()}\n\n{block}"
                if responsibility.strip()
                else block
            )
        if node.kind == "gate":
            responsibility = (
                "[GATE] 执行到此节点时必须等待用户明确选择；开跑前确认不替代此处确认。"
                "不得由模型自行宣告人工通过；继续也不代表交付验收通过。\n"
                + responsibility
            )
        outgoing = [edge for edge in spec.edges if edge.from_node == node.node_id]
        has_fail_branch = any(
            normalize_edge_condition(edge.condition) == "fail" for edge in outgoing
        )
        fail_targets = {
            edge.to_node
            for edge in outgoing
            if normalize_edge_condition(edge.condition) == "fail"
        }
        finite_recheck = bool(
            node.audit_target
            and (not has_fail_branch or node.audit_target in fail_targets)
        )
        failure: dict[str, Any]
        if node.on_blocked == "fallback" and node.fallback_node_ids:
            failure = {
                "action": "fallback",
                "target_node_id": node.fallback_node_ids[0],
                "fallback_node_ids": list(node.fallback_node_ids),
            }
        elif node.node_id in loop_node_ids or node.kind == "decision":
            # A loop owns the complete budget; node retries must not multiply it.
            failure = {"action": "stop", "max_attempts": 1}
        elif node.kind == "audit" and finite_recheck:
            # A canvas repair→audit back-edge is removed from the executable
            # DAG and represented by audit_target.  Prefer that finite recovery
            # contract even when the forward fail edge is still present.
            target = node.audit_target
            if target in skipped_ids:
                target = child_to_parent.get(target, "")
            failure = {
                "action": "route",
                "max_attempts": node.max_audit_retries,
                "target_node_id": target,
            }
            if not target:
                failure = {"action": "stop", "max_attempts": 1}
        elif node.kind == "audit" and has_fail_branch:
            # A one-way error branch is a normal successor, not rework.
            failure = {"action": "stop", "max_attempts": 1}
        elif node.kind == "implement":
            failure = {"action": "retry", "max_attempts": 3}
        else:
            failure = {"action": "stop", "max_attempts": 1}
        payload: dict[str, Any] = {
            "node_id": node.node_id,
            "role": role,
            "label": node.label,
            "kind": "role",
            "responsibility": responsibility,
            "reducer": "collect",
            "failure_policy": failure,
        }
        if node.tools is not None:
            payload["tools"] = list(node.tools)
        if node.deny_tools:
            payload["deny_tools"] = list(node.deny_tools)
        if node.capabilities is not None:
            payload['capabilities'] = node.capabilities
        if node.node_id in coordination_ids:
            payload["tools"] = ["ReadFile", "Glob", "Grep"]
        model = _binding_to_ref(node.model)
        if model is not None:
            payload["model"] = model.as_dict()
        if node.position is not None:
            payload["position"] = dict(node.position)
        if node.kind == "gate":
            payload["responsibility"] = responsibility
        elif node.prefers:
            payload["responsibility"] = f"[prefers:{node.prefers}] {responsibility}"
        nodes.append(payload)

    scheduled_ids = {item["node_id"] for item in nodes}
    edges = _remap_edges_through_parents(
        tuple(forward_edges),
        child_to_parent=child_to_parent,
        scheduled_ids=scheduled_ids,
    )
    finalizer = spec.finalizer_node_id
    if finalizer in skipped_ids:
        finalizer = child_to_parent[finalizer]
    if finalizer not in scheduled_ids and scheduled_ids:
        finalizer = nodes[-1]["node_id"]

    meta: dict[str, Any] = {}
    if spec.goal_contract and spec.goal_contract.run_inputs:
        declarations = []
        for item in spec.goal_contract.run_inputs:
            value = dict(item)
            target = value.get("collect_at", "start")
            if target in skipped_ids:
                value["collect_at"] = child_to_parent[target]
            declarations.append(value)
        meta["run_input_contract"] = declarations
    if spec.meta.get("control_flow_version"):
        meta["control_flow_version"] = spec.meta["control_flow_version"]
    meta["node_kinds"] = {node.node_id: node.kind for node in spec.nodes if node.node_id in scheduled_ids}
    # Work identity and input bindings belong to every projected workflow,
    # including graphs without coordination nodes. The design identity must
    # also survive dispatch and reverse conversion for resume/completion.
    if draft_meta:
        meta["workflow_draft"] = deepcopy(draft_meta)
    if spec.meta.get("task_design"):
        meta["task_design"] = deepcopy(spec.meta["task_design"])
    for key in ("orchestration_mode", "collaboration_spec", "role_models", "workflow_user_limits"):
        if key in spec.meta:
            meta[key] = deepcopy(spec.meta[key])
    if draft_meta.get("coordination_nodes"):
        meta["coordination_original_goals"] = {node.node_id: node.goal for node in spec.nodes
            if node.node_id in spec.meta["workflow_draft"]["coordination_nodes"]}
        meta["coordination_contracts"] = coordination_contracts
    if "parallel" in spec.meta:
        meta["parallel"] = spec.meta["parallel"] is True
    meta["node_plan_briefs"] = {node.node_id: node.plan_brief.as_dict() for node in spec.nodes
                                if node.plan_brief and node.node_id in scheduled_ids}
    review_channels = {node.node_id: list(node.review_channels) for node in spec.nodes
                       if node.review_channels is not None and node.node_id in scheduled_ids}
    if review_channels:
        meta['node_review_channels'] = review_channels
        meta['node_audit_targets'] = {node.node_id: node.audit_target for node in spec.nodes
                                    if node.audit_target and node.node_id in review_channels}
    if loops:
        meta["loops"] = [loop.as_dict() for loop in loops]
    if spec.meta.get("shared_constraints"):
        meta["shared_constraints"] = spec.meta["shared_constraints"]
    from mewcode.orchestration.constraints import compile_constraints
    node_constraints = (compile_constraints(spec.meta.get("annotations") or [], set(spec.node_map()))
                        if "annotations" in spec.meta else spec.meta.get("node_constraints") or {})
    # Non-executable refinement notes belong to their scheduling parent.
    for child_id in list(node_constraints):
        if child_id in skipped_ids:
            node_constraints.setdefault(child_to_parent[child_id], []).extend(node_constraints.pop(child_id))
    if node_constraints:
        meta["node_constraints"] = node_constraints
    graph_id = str((spec.meta or {}).get("pipeline_graph_id") or "").strip()
    if spec.meta.get("conversation_id"):
        meta["conversation_id"] = spec.meta["conversation_id"]
    if spec.meta.get("maintenance_run_bindings"):
        meta["maintenance_run_bindings"] = spec.meta["maintenance_run_bindings"]
    if isinstance((spec.meta or {}).get("audit_rubrics"), dict):
        meta["audit_rubrics"] = spec.meta["audit_rubrics"]
    if graph_id:
        meta["pipeline_graph_id"] = graph_id
    if groups:
        meta["canvas_groups"] = dict(groups)
    if skipped_ids:
        meta["schedule_skipped_node_ids"] = sorted(skipped_ids)
        meta["constraint_pipelines"] = {
            parent_id: [
                {
                    "node_id": child.node_id,
                    "label": child.label,
                    "goal": child.goal,
                }
                for child in children
                if child.node_id in skipped_ids
            ]
            for parent_id, children in constraints_by_parent.items()
            if any(child.node_id in skipped_ids for child in children)
        }
    payload: dict[str, Any] = {
        "flow_id": spec.spec_id or "project.orchestration",
        "version": max(1, int(spec.version)),
        "label": spec.label,
        "nodes": nodes,
        "edges": edges,
        "finalizer": finalizer,
    }
    if meta:
        payload["meta"] = meta
    return ExpertFlowSpec.from_value(payload)


def from_expert_flow(flow: ExpertFlowSpec) -> OrchestrationSpec:
    """Best-effort reverse map for UI display."""
    nodes: list[OrchestrationNode] = []
    for node in flow.nodes:
        kind = (flow.meta.get("node_kinds") or {}).get(node.node_id) or _ROLE_TO_KIND.get(node.role, "implement")
        if node.responsibility.startswith("[GATE]"):
            kind = "gate"
        prefers = ""
        goal = node.responsibility
        if goal.startswith("[prefers:") and "]" in goal:
            prefers = goal[len("[prefers:"): goal.index("]")]
            goal = goal[goal.index("]") + 1:].strip()
        if node.node_id in (flow.meta.get("coordination_original_goals") or {}):
            goal = flow.meta["coordination_original_goals"][node.node_id]
        audit_target = (flow.meta.get('node_audit_targets') or {}).get(node.node_id, '')
        max_retries = 3
        if kind == "audit" and node.failure_policy.action == "route":
            audit_target = node.failure_policy.target_node_id
            max_retries = node.failure_policy.max_attempts
        position = None
        if node.position is not None:
            position = {"x": float(node.position.x), "y": float(node.position.y)}
        nodes.append(
            OrchestrationNode(
                node_id=node.node_id,
                kind=kind,  # type: ignore[arg-type]
                label=node.label,
                goal=compact_plan_text(clean_generated_responsibility(goal)),
                prefers=prefers,
                model=_ref_to_binding(node.model),
                bind_later=node.model is None and kind != "gate",
                audit_target=audit_target,
                max_audit_retries=max_retries,
                position=position,
                tools=node.tools,
                deny_tools=node.deny_tools,
                capabilities=node.capabilities,
                fallback_node_ids=(node.failure_policy.fallback_node_ids or (
                    (node.failure_policy.target_node_id,)
                    if node.failure_policy.action == "fallback" and node.failure_policy.target_node_id
                    else ()
                )),
                on_blocked=("fallback" if node.failure_policy.action == "fallback"
                            and (node.failure_policy.fallback_node_ids or node.failure_policy.target_node_id)
                            else ""),
                plan_brief=PlanBrief.from_value(
                    (flow.meta.get("node_plan_briefs") or {}).get(node.node_id)),
                review_channels=parse_review_channels((flow.meta.get('node_review_channels') or {}).get(node.node_id)),
            )
        )
    edges = tuple(
        OrchestrationEdge(
            from_node=edge.from_node,
            to_node=edge.to_node,
            condition=edge.condition,
            input_when=edge.input_when,
            branch_group=edge.branch_group,
        )
        for edge in flow.edges
    )
    meta: dict[str, Any] = {}
    flow_meta = flow.meta if isinstance(flow.meta, dict) else {}
    for key in ("loops", "shared_constraints", "node_kinds", "control_flow_version", "parallel", "node_constraints", "pipeline_graph_id", "conversation_id", "workflow_draft", "task_design", "coordination_contracts", "coordination_original_goals", "run_input_contract", "maintenance_run_bindings", "orchestration_mode", "collaboration_spec", "role_models"):
        if key in flow_meta:
            meta[key] = flow_meta[key]
    edges += tuple(OrchestrationEdge(from_node=loop["check_node_id"], to_node=target, condition="loop")
                   for loop in flow_meta.get("loops") or []
                   for target in [loop["entry_node_id"], *(loop.get("repair_node_ids", [])
                       if loop.get("repair_dispatch") == "after_decision" else [])])
    if isinstance(flow_meta.get("audit_rubrics"), dict):
        meta["audit_rubrics"] = flow_meta["audit_rubrics"]
    graph_id = str(flow_meta.get("pipeline_graph_id") or "").strip()
    if graph_id:
        meta["pipeline_graph_id"] = graph_id
    groups = flow_meta.get("canvas_groups")
    if isinstance(groups, dict) and groups:
        meta["canvas_groups"] = groups
    return OrchestrationSpec(
        spec_id=flow.flow_id,
        label=flow.label,
        version=flow.version,
        nodes=tuple(nodes),
        edges=edges,
        finalizer_node_id=flow.finalizer_node_id,
        template_id="",
        goal="",
        goal_contract=GoalContract.from_value({"run_inputs": flow_meta.get("run_input_contract", [])}),
        require_confirm_gate=any(n.kind == "gate" for n in nodes),
        meta=meta,
    )


def apply_profile_bindings(
    spec: OrchestrationSpec,
    *,
    manager: dict[str, str] | None = None,
    planner: dict[str, str] | None = None,
    executor: dict[str, str] | None = None,
    auditor: dict[str, str] | None = None,
    frontend: dict[str, str] | None = None,
    backend: dict[str, str] | None = None,
) -> OrchestrationSpec:
    """Fill unbound nodes from classic ExpertProfile-style seats."""
    role_defaults = {
        "plan": planner or manager,
        "implement": executor or manager,
        "audit": auditor or manager,
        "decision": auditor or manager,
        "reduce": manager or executor,
        "gate": None,
    }
    prefer_defaults = {
        "frontend": frontend or executor,
        "backend": backend or executor,
    }
    new_nodes: list[OrchestrationNode] = []
    for node in spec.nodes:
        if node.model and node.model.is_complete:
            new_nodes.append(node)
            continue
        raw = None
        if node.prefers in prefer_defaults:
            raw = prefer_defaults[node.prefers]
        if raw is None:
            raw = role_defaults.get(node.kind)
        binding = ModelBinding.from_value(raw) if raw else None
        new_nodes.append(
            replace(
                node,
                model=binding,
                bind_later=binding is None and node.kind != "gate",
            )
        )
    return replace(spec, nodes=tuple(new_nodes))


def merge_node_models(
    target: OrchestrationSpec,
    source: OrchestrationSpec | None,
) -> OrchestrationSpec:
    """Copy complete models from a previous spec onto matching nodes.

    Prefers the same ``node_id``, then the same ``label`` (a regenerated graph
    often renames ids while keeping stage names), then the same ``kind`` +
    ``prefers``. Nodes that already have a complete model are left unchanged.
    A label is only used when it identifies exactly one previous stage, and
    each previous stage is consumed once so distinct stages cannot collapse
    onto a single model.
    """
    if source is None:
        return target
    source_by_id = source.node_map()
    source_by_label: dict[str, OrchestrationNode] = {}
    ambiguous_labels: set[str] = set()
    source_by_kind: dict[str, OrchestrationNode] = {}
    for node in source.nodes:
        if node.model is None or not node.model.is_complete:
            continue
        label = node.label.strip()
        if label:
            if label in source_by_label:
                ambiguous_labels.add(label)
            else:
                source_by_label[label] = node
        source_by_kind.setdefault(f"{node.kind}:{node.prefers}", node)
        source_by_kind.setdefault(node.kind, node)
    for label in ambiguous_labels:
        source_by_label.pop(label, None)

    def is_bound(node: OrchestrationNode | None) -> bool:
        return node is not None and node.model is not None and node.model.is_complete

    claimed: set[str] = set()
    new_nodes: list[OrchestrationNode] = []
    for node in target.nodes:
        if node.model is not None and node.model.is_complete:
            new_nodes.append(node)
            continue
        previous = source_by_id.get(node.node_id)
        if not is_bound(previous) or previous.node_id in claimed:
            previous = source_by_label.get(node.label.strip())
        if not is_bound(previous) or previous.node_id in claimed:
            previous = source_by_kind.get(f"{node.kind}:{node.prefers}")
        if not is_bound(previous) or previous.node_id in claimed:
            previous = source_by_kind.get(node.kind)
        if is_bound(previous) and previous.node_id not in claimed:
            claimed.add(previous.node_id)
            new_nodes.append(replace(node, model=previous.model, bind_later=False))
            continue
        new_nodes.append(node)
    return replace(target, nodes=tuple(new_nodes))
