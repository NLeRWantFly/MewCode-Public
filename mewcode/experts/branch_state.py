"""Reconcile unexecuted skips produced while their dependency was unresolved."""
from __future__ import annotations


def _has_execution_evidence(stage):
    return stage.get('passed') is True or any(stage.get(key) for key in (
        'node_attempt', 'started_at', 'tool_calls', 'tools', 'messages', 'handoff', 'artifacts',
        'command_receipts', 'pending_permission', 'pending_interaction', 'input_tokens', 'output_tokens'))


def reconcile_unresolved_branch_skips(snapshot):
    states = snapshot.get('node_states') or {}
    flow = snapshot.get('locked_flow') or {}
    paused_roots = {event.get('node_id') for event in snapshot.get('events') or []
                    if isinstance(event, dict) and event.get('phase') == 'branch_paused'}
    flow_state = snapshot.get('flow_state') or {}
    if flow_state.get('phase') == 'branch_paused':
        paused_roots.add(flow_state.get('node_id'))
    unresolved = {identity for identity in paused_roots if isinstance(states.get(identity), dict)
                  and states[identity].get('status') in {
                      'paused', 'blocked', 'needs_attention', 'failed', 'budget_exhausted', 'stalled'}}
    if not unresolved:
        return []
    adjacency = {}
    for edge in flow.get('edges') or []:
        if not isinstance(edge, dict):
            continue
        source, target = edge.get('from_node') or edge.get('from'), edge.get('to_node') or edge.get('to')
        if source and target:
            adjacency.setdefault(source, set()).add(target)
    # The scheduler skips an inactive loop as a whole. Its repair-only nodes
    # need not be reachable in the forward DAG. Include them only when that
    # loop never started and its entry has the same erroneous scheduling mark.
    for loop in (flow.get('meta') or {}).get('loops') or []:
        if not isinstance(loop, dict):
            continue
        entry = loop.get('entry_node_id')
        stage = states.get(entry) or {}
        loop_id = loop.get('loop_id')
        started = any(key == loop_id or key.endswith('/' + str(loop_id))
                      for key in snapshot.get('loop_states') or {})
        if (started or stage.get('status') != 'skipped' or stage.get('reason') != 'branch_not_taken'
                or _has_execution_evidence(stage)):
            continue
        adjacency.setdefault(entry, set()).update(
            key for key in loop.get('node_ids') or [] if key != entry)
    blocked_by = {}
    for root in unresolved:
        pending, visited = list(adjacency.get(root, ())), {root}
        while pending:
            identity = pending.pop()
            if identity in visited:
                continue
            visited.add(identity)
            blocked_by.setdefault(identity, set()).add(root)
            pending.extend(adjacency.get(identity, ()))
    repaired = []
    for identity, roots in blocked_by.items():
        stage = states.get(identity) or {}
        if stage.get('status') != 'skipped' or stage.get('reason') != 'branch_not_taken':
            continue
        # A scheduling event has no execution evidence. Never reset actual work,
        # an explicit fallback selection, a completion, or an uncertain tool.
        if _has_execution_evidence(stage):
            continue
        if any((snapshot.get(key) or {}).get(identity) for key in (
                'pipeline_runs', 'review_queues', 'review_queue_refs', 'command_receipts', 'node_expansions')):
            continue
        if any(identity in (resolution.get('inactive_alternatives') or [])
               for resolution in (snapshot.get('fallback_resolutions') or {}).values()):
            continue
        ledger = (snapshot.get('node_ledgers') or {}).get(identity) or {}
        if any(fact.get('kind') in {'artifact', 'receipt', 'handoff', 'answer'} for fact in ledger.get('facts') or []):
            continue
        stage['branch_skip_history'] = [*(stage.get('branch_skip_history') or []), {
            'status': 'skipped', 'reason': 'branch_not_taken', 'attempt': stage.get('attempt'),
            'reconciled_reason': 'predecessor_unresolved', 'blocked_by': sorted(roots)}]
        stage.update(status='pending', reason='predecessor_unresolved', blocked_by=sorted(roots))
        repaired.append(identity)
    return sorted(repaired)
