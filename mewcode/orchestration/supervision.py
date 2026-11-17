"""Find missing direction/result checkpoints in the actual executable graph.

This is structural coverage, not a claim that a model's review is correct.
One checkpoint may cover a batch; no hidden per-node reviewer is inserted.
"""
from __future__ import annotations

from .convert import to_expert_flow


def _exhaustive_branch_groups(flow) -> set[str]:
    """Branch groups whose declared input options are all represented."""
    grouped = {}
    for edge in flow.edges:
        if edge.input_when is not None and edge.branch_group:
            grouped.setdefault(edge.branch_group, []).append(edge)
    if not grouped:
        return set()

    from .run_inputs import validate_branch_contract

    try:
        declarations = flow.meta.get("run_input_contract") or []
        defined = validate_branch_contract(
            flow.edges, declarations, node_ids={node.node_id for node in flow.nodes})
    except (KeyError, TypeError, ValueError):
        # Invalid or incomplete contracts cannot prove mandatory coverage.
        return set()

    exhaustive = set()
    for group, edges in grouped.items():
        input_name = edges[0].input_when["input"]
        options = (defined.get(input_name) or {}).get("options")
        if options and {edge.input_when["equals"] for edge in edges} == {
                option["value"] for option in options}:
            exhaustive.add(group)
    return exhaustive


def _delivery_required_guards(flow) -> set[int]:
    """Single guards that every possible successful delivery must traverse.

    A rejected guard may stop the whole run before review and delivery. It is
    not an optional review bypass. An independent route to the finalizer must
    keep this proof from succeeding, even when it starts at another root.
    """
    from .run_inputs import validate_branch_contract

    try:
        validate_branch_contract(flow.edges, flow.meta.get("run_input_contract") or [],
                                 node_ids={node.node_id for node in flow.nodes})
    except (KeyError, TypeError, ValueError):
        return set()
    edges = [(index, edge) for index, edge in enumerate(flow.edges)
             if edge.condition in {"always", "pass"}]

    def delivery_reachable(omit=None):
        forward = {node.node_id: set() for node in flow.nodes}
        for index, edge in edges:
            if index != omit:
                forward[edge.from_node].add(edge.to_node)
        seen, todo = set(), list(flow.entry_node_ids())
        while todo:
            node = todo.pop()
            if node == flow.finalizer_node_id:
                return True
            if node not in seen:
                seen.add(node)
                todo.extend(forward[node] - seen)
        return False

    if not delivery_reachable():
        return set()
    return {index for index, edge in edges
            if edge.input_when is not None and not edge.branch_group and not delivery_reachable(index)}


def supervision_diagnostics(spec) -> list[dict]:
    flow = to_expert_flow(spec)
    kinds = flow.meta.get("node_kinds") or {}
    reverse = {node.node_id: set() for node in flow.nodes}
    required_forward = {node.node_id: set() for node in flow.nodes}
    possible_success_forward = {node.node_id: set() for node in flow.nodes}
    exhaustive_groups = _exhaustive_branch_groups(flow)
    delivery_guards = _delivery_required_guards(flow)
    for index, edge in enumerate(flow.edges):
        # A failure-only reviewer does not review successful execution.
        if edge.condition not in {"always", "pass"}:
            continue
        reverse[edge.to_node].add(edge.from_node)
        possible_success_forward[edge.from_node].add(edge.to_node)
        # One edge in a conditional group is selected at runtime. The group is
        # mandatory only when its validated input options cover every choice.
        if edge.input_when is None or edge.branch_group in exhaustive_groups or index in delivery_guards:
            required_forward[edge.from_node].add(edge.to_node)

    def reachable(start, adjacency):
        found, pending = set(), [start]
        while pending:
            for target in adjacency[pending.pop()]:
                if target not in found:
                    found.add(target)
                    pending.append(target)
        return found

    directors = {node.node_id for node in flow.nodes
                 if node.role in {"planner", "manager"} and kinds.get(node.node_id) != "gate"}
    loop_reviewers = {key for loop in flow.meta.get("loops", [])
                      for key in loop.get("reviewer_node_ids", [])}
    coordinators = set((flow.meta.get("workflow_draft") or {}).get("coordination_nodes", []))
    reviewers = {node.node_id for node in flow.nodes if node.role == "auditor"
                 and node.node_id not in coordinators
                 and (kinds.get(node.node_id) != "decision" or node.node_id in loop_reviewers)}
    leads_to_delivery = reachable(flow.finalizer_node_id, reverse) | {flow.finalizer_node_id}
    without_reviewers = {
        node_id: {target for target in targets if node_id not in reviewers and target not in reviewers}
        for node_id, targets in possible_success_forward.items()
    }
    diagnostics = []
    for node in flow.nodes:
        if node.role != "executor":
            continue
        ancestors = reachable(node.node_id, reverse)
        # An explicit review-to-repair loop owns direction for its repair roots.
        # compile_loops removes these dispatch back-edges from flow.edges.
        repair_directed = any(node.node_id in loop.get("repair_node_ids", [])
                              and loop.get("check_node_id") in reviewers
                              for loop in flow.meta.get("loops", []))
        if not ancestors & directors and not repair_directed:
            diagnostics.append({
                "code": "missing_direction_checkpoint", "path": f"nodes/{node.node_id}",
                "entry_ids": [node.node_id],
                "message": f"执行节点 {node.node_id} 缺少上游计划或主控方向交接；明确输入范围、禁止改动、交付物和验证方法，并用依赖连接。人工确认或节点层级不能替代方向监管。",
            })
        covered_reviewers = reachable(node.node_id, required_forward) & reviewers & leads_to_delivery
        bypasses_review = flow.finalizer_node_id in reachable(node.node_id, without_reviewers)
        if not covered_reviewers or bypasses_review:
            diagnostics.append({
                "code": "missing_result_review", "path": f"nodes/{node.node_id}",
                "entry_ids": [node.node_id],
                "message": f"执行节点 {node.node_id} 的成功路径缺少通向交付的独立结果审查；优先复用已有审查节点覆盖相关阶段产物，执行自评和最终汇总不能替代验收。",
            })
    from mewcode.kernel.capabilities import resolver_diagnostics
    return diagnostics + resolver_diagnostics(flow)
