"""Bounded public work reports, separate from execution and acceptance facts."""
from __future__ import annotations

import copy

from mewcode.tools.report_step import WorkIssue, WorkProgress, report_fields

MAX_ISSUES = 16


def current_report(stage):
    report = stage.get('work_report') or {}
    binding = report.get('binding') or {}
    if (report.get('schema_version') != 1 or not binding
            or any(binding.get(key) != stage.get(key, 0 if key == 'epoch' else None)
                   for key in ('input_revision', 'node_attempt', 'epoch'))):
        return None
    return report


def apply_work_report(stage, item, event):
    """Consume a successful reporting-tool receipt, never a raw model message."""
    if item.get('name') != 'report_step' or item.get('is_error') or item.get('status') != 'completed':
        return
    if (item.get('node_attempt') != stage.get('node_attempt')
            or item.get('epoch', 0) != stage.get('epoch', 0)
            or item.get('input_revision', stage.get('input_revision')) != stage.get('input_revision')):
        return
    payload = (item.get('host_observation') or {}).get('work_report')
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        return
    try:
        progress = WorkProgress.model_validate(payload['progress']).model_dump(exclude_none=True) if payload.get('progress') else None
        updates = [WorkIssue.model_validate(row).model_dump() for row in payload.get('issues', [])[:5]]
    except (TypeError, ValueError):
        return
    old = current_report(stage) or {}
    seq = int(event.get('seq') or 0)
    # Reporting tools may finish out of order. The declared observation order
    # governs each field; a late result can add another issue but cannot roll
    # the position or an existing issue back to an earlier observation.
    declared = item.get('declared_at')
    order = float(declared) if type(declared) in (int, float) and declared > 0 else seq
    newer = not old or order > old.get('order', old.get('seq', 0))
    issues = {(row['scope'], row['id']): row for row in old.get('issues', [])}
    for issue in updates:
        key = (issue['scope'], issue['id'])
        previous = issues.get(key) or {}
        if previous and order <= previous.get('order', previous['seq']):
            continue
        issues[key] = dict(issue, seq=seq, order=order,
            first_order=previous.get('first_order', order), first_seq=previous.get('first_seq', seq))
    # Resolved history is expendable before any outstanding problem.
    bounded = sorted(issues.values(), key=lambda row: (row['state'] != 'resolved', row['seq']))[-MAX_ISSUES:]
    report = {'schema_version': 1, 'binding': {key: stage.get(key, 0 if key == 'epoch' else None)
                  for key in ('input_revision', 'node_attempt', 'epoch')},
              'label': str(payload.get('label') or '')[:180], 'status': payload.get('status', 'running'),
              **report_fields(payload), 'seq': seq, 'order': order,
              'updated_at': event.get('server_time'), 'tool_id': item.get('tool_id')} if newer else dict(old)
    report['issues'] = copy.deepcopy(bounded)
    if progress and order > old.get('progress_order', -1):
        report.update(progress=progress, progress_seq=seq, progress_order=order)
    elif old.get('progress'):
        report.update(progress=copy.deepcopy(old['progress']),
            progress_seq=old.get('progress_seq', old.get('seq', 0)),
            progress_order=old.get('progress_order', old.get('order', 0)))
    stage['work_report'] = report


def active_flow(record):
    return (record.get('collaboration') or {}).get('runtime_flow') or record.get('locked_flow') or {}


def _issues(reports):
    rows = {}
    origins = {}
    for report in reports:
        for issue in report.get('issues', []):
            key = (issue['scope'], issue['id'])
            first = issue.get('first_order', issue.get('first_seq', issue['seq']))
            if key not in origins or first < origins[key]['seq']:
                origins[key] = dict(seq=first, node_id=report['node_id'], label=report['node_label'])
            if key not in rows or issue.get('order', issue['seq']) > rows[key].get('order', rows[key]['seq']):
                rows[key] = dict(issue, node_id=report['node_id'])
    for key, row in rows.items():
        row.update(origin_node_id=origins[key]['node_id'], reporter=origins[key]['label'])
    return sorted(rows.values(), key=lambda row: row['seq'], reverse=True)


def project_work_board(view, record, flow=None):
    """Attach current reports and loop regions without walking execution history.

    Caller-owned read models may share static plan objects. Copy only changed
    presentation paths; no scheduler, authority, ledger or verdict is modified.
    """
    flow = flow or active_flow(record)
    stages = record.get('node_states') or {}
    reports = {}
    labels = {row['id']: row['label'] for row in view['structure'].get('nodes', [])}
    for node_id, label in labels.items():
        report = current_report(stages.get(node_id) or {})
        if report:
            reports[node_id] = dict(copy.deepcopy(report), node_id=node_id, node_label=label)
    nodes = [dict(row, work_report=reports.get(row['id'])) for row in view['structure'].get('nodes', [])]
    view['plan'] = [dict(group, steps=[dict(step, work_report=reports.get(step['id']))
                         for step in group.get('steps', [])]) for group in view['plan']]
    loops = []
    for spec in (flow.get('meta') or {}).get('loops', []):
        members = [key for key in spec.get('node_ids', []) if key in labels]
        if not members:
            continue
        loop_id = spec['loop_id']
        state = (record.get('loop_states') or {}).get(loop_id) or {}
        sources = [reports[key] for key in members if key in reports]
        latest = max(sources, key=lambda row: row.get('order', row['seq']), default=None)
        counted = max((row for row in sources if row.get('progress')),
                      key=lambda row: row.get('progress_order', row.get('progress_seq', row['seq'])), default=None)
        loops.append({'id': loop_id, 'label': spec.get('label') or spec.get('title') or '循环处理',
            'node_ids': members, 'entry_node_id': spec.get('entry_node_id'),
            'check_node_id': spec.get('check_node_id'),
            'round': state.get('round'), 'status': state.get('status', 'pending'),
            'active_node_ids': [key for key in record.get('active_node_ids', []) if key in members],
            'report': latest, 'progress': copy.deepcopy(counted.get('progress')) if counted else None,
            'progress_node_id': counted['node_id'] if counted else None, 'issues': _issues(sources)})
    view['structure'] = dict(view['structure'], nodes=nodes, loops=loops)
    latest = max(reports.values(), key=lambda row: row.get('order', row['seq']), default=None)
    view['work_board'] = {'report': latest, 'issues': _issues(reports.values())}
    if latest:
        counted = max((row for row in reports.values() if row.get('progress')),
                      key=lambda row: row.get('progress_order', row.get('progress_seq', row['seq'])), default=None)
        view['work_board']['progress'] = copy.deepcopy(counted.get('progress')) if counted else None
    return view
