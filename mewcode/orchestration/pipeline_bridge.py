"""Bidirectional bridge between OrchestrationSpec and PipelineGraph (canvas).

Pipeline execution is not used here — the graph is a visual editor for the
product orchestration spec. Kind / bindings / audit metadata ride in
``variables["__orch.payload__"]`` so round-trips stay lossless.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from mewcode.orchestration.convert import (
    clean_generated_responsibility,
    resolve_canvas_groups,
)
from mewcode.orchestration.extract import repair_extracted_graph_cycles
from mewcode.orchestration.loops import compile_loops, is_loop_edge
from mewcode.orchestration.kinds import parse_tools
from mewcode.orchestration.spec import (
    NODE_KINDS,
    GoalContract,
    LoopPolicy,
    ModelBinding,
    OrchestrationEdge,
    OrchestrationError,
    OrchestrationNode,
    OrchestrationSpec,
    PlanBrief,
    SpecAction,
    parse_review_channels,
)
from mewcode.pipeline.models import (
    GraphEdge,
    GraphNode,
    NodeType,
    PipelineGraph,
    Position,
)

ORCH_FLAG = "__orchestration__"
ORCH_PAYLOAD = "__orch.payload__"
START_ID = "__orch_start__"
END_ID = "__orch_end__"

_KIND_GAP_X = 280.0
_KIND_Y = 160.0


def is_orchestration_graph(graph: PipelineGraph | dict[str, Any] | None) -> bool:
    if graph is None:
        return False
    variables = (
        graph.variables
        if isinstance(graph, PipelineGraph)
        else (graph.get("variables") if isinstance(graph, dict) else None)
    )
    if not isinstance(variables, dict):
        return False
    return str(variables.get(ORCH_FLAG) or "") in {"1", "true", "yes"}


def _meta_int(meta: dict[str, Any], key: str, default: int) -> int:
    """Coerce a meta field that the canvas may have stored as a string."""
    value = meta.get(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # 1e999 in JSON parses to inf, and int(inf) raises OverflowError.
        raise OrchestrationError(f"canvas meta {key} must be an integer, got {value!r}") from None


def _payload_from_variables(
    variables: dict[str, str], *, strict: bool = False
) -> dict[str, Any]:
    """Parse the canvas payload.

    An absent payload means an older canvas and is fine. A payload that is
    present but unparseable is different: treating it as empty would rebuild the
    spec from graph nodes only, silently dropping every model binding, plan
    brief, group and annotation. Callers that rebuild a spec ask for ``strict``
    so the user gets an error instead of a quietly degraded orchestration.
    """
    raw = variables.get(ORCH_PAYLOAD) or ""
    if not str(raw).strip():
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        if strict:
            raise OrchestrationError(
                f"画布 payload 无法解析（可能被截断或手工改坏）：{exc}"
            ) from None
        return {}
    if not isinstance(value, dict):
        if strict:
            raise OrchestrationError("画布 payload 必须是 JSON 对象")
        return {}
    return value


def _layout_position(index: int, node: OrchestrationNode) -> Position:
    if node.position and "x" in node.position and "y" in node.position:
        return Position(x=float(node.position["x"]), y=float(node.position["y"]))
    return Position(x=80.0 + index * _KIND_GAP_X, y=_KIND_Y)


_GROUP_PAD = 24.0
_GROUP_HEADER_H = 104.0
_GROUP_MIN_W = 460.0
_GROUP_MIN_H = 320.0
_GROUP_MAX_H = 1800.0
_CHILD_CARD_H = 160.0
_CHILD_GAP = 28.0


def _node_recovery_meta(orch_node: OrchestrationNode) -> dict[str, Any]:
    """Fallback/escalation fields that ride the canvas meta losslessly."""
    entry: dict[str, Any] = {"on_blocked": orch_node.on_blocked}
    entry["fallback_node_ids"] = list(orch_node.fallback_node_ids)
    if orch_node.escalation_model is not None:
        entry["escalation_model"] = orch_node.escalation_model.as_dict()
    if orch_node.max_auto_recovery != 1:
        entry["max_auto_recovery"] = orch_node.max_auto_recovery
    if orch_node.loop_policy is not None:
        entry["loop_policy"] = orch_node.loop_policy.as_dict()
    if orch_node.review_channels is not None:
        entry["review_channels"] = list(orch_node.review_channels)
    return entry


def _child_is_peer_layout(parent: GraphNode, child: GraphNode) -> bool:
    """True when a child still sits in the sequential peer row, not inside the parent."""
    dx = float(child.position.x) - float(parent.position.x)
    dy = float(child.position.y) - float(parent.position.y)
    if abs(dx) <= _GROUP_MIN_W and -8 <= dy <= _GROUP_MAX_H:
        return False
    if abs(dy) <= 80 and abs(dx) >= 200:
        return True
    return abs(dx) >= _GROUP_MIN_W


def _apply_nested_refine_layout(
    nodes: list[GraphNode],
    groups: dict[str, dict[str, Any]],
    node_meta: dict[str, Any],
) -> None:
    """Stack unnamed refine children inside the parent box instead of a peer row."""
    if not groups:
        return
    index_by_id = {node.id: index for index, node in enumerate(nodes)}
    for parent_id, info in groups.items():
        parent_index = index_by_id.get(parent_id)
        if parent_index is None:
            continue
        parent = nodes[parent_index]
        if parent.type != NodeType.TASK:
            continue
        child_ids = [
            str(item).strip()
            for item in (info.get("children") or [])
            if str(item).strip()
        ]
        children = []
        for child_id in child_ids:
            child_index = index_by_id.get(child_id)
            if child_index is None:
                continue
            child = nodes[child_index]
            if child.type == NodeType.TASK:
                children.append(child)
        if not children or not any(
            _child_is_peer_layout(parent, child) for child in children
        ):
            continue
        origin_x = float(parent.position.x)
        origin_y = float(parent.position.y)
        inner_w = max(220.0, _GROUP_MIN_W - _GROUP_PAD * 2)
        cursor_y = origin_y + _GROUP_HEADER_H + _GROUP_PAD
        child_x = origin_x + _GROUP_PAD
        stacked = 0
        for child in children:
            child_index = index_by_id[child.id]
            nodes[child_index] = child.model_copy(
                update={"position": Position(x=child_x, y=cursor_y)}
            )
            entry = node_meta.setdefault(child.id, {})
            entry["canvas_size"] = {"width": inner_w, "height": _CHILD_CARD_H}
            cursor_y += _CHILD_CARD_H + _CHILD_GAP
            stacked += 1
        content_h = (
            cursor_y - origin_y + _GROUP_PAD - _CHILD_GAP
            if stacked
            else _GROUP_MIN_H
        )
        entry = node_meta.setdefault(parent_id, {})
        entry["canvas_group_size"] = {
            "width": _GROUP_MIN_W,
            "height": min(_GROUP_MAX_H, max(_GROUP_MIN_H, content_h)),
        }
        entry["checkpoint_layout_version"] = 1


def to_pipeline_graph(
    spec: OrchestrationSpec,
    *,
    graph_id: str = "",
    version: int = 1,
) -> PipelineGraph:
    """Materialize a savable PipelineGraph for the orchestration canvas."""
    if not spec.nodes:
        raise OrchestrationError("orchestration needs at least one node")
    forward, loops = compile_loops(spec.nodes, spec.edges, spec.meta.get("loops"))
    display_edges = list(spec.edges)
    returns = {(edge.from_node, edge.to_node) for edge in display_edges if is_loop_edge(edge.condition)}
    for loop in loops:
        targets = [loop.entry_node_id, *(loop.repair_node_ids if loop.repair_dispatch == "after_decision" else [])]
        for target in targets:
            if (loop.check_node_id, target) not in returns:
                display_edges.append(OrchestrationEdge(loop.check_node_id, target, "loop"))

    resolved_groups = resolve_canvas_groups(spec)
    if resolved_groups:
        spec = replace(
            spec,
            meta={**(spec.meta or {}), "canvas_groups": resolved_groups},
        )

    node_meta: dict[str, Any] = {}
    nodes: list[GraphNode] = [
        GraphNode(
            id=START_ID,
            type=NodeType.START,
            label="开始",
            position=Position(x=40.0, y=_KIND_Y + 28),
        )
    ]
    canvas_groups = {}
    raw_groups = (spec.meta or {}).get("canvas_groups")
    if isinstance(raw_groups, dict):
        canvas_groups = {
            str(gid): dict(info)
            for gid, info in raw_groups.items()
            if isinstance(gid, str) and isinstance(info, dict)
        }

    for index, orch_node in enumerate(spec.nodes):
        binding = orch_node.model.as_dict() if orch_node.model is not None else {}
        entry: dict[str, Any] = {
            "kind": orch_node.kind,
            "prefers": orch_node.prefers,
            "bind_later": orch_node.bind_later,
            "audit_target": orch_node.audit_target,
            "max_audit_retries": orch_node.max_audit_retries,
            "model": binding,
        }
        if orch_node.plan_brief is not None:
            entry["plan_brief"] = orch_node.plan_brief.as_dict()
        if orch_node.tools is not None:
            entry["tools"] = list(orch_node.tools)
        if orch_node.deny_tools:
            entry["deny_tools"] = list(orch_node.deny_tools)
        if orch_node.capabilities is not None:
            entry['capabilities'] = orch_node.capabilities
        entry.update(_node_recovery_meta(orch_node))
        group_info = canvas_groups.get(orch_node.node_id)
        if group_info is not None:
            entry["refine_group"] = True
            entry["group_expanded"] = bool(group_info.get("expanded", True))
            entry["group_mode"] = group_info.get("mode", "constraints")
        for gid, info in canvas_groups.items():
            children = info.get("children") if isinstance(info.get("children"), list) else []
            if orch_node.node_id in {str(item) for item in children}:
                entry["parent_group"] = gid
                break
        node_meta[orch_node.node_id] = entry
        model_id = str(binding.get("model_id") or "") or None
        effort = str(binding.get("effort") or "") or None
        title = orch_node.label or orch_node.node_id
        if orch_node.kind:
            title = f"[{orch_node.kind}] {title}"
        nodes.append(
            GraphNode(
                id=orch_node.node_id,
                type=NodeType.TASK,
                title=title,
                label=orch_node.label or orch_node.node_id,
                goal=orch_node.goal or orch_node.label or orch_node.node_id,
                model=model_id,
                effort=effort,
                position=_layout_position(index, orch_node),
            )
        )
    _apply_nested_refine_layout(nodes, canvas_groups, node_meta)
    nodes.append(
        GraphNode(
            id=END_ID,
            type=NodeType.END,
            label="结束",
            position=Position(
                x=80.0 + max(len(spec.nodes) - 1, 0) * _KIND_GAP_X + 280.0,
                y=_KIND_Y + 28,
            ),
        )
    )

    edges: list[GraphEdge] = []
    roots = {node.node_id for node in spec.nodes}
    has_incoming = {edge.to_node for edge in forward}
    entry_ids = [node.node_id for node in spec.nodes if node.node_id not in has_incoming]
    if not entry_ids:
        entry_ids = [spec.nodes[0].node_id]
    for entry in entry_ids:
        edges.append(
            GraphEdge(id=f"e_{START_ID}_{entry}", from_node=START_ID, to_node=entry)
        )
    for index, edge in enumerate(display_edges):
        edges.append(
            GraphEdge(
                id=f"e_{edge.from_node}_{edge.to_node}_{index}",
                from_node=edge.from_node,
                to_node=edge.to_node,
                label="" if edge.condition in {"", "always"} else edge.condition,
            )
        )
    finals = {edge.from_node for edge in forward}
    exit_ids = [
        node.node_id
        for node in spec.nodes
        if node.node_id not in finals or node.node_id == spec.finalizer_node_id
    ]
    # Prefer finalizer; ensure at least one link into end.
    if spec.finalizer_node_id and spec.finalizer_node_id in roots:
        exit_ids = [spec.finalizer_node_id]
    elif not exit_ids:
        exit_ids = [spec.nodes[-1].node_id]
    seen_exit: set[str] = set()
    for exit_id in exit_ids:
        if exit_id in seen_exit:
            continue
        seen_exit.add(exit_id)
        edges.append(
            GraphEdge(id=f"e_{exit_id}_{END_ID}", from_node=exit_id, to_node=END_ID)
        )

    meta = dict(spec.meta or {})
    if loops:
        meta["loops"] = [loop.as_dict() for loop in loops]
    if graph_id:
        meta["pipeline_graph_id"] = graph_id
    payload = {
        "spec_id": spec.spec_id,
        "label": spec.label,
        "version": spec.version,
        "finalizer_node_id": spec.finalizer_node_id,
        "template_id": spec.template_id,
        "goal": spec.goal,
        "require_confirm_gate": spec.require_confirm_gate,
        "meta": meta,
        "nodes": node_meta,
    }
    if spec.goal_contract is not None:
        payload["goal_contract"] = spec.goal_contract.as_dict()
    input_edges = {f"e_{edge.from_node}_{edge.to_node}_{index}": {
        "from_node": edge.from_node, "to_node": edge.to_node,
        **({"input_when": dict(edge.input_when)} if edge.input_when is not None else {}),
        **({"branch_group": edge.branch_group} if edge.branch_group else {})}
        for index, edge in enumerate(display_edges) if edge.input_when is not None or edge.branch_group}
    if input_edges:
        payload["edge_inputs"] = input_edges
    if spec.actions:
        payload["actions"] = [action.as_dict() for action in spec.actions]
    variables = {
        ORCH_FLAG: "1",
        ORCH_PAYLOAD: json.dumps(payload, ensure_ascii=False),
    }
    graph_kwargs: dict[str, Any] = {
        "name": spec.label or "专家团编排",
        "description": spec.goal or "orchestration canvas",
        "version": max(1, int(version or 1)),
        "nodes": nodes,
        "edges": edges,
        "variables": variables,
    }
    if graph_id:
        graph_kwargs["graph_id"] = graph_id
    return PipelineGraph(**graph_kwargs)


_LAYOUT_META_KEYS = (
    "refine_group",
    "parent_group",
    "group_expanded",
    "group_mode",
    "canvas_group_size",
    "canvas_size",
    "checkpoint_layout_version",
)


def _payload_canvas_groups(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    raw = meta.get("canvas_groups")
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


def apply_canvas_groups_to_node_meta(
    node_meta: dict[str, Any],
    groups: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Write refine_group / parent_group flags from canvas_groups onto node meta."""
    if not groups:
        return node_meta
    next_meta = {
        str(node_id): dict(item) if isinstance(item, dict) else {}
        for node_id, item in node_meta.items()
    }
    live_ids = set(next_meta)
    child_to_parent: dict[str, str] = {}
    for parent_id, info in groups.items():
        if parent_id not in live_ids:
            continue
        entry = next_meta.setdefault(parent_id, {})
        entry["refine_group"] = True
        entry["group_expanded"] = bool(info.get("expanded", True))
        entry["group_mode"] = info.get("mode", "constraints")
        for child_id in info.get("children") or []:
            if child_id in live_ids and child_id != parent_id:
                child_to_parent[child_id] = parent_id
    for child_id, parent_id in child_to_parent.items():
        entry = next_meta.setdefault(child_id, {})
        entry["parent_group"] = parent_id
        entry.pop("refine_group", None)
    return next_meta


