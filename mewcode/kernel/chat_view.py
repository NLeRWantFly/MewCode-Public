"""Small chat reads of committed display data; never read execution state."""
from __future__ import annotations

import json

from mewcode.kernel.sqlite_state import pointer


# These are internal diagnostic/context histories, not rendered chat content.
STAGE_HISTORY = frozenset({'segment_health_history', 'segment_health',
                           'interaction_history', 'slice_progress_baseline'})
FLOW_CONTEXT = frozenset({'collaboration_spec', 'workflow_draft',
                         'node_plan_briefs', 'run_input_contract'})
TOOL_TYPES = frozenset({'expert_tool', 'expert_tool_start', 'expert_tool_wait',
                        'expert_tool_state', 'expert_tool_result'})


def compact_flow(flow):
    return dict(flow, meta={key: value for key, value in (flow.get('meta') or {}).items()
                            if key not in FLOW_CONTEXT}) if isinstance(flow, dict) else flow


def compact(snapshot):
    """Legacy display-only adapter; all authoritative state remains intact."""
    result = dict(snapshot)
    result['locked_flow'] = compact_flow(snapshot.get('locked_flow'))
    result['node_states'] = {key: {name: value for name, value in stage.items() if name not in STAGE_HISTORY}
                             for key, stage in (snapshot.get('node_states') or {}).items()}
    result['stages'] = result['node_states']
    result['events'] = [dict({key: value for key, value in event.items() if key not in STAGE_HISTORY},
                            **({'flow': compact_flow(event['flow'])} if 'flow' in event else {}))
                        for event in snapshot.get('events', [])]
    return result


def read(database, db, space='summary'):
    """Skip large diagnostic subtrees before materializing existing projections.

    The read transaction is owned by the caller, so stages, sequence boundary
    and recent tools always describe the same committed revision.
    """
    def keys(parts):
        return [json.loads(row[0]) for row in db.execute(
            'SELECT item_key FROM cells WHERE run_id=? AND space=? AND parent=? ORDER BY ordinal,item_key',
            (database.run_id, space, pointer(parts)))]

    def object_at(parts, omit=()):
        return {key: database._read(db, space, (*parts, key)) for key in keys(parts) if key not in omit}

    result = object_at((), {'node_states', 'stages', 'events'})
    if not result:
        raise LookupError('run read model is not ready')
    result['locked_flow'] = compact_flow(result.get('locked_flow'))
    result['node_states'] = {key: object_at(('node_states', key), STAGE_HISTORY) for key in keys(('node_states',))}
    result['stages'] = result['node_states']
    events = []
    for key in keys(('events',)):
        event = object_at(('events', key), STAGE_HISTORY)
        if 'flow' in event:
            event['flow'] = compact_flow(event['flow'])
        events.append(event)
    # Restore a bounded recent tool trail even though the overview projection
    # intentionally stores tools in indexed history instead of node_states.
    tail = db.execute('SELECT seq FROM history_index WHERE run_id=? AND seq<=? ORDER BY seq DESC LIMIT 100',
                      (database.run_id, result.get('last_seq', 0))).fetchall()
    recent = []
    for (seq,) in reversed(tail):
        try:
            kind = database._read(db, 'history', ('events', seq, 'type'))
        except LookupError:
            continue
        if kind in TOOL_TYPES:
            event = database._read(db, 'history', ('events', seq))
            event['history'] = True
            event['snapshot_aggregate'] = True
            recent.append(event)
    if result.get('status') != 'running':
        def identity(event):
            return tuple(event.get(key) for key in ('node_id', 'attempt', 'node_attempt', 'member_id')) + (
                event.get('id') or event.get('tool_id'),)
        finished = {identity(event) for event in recent if event['type'] == 'expert_tool_result'}
        for event in recent:
            if event['type'] in {'expert_tool', 'expert_tool_start'} and identity(event) not in finished:
                event.update(type='expert_tool_state', status='outcome_unknown')
    result['events'] = events + recent[-40:]
    return result
