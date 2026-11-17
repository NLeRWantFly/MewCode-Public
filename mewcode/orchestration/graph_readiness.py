"""Additional deterministic readiness checks for candidate stage graphs."""
from __future__ import annotations

import copy
from collections import Counter, deque

from .graph_inspection import diagnose, topology
from .draft_sources import digest
from .plan_integrity import unresolved_plan_inputs
from .spec import OrchestrationSpec
from .workflow_draft import check_entry_refs, infer_draft_finalizer, project_graph


def _diagnostic(code, path, entry_ids, message, *, severity=None):
    diagnostic = {"code": code, "path": path, "entry_ids": entry_ids, "message": message}
    if severity is not None:
        diagnostic["severity"] = severity
    return diagnostic


def _reaches(start, adjacency):
    found, queue = {start}, deque([start])
    while queue:
        for target in adjacency.get(queue.popleft(), ()):
            if target not in found:
                found.add(target)
                queue.append(target)
    return found


def _input_binding_diagnostics(document):
    """Expose binding failures with their stable, field-specific codes."""
    diagnostics = []
    entries = document.get("entries", [])
    for entry in entries:
        if (entry.get("kind") != "node" or entry.get("state") == "retired"
                or "input_bindings" not in (entry.get("data") or {})):
            continue
        try:
            check_entry_refs(entries, focus={entry["id"]})
        except (ValueError, TypeError) as exc:
            code = getattr(exc, "code", "invalid_input_binding")
            if code not in {"invalid_input_binding", "unreachable_input_producer",
                            "unreachable_input_binding"}:
                continue
            diagnostics.append(_diagnostic(
                code, f"entries/{entry['id']}/data.input_bindings", [entry["id"],
                    *([getattr(exc, "diagnostic", {}).get("producer_id")
                       or getattr(exc, "diagnostic", {}).get("target_id")]
                      if (getattr(exc, "diagnostic", {}).get("producer_id")
                          or getattr(exc, "diagnostic", {}).get("target_id")) else [])], str(exc)))
    return diagnostics


def _audit_rework_route_diagnostics(spec, active_edges):
    """Report only explicit audit-target/fail-edge contradictions.

    A target-less audit is not evidence that rework was requested. Existing
    loop contracts own their internal failure dispatch and are excluded here.
    """
    from mewcode.experts.flow import normalize_edge_condition

    nodes = spec.node_map()
    loop_members = {node_id for loop in spec.meta.get("loops", [])
                    for node_id in loop.get("node_ids", []) if isinstance(node_id, str)}
    diagnostics = []
    for node in spec.nodes:
        if node.kind != "audit" or not node.audit_target or node.node_id in loop_members:
            continue
        target = nodes.get(node.audit_target)
        if target is None:
            # The projection validator reports unresolved references already.
            continue
        if target.kind != "implement":
            diagnostics.append(_diagnostic(
                "audit_rework_route_gap", f"nodes/{node.node_id}/audit_target",
                [node.node_id],
                f"审计节点 {node.node_id} 的显式返修目标 {node.audit_target} 不是实现节点，不能作为有效返修路线。"
            ))
            continue

        fail_edges = [edge for edge in active_edges
                      if edge.get("from_node") == node.node_id
                      and normalize_edge_condition(edge.get("condition", "always")) == "fail"]
        if not fail_edges or any(edge.get("to_node") == node.audit_target for edge in fail_edges):
            continue

        diagnostic = _diagnostic(
            "audit_rework_route_gap", f"nodes/{node.node_id}/audit_target",
            [node.node_id, *[edge["entry_id"] for edge in fail_edges]],
            f"审计节点 {node.node_id} 声明返修到实现节点 {node.audit_target}，但现有 fail 边均未指向该目标；运行时会停止而不会按 audit_target 返修。"
        )
        if (len(fail_edges) == 1 and not fail_edges[0].get("input_when")
                and not fail_edges[0].get("branch_group")):
            edge = fail_edges[0]
            diagnostic["suggested_fix"] = (
                f"将唯一无条件 fail 边 {edge['entry_id']} 的 to_node 对齐到显式 audit_target "
                f"{node.audit_target}，保留 condition=fail；不要改变其它节点或验收。"
            )
        diagnostics.append(diagnostic)
    return diagnostics