def _graph_has_canvas_only_edits(
    graph: PipelineGraph,
    spec: OrchestrationSpec,
) -> bool:
    """True when the stored canvas has stages or stage-edges the spec lacks."""
    spec_ids = {node.node_id for node in spec.nodes}
    graph_ids = {node.id for node in graph.nodes if node.type == NodeType.TASK}
    if graph_ids - spec_ids:
        return True
    spec_keys = {
        (
            edge.from_node,
            edge.to_node,
            str(edge.condition or "always").strip() or "always",
        )
        for edge in spec.edges
    }
    for edge in graph.edges:
        if edge.from_node not in graph_ids or edge.to_node not in graph_ids:
            continue
        condition = str(edge.label or "always").strip() or "always"
        if (edge.from_node, edge.to_node, condition) not in spec_keys:
            return True
    return False


def graph_has_canvas_nesting(graph: PipelineGraph | dict[str, Any] | None) -> bool:
    """True when the stored canvas carries refine nesting flags or canvas_groups."""
    if graph is None:
        return False
    variables = (
        graph.variables
        if isinstance(graph, PipelineGraph)
        else (graph.get("variables") if isinstance(graph, dict) else None)
    )
    if not isinstance(variables, dict):
        return False
    payload = _payload_from_variables(variables)
    if _payload_canvas_groups(payload):
        return True
    nodes = payload.get("nodes")
    if not isinstance(nodes, dict):
        return False
    for item in nodes.values():
        if not isinstance(item, dict):
            continue
        if item.get("refine_group") or str(item.get("parent_group") or "").strip():
            return True
    return False


