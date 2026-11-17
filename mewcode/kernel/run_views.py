"""Build display documents at commit time, never from a query or notification."""
from __future__ import annotations

import copy


DESIGN_VERSION = 2  # Include evidence-bound offers for unstarted operation retries.
TOOL_EVENTS = frozenset({'expert_tool', 'expert_tool_start', 'expert_tool_result',
                         'expert_node_tool', 'expert_member_tool'})


def update_tools(snapshot, previous, models, events):
    """Patch aggregate event batches, including usage buffered before a tool."""
    from mewcode.expert_runs import PUBLIC_AGGREGATE_EVENT_TYPES
    if previous.get('status') != 'running' or not previous.get('created_at'):
        return None
    loop_events = [event for event in events
                   if event.get('type') == 'expert_flow_state' and event.get('phase') == 'loop']
    if any(event.get('type') not in PUBLIC_AGGREGATE_EVENT_TYPES
           and not (event.get('type') == 'expert_flow_state' and event.get('phase') == 'loop')
           for event in events):
        return None
    for event in events:
        if event.get('type') not in {'expert_tool', 'expert_tool_start', 'expert_tool_result'}:
            continue
        member = event.get('member_id')
        key = f"{event.get('node_id')}::{member}" if member else event.get('node_id')
        stage = (previous.get('members' if member else 'node_states') or {}).get(key)
        if not isinstance(stage, dict):
            return None
        decisions = (stage.get('scheduler_recovery') or {}).get('decisions') or []
        if (decisions and stage.get('status') != 'completed'
                and decisions[-1].get('action') in {'human', 'hold_until_clearance'}):
            return None
    window = snapshot.get('events', [])[-50:]
    if any(type(event.get('seq')) is not int for event in window):
        return None
    from mewcode.expert_runs import redact_for_display, compact_expert_event
    from mewcode.kernel.events import digest
    retained = {event['seq'] for event in window}
    from mewcode.unicode_text import utf8_safe_json
    discharged = set(models['public'].get('discharged_audit_node_ids') or [])
    added = [utf8_safe_json(redact_for_display(compact_expert_event(
        dict(event, findings_discharged=event['node_id'] in discharged) if event.get('node_id') else event)))
        for event in loop_events if event.get('seq') in retained]
    result = dict(models)
    for space in ('public', 'summary'):
        old = models[space]
        kept = [event for event in old.get('events', [])
                if not event.get('snapshot_aggregate') and event.get('seq') in retained]
        aggregate = [event for event in old.get('events', []) if event.get('snapshot_aggregate')]
        result[space] = dict(old, kernel_revision=snapshot['kernel_revision'],
            last_seq=snapshot['last_seq'], updated_at=snapshot['updated_at'],
            events=[*kept, *added, *aggregate][-100:])
    view = dict(models['workbench'], task=dict(models['workbench']['task'],
                                              updated_at=snapshot['updated_at'],
                                              kernel_revision=snapshot['kernel_revision']))
    report_changed = any((stage.get('work_report') or {}) !=
                         ((previous.get('node_states') or {}).get(key) or {}).get('work_report', {})
                         for key, stage in (snapshot.get('node_states') or {}).items())
    if report_changed:
        from mewcode.experts.work_reporting import project_work_board
        project_work_board(view, snapshot)
        for space in ('public', 'summary'):
            states = dict(result[space].get('node_states') or {})
            for key, stage in (snapshot.get('node_states') or {}).items():
                if stage.get('work_report'):
                    states[key] = dict(states.get(key) or {},
                        work_report=utf8_safe_json(redact_for_display(stage['work_report'])))
            public = dict(result[space], node_states=states, stages=states)
            result[space] = dict(public, events=_stream_display_events(snapshot, previous, public))
    view.pop('version', None)
    view['version'] = digest(view)
    result['workbench'] = view
    result['aux'] = dict(models['aux'], kernel_revision=snapshot['kernel_revision'],
                         updated_at=snapshot['updated_at'])
    result['design'] = dict(models['design'], kernel_revision=snapshot['kernel_revision'])
    result['history'] = {'events': [redact_for_display(compact_expert_event(e)) for e in events]}
    return result


