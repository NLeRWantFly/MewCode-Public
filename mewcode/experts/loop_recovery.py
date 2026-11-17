"""Dependency-aware checkpoint recovery; no model calls or workspace writes."""
from __future__ import annotations

import copy

from mewcode.orchestration.loops import is_loop_edge


def reusable_result(node_id, result, contract):
    """Presence in a checkpoint is not completion, especially for reviewers."""
    if result.get("status") == "review_pending":
        request = result.get("verification_request") or {}
        return bool(node_id not in contract.reviewer_node_ids and request.get("version") == 1
            and request.get("owner") == node_id and request.get("contract_hash") == contract.contract_hash
            and request.get("reviewer_node_id") in contract.reviewer_node_ids
            and request.get("candidate_revisions") and request.get("request_id")
            and not result.get("error_kind") and not (result.get("handoff") or {}).get("blockers")
            and not (result.get("handoff") or {}).get("question"))
    if result.get("status") not in {"completed", "skipped"}:
        return False
    handoff = result.get("handoff") or {}
    if handoff.get("status") in {"blocked", "needs_attention"} or handoff.get("blockers"):
        return False
    if node_id in contract.reviewer_node_ids and not result.get("loop_decision"):
        return False
    return True


def affected_nodes(flow, contract, roots):
    body = set(contract.node_ids)
    affected, pending = set(), list(roots)
    while pending:
        node_id = pending.pop()
        if node_id not in body or node_id in affected:
            continue
        affected.add(node_id)
        pending.extend(edge.to_node for edge in flow.edges
                       if edge.from_node == node_id and not is_loop_edge(edge.condition))
    return affected


def unfinished_roots(flow, contract, state):
    results = state.get("round_results") or {}
    from mewcode.orchestration.loops import fingerprint
    from mewcode.orchestration.parallel import parse_groups
    settled = set()
    for group in parse_groups(contract.parallel_groups):
        receipt = (state.get('parallel_settlements') or {}).get(group.group_id) or {}
        if receipt.get('contract_hash') != contract.contract_hash or receipt.get('group') != group.as_dict():
            continue
        for key, row in receipt.get('branches', {}).items():
            if (key in group.node_ids and key in results
                    and results[key].get('error_kind') != 'branch_timeout'
                    and row.get('result_hash') == fingerprint(results[key])):
                # Retired outer timeouts are interrupted work, not terminal
                # alternatives. Resume them and invalidate dependent fusion.
                settled.add(key)
    unfinished = {node_id for node_id, result in results.items()
                  if node_id not in settled and not reusable_result(node_id, result, contract)}
    roots = []
    for batch in flow.topological_batches():
        for node_id in batch:
            if node_id in unfinished and node_id not in affected_nodes(flow, contract, roots):
                roots.append(node_id)
    return roots


def invalidate_results(flow, contract, state, roots, *, reason):
    """Keep unrelated work; archive dependent results without deleting artifacts."""
    affected = affected_nodes(flow, contract, roots)
    results = state.setdefault("round_results", {})
    previous = {key: results.pop(key) for key in list(results) if key in affected}
    state.setdefault("recovery_history", []).append({
        "round": state.get("round", 0), "roots": list(roots), "reason": reason,
        "results": copy.deepcopy(previous),
    })
    state["reports"] = {}
    state["next_node_id"] = roots[0] if roots else contract.entry_node_id
    state["recovery_note"] = "本次已由调度器接续。历史停止状态已处理；复用已有产物，先核对实际结果，勿重复已完成工具操作。"
    return affected, previous


def reopen_team_checkpoint(flow, contract, state):
    """Only called by the host after an explicit, validated user Continue."""
    roots = unfinished_roots(flow, contract, state)
    if not roots:
        if state.get("round_open"):
            # Interrupted before dispatch, or reviewer protocol submission.
            roots = [state.get("failed_node_id") or contract.check_node_id]
        else:
            requested = [node_id for report in (state.get("reports") or {}).values()
                         for node_id in report.get("repair_node_ids") or []]
            requested = [node_id for node_id in requested if node_id in contract.node_ids
                         and node_id not in contract.reviewer_node_ids]
            waiting_answer = any(report.get("decision") == "BLOCKED" and report.get("question")
                                 for report in (state.get("reports") or {}).values())
            roots = requested or (list(contract.reviewer_node_ids) if waiting_answer else [contract.entry_node_id])
    roots = [node_id for node_id in roots if node_id in contract.node_ids] or [contract.entry_node_id]
    affected, previous = invalidate_results(flow, contract, state, roots, reason="explicit_continue")
    resumes = set(state.get("interrupted_nodes") or []) - affected
    for root in roots:
        old = previous.get(root) or {}
        if (old.get("status") in {"budget_exhausted", "stalled"}
                or old.get("error_kind") in {"branch_timeout", "provider_interrupted", "safety_review_rate_limited", "safety_review_context_repaired"}):
            resumes.add(root)
    state.update(status="running", reason="", error_kind="", failed_node_id="", stalled=0,
                 interrupted_nodes=sorted(resumes), budget_resume_pending=False)
    state.pop("interrupted_node", None)
    if not state.get("round_open"):
        state["carry_results"] = copy.deepcopy(state["round_results"])
    return affected


def invalidate_nested_inputs(flow, definitions, states, parent_key, affected, roots):
    """A completed child cannot be restored against invalidated parent inputs."""
    parent = states[parent_key]
    prefix = f"{parent_key}/{parent.get('round', 0)}/"
    for key, child in states.items():
        contract = definitions.get(key.split('/')[-1])
        if not key.startswith(prefix) or contract is None:
            continue
        inside = [root for root in roots if root in contract.node_ids]
        if inside:
            if child.get('status') == 'completed':
                invalidate_results(flow, contract, child, inside, reason='incomplete_parent_result')
                child.update(status='running', reason='', error_kind='', failed_node_id='', round_open=True)
            continue  # This child's own recovery retains its unaffected members.
        if contract.entry_node_id not in affected:
            continue
        previous = copy.deepcopy({name: value for name, value in child.items() if name != 'recovery_history'})
        child.setdefault('recovery_history', []).append({'reason': 'upstream_inputs_changed', 'state': previous})
        # Resource accounting and the contract/authorized policy stay intact.
        child.update(status='running', reason='', error_kind='', failed_node_id='', round_open=True,
                     round_results={}, reports={}, scores=None, score=0, stalled=0, history=[], evidence_history=[],
                     item_index=0, completed_items=[], item_results={}, interrupted_nodes=[], next_node_id=contract.entry_node_id)
        child.pop('carry_results', None)
        child.pop('interrupted_node', None)
        child['recovery_note'] = '上游输入已更新；旧结果仅作历史记录，请基于新输入核对产物并完成当前子任务。'


def choose_loop_recovery(state, node_id, *, policy, trigger, made_progress, identity):
    """Loop domain state is retained; the shared controller chooses the action."""
    from mewcode.kernel.escalation import decide_and_record
    if getattr(policy, "recovery_decider", None) is not None:
        return policy.recovery_decider(trigger=trigger, made_progress=made_progress, identity=identity)
    record = state.setdefault("controller_record", {"node_states": {node_id: {}}, "node_ledgers": {}})
    return decide_and_record(record, node_id, trigger=trigger, made_progress=made_progress, identity=str(identity))