def rehydrate_canvas_nesting(
    graph: PipelineGraph,
    *,
    groups: dict[str, Any] | None = None,
) -> PipelineGraph:
    """Restore refine_group / parent_group onto node meta from canvas_groups."""
    if not is_orchestration_graph(graph):
        return graph
    payload = _payload_from_variables(graph.variables or {})
    node_meta = (
        dict(payload.get("nodes"))
        if isinstance(payload.get("nodes"), dict)
        else {}
    )
    source_groups = groups if isinstance(groups, dict) else None
    if not source_groups:
        source_groups = _payload_canvas_groups(payload)
    parsed = {
        str(gid): dict(info)
        for gid, info in (source_groups or {}).items()
        if isinstance(gid, str) and isinstance(info, dict)
    }
    if not parsed:
        return graph
    # Normalize children lists.
    for info in parsed.values():
        children = info.get("children") if isinstance(info.get("children"), list) else []
        info["children"] = [str(item).strip() for item in children if str(item).strip()]
    next_meta = apply_canvas_groups_to_node_meta(node_meta, parsed)
    meta = dict(payload.get("meta") if isinstance(payload.get("meta"), dict) else {})
    meta["canvas_groups"] = parsed
    payload = {**payload, "nodes": next_meta, "meta": meta}
    return graph.model_copy(
        update={
            "variables": {
                ORCH_FLAG: "1",
                ORCH_PAYLOAD: json.dumps(payload, ensure_ascii=False),
            }
        }
    )


