"""Replace the old config form with specification-bound startup answers.

Reads do not prepare choices or call a model. Answers remain in DraftStore;
TaskDesign passes them through the original confirm/resolve_run_inputs path.
"""
from __future__ import annotations

import copy

from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration import inbox
from mewcode.orchestration.run_inputs import resolve_run_inputs


def saved_spec(srv, project, cid, task_id):
    plan = srv._conversation_plan_store(cid).get(project)
    spec = plan.spec.as_dict() if plan and plan.spec else None
    if not spec or (spec.get('meta', {}).get('task_design') or {}).get('task_id') != task_id:
        return None
    return spec


def receipt(state, task_id, spec):
    value = state.get('workbench_startup') or {}
    return value if value.get('task_id') == task_id and value.get('spec_hash') == digest(spec) else None


def collect(srv, project, cid, task, state):
    design = state.get('task_design') or {}
    if design.get('intent') != 'execute' or task.get('run_id') or task.get('readonly'):
        return []
    spec = saved_spec(srv, project, cid, task['id'])
    definitions = (spec or {}).get('goal_contract', {}).get('run_inputs') or []
    questions = [copy.deepcopy(row) for row in definitions if row.get('collect_at', 'start') == 'start']
    if not questions or receipt(state, task['id'], spec):
        return []
    for row in questions:
        row.setdefault('question', row.get('label') or row['name'])
    return [inbox.make('run_inputs', task, 'startup', title='确认本次运行参数',
        background='填写本次使用的输入；提交后可继续任务。', questions=questions,
        basis=[spec, design.get('revision')], context=dict(spec_hash=digest(spec), design_revision=design.get('revision')))]


def save(srv, project, cid, task_id, context, values):
    store = DraftStore(project, cid)
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        state = store._get(db)
        design = state.get('task_design') or {}
        spec = saved_spec(srv, project, cid, task_id)
        if (not spec or design.get('task_id') != task_id or design.get('run_id')
                or design.get('intent') != 'execute' or digest(spec) != context.get('spec_hash')
                or design.get('revision') != context.get('design_revision') or receipt(state, task_id, spec)):
            raise ValueError(inbox.STALE)
        normalized = resolve_run_inputs((spec.get('goal_contract') or {}).get('run_inputs') or [], values)
        state['workbench_startup'] = dict(task_id=task_id, spec_hash=digest(spec), values=normalized)
        store._put(db, state)
    return dict(ok=True, run_inputs=normalized, execution_started=False)


def execution_values(srv, project, cid, design, spec):
    if design.get('run_id'):
        record = srv.EXPERT_RUNS.load(design['run_id']) or {}
        if record.get('conversation_id') != cid:
            raise ValueError('任务运行归属已变化，未采用旧参数。')
        return copy.deepcopy(record.get('run_inputs') or {})
    state = DraftStore.read_state(project, cid) or {}
    value = receipt(state, design['task_id'], spec)
    return copy.deepcopy(value['values']) if value else {}
