"""Initial work slots for a freshly authorized finite workflow, not money."""
from __future__ import annotations

from mewcode.orchestration.loops import LoopContract


def prepare(value, flow):
    """Size fresh elastic scheduling capacity to fit the declared first pass.

    Existing runs, spent usage, token limits and authorizations are untouched.
    This runs before the first reservation of a newly created execution only.
    """
    loops = [LoopContract.from_value(row) for row in flow.meta.get('loops') or []]
    finite = [loop for loop in loops if loop.mode == 'foreach']
    if not finite:
        return
    if ((value.get('policy') or {}).get('lineage_budget_limits') or {}).get('root', {}).get('attempts') is not None:
        return  # An explicit host/user ceiling is never enlarged here.
    attempts = 0
    for node in flow.nodes:
        count = 1
        for loop in finite:
            if node.node_id in loop.node_ids:
                count *= len(loop.items)
        attempts += count
    from mewcode.kernel import lineage_budget
    budget = lineage_budget.ensure(value)
    old = budget['limits']['root']['attempts']
    if attempts <= old:
        return
    if budget['reservations'] or budget['grants']:
        raise ValueError('初始队列容量只能在全新运行的首次预留前确定')
    # Keep a small shared repair reserve; a failed page cannot consume a new
    # allocation by rewriting its explanation or resetting the queue.
    limit = attempts + len(flow.nodes) * value['policy'].get('max_attempts', 3)
    value['policy'].setdefault('lineage_budget_limits', {}).setdefault('root', {})['attempts'] = limit
    budget['limits']['root']['attempts'] = limit
    value['lineage_budget'] = budget
    value['initial_work_capacity'] = {'first_pass_attempts': attempts, 'root_attempt_slots': limit,
        'finite_item_counts': {loop.loop_id: len(loop.items) for loop in finite},
        'scope': 'Scheduling slots only; actual permissions, spending caps and quality checks remain required.'}
