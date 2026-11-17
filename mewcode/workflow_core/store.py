"""Workflow state in the existing durable run journal."""
from __future__ import annotations

import copy
import time

from .migration import migrate
from .schema import walk


class RunBackend:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    def read(self):
        record = self.store.read_execution_fields(self.run_id, 'workflow_core')
        return record['workflow_core']

    def query(self, project):
        """Detach a read-only projection without cloning archived conversations."""
        with self.store._lock:
            return copy.deepcopy(project(self.store._cached(self.run_id)['workflow_core']))

    def change(self, callback):
        with self.store._lock:
            current = self.store._cached(self.run_id)
            if current is None:
                raise KeyError(self.run_id)
            state = copy.deepcopy(current['workflow_core'])
            result = callback(state)
            if state != current['workflow_core']:
                def update(record):
                    record['workflow_core'] = state
                    synchronize(record)
                self.store.mutate(self.run_id, update, durable=True, return_snapshot=False)
            return copy.deepcopy(result)

    def publish(self, identity, kind, fields, **metadata):
        return self.store.record_workflow_attempt(self.run_id, identity, kind, fields, **metadata)


def publish_attempt(state, identity, kind, fields, *, stamp, execution=None, phases=()):
    """Update one attempt and its own conversation, leaving archived turns untouched."""
    attempt = state['attempts'].get(identity)
    if not attempt:
        return None
    attempt['last_activity_at'] = stamp
    if execution:
        attempt['execution'] = copy.deepcopy(execution)
    if phases:
        saved = list(attempt.get('phase_events') or [])
        last = saved[-1].get('at', 0) if saved else 0
        saved.extend(copy.deepcopy(row) for row in phases if row['at'] > last)
        attempt['phase_events'] = saved[-400:]
    if kind == 'progress':
        text = str(fields.get('text') or '').strip()
        progress = list(attempt['progress'])
        if text and (not progress or progress[-1]['text'] != text):
            progress.append(dict(text=text, at=stamp, id=fields.get('id', '')))
            attempt['progress'] = progress
    elif kind == 'artifact':
        artifact = copy.deepcopy(fields['artifact'])
        if not any(item.get('id') == artifact.get('id') for item in attempt['artifacts']):
            attempt['artifacts'] = [*attempt['artifacts'], artifact]
    elif kind == 'checkpoint':
        for key in ('messages', 'pending_tools', 'usage', 'partial_text'):
            if key in fields:
                attempt[key] = copy.deepcopy(fields[key])
        from . import dialogue
        dialogue.checkpoint(state, attempt)
    else:
        attempt['diagnostics'] = [*attempt['diagnostics'], dict(kind=kind, at=stamp, **copy.deepcopy(fields))]
    return dict(node_id=attempt['node_id'], attempt_id=identity,
                graph_revision=attempt['graph_revision'], iteration=copy.deepcopy(attempt['iteration']))


def synchronize(record):
    state = record['workflow_core']
    record['status'] = state['status'] if state['status'] != 'pending' else 'paused'
    record['active_node_ids'] = ([path for path, row in state['nodes'].items() if row['status'] == 'running']
                                 if state['status'] == 'running' else [])
    if state['status'] in {'completed', 'terminated'}:
        record.setdefault('completed_at', time.time())
    else:
        record['completed_at'] = None
    nodes = dict(walk(state['graph']))
    for path, row in state['nodes'].items():
        attempt = state['attempts'].get(row.get('attempt_id')) or {}
        result = row.get('result') or {}
        stage = record.setdefault('node_states', {}).setdefault(path, {})
        stage.update(node_id=path, label=nodes.get(path, {}).get('label', path), role='executor',
                     status=row['status'], node_attempt=attempt.get('number', 0), text=result.get('text', ''),
                     partial_text=attempt.get('partial_text', ''), input_revision=attempt.get('id', ''),
                     workflow_attempt_id=attempt.get('id', ''), graph_revision=state['graph']['revision'],
                     started_at=attempt.get('started_at'), completed_at=attempt.get('ended_at'),
                     workflow_delivery_id=row.get('published_delivery_id', ''),
                     artifacts=copy.deepcopy(result.get('artifacts') if row.get('published_delivery_id')
                                             else attempt.get('artifacts') or result.get('artifacts') or []))
    record['status_reason'] = (state.get('runtime_fault') or {}).get('reason') or next(
        (row.get('reason', '') for row in state['nodes'].values()
         if row['status'] in {'unknown', 'needs_input', 'failed', 'blocked', 'paused'} and row.get('reason')), '')


def ensure(store, run_id, graph=None):
    record = store.load(run_id)
    if not record:
        raise ValueError('运行不存在')
    if not record.get('workflow_core'):
        def update(value):
            migrate(value, graph)
            synchronize(value)
        if 'kernel_revision' not in record:
            # Import through the journal writer, preserving the entire old snapshot.
            with store._lock:
                value = copy.deepcopy(store._cached(run_id))
                update(value)
                value.pop('migration_required', None)
                value['legacy'] = False
                store._write(value)
        else:
            store.mutate(run_id, update, durable=True)
    return RunBackend(store, run_id)