def _canvas_groups_from_node_meta(
    node_meta: dict[str, Any],
    labels: dict[str, str],
    preferred_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Rebuild groups from node flags, keeping any name the user already gave.

    ``labels`` maps a group id to its *child node's* label, which is only a
    fallback: a container the user renamed must keep that name across a sync.
    """
    preferred = preferred_labels or {}
    groups: dict[str, Any] = {}
    for node_id, meta_item in node_meta.items():
        if not isinstance(meta_item, dict) or not meta_item.get("refine_group"):
            continue
        key = str(node_id)
        label = str(preferred.get(key) or "").strip() or labels.get(key, key)
        groups[key] = {
            "expanded": bool(meta_item.get("group_expanded", True)),
            "mode": meta_item.get("group_mode", "constraints"),
            "children": [],
            "label": label,
        }
    for node_id, meta_item in node_meta.items():
        if not isinstance(meta_item, dict):
            continue
        parent = str(meta_item.get("parent_group") or "").strip()
        if parent and parent in groups:
            groups[parent]["children"].append(str(node_id))
    return groups


def sync_orchestration_graph_from_spec(
    graph: PipelineGraph,
    spec: OrchestrationSpec,
) -> PipelineGraph:
    """Refresh stage content from ``spec`` without destroying canvas nesting.

    Parent/child refine groups are a constraint pipeline on the parent task.  They
    live in the stored PipelineGraph payload.  Reopening the canvas must not
    flatten them by regenerating a brand-new graph from a plan session that may
    have dropped ``meta.canvas_groups``.
    """
    if not is_orchestration_graph(graph):
        return to_pipeline_graph(
            spec,
            graph_id=str(graph.graph_id or ""),
            version=max(1, int(graph.version or 1)),
        )
    if not spec.nodes:
        raise OrchestrationError("orchestration needs at least one node")

    resolved_groups = resolve_canvas_groups(spec)
    if resolved_groups:
        # Structural truth comes from the spec, but a group's *name* is canvas
        # metadata: an inferred group takes its label from the child node, so
        # regenerating from a plan session that dropped canvas_groups used to
        # rename every container on reopen. Prefer the stored label unless the
        # spec declares one itself.
        declared = (spec.meta or {}).get("canvas_groups")
        declared_ids = set(declared) if isinstance(declared, dict) else set()
        stored_groups = _payload_canvas_groups(_payload_from_variables(graph.variables or {}))
        merged_groups: dict[str, dict[str, Any]] = {}
        for group_id, info in resolved_groups.items():
            key = str(group_id)
            entry = {**info}
            declared_label = str(entry.get("label") or "").strip() if key in declared_ids else ""
            stored_label = str((stored_groups.get(key) or {}).get("label") or "").strip()
            label = declared_label or stored_label or str(entry.get("label") or "").strip()
            if label:
                entry["label"] = label
            merged_groups[key] = entry
        spec = replace(
            spec,
            meta={**(spec.meta or {}), "canvas_groups": merged_groups},
        )

    # Prefer nesting from the stored graph payload; fall back to plan-session
    # canvas_groups when node flags were stripped but meta still knows parents.
    spec_groups = (spec.meta or {}).get("canvas_groups")
    graph = rehydrate_canvas_nesting(
        graph,
        groups=spec_groups if isinstance(spec_groups, dict) else None,
    )
    payload = _payload_from_variables(graph.variables or {})
    old_node_meta = (
        dict(payload.get("nodes"))
        if isinstance(payload.get("nodes"), dict)
        else {}
    )
    has_nesting = graph_has_canvas_nesting(graph)
    has_canvas_edits = _graph_has_canvas_only_edits(graph, spec)
    if not has_nesting and not has_canvas_edits:
        # Flat canvas that still matches the spec — rebuild so a newer extract
        # can replace topology. Canvas-only stages/edges must take the merge
        # path below or reopen would wipe 「修订实验」 boxes.
        positioned = []
        by_id = {node.id: node for node in graph.nodes}
        for orch_node in spec.nodes:
            if orch_node.position is not None:
                positioned.append(orch_node)
                continue
            existing = by_id.get(orch_node.node_id)
            if existing is not None and existing.type == NodeType.TASK:
                positioned.append(
                    replace(
                        orch_node,
                        position={
                            "x": float(existing.position.x),
                            "y": float(existing.position.y),
                        },
                    )
                )
            else:
                positioned.append(orch_node)
        return to_pipeline_graph(
            replace(spec, nodes=tuple(positioned)),
            graph_id=str(graph.graph_id or ""),
            version=max(1, int(graph.version or 1)),
        )

    spec_map = spec.node_map()
    kept_nodes: list[GraphNode] = []
    for graph_node in graph.nodes:
        if graph_node.type in {NodeType.START, NodeType.END}:
            kept_nodes.append(graph_node)
            continue
        if graph_node.type != NodeType.TASK:
            kept_nodes.append(graph_node)
            continue
        orch_node = spec_map.get(graph_node.id)
        if orch_node is None:
            # Canvas-only stages (added in the editor, not yet in the incoming
            # plan-session spec) must survive reopen. Dropping them is how
            # 「修订实验」boxes disappeared after closing the canvas.
            kept_nodes.append(graph_node)
            continue
        binding = orch_node.model.as_dict() if orch_node.model is not None else {}
        model_id = str(binding.get("model_id") or "") or None
        effort = str(binding.get("effort") or "") or None
        title = orch_node.label or orch_node.node_id
        if orch_node.kind:
            title = f"[{orch_node.kind}] {title}"
        kept_nodes.append(
            graph_node.model_copy(
                update={
                    "title": title,
                    "label": orch_node.label or orch_node.node_id,
                    "goal": orch_node.goal or orch_node.label or orch_node.node_id,
                    "model": model_id,
                    "effort": effort,
                }
            )
        )

    live_ids = {node.id for node in kept_nodes if node.type == NodeType.TASK}
    for index, orch_node in enumerate(spec.nodes):
        if orch_node.node_id in live_ids:
            continue
        binding = orch_node.model.as_dict() if orch_node.model is not None else {}
        model_id = str(binding.get("model_id") or "") or None
        effort = str(binding.get("effort") or "") or None
        title = orch_node.label or orch_node.node_id
        if orch_node.kind:
            title = f"[{orch_node.kind}] {title}"
        kept_nodes.insert(
            -1 if kept_nodes and kept_nodes[-1].type == NodeType.END else len(kept_nodes),
            GraphNode(
                id=orch_node.node_id,
                type=NodeType.TASK,
                title=title,
                label=orch_node.label or orch_node.node_id,
                goal=orch_node.goal or orch_node.label or orch_node.node_id,
                model=model_id,
                effort=effort,
                position=_layout_position(index, orch_node),
            ),
        )
        live_ids.add(orch_node.node_id)

    node_meta: dict[str, Any] = {}
    labels = {node.node_id: (node.label or node.node_id) for node in spec.nodes}
    for graph_node in kept_nodes:
        if graph_node.type == NodeType.TASK and graph_node.id not in labels:
            labels[graph_node.id] = graph_node.label or graph_node.id
    for orch_node in spec.nodes:
        prev = (
            dict(old_node_meta[orch_node.node_id])
            if isinstance(old_node_meta.get(orch_node.node_id), dict)
            else {}
        )
        binding = orch_node.model.as_dict() if orch_node.model is not None else {}
        entry: dict[str, Any] = {
            "kind": orch_node.kind,
            "prefers": orch_node.prefers,
            "bind_later": orch_node.bind_later,
            "audit_target": orch_node.audit_target,
            "max_audit_retries": orch_node.max_audit_retries,
            "model": binding,
        }
        if orch_node.plan_brief is not None:
            entry["plan_brief"] = orch_node.plan_brief.as_dict()
        if orch_node.tools is not None:
            entry["tools"] = list(orch_node.tools)
        if orch_node.deny_tools:
            entry["deny_tools"] = list(orch_node.deny_tools)
        if orch_node.capabilities is not None:
            entry['capabilities'] = orch_node.capabilities
        entry.update(_node_recovery_meta(orch_node))
        for key in _LAYOUT_META_KEYS:
            if key in prev:
                entry[key] = prev[key]
        parent = str(entry.get("parent_group") or "").strip()
        if parent and parent not in live_ids:
            entry.pop("parent_group", None)
        if not entry.get("refine_group"):
            entry.pop("refine_group", None)
        if not str(entry.get("parent_group") or "").strip():
            entry.pop("parent_group", None)
        node_meta[orch_node.node_id] = entry

    extra_ids = [
        node.id
        for node in kept_nodes
        if node.type == NodeType.TASK and node.id not in node_meta
    ]
    for extra_id in extra_ids:
        prev = (
            dict(old_node_meta[extra_id])
            if isinstance(old_node_meta.get(extra_id), dict)
            else {}
        )
        extra_entry: dict[str, Any] = {
            "kind": str(prev.get("kind") or "implement"),
            "prefers": str(prev.get("prefers") or ""),
            "bind_later": bool(prev.get("bind_later")),
            "audit_target": str(prev.get("audit_target") or ""),
            "max_audit_retries": int(prev.get("max_audit_retries") or 3),
            "model": prev.get("model") if isinstance(prev.get("model"), dict) else {},
        }
        if prev.get("plan_brief") is not None:
            extra_entry["plan_brief"] = prev.get("plan_brief")
        if prev.get("review_channels") is not None:
            extra_entry["review_channels"] = list(parse_review_channels(prev["review_channels"]))
        for key in _LAYOUT_META_KEYS:
            if key in prev:
                extra_entry[key] = prev[key]
        if extra_entry.get("refine_group"):
            extra_entry["refine_group"] = True
        parent = str(extra_entry.get("parent_group") or "").strip()
        if parent:
            extra_entry["parent_group"] = parent
        else:
            extra_entry.pop("parent_group", None)
        node_meta[extra_id] = extra_entry

    live_all = {node.id for node in kept_nodes}
    edges = [
        edge
        for edge in graph.edges
        if edge.from_node in live_all and edge.to_node in live_all
    ]

    meta = dict(payload.get("meta") if isinstance(payload.get("meta"), dict) else {})
    meta.update(dict(spec.meta or {}))
    if graph.graph_id:
        meta["pipeline_graph_id"] = graph.graph_id
    # A group's name is canvas metadata, not derived topology: prefer the label
    # from the spec (which already merged the stored one) and then from the
    # stored payload, before falling back to the child node's label.
    preferred_labels: dict[str, str] = {}
    for source in (meta.get("canvas_groups"), (payload.get("meta") or {}).get("canvas_groups")):
        if not isinstance(source, dict):
            continue
        for group_id, info in source.items():
            if not isinstance(info, dict):
                continue
            label = str(info.get("label") or "").strip()
            if label:
                preferred_labels.setdefault(str(group_id), label)
    groups = _canvas_groups_from_node_meta(node_meta, labels, preferred_labels)
    if groups:
        meta["canvas_groups"] = groups
    else:
        meta.pop("canvas_groups", None)

    next_payload = {
        "spec_id": spec.spec_id,
        "label": spec.label,
        "version": spec.version,
        "finalizer_node_id": spec.finalizer_node_id,
        "template_id": spec.template_id,
        "goal": spec.goal,
        "require_confirm_gate": spec.require_confirm_gate,
        "meta": meta,
        "nodes": node_meta,
    }
    return graph.model_copy(
        update={
            "name": spec.label or graph.name,
            "description": spec.goal or graph.description,
            "nodes": kept_nodes,
            "edges": edges,
            "variables": {
                ORCH_FLAG: "1",
                ORCH_PAYLOAD: json.dumps(next_payload, ensure_ascii=False),
            },
        }
    )


_LOOP_EDGE_LABELS = frozenset({"循环", "loop", "rework"})


def _is_loop_edge_label(label: str) -> bool:
    return str(label or "").strip() in _LOOP_EDGE_LABELS


def _meta_dict(node_meta: dict[str, Any], node_id: str) -> dict[str, Any]:
    item = node_meta.get(node_id)
    return item if isinstance(item, dict) else {}


def _group_id_of(
    node_id: str,
    node_meta: dict[str, Any],
    groups: dict[str, Any],
) -> str:
    parent = str(_meta_dict(node_meta, node_id).get("parent_group") or "").strip()
    if parent:
        return parent
    for group_id, info in groups.items():
        children = info.get("children") if isinstance(info, dict) else []
        if node_id in {str(item) for item in (children or [])}:
            return str(group_id)
    return ""


def _same_group_reverse(
    from_id: str,
    to_id: str,
    node_meta: dict[str, Any],
    groups: dict[str, Any],
    positions: dict[str, float],
) -> bool:
    parent = _group_id_of(from_id, node_meta, groups)
    if not parent or parent != _group_id_of(to_id, node_meta, groups):
        return False
    from_y = float(positions.get(from_id, 0.0))
    to_y = float(positions.get(to_id, 0.0))
    if abs(from_y - to_y) > 1.0:
        return from_y > to_y
    children = [
        str(item)
        for item in ((groups.get(parent) or {}).get("children") or [])
        if str(item)
    ]
    if from_id in children and to_id in children:
        return children.index(from_id) > children.index(to_id)
    return from_id > to_id


def _points_to_own_group(
    from_id: str,
    to_id: str,
    node_meta: dict[str, Any],
    groups: dict[str, Any],
) -> bool:
    parent = _group_id_of(from_id, node_meta, groups)
    return bool(parent) and parent == to_id


def _edge_reaches(start: str, goal: str, outgoing: dict[str, set[str]]) -> bool:
    stack = [start]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == goal:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(outgoing.get(current) or ())
    return False


def _rewrite_canvas_loops_for_spec(
    orch_nodes: list[OrchestrationNode],
    orch_edges: list[OrchestrationEdge],
    node_meta: dict[str, Any],
    groups: dict[str, Any],
    *, legacy: bool = True,
) -> tuple[list[OrchestrationNode], list[OrchestrationEdge], list[str]]:
    """Migrate legacy audit wires only; explicit loop wires keep their semantics."""
    if not legacy:
        return orch_nodes, orch_edges, []
    by_id = {node.node_id: node for node in orch_nodes}
    positions = {
        node.node_id: float((node.position or {}).get("y") or 0.0)
        for node in orch_nodes
    }
    kept: list[OrchestrationEdge] = []
    audit_targets: dict[str, str] = {}
    notes: list[str] = []
    for edge in orch_edges:
        if is_loop_edge(edge.condition) and (not legacy or edge.condition not in {"循环", "rework"}):
            kept.append(edge)
            continue
        reverse = (
            edge.from_node == edge.to_node
            or _is_loop_edge_label(edge.condition)
            or _points_to_own_group(
                edge.from_node, edge.to_node, node_meta, groups
            )
            or (legacy and _same_group_reverse(
                edge.from_node, edge.to_node, node_meta, groups, positions
            ))
        )
        if not reverse:
            kept.append(edge)
            continue
        source = by_id.get(edge.from_node)
        target = by_id.get(edge.to_node)
        migrated = False
        if source is not None and target is not None:
            if source.kind == "implement" and target.kind == "audit":
                audit_targets[target.node_id] = source.node_id
                migrated = True
            elif source.kind == "audit" and target.kind == "implement":
                audit_targets[source.node_id] = target.node_id
                migrated = True
        if migrated:
            notes.append(f"兼容旧审核返工 {edge.from_node} → {edge.to_node}；新循环请使用 loop 回边")
        elif legacy and groups and (
            _points_to_own_group(edge.from_node, edge.to_node, node_meta, groups)
            or _same_group_reverse(edge.from_node, edge.to_node, node_meta, groups, positions)
        ):
            # Old checkpoint canvases used decorative reverse spines. Keep the
            # pre-contract interpretation only for these versioned legacy files.
            notes.append(f"保留旧 checkpoint 顺序语义：{edge.from_node} → {edge.to_node}")
        else:
            kept.append(edge)
    nodes: list[OrchestrationNode] = []
    for node in orch_nodes:
        mapped = str(audit_targets.get(node.node_id) or node.audit_target or "").strip()
        if mapped and mapped != node.audit_target:
            node = replace(node, audit_target=mapped)
        nodes.append(node)
    return nodes, kept, notes


def _fill_checkpoint_forward_edges(
    orch_nodes: list[OrchestrationNode],
    orch_edges: list[OrchestrationEdge],
    groups: dict[str, Any],
) -> list[OrchestrationEdge]:
    """Sequence stacked checkpoints in the spec without drawing canvas spine wires."""
    live_ids = {node.node_id for node in orch_nodes}
    positions = {
        node.node_id: float((node.position or {}).get("y") or 0.0)
        for node in orch_nodes
    }
    existing = {(edge.from_node, edge.to_node) for edge in orch_edges}
    outgoing: dict[str, set[str]] = {}
    for edge in orch_edges:
        outgoing.setdefault(edge.from_node, set()).add(edge.to_node)
    filled = list(orch_edges)
    for info in groups.values():
        children = [
            str(item)
            for item in (info.get("children") or [])
            if str(item) in live_ids
        ]
        children.sort(key=lambda node_id: (positions.get(node_id, 0.0), node_id))
        for index, frm in enumerate(children[:-1]):
            later = set(children[index + 1 :])
            if outgoing.get(frm, set()) & later:
                continue
            to = children[index + 1]
            if (frm, to) in existing:
                continue
            if _edge_reaches(to, frm, outgoing):
                continue
            filled.append(
                OrchestrationEdge(from_node=frm, to_node=to, condition="always")
            )
            existing.add((frm, to))
            outgoing.setdefault(frm, set()).add(to)
    return filled


def from_pipeline_graph(graph: PipelineGraph) -> OrchestrationSpec:
    """Rebuild OrchestrationSpec from an orchestration-tagged PipelineGraph."""
    if not is_orchestration_graph(graph):
        raise OrchestrationError("pipeline graph is not an orchestration canvas")
    payload = _payload_from_variables(graph.variables or {}, strict=True)
    node_meta = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}

    orch_nodes: list[OrchestrationNode] = []
    for graph_node in graph.nodes:
        if graph_node.type in {NodeType.START, NodeType.END}:
            continue
        if graph_node.type != NodeType.TASK:
            # Ignore helper nodes the user may have added; only tasks map back.
            continue
        meta = node_meta.get(graph_node.id) if isinstance(node_meta.get(graph_node.id), dict) else {}
        kind = str(meta.get("kind") or "").strip()
        if kind not in NODE_KINDS:
            # Title prefix [plan] … or default implement
            title = str(graph_node.title or "")
            if title.startswith("[") and "]" in title:
                maybe = title[1 : title.index("]")].strip()
                if maybe in NODE_KINDS:
                    kind = maybe
            if kind not in NODE_KINDS:
                kind = "implement"
        label = str(graph_node.label or "").strip()
        if not label:
            label = str(graph_node.title or graph_node.id).strip()
            if label.startswith("[") and "]" in label:
                label = label[label.index("]") + 1 :].strip() or graph_node.id
        goal = clean_generated_responsibility(graph_node.goal) or label
        binding_raw = meta.get("model") if isinstance(meta.get("model"), dict) else {}
        model_id = str(graph_node.model or binding_raw.get("model_id") or "").strip()
        effort = str(
            graph_node.effort
            if graph_node.effort not in (None, "", "auto")
            else (binding_raw.get("effort") or "")
        ).strip()
        if effort == "auto":
            effort = ""
        provider_key = str(binding_raw.get("provider_key") or "").strip()
        model = None
        if provider_key or model_id:
            model = ModelBinding(
                provider_key=provider_key,
                model_id=model_id,
                effort=effort,
            )
        brief = PlanBrief.from_value(meta.get("plan_brief"))
        orch_nodes.append(
            OrchestrationNode(
                node_id=graph_node.id,
                kind=kind,  # type: ignore[arg-type]
                label=label,
                goal=goal,
                prefers=str(meta.get("prefers") or "").strip(),
                model=model,
                bind_later=bool(meta.get("bind_later")),
                audit_target=str(meta.get("audit_target") or "").strip(),
                max_audit_retries=_meta_int(meta, "max_audit_retries", 3),
                position={
                    "x": float(graph_node.position.x),
                    "y": float(graph_node.position.y),
                },
                plan_brief=brief,
                tools=parse_tools(meta.get("tools")),
                deny_tools=parse_tools(meta.get("deny_tools")) or (),
                capabilities=meta.get('capabilities'),
                fallback_node_ids=tuple(
                    cleaned
                    for cleaned in (
                        item.strip() if isinstance(item, str) else ""
                        for item in (meta.get("fallback_node_ids") or [])
                    )
                    if cleaned
                ),
                on_blocked=str(meta.get("on_blocked") or "").strip(),
                escalation_model=ModelBinding.from_value(meta.get("escalation_model")),
                max_auto_recovery=_meta_int(meta, "max_auto_recovery", 1),
                loop_policy=LoopPolicy.from_value(meta.get("loop_policy")),
                review_channels=parse_review_channels(meta.get("review_channels")),
            )
        )
    if not orch_nodes:
        raise OrchestrationError("canvas has no stage task nodes to sync")

    stage_ids: set[str] = set()
    for node in orch_nodes:
        if node.node_id in stage_ids:
            raise OrchestrationError(
                f"画布上有重复的节点 id：{node.node_id}（同一 id 会被当成同一个节点，回执无法区分）"
            )
        stage_ids.add(node.node_id)
    orch_edges: list[OrchestrationEdge] = []
    for edge in graph.edges:
        if edge.from_node not in stage_ids or edge.to_node not in stage_ids:
            continue
        if edge.from_node == edge.to_node:
            # A self wire cannot be scheduled; a real loop needs an explicit
            # check node and a declared contract, so drop it instead of
            # producing a spec that can never make progress.
            continue
        condition = str(edge.label or "always").strip() or "always"
        branch = (payload.get("edge_inputs") or {}).get(edge.id) or {}
        if branch and (branch.get("from_node"), branch.get("to_node")) != (edge.from_node, edge.to_node):
            raise OrchestrationError("条件分支已重新连线，请先重新绑定参数条件；未静默删除分支条件")
        orch_edges.append(
            OrchestrationEdge(
                from_node=edge.from_node,
                to_node=edge.to_node,
                condition=condition,
                input_when=branch.get("input_when"),
                branch_group=branch.get("branch_group", ""),
            )
        )

    finalizer = str(payload.get("finalizer_node_id") or "").strip()
    if finalizer not in stage_ids:
        # Prefer node that links to END; else last stage.
        linked_to_end = [
            edge.from_node
            for edge in graph.edges
            if edge.to_node == END_ID and edge.from_node in stage_ids
        ]
        finalizer = linked_to_end[-1] if linked_to_end else orch_nodes[-1].node_id

    meta = dict(payload.get("meta") if isinstance(payload.get("meta"), dict) else {})
    if graph.graph_id:
        meta["pipeline_graph_id"] = graph.graph_id
        meta["pipeline_graph_version"] = int(graph.version or 1)
        meta["pipeline_graph_updated_at"] = str(graph.updated_at or "")

    # Preserve refine-group structure for the canvas (Comfy-like containers).
    # The payload keeps the label the user gave the container, so prefer it:
    # rebuilding it from the child node's label renamed every group on reopen.
    saved_groups = _payload_canvas_groups(payload)
    groups: dict[str, Any] = {}
    for node_id, meta_item in node_meta.items():
        if not isinstance(meta_item, dict) or not meta_item.get("refine_group"):
            continue
        saved = saved_groups.get(str(node_id)) or {}
        label = str(saved.get("label") or "").strip() or next(
            (n.label for n in orch_nodes if n.node_id == node_id),
            str(node_id),
        )
        groups[str(node_id)] = {
            "expanded": bool(meta_item.get("group_expanded", True)),
            "mode": meta_item.get("group_mode", "constraints"),
            "children": [],
            "label": label,
        }
    for node_id, meta_item in node_meta.items():
        if not isinstance(meta_item, dict):
            continue
        parent = str(meta_item.get("parent_group") or "").strip()
        if parent and parent in groups:
            groups[parent]["children"].append(str(node_id))
    if groups:
        meta["canvas_groups"] = groups

    orch_nodes, orch_edges, loop_notes = _rewrite_canvas_loops_for_spec(
        orch_nodes, orch_edges, node_meta, groups,
        legacy=not meta.get("loops") and int(meta.get("control_flow_version") or 0) < 2,
    )
    if not meta.get("loops") and not any(is_loop_edge(edge.condition) for edge in orch_edges):
        orch_edges = _fill_checkpoint_forward_edges(orch_nodes, orch_edges, groups)
    try:
        _, loops = compile_loops(orch_nodes, orch_edges, meta.get("loops"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise OrchestrationError(str(exc)) from exc
    if loops:
        meta["loops"] = [loop.as_dict() for loop in loops]
    if loop_notes:
        meta["canvas_loop_rewrites"] = loop_notes

    spec = OrchestrationSpec(
        spec_id=str(payload.get("spec_id") or "orchestration").strip(),
        label=str(payload.get("label") or graph.name or "编排图").strip(),
        version=max(int(payload.get("version") or 1), int(graph.version or 1)),
        nodes=tuple(orch_nodes),
        edges=tuple(orch_edges),
        finalizer_node_id=finalizer,
        template_id=str(payload.get("template_id") or "").strip(),
        goal=str(payload.get("goal") or graph.description or "").strip(),
        require_confirm_gate=bool(payload.get("require_confirm_gate", True)),
        meta=meta,
        goal_contract=GoalContract.from_value(payload.get("goal_contract")),
        actions=tuple(
            action
            for action in (SpecAction.from_value(item) for item in (payload.get("actions") or []))
            if action is not None
        ),
    )
    raw, repairs = repair_extracted_graph_cycles(spec.as_dict())
    if repairs:
        spec = OrchestrationSpec.from_value(raw)
    resolved = resolve_canvas_groups(spec)
    if resolved:
        spec = replace(
            spec,
            meta={**(spec.meta or {}), "canvas_groups": resolved},
        )
    return spec


def merge_model_provider_keys(
    spec: OrchestrationSpec,
    *,
    previous: OrchestrationSpec | None = None,
    resolve_provider: Any | None = None,
) -> OrchestrationSpec:
    """Fill missing provider_key after canvas edits (model id only on nodes)."""
    prev_map = previous.node_map() if previous is not None else {}
    nodes: list[OrchestrationNode] = []
    for node in spec.nodes:
        model = node.model
        if model is None or (model.provider_key and model.model_id):
            nodes.append(node)
            continue
        if not model.model_id:
            nodes.append(node)
            continue
        provider_key = model.provider_key
        prev = prev_map.get(node.node_id)
        if (
            not provider_key
            and prev
            and prev.model
            and prev.model.model_id == model.model_id
            and prev.model.provider_key
        ):
            provider_key = prev.model.provider_key
        if not provider_key and callable(resolve_provider):
            try:
                provider_key = str(resolve_provider(model.model_id) or "").strip()
            except Exception:
                provider_key = ""
        nodes.append(
            replace(
                node,
                model=ModelBinding(
                    provider_key=provider_key,
                    model_id=model.model_id,
                    effort=model.effort,
                ),
            )
        )
    return replace(spec, nodes=tuple(nodes))
