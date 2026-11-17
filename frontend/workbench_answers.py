"""Scoped workbench answers forwarded to the existing host operations."""
from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

from frontend.prompt_dispatch import dispatch, PromptError, revision_reply_id
from mewcode.orchestration import inbox


def internal_request(request, body, *, headers=None):
    """Call a known host route in-process with its original typed body guards."""
    scope = dict(request.scope)
    additions = {key.lower().encode(): value.encode() for key, value in (headers or {}).items()}
    scope['headers'] = [(key, value) for key, value in scope.get('headers', []) if key not in additions] + list(additions.items())
    value = Request(scope)
    value._json = body
    return value


async def route(srv, path, request, body, headers=None):
    endpoint = next(row.endpoint for row in srv.app.routes if getattr(row, 'path', None) == path and 'POST' in getattr(row, 'methods', set()))
    return await endpoint(internal_request(request, body, headers=headers))


async def answer(workbench, request, item, body):
    srv, context = workbench.srv, item['context']
    cid, run_id = body['conversation_id'], item['run_id']
    project, _ = workbench.scope(body)
    common = dict(conversation_id=cid, project=project, run_id=run_id)
    runtime = srv.SESSION.runtime_for(cid)
    answers = body.get('answers') or {}
    action = body.get('action') or answers.get('action') or answers.get('decision') or ''
    if item.get('resume_required'):
        if action != 'resume':
            raise PromptError('任务已暂停，请先继续任务并核对当前问题。', 409)
        if srv.SESSION.chat_busy(cid):
            raise PromptError()
        record = srv.EXPERT_RUNS.load(run_id)
        if not record or record.get('run_id') != run_id or record.get('conversation_id') != cid:
            raise PromptError()
        binding = (record.get('locked_flow', {}).get('meta') or {}).get('task_design') or {}
        if binding.get('task_id'):
            from mewcode.orchestration.task_design import TaskDesign
            current = TaskDesign(project, cid).read() or {}
            if (current.get('task_id') != binding['task_id'] or current.get('task_id') != item['task_id']
                    or current.get('run_id') != run_id):
                raise PromptError()
            # Restore the owning task before dispatch. The legacy launcher
            # leaves it paused, so the worker's control guard cancels before
            # it can restore the saved question or permission request.
            result = await route(srv, '/api/orchestration/task-design', request, dict(common,
                action='resume', expected_task_id=current['task_id'], expected_revision=current['revision']))
            if isinstance(result, JSONResponse):
                return result
            return dict(result, answered=False, refresh_required=True)
        return dict(await srv.launch_expert_resume(record), answered=False, refresh_required=True)
    kind = item['type']
    if body.get('action') == 'resume' and kind in {'permission', 'human_oncall', 'ask_user'}:
        # A persisted question may have become live since the card was shown.
        # Refresh it; a request to resume is never an answer to that question.
        raise PromptError('问题已恢复，请根据当前显示的选项作答。', 409)
    if kind == 'run_action':
        from frontend.workbench_controls import answer as run_action_answer
        return await run_action_answer(workbench, item, body)
    if kind.startswith('maintenance_'):
        from frontend.workbench_maintenance import answer as maintenance_answer
        return await maintenance_answer(workbench, request, item, body)
    if kind == 'design_choice':
        name = context['question_id']
        if set(answers) != {name}:
            raise PromptError('请选择当前设计问题的方案', 400)
        return await route(srv, '/api/orchestration/graph-autobuild/discussion', request, dict(
            common, action='choose', candidate_id=context['candidate_id'], sequence=context['sequence'],
            selections=[dict(question_id=name, value=answers[name])], notes=body.get('notes') or {},
            choice_bundle_id=context['choice_bundle_id'], choice_version=context['choice_version']))
    if kind in {'permission', 'human_oncall', 'ask_user', 'plan_revision'}:
        message = dict(common, answers=answers, notes=body.get('notes'), cancelled=body.get('cancelled') is True,
            decision=action, recovery_action=action, recovery_info=body.get('recovery_info', ''),
            request_id=context.get('request_id'), choice_bundle_id=context.get('choice_bundle_id', ''),
            choice_version=context.get('choice_version', ''), choice_inputs=body.get('choice_inputs') or {},
            grant_extra_budget=body.get('grant_extra_budget') is True, sync_canvas=body.get('sync_canvas') is True)
        if kind == 'plan_revision':
            message['request_id'] = revision_reply_id(cid, run_id, context['proposal'])
        # Explicit client metadata may never be replaced by the current one.
        for key in ('request_id', 'choice_bundle_id', 'choice_version'):
            if key in body and body[key] != message.get(key):
                raise PromptError()
        return await dispatch(srv, kind, message, transport='workbench', runtime=runtime)
    if kind == 'file_grant':
        if action == 'decline':
            runtime.pending_file_grants = [row for row in runtime.pending_file_grants if row != context]
            return dict(ok=True, granted=False)
        if action != 'grant':
            raise PromptError('请选择是否授权', 400)
        return await srv.api_orchestration_file_grant(internal_request(request, dict(common, path=context['path'], access=context['access']),
                                                                 headers={'X-Mew-File-Grant': '1'}))
    if kind == 'collaboration_recovery':
        allowed = {row['value'] for row in item['input_schema'][0]['options']}
        if action not in allowed:
            raise PromptError('请选择当前恢复方式', 400)
        if action == 'inspect':
            return dict(ok=True, answered=False, inspect_node_ids=context['node_ids'])
        return await route(srv, '/api/orchestration/task-design/recovery', request, dict(common,
            expected_task_id=item['task_id'], gate_id=context['id'], expected_version=context['spec_version'], request_id=uuid.uuid4().hex,
            action=action, reason=body.get('reason') or answers.get('reason', ''), model=body.get('model'),
            budget_amount=body.get('budget_amount')))
    if kind == 'recovery_case':
        pending = context['pending']
        bundle = pending.get('choice_bundle') or pending.get('prepared_choice_bundle') or pending.get('bundle') or {}
        choice = body.get('choice') or answers.get('choice') or action
        text = body.get('answer', '')
        if pending.get('kind') == 'input' and answers:
            choice = choice or 'input'
            text = json.dumps({key: value for key, value in answers.items() if key not in {'choice', 'action'}}, ensure_ascii=False)
        elif bundle and not choice:
            choice = answers
        elif not bundle and not text and isinstance(choice, (str, int, float, bool)):
            value = (body.get('choice_inputs') or {}).get(str(choice), '')
            text = '\n'.join(value) if isinstance(value, list) else str(value)
        for key, expected in (('choice_bundle_id', bundle.get('id', '')), ('choice_version', bundle.get('version', ''))):
            if key in body and body[key] != expected:
                raise PromptError()
        return await srv.api_recovery_case_answer(srv.RecoveryCaseAnswerBody(**common, case_id=context['case_id'],
            round=len(context['rounds']), choice=choice, answer=text,
            note=body.get('note') or '\n'.join(str(value) for value in (body.get('notes') or {}).values() if value),
            remember_scope=body.get('remember_scope', 'run'), choice_bundle_id=bundle.get('id', ''),
            choice_version=bundle.get('version', ''), choice_inputs=body.get('choice_inputs') or {}))
    if kind == 'lesson':
        if action not in {'accept', 'decline'}:
            raise PromptError('请选择采用或不采用', 400)
        lesson = srv.EXPERT_LESSONS.update(context['flow_id'], context['lesson_id'], status='active' if action == 'accept' else 'disabled')
        return dict(ok=True, lesson=lesson)
    if kind == 'run_inputs':
        if context.get('spec_hash'):
            from frontend.workbench_startup import save
            return save(srv, project, cid, item['task_id'], context, answers)
        from frontend.interaction_choices import lookup_startup_bundle
        from mewcode.orchestration.interaction_choices import resolve_answers
        from mewcode.orchestration.run_inputs import resolve_run_inputs
        cached = next((row for row in workbench.startup_choices(dict(id=item['task_id']), cid) if row['bundle']['id'] == context['choice_bundle_id']), None)
        if not cached:
            raise PromptError()
        bundle = lookup_startup_bundle(srv, cid, context['choice_bundle_id'], context['choice_version'], cached['spec'])
        resolved = resolve_answers(bundle, answers, bundle_id=bundle['id'], version=bundle['version'], choice_inputs=body.get('choice_inputs') or {})
        if resolved['actions']:
            raise PromptError('请先准备或补充选项，运行参数尚未提交', 400)
        definitions = (cached['spec'].get('goal_contract') or {}).get('run_inputs') or []
        normalized = resolve_run_inputs(definitions, resolved['values'])
        if runtime is None:
            raise PromptError('当前对话没有可保存参数的运行环境', 409)
        runtime.orchestration_run_inputs = normalized
        cached['answered_version'] = bundle['version']
        return dict(ok=True, run_inputs=normalized, execution_started=False)
    if kind in {'team_intervention', 'team_plan'}:
        host = workbench.team_host(body, project)
        if host is None:
            raise PromptError()
        source, basis = context['source'], context['basis']
        args = dict(control_epoch=basis.get('epoch'), command_id=uuid.uuid4().hex)
        reason = body.get('reason') or answers.get('reason') or '用户在任务工作台作出明确选择'
        if kind == 'team_plan':
            command = 'apply_plan' if action == 'accept' else 'dismiss_plan'
            args['proposal_id'] = source['proposal_id']
        else:
            args['intervention_id'] = source['intervention_id']
            if source.get('state') == 'needs_clarification':
                proposed = body.get('request') or answers.get('request')
                if not isinstance(proposed, dict):
                    raise PromptError('请明确修改意图和范围', 400)
                return host.command(run_id, 'clarify_intervention', dict(args, request=proposed, explanation=reason))
            command = 'apply_intervention' if action == 'accept' else 'dismiss_intervention'
        if action == 'accept':
            args['approved'] = True
        elif action == 'decline':
            args['reason'] = reason
        else:
            raise PromptError('请选择确认或不采用', 400)
        return host.command(run_id, command, args)
    if kind == 'revision_draft':
        from frontend.workbench_revision import answer_draft
        return await answer_draft(workbench, item, body)
    raise PromptError(inbox.MISSING, 404)