def readiness(document, base, *, conversation_id, revision):
    """Return diagnostic completeness without changing save or execution rules."""
    try:
        diagnostics = list(diagnose(document, base, conversation_id=conversation_id,
                                    revision=revision)["diagnostics"])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        diagnostics = [_diagnostic("projection_invalid", "$", [], str(exc))]
    binding_diagnostics = _input_binding_diagnostics(document)
    if binding_diagnostics:
        binding_messages = {item["message"] for item in binding_diagnostics}
        diagnostics = [item for item in diagnostics
                       if not (item.get("code") == "invalid_reference"
                               and item.get("message") in binding_messages)]
        diagnostics.extend(binding_diagnostics)
    nodes, edges, loops = topology(document, base)
    node_map = {node["node_id"]: node for node in nodes}
    node_ids = set(node_map)
    active_edges = [edge for edge in edges if edge.get("state") != "retired"]
    from .run_inputs import validate_branch_contract
    declarations = [entry["data"] for entry in document["entries"] if entry["kind"] == "input" and entry["state"] == "active"]
    try:
        validate_branch_contract(active_edges, declarations, node_ids=node_ids)
    except ValueError as exc:
        # diagnose() already reports every missing collection target at its
        # precise field. Retain other input/branch failures from this validator.
        if not ("收集阶段不存在" in str(exc) and any(
                row["code"] == "input_collection_target_missing" for row in diagnostics)):
            diagnostics.append(_diagnostic("input_semantics", "goal_contract/run_inputs", [
                edge["entry_id"] for edge in active_edges if edge.get("input_when") is not None or edge.get("branch_group")], str(exc)))

    undirected = {node_id: set() for node_id in node_ids}
    forward = {node_id: set() for node_id in node_ids}
    for edge in active_edges:
        source, target = edge.get("from_node"), edge.get("to_node")
        if (not isinstance(source, str) or not isinstance(target, str) or
                source not in node_ids or target not in node_ids):
            continue
        undirected[source].add(target)
        undirected[target].add(source)
        if edge.get("condition", "always") != "loop":
            forward[source].add(target)
    remaining, components = set(node_ids), []
    while remaining:
        component = _reaches(min(remaining), undirected)
        components.append(component)
        remaining -= component
    if len(components) > 1:
        isolated = sorted(node_id for component in components if len(component) == 1 for node_id in component)
        parts = sorted((sorted(component) for component in components), key=lambda part: part[0])
        diagnostic = _diagnostic("disconnected", "nodes", sorted(node_ids),
            "图不连通；连通分量：" + "；".join("、".join(part) for part in parts))
        diagnostic.update(components=parts, isolated_node_ids=isolated)
        diagnostics.append(diagnostic)

    structural = copy.deepcopy(document)
    for entry in structural.get("entries", []):
        if entry.get("state") == "conflict":
            entry["state"] = "active"
    from .draft_dedupe import apply_dedupe
    structural, _, _ = apply_dedupe(structural)
    projected = None
    try:
        projected = project_graph(structural, base, conversation_id=conversation_id,
                                  revision=revision)
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    finalizer = ((base or {}).get("finalizer_node_id") or (projected.finalizer_node_id if projected else "")
                 or (infer_draft_finalizer(structural) if not any(
                     d["code"] == "invalid_reference" for d in diagnostics) else ""))
    skipped = []
    if finalizer:
        for node_id in sorted(node_ids):
            if finalizer not in _reaches(node_id, forward):
                diagnostics.append(_diagnostic("unreachable_to_finalizer", f"nodes/{node_id}",
                                               [node_id], f"节点 {node_id} 没有到终验节点 {finalizer} 的前向路径"))
        for loop in loops:
            data = loop
            members = data.get("node_ids")
            member_ids = {item for item in members if isinstance(item, str)} if isinstance(members, list) else set()
            if (finalizer in member_ids or finalizer == data.get("entry_node_id") or
                    finalizer == data.get("check_node_id")):
                diagnostics.append(_diagnostic("finalizer_in_loop", f"meta/loops/{loop['entry_id']}",
                                               [loop["entry_id"], finalizer], f"终验节点 {finalizer} 位于循环内"))
    else:
        skipped = ["unreachable_to_finalizer", "finalizer_in_loop"]
    for loop in loops:
        queue_text = str(loop.get("criteria", "")) + str(loop.get("exit_condition", ""))
        for node_id in loop.get("reviewer_node_ids", []):
            acceptance = str((node_map.get(node_id, {}).get("plan_brief") or {}).get("acceptance", []))
            if any(word in acceptance and word not in queue_text for word in ("全部", "整本", "所有页")):
                diagnostics.append(_diagnostic("batch_audit_scope", f"nodes/{node_id}/plan_brief/acceptance",
                    [loop["entry_id"], node_id], "批内审查可能含全量验收，请区分当前项验收与队列级完成。",
                    severity="warning"))

    for node in nodes:
        if node.get("kind") == "gate":
            continue
        brief = node.get("plan_brief") or {}
        node_id = node["node_id"]
        for key, code, label in (("outputs", "missing_outputs", "产出"),
                                 ("acceptance", "missing_acceptance", "验收")):
            if not brief.get(key):
                diagnostics.append(_diagnostic(code, f"nodes/{node_id}/plan_brief/{key}",
                                               [node_id], f"节点 {node_id} 缺少{label}"))

    input_spec = projected
    if input_spec is None and nodes:
        try:
            input_spec = OrchestrationSpec.from_value({
                "spec_id": "readiness", "label": "就绪诊断", "nodes": nodes,
                "edges": [], "finalizer_node_id": finalizer or nodes[-1]["node_id"]})
        except (ValueError, TypeError, KeyError):
            pass
    if input_spec is not None:
        for problem in unresolved_plan_inputs(input_spec):
            node_id = problem.split(":", 1)[0]
            diagnostics.append(_diagnostic("unresolved_reference", f"nodes/{node_id}/plan_brief/known_inputs",
                                           [node_id], problem))

    if projected is None:
        skipped.extend(["supervision", "audit_rework_route", "entry_not_projected"])
    if projected is not None:
        from .supervision import supervision_diagnostics
        try:
            diagnostics.extend(supervision_diagnostics(projected))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            diagnostics.append(_diagnostic("supervision_projection_invalid", "nodes", [], str(exc)))
        diagnostics.extend(_audit_rework_route_diagnostics(projected, active_edges))
        projected_nodes = {node.node_id for node in projected.nodes}
        projected_edges = Counter((edge.from_node, edge.to_node, edge.condition, digest(edge.input_when), edge.branch_group) for edge in projected.edges)
        projected_loops = Counter(str(loop.get("loop_id") or "") for loop in projected.meta.get("loops", []))
        collapsed_ids = {e["id"] for e in structural["entries"] if e["state"] == "retired"}
        for node in nodes:
            if node["node_id"] not in projected_nodes:
                diagnostics.append(_diagnostic("entry_not_projected", f"entries/{node['entry_id']}",
                                               [node["entry_id"]], f"节点条目 {node['entry_id']} 未投影到 spec"))
        for edge in active_edges:
            if edge["entry_id"] in collapsed_ids:
                continue
            signature = (edge.get("from_node"), edge.get("to_node"), edge.get("condition", "always"), digest(edge.get("input_when")), edge.get("branch_group", ""))
            if projected_edges[signature]:
                projected_edges[signature] -= 1
            else:
                diagnostics.append(_diagnostic("entry_not_projected", f"entries/{edge['entry_id']}",
                                               [edge["entry_id"]], f"依赖条目 {edge['entry_id']} 未投影到 spec"))
        for loop in loops:
            if loop["entry_id"] in collapsed_ids:
                continue
            loop_id = str(loop.get("loop_id") or "")
            if projected_loops[loop_id]:
                projected_loops[loop_id] -= 1
            else:
                diagnostics.append(_diagnostic("entry_not_projected", f"entries/{loop['entry_id']}",
                                               [loop["entry_id"]], f"循环条目 {loop['entry_id']} 未投影到 spec"))
    blocking = [diagnostic for diagnostic in diagnostics
                if diagnostic.get("severity") != "warning"]
    result = {"ready": not blocking, "diagnostics": diagnostics}
    if skipped:
        result["skipped"] = skipped
        result["skipped_reason"] = "当前投影或终验节点不可用；列出的下层检查尚未执行，不能视为通过。"
    return result
