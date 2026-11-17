"""Structural validation for OrchestrationSpec."""

from __future__ import annotations

from mewcode.orchestration.spec import NODE_KINDS, OrchestrationError, OrchestrationSpec
from mewcode.orchestration.loops import compile_loops
from mewcode.orchestration.plan_integrity import unresolved_plan_inputs

MISSING_BINDINGS_PREFIX = "缺少模型绑定"


def unbound_node_ids(spec: OrchestrationSpec) -> list[str]:
    """Node ids that still need a complete model before confirm/run."""
    if spec.meta.get('workflow_core'):
        from mewcode.workflow_core.planning import model_nodes
        return [path for path, node in model_nodes(spec.meta['workflow_core'])
                if not node.get('model') or not node['model'].get('provider_key') or not node['model'].get('model_id')]
    missing: list[str] = []
    for node in spec.nodes:
        if node.kind == "gate":
            continue
        if node.model is None or not node.model.is_complete:
            missing.append(node.node_id)
    return missing


def validate_spec(
    spec: OrchestrationSpec,
    *,
    require_bindings: bool = False,
) -> list[str]:
    """Return warnings; raise OrchestrationError for hard failures."""
    if spec.meta.get('workflow_core'):
        from mewcode.workflow_core.schema import normalize
        normalize(spec.meta['workflow_core'])
        missing = unbound_node_ids(spec) if require_bindings else []
        if missing:
            raise OrchestrationError(f"{MISSING_BINDINGS_PREFIX}: {', '.join(missing)}")
        return []
    warnings: list[str] = []
    input_problems = unresolved_plan_inputs(spec)
    if require_bindings and input_problems:
        raise OrchestrationError("；".join(input_problems))
    warnings.extend(input_problems)
    # Duplicate ids make receipts, scheduling and run state ambiguous, so they
    # have to be rejected here rather than failing later as an opaque compiler
    # error (or silently collapsing in node_map()).
    seen_ids: set[str] = set()
    for node in spec.nodes:
        if node.node_id in seen_ids:
            raise OrchestrationError(
                f"duplicate node_id: {node.node_id}（同一个 id 出现多次，回执与调度无法区分）"
            )
        seen_ids.add(node.node_id)
    nodes = spec.node_map()
    from .run_inputs import validate_branch_contract
    try:
        validate_branch_contract(spec.edges, spec.goal_contract.run_inputs if spec.goal_contract else (), node_ids=set(nodes))
    except ValueError as exc:
        raise OrchestrationError(str(exc)) from exc
    if spec.finalizer_node_id not in nodes:
        raise OrchestrationError(
            f"finalizer_node_id not found: {spec.finalizer_node_id}"
        )
    if require_bindings:
        missing = unbound_node_ids(spec)
        if missing:
            raise OrchestrationError(
                f"{MISSING_BINDINGS_PREFIX}: {', '.join(missing)}"
            )
    for node in spec.nodes:
        if node.kind not in NODE_KINDS:
            raise OrchestrationError(f"unknown kind on {node.node_id}: {node.kind}")
        if node.kind in {"audit", "decision"} and set(node.tools or ()) & {"*", "WriteFile", "EditFile", "Bash", "Agent"}:
            warnings.append(f"node {node.node_id}: 写入工具配置将被审计/循环审查只读边界过滤")
        if node.kind == "audit" and node.audit_target:
            if node.audit_target not in nodes:
                raise OrchestrationError(
                    f"audit {node.node_id} target missing: {node.audit_target}"
                )
            if nodes[node.audit_target].kind != "implement":
                warnings.append(
                    f"audit {node.node_id} target {node.audit_target} is not implement"
                )
        if node.kind != "gate" and (
            node.model is None or not node.model.is_complete
        ):
            if not node.bind_later:
                warnings.append(f"node {node.node_id} has no model binding yet")
        for fallback_id in node.fallback_node_ids:
            if fallback_id not in nodes:
                raise OrchestrationError(
                    f"node {node.node_id} fallback target missing: {fallback_id}"
                )
            if fallback_id == node.node_id:
                raise OrchestrationError(
                    f"node {node.node_id} fallback cannot target itself"
                )
        if node.on_blocked == "fallback" and not node.fallback_node_ids:
            warnings.append(f"node {node.node_id}: on_blocked=fallback but no fallback_node_ids")
        if node.on_blocked == "escalate_model" and (
            node.escalation_model is None or not node.escalation_model.is_complete
        ):
            warnings.append(f"node {node.node_id}: escalate_model without bound escalation_model")
        if node.loop_policy is not None:
            policy = node.loop_policy
            if policy.on_stall == "fallback":
                if not policy.fallback_node_id:
                    raise OrchestrationError(
                        f"node {node.node_id}: loop on_stall=fallback requires fallback_node_id"
                    )
                if policy.fallback_node_id not in nodes:
                    raise OrchestrationError(
                        f"node {node.node_id}: loop fallback target missing: {policy.fallback_node_id}"
                    )

    for edge in spec.edges:
        if edge.from_node not in nodes:
            raise OrchestrationError(f"edge from unknown node: {edge.from_node}")
        if edge.to_node not in nodes:
            raise OrchestrationError(f"edge to unknown node: {edge.to_node}")

    try:
        forward_edges, loops = compile_loops(spec.nodes, spec.edges, spec.meta.get("loops"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise OrchestrationError(str(exc)) from exc
    # Only explicit, bounded return edges may cycle. All other edges stay a DAG.
    indegree = {node_id: 0 for node_id in nodes}
    outs: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in forward_edges:
        outs[edge.from_node].append(edge.to_node)
        indegree[edge.to_node] += 1
    queue = [node_id for node_id, deg in indegree.items() if deg == 0]
    seen = 0
    while queue:
        current = queue.pop()
        seen += 1
        for nxt in outs[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen != len(nodes):
        raise OrchestrationError(
            "编排图存在未声明的循环依赖（cycle）：请标记 condition=loop 回边并声明退出条件和预算，"
            "或使用兼容的 audit_target；不能把普通依赖当作回边"
        )

    kinds = {node.kind for node in spec.nodes}
    if "implement" in kinds and "plan" not in kinds:
        warnings.append("implement nodes without a plan node — consider adding plan")
    if spec.require_confirm_gate and "gate" not in kinds:
        warnings.append("require_confirm_gate is true but no gate node in graph")
    return warnings
