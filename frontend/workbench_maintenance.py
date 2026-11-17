"""Move maintenance history into the workbench and decisions into its inbox.

Original maintenance endpoints still own model execution, permissions, graph
validation and run adoption. This adapter only binds the selected task.
"""
from __future__ import annotations

import copy

from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.orchestration import inbox
from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.maintenance import MaintenanceStore


def records(srv, project, cid, task, record=None):
    ledger = getattr(srv, 'MAINTENANCE_STORE', None)
    rows = MaintenanceStore.read_states(project, cid, getattr(ledger, 'path', None))
    run_id = task.get('run_id') or ''
    flow_id = ((record or {}).get('locked_flow') or {}).get('flow_id')
    return [row for row in rows if row.get('run_id', '') == run_id and (
        (flow_id and row.get('base', {}).get('spec_id') == flow_id) or
        (row.get('base', {}).get('meta', {}).get('task_design') or {}).get('task_id') == task['id'])]


def project(srv, project_dir, cid, task, record=None):
    rows = records(srv, project_dir, cid, task, record)
    cards, summaries = [], []
    plan = srv._conversation_plan_store(cid).get(project_dir) if rows else None
    current_hash = digest(plan.spec.as_dict()) if plan and plan.spec else ''
    for state in rows:
        summary = MaintenanceStore.public(None, state)
        pending = getattr(srv, 'workbench_maintenance_pending', lambda _: None)(state['id'])
        summary['stale'] = state['base_hash'] != current_hash
        summary['agent'] = dict(summary.get('agent') or {}, pending=pending)
        if state['status'] == 'running' and not pending and (state.get('agent') or {}).get('pending'):
            summary['interrupted'] = True
        summaries.append(summary)
        if task.get('readonly'):
            continue
        context = dict(id=state['id'], revision=state['revision'], proposal_id=(state.get('proposal') or {}).get('id'))
        if pending and state['status'] == 'running':
            permission = pending['kind'] == 'permission'
            questions = ([inbox.choice('decision','是否允许这次维护操作',[('allow','仅允许这一次'),('deny','不允许')])]
                if permission else [dict(q, name=q.get('name') or q.get('id') or 'q'+str(i)) for i,q in enumerate(pending.get('questions') or [])])
            cards.append(inbox.make('maintenance_question', task, pending['request_id'], title='确认维护中的操作' if permission else '补充维护所需的信息',
                background=pending.get('description') or pending.get('reason'), questions=questions,
                basis=[state['id'], state.get('call_id'), pending], context=dict(context, pending=pending)))
        proposal = summary.get('proposal')
        if state['status'] == 'ready' and proposal and not summary['stale']:
            cards.append(inbox.make('maintenance_proposal', task, proposal['id'], title='确认维护修订', background=proposal.get('summary'),
                questions=[inbox.choice('action','如何处理这份修订',[('apply','应用这份修订'),('decline','保留原有内容')])],
                basis=[state['revision'], state['base_hash'], proposal], context=dict(context, proposal=proposal)))
        adoption = state.get('run_adoption') or {}
        if state['status'] == 'applied' and adoption.get('status') == 'choice_required':
            cards.append(inbox.make('maintenance_adoption', task, state['id'], title='选择新版计划的应用范围', background=state.get('run_notice'),
                questions=[inbox.choice('action','新版计划如何用于运行',[(row['id'],row['label']) for row in adoption.get('choices',[])])],
                basis=[state['revision'], adoption], context=context))
    return summaries, cards


def check_owner(workbench, body):
    project_dir, cid = workbench.scope(body)
    _, _, _, design, record = workbench.select(body)
    view = workbench.view(body)
    if not view or view['task'].get('readonly') or view['task']['mode'] == 'team':
        raise ValueError('当前任务不能发起编排维护。')
    flow_id, run_id = workbench.srv.workbench_maintenance_owner(project_dir, cid)
    expected_flow = ((record or {}).get('locked_flow') or {}).get('flow_id')
    if run_id != view['task'].get('run_id', '') or (expected_flow and expected_flow != flow_id):
        raise ValueError('当前编排归属已变化，请回到任务所属运行。')
    if body.get('id') and body['id'] not in {row['id'] for row in records(workbench.srv, project_dir, cid, view['task'], record)}:
        raise ValueError('维护记录不属于当前任务。')
    return project_dir, cid


async def answer(workbench, request, item, body):
    from frontend.workbench_answers import route
    context, srv = item['context'], workbench.srv
    project_dir, cid = check_owner(workbench, body)
    data = dict(project=project_dir, conversation_id=cid, id=context['id'], revision=context['revision'])
    answers = body.get('answers') or {}
    kind = item['type']
    if kind == 'maintenance_question':
        data.update(request_id=context['pending']['request_id'], answers=answers, decision=answers.get('decision'))
        operation = 'answer'
    elif kind == 'maintenance_proposal':
        data.update(proposal_id=context['proposal_id'], confirmed_run_action_ids=body.get('confirmed_run_action_ids') or [])
        if answers.get('action') == 'decline':
            def decline(state):
                if state['revision'] != context['revision'] or (state.get('proposal') or {}).get('id') != context['proposal_id'] or state['status'] != 'ready':
                    raise ValueError(inbox.STALE)
                state.update(proposal=None)
                state['messages'].append(dict(role='user', text='本次保留原有内容，未应用维护修订。', kind='decision'))
            ledger = getattr(srv, 'MAINTENANCE_STORE', None) or MaintenanceStore()
            srv.MAINTENANCE_STORE = ledger
            ledger.mutate(project_dir, cid, context['id'], decline)
            return dict(ok=True)
        if answers.get('action') != 'apply':
            raise ValueError('请选择是否应用这份修订。')
        operation = 'apply'
    else:
        if answers.get('action') not in {row['value'] for row in item['input_schema'][0]['options']}:
            raise ValueError('请选择当前可用的应用范围。')
        operation = 'choose-adoption'
        data.update(proposal_id=context['proposal_id'], choice=answers['action'])
    return await route(srv, '/api/orchestration/maintenance/'+operation, request, data)


def install(app, workbench):
    @app.post('/api/workbench/maintenance')
    async def operation(request: Request):
        from frontend.workbench_answers import route
        if request.headers.get('x-mew-workbench') != '1' or request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'error':'请在任务工作台操作'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'error':'不接受跨站操作'}, status_code=403)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get('action') not in {'open','send','stop','repair-runtime'}:
                raise ValueError('维护操作无效。')
            project_dir, cid = check_owner(workbench, body)
            data = {key:copy.deepcopy(body[key]) for key in ('id','revision','message','mode','node_ids','request','fresh') if key in body}
            data.update(project=project_dir, conversation_id=cid)
            result = await route(workbench.srv, '/api/orchestration/maintenance/'+body['action'], request, data)
            return result
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return JSONResponse({'error':str(exc)}, status_code=409)