def update_calls(snapshot, previous, models):
    """Call proposals/reviews may leave every public execution fact unchanged."""
    if previous.get('node_ledgers') != snapshot.get('node_ledgers'):
        return None
    # Calls can affect recovery-gate options even before a node's facts change.
    # Recompute that small projection before reusing the larger workbench.
    design = build_design(snapshot)
    if dict(design, kernel_revision=models['design']['kernel_revision']) != models['design']:
        return None
    return update_stream(snapshot, previous, models)


def _stream_display_events(snapshot, previous, public):
    """Reuse redacted aggregates for nodes whose displayed fields did not change."""
    from mewcode.expert_runs import (PRIVATE_STAGE_FIELDS, PUBLIC_SUMMARY_EVENT_TYPES,
        _append_aggregate_display_events, compact_expert_event, redact_for_display)
    from mewcode.unicode_text import utf8_safe_json
    cached = {}
    for event in public.get('events', []):
        if event.get('snapshot_aggregate'):
            cached.setdefault(event.get('node_id'), []).append(event)
    display = dict(run_id=snapshot['run_id'], status=snapshot['status'], events=[
        compact_expert_event(event) for event in (snapshot.get('events') or [])[-50:]
        if event.get('type') in PUBLIC_SUMMARY_EVENT_TYPES])
    _append_aggregate_display_events(display,
        source=dict(snapshot, node_states={}, stages={}), include_details=False)
    discharged = set(public.get('discharged_audit_node_ids') or [])

    def publish(rows):
        return utf8_safe_json(redact_for_display([
            dict(event, findings_discharged=event.get('node_id') in discharged)
            if event.get('node_id') else event for event in rows]))

    result = publish(display['events'])
    visible = {'text', 'partial_text', 'display_reasoning'}
    for node_id, stage in sorted((snapshot.get('node_states') or {}).items()):
        old = (previous.get('node_states') or {}).get(node_id, {})
        keys = {key for key in set(stage) | set(old)
                if key not in PRIVATE_STAGE_FIELDS or key in visible}
        expected_count = (1 + bool(stage.get('text') or stage.get('partial_text'))
                          + bool(stage.get('display_reasoning')) + bool(stage.get('usage')))
        rows = cached.get(node_id, [])
        if (len(rows) == expected_count and all(key in old and key in stage
                and (stage[key] is old[key] or stage[key] == old[key]) for key in keys)):
            result.extend(rows)
            continue
        node_display = dict(run_id=snapshot['run_id'], status=snapshot['status'], events=[])
        _append_aggregate_display_events(node_display,
            source=dict(snapshot, node_states={node_id: stage}), include_details=False)
        result.extend(publish(node_display['events']))
    return result[-100:]


def update_stream(snapshot, previous, models):
    """Stream text is private context plus a bounded public display update.

    Changes to recovery facts still rebuild the dependent decision views.
    History receipts do not change during a stream checkpoint.
    """
    if previous.get('node_ledgers') != snapshot.get('node_ledgers'):
        return None
    for stage in (snapshot.get('node_states') or {}).values():
        decisions = (stage.get('scheduler_recovery') or {}).get('decisions') or []
        if (decisions and stage.get('status') != 'completed'
                and decisions[-1].get('action') in {'human', 'hold_until_clearance'}):
            return None
    result = update_tools(snapshot, previous, models, [])
    if result is None:
        return None
    from mewcode.expert_runs import PRIVATE_STAGE_FIELDS, redact_for_display
    from mewcode.unicode_text import utf8_safe_json
    public = result['public']
    states = dict(public.get('node_states') or {})
    for node_id, stage in (snapshot.get('node_states') or {}).items():
        old = (previous.get('node_states') or {}).get(node_id)
        if old is None or node_id not in states:
            return None
        changed = {key: value for key, value in stage.items() if key not in PRIVATE_STAGE_FIELDS
                   and (key not in old or type(old[key]) is not type(value) or old[key] != value)}
        if changed.keys() - {'node_id', 'member_id', 'stream_activity', 'safety_review_state'}:
            return None
        if changed:
            states[node_id] = dict(states[node_id], **utf8_safe_json(redact_for_display(changed)))
    # Large static flow/contract documents keep their already-redacted objects.
    # Only stream fields, usage and the bounded aggregate display can change.
    public = dict(public, node_states=states, stages=states,
                  usage=utf8_safe_json(redact_for_display(snapshot.get('usage') or {})),
                  events=_stream_display_events(snapshot, previous, public))
    result.update(public=public, summary=dict(public))
    return result