def install_answers(app, workbench):
    locks = {}

    @app.post('/api/workbench/inbox/{item_id}/prepare')
    async def prepare_item(item_id: str, request: Request):
        if request.headers.get('x-mew-workbench') != '1':
            return JSONResponse({'error': '请在任务工作台明确选择'}, status_code=403)
        origin = request.headers.get('origin')
        if (origin and origin != str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'error': '不接受跨站作答'}, status_code=403)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise PromptError('请求必须是对象', 400)
            workbench.scope(body)
            view = workbench.view(body)
            item = next((row for row in (view or {}).get('inbox', []) if row['id'] == item_id), None)
            if not item or item['type'] not in {'ask_user', 'human_oncall', 'run_inputs', 'recovery_case', 'design_choice'}:
                raise PromptError(inbox.STALE)
            actions = {'research': '研究或补充材料', 'revise': '修改当前问题的约束', 'configure': '补充配置说明', 'retry_prepare': '重新查找候选方案'}
            available = {choice.get('action') for question in (item.get('choice_bundle') or {}).get('questions', []) for choice in question.get('choices', []) if choice.get('kind') == 'action'}
            if body.get('action') not in actions or (body['action'] not in available and not (item['type'] == 'design_choice' and item.get('prepare_choices'))) or item.get('resume_required'):
                raise PromptError('请选择当前可用的准备动作', 400)
            context = item['context']
            values = dict(conversation_id=body['conversation_id'], run_id=item['run_id'],
                          request_id=context.get('request_id', ''), node_id=context.get('node_id', ''),
                          refresh=True, instruction=actions[body['action']] + '，然后准备新的候选选项。',
                          action_input='\n'.join(body['input']) if isinstance(body.get('input'), list) else str(body.get('input') or ''))
            if item['type'] == 'run_inputs':
                cached = next(row for row in workbench.startup_choices(dict(id=item['task_id']), body['conversation_id']) if row['bundle']['id'] == context['choice_bundle_id'])
                values.update(spec=cached['spec'], request_id='startup', run_id='')
            if item['type'] == 'recovery_case':
                values.update(case_id=context['case_id'], request_id=context['pending'].get('request_id') or context['case_id'])
            if item['type'] == 'design_choice':
                values.update(candidate_id=context['candidate_id'], sequence=context['sequence'], request_id='graph-choices')
            result = await route(workbench.srv, '/api/orchestration/interaction-choices/prepare', request, values)
            from mewcode.orchestration.workbench_events import changed
            changed('runtime', conversation_id=body['conversation_id'], run_id=item['run_id'])
            return result
        except (PromptError, ValueError, TypeError, KeyError, StopIteration) as exc:
            return JSONResponse({'error': str(exc)}, status_code=getattr(exc, 'status', 409))

    @app.post('/api/workbench/inbox/{item_id}/answer')
    async def answer_item(item_id: str, request: Request):
        if request.headers.get('x-mew-workbench') != '1':
            return JSONResponse({'error': '请在任务工作台明确作答'}, status_code=403)
        origin = request.headers.get('origin')
        if (origin and origin != str(request.base_url).rstrip('/')) or request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'error': '不接受跨站作答'}, status_code=403)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise PromptError('作答必须是对象', 400)
            authorize = getattr(app.state, 'team_v2_authorize', None)
            body['_team_authorized'] = bool(authorize and authorize(request))
            workbench.scope(body)
            lock = locks.setdefault((body['conversation_id'], body.get('task_id', ''), body.get('run_id', '')), asyncio.Lock())
            async with lock:
                view = workbench.view(body)
                item = next((row for row in (view or {}).get('inbox', []) if row['id'] == item_id), None)
                if item is None:
                    raise PromptError(inbox.STALE if inbox.known_id(item_id) else inbox.MISSING, 409 if inbox.known_id(item_id) else 404)
                result = await answer(workbench, request, item, body)
                from mewcode.orchestration.workbench_events import changed
                changed('runtime', conversation_id=body['conversation_id'], run_id=item['run_id'])
                return result if isinstance(result, JSONResponse) else JSONResponse(result)
        except PromptError as exc:
            return JSONResponse({'error': str(exc)}, status_code=exc.status)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
