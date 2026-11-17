"""Reserve the first execution of every reviewed child and its parent join.

These reservations live in the existing lineage ledger and graph publication.
They are not new allowances: dispatch claims one and the real start settles it.
Call mutators only inside the host's existing transaction.
"""
from __future__ import annotations

from mewcode.kernel import lineage_budget


def expansion_capacity(value, contract_id):
    """A read-only cost projection, after any current attempt's reservation."""
    lineage_id = value['contracts'][contract_id].get('lineage_id') or contract_id
    # A projection reads bounded counters, not reservation history. Actual
    # prepare/dispatch still validate and reserve against the complete ledger.
    budget = value['lineage_budget']
    grants = budget['grants'].values()
    remaining = {}
    for scope, key in (('root', None), ('lineage', lineage_id)):
        counters = budget['root'] if scope == 'root' else budget['lineages'].get(lineage_id, {})
        limit = budget['limits'][scope]['attempts'] + sum(
            row['amount'] for row in grants if row['kind'] == 'attempts' and row['lineage_id'] == key)
        used = counters.get('used', {}).get('attempts', 0)
        reserved = counters.get('reserved', {}).get('attempts', 0)
        if any(type(number) is not int or number < 0 for number in (limit, used, reserved)):
            raise ValueError('执行额度投影需要有效的非负整数计数')
        remaining[scope] = max(0, limit - used - reserved)
    mode = 'elastic' if lineage_budget.elastic(value) else 'fixed'
    advisory_remaining = remaining['lineage']
    if mode == 'elastic':
        remaining.pop('lineage')
    available = min(remaining.values())
    from mewcode.kernel.collaboration import check_attempt
    accounted = bool(dispatch_reservation(value, contract_id))
    for identity, row in value.get('attempts', {}).items():
        if row.get('contract_id') != contract_id or row.get('status') not in {'dispatched', 'checking'}:
            continue
        try:
            check_attempt(value, identity)
        except ValueError:
            continue
        accounted = True
        break
    current_cost = 0 if accounted else 1
    minimum = 3 + current_cost
    return {'lineage_id': lineage_id, 'allocation_mode': mode,
            'advisory_lineage_remaining': advisory_remaining, 'available_attempts': available,
            'remaining_by_scope': remaining, 'minimum_worker_attempts': 2,
            'parent_join_attempts': 1, 'minimum_additional_attempts': minimum,
            'minimum_shortfall': max(0, minimum - available),
            'max_children': min(6, max(0, available - 1 - current_cost)),
            'current_attempt_already_accounted': accounted,
            'current_attempt_additional_cost': current_cost,
            'dispatch_requires_live_reservation': True,
            'retry_allowance_included': False}


def reserve_expansion(value, fragment, publication_id):
    """Preflight the complete cost, then reserve each first attempt separately."""
    keys = [*fragment['child_ids'], fragment['parent_id']]
    lineage_id = fragment['lineage_id']
    # A single preflight reports the total missing amount, rather than the
    # last worker's one unit. Its temporary reservation is never persisted.
    preview = {'lineage_budget': value['lineage_budget'], 'policy': value.get('policy')}
    lineage_budget.reserve(preview, 'graph-preflight:' + publication_id,
                           lineage_id=lineage_id, kind='attempts', amount=len(keys))
    allocated = {}
    for key in keys:
        identity = 'graph-attempt:' + publication_id + ':' + key
        lineage_budget.reserve(value, identity, lineage_id=lineage_id, kind='attempts')
        allocated[key] = identity
    return allocated


def verify_expansion_reservations(value, publication):
    keys = {*publication['fragment']['child_ids'], publication['fragment']['parent_id']}
    allocated = publication.get('attempt_reservations')
    if not isinstance(allocated, dict) or set(allocated) != keys:
        raise ValueError('分工必须为全部子任务和父汇总预留执行额度')
    claimed = {row.get('budget_reservation_id') for row in value['attempts'].values()}
    budget = lineage_budget.ensure({'lineage_budget': value['lineage_budget']})
    for key, identity in allocated.items():
        expected = 'graph-attempt:' + publication['id'] + ':' + key
        row = budget['reservations'].get(identity, {})
        if (identity != expected or identity in claimed or row.get('status') != 'reserved'
                or row.get('kind') != 'attempts' or row.get('amount') != 1
                or row.get('lineage_id') != publication['fragment']['lineage_id']):
            raise ValueError('分工执行预留已失效或被其它尝试占用')


def dispatch_reservation(value, contract_id):
    """Claim only an unused permit for this exact published contract version."""
    claimed = {row.get('budget_reservation_id') for row in value['attempts'].values()}
    matches = []
    for item in value.get('graph_publications', {}).values():
        identity = item.get('attempt_reservations', {}).get(contract_id)
        if not identity or item['status'] != 'published' or identity in claimed:
            continue
        original = item['fragment']['contracts'][contract_id]
        if original['version'] != value['contracts'][contract_id]['version']:
            continue
        permit = value['lineage_budget']['reservations'][identity]
        if permit['status'] == 'reserved':
            matches.append(identity)
    if len(matches) > 1:
        raise ValueError('当前合同存在多个未认领的执行预留')
    return matches[0] if matches else None


def release_unclaimed(value, publication, *, obsolete_only=False):
    """Return unused permits on rejection or revision; never refund real work."""
    claimed = {row.get('budget_reservation_id') for row in value['attempts'].values()}
    for key, identity in publication.get('attempt_reservations', {}).items():
        if identity in claimed:
            continue
        if obsolete_only and (value['contracts'].get(key, {}).get('version')
                              == publication['fragment']['contracts'][key]['version']):
            continue
        if value['lineage_budget']['reservations'][identity]['status'] == 'reserved':
            lineage_budget.release(value, identity)


def release_obsolete(value):
    for item in value.get('graph_publications', {}).values():
        if item['status'] == 'published':
            release_unclaimed(value, item, obsolete_only=True)