def update_loop(snapshot, previous, models):
    """Loop scheduling changes no plan, contract, acceptance or history facts."""
    if previous.get('node_ledgers') != snapshot.get('node_ledgers'):
        return None
    # A newly projected blocker or owner must be published by the full builder.
    for name, stage in snapshot.get('node_states', {}).items():
        old = previous.get('node_states', {}).get(name, {})
        if stage != old:
            return None
    result = update_tools(snapshot, previous, models, [])
    if result is None:
        return None
    from mewcode.expert_runs import compact_loop_state, redact_for_display
    from mewcode.unicode_text import utf8_safe_json
    fields = utf8_safe_json(redact_for_display(dict(
        flow_state=snapshot['flow_state'],
        loop_states={key: compact_loop_state(value) for key, value in snapshot['loop_states'].items()})))
    for space in ('public', 'summary'):
        result[space] = dict(result[space], **fields)
    from mewcode.experts.work_reporting import project_work_board
    view = dict(result['workbench'])
    project_work_board(view, snapshot)
    from mewcode.kernel.events import digest
    view.pop('version', None)
    view['version'] = digest(view)
    result['workbench'] = view
    return result


def build_design(snapshot):
    """Compatibility card, including decision bindings, built by the writer."""
    from mewcode.orchestration.task_view import collaboration_projection
    collaborative = snapshot.get('orchestration_mode') == 'collaborative'
    return dict(version=DESIGN_VERSION, run_id=snapshot['run_id'],
                conversation_id=snapshot.get('conversation_id', ''),
                task_id=(snapshot.get('collaboration') or {}).get('task_id', ''),
                kernel_revision=snapshot.get('kernel_revision', 0),
                collaboration=collaboration_projection(snapshot) if collaborative else None)


def build(snapshot, *, history_events=None):
    from mewcode.expert_runs import ExpertRunStore, redact_for_display, compact_expert_event
    from mewcode.orchestration import task_view, inbox
    # The overview has a fixed history window. Full receipts remain in current
    # state and the indexed history; this is not an execution-context projection.
    source = dict(snapshot, events=(snapshot.get('events') or [])[-50:])
    public = ExpertRunStore._public_value(source, include_details=False)
    public['events'] = public.get('events', [])[-100:]
    view = task_view.from_record(snapshot)
    static_inbox = inbox.collect(view['task'], record=snapshot)
    flow = snapshot.get('locked_flow') or {}
    aux = dict(run_id=snapshot['run_id'], conversation_id=snapshot.get('conversation_id', ''),
               kernel_revision=snapshot.get('kernel_revision', 0), status=snapshot.get('status'),
               updated_at=snapshot.get('updated_at'), status_reason=snapshot.get('status_reason', ''),
               graph_routing={'version': flow.get('version'), 'meta': {
                   'pipeline_graph_id': (flow.get('meta') or {}).get('pipeline_graph_id')}},
               locked_flow={'flow_id': flow.get('flow_id'), 'nodes': [
                   {k: row[k] for k in ('node_id', 'label') if k in row} for row in flow.get('nodes', [])]},
               static_inbox=static_inbox)
    history = (snapshot.get('events') or []) if history_events is None else history_events
    events = [redact_for_display(compact_expert_event(event)) for event in history]
    # This document is private to the host read adapter; its static questions
    # contain their original binding. Only task_view.finish exposes them.
    return {'public': public, 'summary': copy.deepcopy(public), 'workbench': view,
            'aux': aux, 'history': {'events': events}, 'design': build_design(snapshot)}


def overlay(view, design, *, runtime=None):
    view = copy.deepcopy(view)
    task = view['task']
    if runtime and runtime.get('run_id') == task.get('run_id'):
        task['execution_status'] = runtime.get('status', task.get('execution_status', ''))
        task['kernel_revision'] = runtime.get('kernel_revision', task.get('kernel_revision', 0))
    if design and design.get('task_id') == task['id'] and design.get('run_id') == task['run_id']:
        task['design_revision'] = design.get('revision')
        if design.get('status') == 'paused' and task.get('status') == '已暂停' and design.get('reason'):
            task['reason'] = design['reason']
    from mewcode.orchestration.task_view import align_execution_display
    align_execution_display(view)
    return view
