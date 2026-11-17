"""T84 decision scope, original schemas, stale cards and shared dispatch."""
import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from frontend.prompt_dispatch import dispatch, PromptError, remote_prompt, STALE
from mewcode.agent import AskUserRequest, HumanOnCallRequest, PermissionResponse
from mewcode.orchestration import inbox, task_view
from tests.test_collaboration_mode import RUN_ID, store_for
from tests.test_task_design_entry import task_api_env
from frontend.workbench_answers import answer, internal_request
from fastapi import Request
from unittest.mock import AsyncMock, Mock


def request_for(body):
    return internal_request(Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []}), body)


def host_for(runtime, project, *, srv=server):
    return SimpleNamespace(srv=srv, scope=lambda body: (str(project), runtime.conversation_id))


def runtime_for(cid='conversation', run_id=RUN_ID):
    return SimpleNamespace(conversation_id=cid, expert_run_id=run_id, execution_kind='orchestration',
        ask_user_request=None, human_oncall_request=None, permission_future=None, permission_request={}, pending_file_grants=[])


def questions():
    return [dict(name='choice', question='继续核验吗', options=[dict(value='go', label='继续'), dict(value='wait', label='暂缓')])]


def test_safe_display_retains_original_schema_and_canonical_values():
    original = [dict(name='input', type='list', separator='\n', required=True, question='N12 需要文件',
                     options=[dict(value='N12', label='N12 的输入'), dict(value='blocked', label='`内部代码`')])]
    row = inbox.make('ask_user', dict(id='task', run_id='run'), 'q1', title='N12 确认',
                     background='unknown_effect .mewcode/cache', questions=original, labels={'N12': '核验资料'})
    assert row['title'] == '核验资料 确认'
    assert 'unknown_effect' not in row['background'] and '.mewcode/' not in row['background']
    assert row['input_schema'] == original
    assert row['questions'][0]['options'][0]['value'] == 'N12'
    assert row['questions'][0]['options'][0]['label'] == '核验资料 的输入'
    assert '`' not in row['questions'][0]['options'][1]['label']
    assert row['questions'][0]['type'] == 'list' and row['mandatory'] and row['deadline'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,request_type', [('ask_user', AskUserRequest), ('human_oncall', HumanOnCallRequest)])
async def test_live_and_persisted_questions_deduplicate_and_answer_once(tmp_path, monkeypatch, kind, request_type):
    store = store_for(tmp_path, selected='classic')
    record = store.load(RUN_ID)
    runtime = runtime_for()
    question = request_type('stable-question', kind, questions(), asyncio.get_running_loop().create_future())
    setattr(runtime, kind + '_request', question)
    record['node_states'] = {'work': dict(pending_interaction=dict(kind=kind, request_id=question.request_id, questions=question.questions))}
    task = task_view.from_record(record)['task']
    first = inbox.collect(task, record=record, runtime=runtime)
    assert len(first) == 1 and first[0]['live']
    offline = inbox.collect(task, record=record)
    assert offline[0]['id'] == first[0]['id'] and offline[0]['resume_required']
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    message = dict(conversation_id='conversation', request_id=question.request_id, answers={'choice': 'go'})
    assert (await dispatch(server, kind, message))['ok']
    assert question.future.result() == {'choice': 'go'}
    assert inbox.collect(task, record=record, runtime=runtime) == []
    with pytest.raises(PromptError, match=STALE):
        await dispatch(server, kind, message)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,request_type', [('ask_user', AskUserRequest), ('human_oncall', HumanOnCallRequest)])
async def test_stale_resume_refreshes_live_question_without_answering(tmp_path, monkeypatch, kind, request_type):
    runtime = runtime_for()
    question = request_type('restored-question', kind, questions(), asyncio.get_running_loop().create_future())
    setattr(runtime, kind + '_request', question)
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    row = inbox.make(kind, dict(id='task', run_id=RUN_ID, conversation_id='conversation'), question.request_id,
                     title='请回答问题', questions=questions(), context=dict(request_id=question.request_id))
    body = dict(conversation_id='conversation', action='resume', answers={})
    with pytest.raises(PromptError, match='问题已恢复') as error:
        await answer(host_for(runtime, tmp_path), request_for(body), row, body)
    assert error.value.status == 409
    assert not question.future.done()
    body = dict(conversation_id='conversation', answers={'choice': 'wait'})
    assert (await answer(host_for(runtime, tmp_path), request_for(body), row, body))['ok']
    assert question.future.result() == {'choice': 'wait'}


@pytest.mark.asyncio
async def test_resume_is_still_a_valid_answer_value(tmp_path, monkeypatch):
    runtime = runtime_for()
    fields = [dict(name='action', question='如何继续', options=[dict(value='resume', label='继续')])]
    question = AskUserRequest('normal-choice', 'ask_user', fields, asyncio.get_running_loop().create_future())
    runtime.ask_user_request = question
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    row = inbox.make('ask_user', dict(id='task', run_id=RUN_ID, conversation_id='conversation'), question.request_id,
                     title='请回答问题', questions=fields, context=dict(request_id=question.request_id))
    body = dict(conversation_id='conversation', answers={'action': 'resume'})
    assert (await answer(host_for(runtime, tmp_path), request_for(body), row, body))['ok']
    assert question.future.result() == {'action': 'resume'}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['human_oncall', 'ask_user', 'permission'])
@pytest.mark.parametrize('started', [True, False])
async def test_saved_prompt_resume_updates_task_before_dispatch(task_api_env, monkeypatch, kind, started):
    from mewcode.orchestration.task_design import TaskDesign
    _, _, _, owner, _ = task_api_env
    design = TaskDesign(owner['project'], owner['conversation_id'])
    task = design.start('execute')
    task = design.transition(task, 'paused', '保留原问题', run_id=RUN_ID)
    pending = dict(kind=kind, request_id='saved-prompt', tool=kind, questions=questions())
    record = dict(run_id=RUN_ID, conversation_id=owner['conversation_id'], run_inputs={},
                  locked_flow=dict(meta=dict(task_design=dict(task_id=task['task_id']))),
                  node_states=dict(work=dict(pending_interaction=pending)))
    original = copy.deepcopy(record)
    runtime = runtime_for(owner['conversation_id'], run_id='')
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    monkeypatch.setattr(server.EXPERT_RUNS, 'load', lambda run_id: record)
    legacy = AsyncMock(side_effect=AssertionError('must resume the owning task'))
    monkeypatch.setattr(server, 'launch_expert_resume', legacy)
    spec = SimpleNamespace(as_dict=lambda: dict(nodes=[dict(id='saved')], meta=dict(task_design=dict(intent='execute'))))
    monkeypatch.setattr(server, '_conversation_plan_store', lambda cid: SimpleNamespace(get=lambda project: SimpleNamespace(spec=spec)))
    confirmations = []

    async def confirm(request):
        body = await request.json()
        # The same condition enforced at the worker's node boundary must hold
        # before it restores the original unanswered request.
        current = design.read()
        assert current['status'] == 'ready' and current['run_id'] == RUN_ID
        assert body['task_design_dispatch'] == design.execution_token(current)
        confirmations.append(body)
        return dict(started=started, clarify_prompt='恢复条件尚未满足')

    monkeypatch.setattr(server, 'api_orchestration_confirm', confirm)
    row = inbox.make(kind, dict(id=task['task_id'], run_id=RUN_ID), pending['request_id'],
                     title='继续后回答', questions=questions(), context=dict(node_id='work'), live=False)
    row['resume_required'] = True
    body = dict(owner, action='resume')
    result = await answer(host_for(runtime, owner['project']), request_for(body), row, body)
    assert len(confirmations) == 1 and not legacy.called
    assert record == original  # Resuming supplies no answer or permission.
    if started:
        assert result['resumed'] and result['refresh_required'] and result['answered'] is False
        assert design.read()['status'] == 'running'
    else:
        assert result.status_code == 409
        assert design.read()['status'] == 'paused'


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['task', 'run', 'conversation'])
async def test_saved_prompt_resume_rejects_changed_owner(task_api_env, monkeypatch, changed):
    from mewcode.orchestration.task_design import TaskDesign
    _, _, _, owner, _ = task_api_env
    design = TaskDesign(owner['project'], owner['conversation_id'])
    task = design.start('execute')
    task = design.transition(task, 'paused', '保留原问题', run_id=RUN_ID)
    record = dict(run_id=RUN_ID, conversation_id=owner['conversation_id'],
                  locked_flow=dict(meta=dict(task_design=dict(task_id=task['task_id']))))
    if changed == 'task':
        record['locked_flow']['meta']['task_design']['task_id'] = 'old-task'
    else:
        record['run_id' if changed == 'run' else 'conversation_id'] = 'other'
    runtime = runtime_for(owner['conversation_id'])
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    monkeypatch.setattr(server.EXPERT_RUNS, 'load', lambda run_id: record)
    legacy, confirm = AsyncMock(), AsyncMock()
    monkeypatch.setattr(server, 'launch_expert_resume', legacy)
    monkeypatch.setattr(server, 'api_orchestration_confirm', confirm)
    row = dict(type='human_oncall', task_id=task['task_id'], run_id=RUN_ID,
               context={}, resume_required=True)
    body = dict(owner, action='resume')
    with pytest.raises(PromptError):
        await answer(host_for(runtime, owner['project']), request_for(body), row, body)
    assert design.read() == task
    assert not legacy.called and not confirm.called


@pytest.mark.asyncio
async def test_saved_prompt_without_task_design_keeps_legacy_resume(tmp_path, monkeypatch):
    runtime = runtime_for()
    record = dict(run_id=RUN_ID, conversation_id='conversation')
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    monkeypatch.setattr(server.SESSION, 'chat_busy', lambda cid: False)
    monkeypatch.setattr(server.EXPERT_RUNS, 'load', lambda run_id: record)
    launch = AsyncMock(return_value=dict(status='running'))
    monkeypatch.setattr(server, 'launch_expert_resume', launch)
    body = dict(conversation_id='conversation', action='resume')
    row = dict(type='ask_user', run_id=RUN_ID, context={}, resume_required=True)
    result = await answer(host_for(runtime, tmp_path), request_for(body), row, body)
    launch.assert_awaited_once_with(record)
    assert result == dict(status='running', answered=False, refresh_required=True)


@pytest.mark.asyncio
async def test_permission_id_binds_future_run_and_node_epoch(tmp_path, monkeypatch):
    store = store_for(tmp_path, selected='classic')
    runtime = runtime_for()
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    def new_request():
        runtime.permission_future = asyncio.get_running_loop().create_future()
        runtime.permission_request_future = runtime.permission_future
        runtime.permission_request = inbox.permission_identity(runtime, dict(tool='Bash', description='运行已列出的检查'), run_id=RUN_ID, node_id='work')
        store.mutate(RUN_ID, lambda record: record.setdefault('node_states', {}).setdefault('work', {}).update(pending_permission=copy.deepcopy(runtime.permission_request)), durable=True)
        return runtime.permission_request['request_id']
    old = new_request()
    record = store.load(RUN_ID)
    task = task_view.from_record(record)['task']
    assert len(inbox.collect(task, record=record, runtime=runtime)) == 1
    new = new_request()
    assert new != old
    with pytest.raises(PromptError, match=STALE):
        await dispatch(server, 'permission', dict(conversation_id='conversation', request_id=old, decision='allow'))
    assert not runtime.permission_future.done()
    with pytest.raises(PromptError):
        await dispatch(server, 'permission', dict(conversation_id='conversation', request_id=new))
    assert not runtime.permission_future.done()
    await dispatch(server, 'permission', dict(conversation_id='conversation', request_id=new, decision='deny'))
    assert runtime.permission_future.result() == PermissionResponse.DENY
    new_request()
    runtime.expert_run_id = 'another-run'
    with pytest.raises(PromptError, match=STALE):
        await dispatch(server, 'permission', dict(conversation_id='conversation', request_id=runtime.permission_request['request_id'], decision='allow'))
    runtime.expert_run_id = ''
    assert inbox.collect(task, record={}, runtime=runtime) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,request_type', [('ask_user', AskUserRequest), ('human_oncall', HumanOnCallRequest)])
async def test_remote_frozen_bundle_never_uses_new_options(tmp_path, monkeypatch, kind, request_type):
    runtime = runtime_for(run_id='')
    question = request_type('choice-id', kind, questions(), asyncio.get_running_loop().create_future())
    question.choice_bundle = dict(id='bundle', version='v1', questions=[])
    question.choice_display_signature = 'state1'
    question.choice_state_signature = lambda: 'state1'
    setattr(runtime, kind + '_request', question)
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    shown = remote_prompt(runtime, dict(type=kind, request_id='choice-id', questions=question.questions))
    question.choice_bundle['version'] = 'v2'
    with pytest.raises(PromptError, match=STALE):
        await dispatch(server, kind, dict(conversation_id='conversation', request_id=shown['request_id'], answers={'choice': 'go'}), transport='remote')
    assert not question.future.done()
    with pytest.raises(PromptError, match=STALE):
        await dispatch(server, kind, dict(conversation_id='conversation', request_id=question.request_id, choice_bundle_id='bundle', choice_version='v1', answers={'choice': 'go'}))
    question.choice_state_signature = lambda: 'state2'
    with pytest.raises(PromptError, match=STALE):
        await dispatch(server, kind, dict(conversation_id='conversation', request_id=question.request_id, choice_bundle_id='bundle', choice_version='v2', answers={'choice': 'go'}))


@pytest.mark.asyncio
async def test_api_ordinary_question_survives_and_task_card_expires(task_api_env, tmp_path, monkeypatch):
    _, _, _, owner, _ = task_api_env
    store = store_for(tmp_path / 'run', selected='classic')
    store.mutate(RUN_ID, lambda row: row.update(conversation_id=owner['conversation_id']), durable=True)
    runtime = runtime_for(owner['conversation_id'])
    runtime.ask_user_request = AskUserRequest('q1', 'AskUserQuestion', [dict(name='path', type='text', question='填写文件路径')], asyncio.get_running_loop().create_future())
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime if cid == owner['conversation_id'] else None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        params = dict(owner, run_id=RUN_ID)
        view = (await client.get('/api/workbench/view', params=params)).json()
        card = view['inbox'][0]
        assert card['questions'][0]['type'] == 'text' and 'options' not in card['questions'][0]
        response = await client.post('/api/workbench/inbox/' + card['id'] + '/answer', headers={'X-Mew-Workbench': '1'}, json=dict(params, answers={'path': 'docs/input.csv'}))
        assert response.status_code == 200, response.text
        again = await client.post('/api/workbench/inbox/' + card['id'] + '/answer', headers={'X-Mew-Workbench': '1'}, json=dict(params, answers={'path': 'other'}))
        assert again.status_code == 409 and again.json()['error'] == STALE
        missing = await client.post('/api/workbench/inbox/unknown/answer', headers={'X-Mew-Workbench': '1'}, json=params)
        assert missing.status_code == 404 and missing.json()['error'] == inbox.MISSING
    runtime.expert_run_id = ''
    runtime.ask_user_request = AskUserRequest('ordinary', 'AskUserQuestion', questions(), asyncio.get_running_loop().create_future())
    assert (await dispatch(server, 'ask_user', dict(conversation_id=owner['conversation_id'], request_id='ordinary', answers={'choice': 'go'})))['ok']


@pytest.mark.asyncio
async def test_permission_epoch_future_and_cross_conversation_are_fenced(tmp_path, monkeypatch):
    store = store_for(tmp_path, selected='classic')
    runtime = runtime_for()
    future = asyncio.get_running_loop().create_future()
    runtime.permission_future = runtime.permission_request_future = future
    runtime.permission_request = inbox.permission_identity(runtime, {}, run_id=RUN_ID, node_id='work', epoch=1)
    store.mutate(RUN_ID, lambda row: row.setdefault('node_states', {}).setdefault('work', {}).update(
        epoch=2, pending_permission=runtime.permission_request), durable=True)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    message = dict(conversation_id='conversation', request_id=runtime.permission_request['request_id'], decision='allow')
    for patch in ({}, {'conversation_id': 'another'}):
        with pytest.raises(PromptError, match=STALE):
            await dispatch(server, 'permission', dict(message, **patch), runtime=runtime)
    runtime.permission_request_future = asyncio.get_running_loop().create_future()
    with pytest.raises(PromptError, match=STALE):
        await dispatch(server, 'permission', message, runtime=runtime)
    assert not future.done()


@pytest.mark.asyncio
async def test_question_old_task_binding_rejected(monkeypatch):
    runtime = runtime_for()
    request = AskUserRequest('q', 'AskUserQuestion', questions(), asyncio.get_running_loop().create_future())
    request.task_design_binding, request.task_design_owner = {'task_id': 'old'}, ('project', 'conversation')
    runtime.ask_user_request = request
    monkeypatch.setattr('mewcode.orchestration.task_design.validate_question_binding', Mock(side_effect=ValueError('old task')))
    with pytest.raises(PromptError, match=STALE):
        await dispatch(server, 'ask_user', dict(conversation_id='conversation', request_id='q', answers={'choice': 'go'}), runtime=runtime)
    assert not request.future.done()


@pytest.mark.asyncio
async def test_plan_summary_reject_and_new_generation(tmp_path, monkeypatch):
    from mewcode.experts.recovery_planner import resolve_stall
    from tests.test_recovery_planner import _node
    store = store_for(tmp_path, selected='classic')
    def proposal():
        state = {'blocker': 'missing_evidence', 'rubric': {'criteria': [{'criterion_id': 'REQ-01', 'severity': 'major', 'acceptance_test': '结果可核验'}]}}
        return resolve_stall(state, node=_node())['proposal']
    first, renewed = proposal(), proposal()
    assert first['request_id'] != renewed['request_id']
    store.mutate(RUN_ID, lambda row: row.update(pending_recovery_revision=first), durable=True)
    runtime = runtime_for()
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server, '_active_conversation_id', lambda: 'conversation')
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    record = store.load(RUN_ID)
    item = next(row for row in inbox.collect(task_view.from_record(record)['task'], record=record) if row['type'] == 'plan_revision')
    body = dict(conversation_id='conversation', action='reject')
    assert (await answer(host_for(runtime, tmp_path), request_for(body), item, body))['ok']
    assert not store.load(RUN_ID).get('pending_recovery_revision')
    store.mutate(RUN_ID, lambda row: row.update(pending_recovery_revision=renewed), durable=True)
    with pytest.raises(PromptError, match=STALE):
        await answer(host_for(runtime, tmp_path), request_for(body), item, body)


@pytest.mark.asyncio
async def test_file_grant_and_lesson_forward_explicit_decisions(tmp_path, monkeypatch):
    runtime = runtime_for()
    runtime.pending_file_grants = [dict(path='docs/data.csv', access='read', request_id='offer')]
    task = dict(id='task', conversation_id='conversation', run_id=RUN_ID)
    lessons = [dict(flow_id='flow', lesson_id='lesson', run_id=RUN_ID, status='pending', text='核对来源后再汇总')]
    cards = inbox.collect(task, runtime=runtime, lessons=lessons)
    grant, lesson = [next(row for row in cards if row['type'] == kind) for kind in ('file_grant', 'lesson')]
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    grant_endpoint = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(server, 'api_orchestration_file_grant', grant_endpoint)
    update = Mock(return_value=dict(status='active'))
    monkeypatch.setattr(server.EXPERT_LESSONS, 'update', update)
    host = host_for(runtime, tmp_path)
    body = dict(conversation_id='conversation', action='grant', path='untrusted', access='read_write')
    await answer(host, request_for(body), grant, body)
    forwarded = grant_endpoint.call_args.args[0]
    assert forwarded.headers['x-mew-file-grant'] == '1'
    assert (await forwarded.json())['path'] == 'docs/data.csv'
    assert (await forwarded.json())['access'] == 'read'
    await answer(host, request_for(body), lesson, dict(body, action='accept'))
    update.assert_called_once_with('flow', 'lesson', status='active')
    await answer(host, request_for(body), grant, dict(body, action='decline'))
    assert runtime.pending_file_grants == []


@pytest.mark.asyncio
async def test_collaboration_recovery_uses_original_host_gate(task_api_env, tmp_path, monkeypatch):
    from mewcode.orchestration.task_design import TaskDesign
    from tests.test_collaboration_recovery import exhausted_gate
    _, _, _, owner, _ = task_api_env
    store = store_for(tmp_path / 'run')
    gate = exhausted_gate(store, tmp_path)
    design = TaskDesign(owner['project'], owner['conversation_id'])
    task = design.start('execute', orchestration_mode='collaborative')
    design.transition(task, 'blocked', run_id=RUN_ID)
    store.mutate(RUN_ID, lambda row: (row.update(conversation_id=owner['conversation_id']), row['collaboration'].update(task_id=task['task_id'])), durable=True)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        params = dict(owner, run_id=RUN_ID)
        card = next(row for row in (await client.get('/api/workbench/view', params=params)).json()['inbox'] if row['type'] == 'collaboration_recovery')
        assert card['context']['id'] == gate['id'] and card['mandatory']
        response = await client.post('/api/workbench/inbox/' + card['id'] + '/answer', headers={'X-Mew-Workbench': '1'}, json=dict(params, action='retry', reason='明确再试一次'))
        assert response.status_code == 200, response.text
        assert store.load(RUN_ID)['collaboration']['recovery_gates'][gate['id']]['status'] != 'waiting'


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['add_budget', 'enable_elastic_budget'])
async def test_budget_gate_uses_exact_amount_and_rejects_stale_workbench_card(task_api_env, tmp_path, monkeypatch, action):
    from mewcode.orchestration.task_design import TaskDesign
    from mewcode.kernel import lineage_budget
    from mewcode.kernel.collaboration_recovery import open_gate
    _, _, _, owner, _ = task_api_env
    store = store_for(tmp_path / 'run')
    design = TaskDesign(owner['project'], owner['conversation_id'])
    task = design.start('execute', orchestration_mode='collaborative')
    design.transition(task, 'blocked', run_id=RUN_ID)
    def exhaust(record):
        value = record['collaboration']
        value['policy']['lineage_budget_mode'] = 'fixed'
        value['task_id'] = task['task_id']
        lineage_budget.reserve(value, 'spent', kind='attempts', amount=9, lineage_id='work')
        lineage_budget.settle(value, 'spent')
        open_gate(record, scope='contract:work', keys=['work'], label='执行额度已用尽',
            reason='当前分支已用9次', kind='lineage_budget_exhausted',
            budget=dict(kind='attempts', scope='lineage', lineage_id='work', requested=1, available=0))
        record.update(conversation_id=owner['conversation_id'], status='paused', active_node_ids=[])
    store.mutate(RUN_ID, exhaust, durable=True)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        params = dict(owner, run_id=RUN_ID)
        card = next(row for row in (await client.get('/api/workbench/view', params=params)).json()['inbox']
                    if row['type'] == 'collaboration_recovery')
        assert [row['value'] for row in card['input_schema'][0]['options']] == ['enable_elastic_budget', 'add_budget', 'revise', 'inspect']
        url = '/api/workbench/inbox/' + card['id'] + '/answer'
        before = copy.deepcopy(store.load(RUN_ID)['collaboration']['lineage_budget'])
        bad = await client.post(url, headers={'X-Mew-Workbench': '1'},
            json=dict(params, action='add_budget', reason='补充额度', budget_amount=True))
        assert bad.status_code == 409
        assert store.load(RUN_ID)['collaboration']['lineage_budget'] == before
        body = dict(params, action=action, reason='确认使用选定的额度策略')
        if action == 'add_budget':
            body['budget_amount'] = 3
        response = await client.post(url, headers={'X-Mew-Workbench': '1'}, json=body)
        assert response.status_code == 200, response.text
        budget = store.load(RUN_ID)['collaboration']['lineage_budget']
        assert budget['lineages']['work']['used']['attempts'] == 9
        if action == 'add_budget':
            assert list(budget['grants'].values())[0]['amount'] == 3
            assert list(budget['grants'].values())[0]['lineage_id'] == 'work'
        else:
            assert budget == before
            assert store.load(RUN_ID)['collaboration']['policy']['lineage_budget_mode'] == 'elastic'
        stale = await client.post(url, headers={'X-Mew-Workbench': '1'}, json=body)
        assert stale.status_code == 409
        assert store.load(RUN_ID)['collaboration']['lineage_budget'] == budget


@pytest.mark.asyncio
async def test_coordinator_recovery_card_rebinds_same_route_once(task_api_env, tmp_path, monkeypatch):
    from mewcode.orchestration.task_design import TaskDesign
    from mewcode.kernel.collaboration_recovery import open_gate
    from tests.test_collaboration_mode import CATALOG, EXECUTOR
    _, _, _, owner, _ = task_api_env
    store = store_for(tmp_path / 'run')
    design = TaskDesign(owner['project'], owner['conversation_id'])
    task = design.start('execute', orchestration_mode='collaborative')
    design.transition(task, 'blocked', run_id=RUN_ID)
    def prepare(record):
        value = record['collaboration']
        value.update(task_id=task['task_id'], coordinator=EXECUTOR.as_dict(),
                     coordinator_lease={'id': 'lost', 'epoch': value['coordinator_epoch'], 'status': 'active'})
        open_gate(record, scope='contract:work', keys=['work'], label='协调服务待恢复',
                  reason='调用结果尚未确认', kind='coordinator_unavailable')
        record.update(conversation_id=owner['conversation_id'], status='paused', active_node_ids=[])
    store.mutate(RUN_ID, prepare, durable=True)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server.MODEL_REGISTRY, 'catalog', AsyncMock(return_value=CATALOG))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        params = dict(owner, run_id=RUN_ID)
        card = next(row for row in (await client.get('/api/workbench/view', params=params)).json()['inbox']
                    if row['type'] == 'collaboration_recovery')
        assert [row['value'] for row in card['input_schema'][0]['options']] == ['resume_coordinator', 'switch_coordinator', 'revise']
        original = copy.deepcopy(store.load(RUN_ID)['collaboration'])
        url = '/api/workbench/inbox/' + card['id'] + '/answer'
        body = dict(params, action='resume_coordinator', reason='确认仍使用原协调服务')
        response = await client.post(url, headers={'X-Mew-Workbench': '1'}, json=body)
        assert response.status_code == 200, response.text
        value = store.load(RUN_ID)['collaboration']
        assert value['coordinator_epoch'] == original['coordinator_epoch'] + 1
        assert value['coordinator'] == EXECUTOR.as_dict()
        assert value['lineage_budget'] == original['lineage_budget']
        assert (await client.post(url, headers={'X-Mew-Workbench': '1'}, json=body)).status_code == 409
        assert store.load(RUN_ID)['collaboration'] == value


@pytest.mark.asyncio
async def test_recovery_case_summary_and_answer_preserve_typed_values(tmp_path, monkeypatch):
    from mewcode.experts.recovery_case import open_case, apply_event
    from mewcode.experts.case_runtime import answer_case
    store = store_for(tmp_path, selected='classic')
    record = store.load(RUN_ID)
    case = open_case(record, 'work', error='缺少范围')
    pending = dict(kind='input', options=[dict(value='input', label='补充范围')],
                   input_definitions=[dict(name='scope', type='text', required=True, question='填写处理范围', collect_at='work')])
    record['locked_flow'].setdefault('meta', {})['run_input_contract'] = copy.deepcopy(pending['input_definitions'])
    case = apply_event(record, case['case_id'], dict(kind='wait', identity='input-wait', pending=pending))
    card = next(row for row in inbox.collect(task_view.from_record(record)['task'], record=record) if row['type'] == 'recovery_case')
    assert card['input_schema'][0] == pending['input_definitions'][0]
    runtime = runtime_for()
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    async def original(body):
        return answer_case(record, body.case_id, round=body.round, choice=body.choice, answer=body.answer,
                           bundle_id=body.choice_bundle_id, version=body.choice_version, choice_inputs=body.choice_inputs)
    monkeypatch.setattr(server, 'api_recovery_case_answer', original)
    body = dict(conversation_id='conversation', answers={'scope': '仅检查指定表格'})
    result = await answer(host_for(runtime, tmp_path), request_for(body), card, body)
    assert not result['pending']
    assert not any(row['type'] == 'recovery_case' for row in inbox.collect(task_view.from_record(record)['task'], record=record))


@pytest.mark.asyncio
async def test_recovery_context_and_manual_choice_keep_original_protocol(tmp_path, monkeypatch):
    from mewcode.experts.recovery_case import open_case, apply_event, save_case
    store = store_for(tmp_path, selected='classic')
    record = store.load(RUN_ID)
    case = open_case(record, 'work', error='缺少目录')
    case = apply_event(record, case['case_id'], dict(kind='wait', identity='decision', pending=dict(
        kind='incident', options=[dict(value='manual', label='自己说明', action='manual', requires_input=True)])))
    case['handling'] = dict(owner='user', handling_state='needs_user', reason='需要目录来源')
    case['report'] = dict(diagnosis=dict(symptom=dict(summary='目录未交接', evidence_id='E-17')))
    save_case(record, case)
    task = task_view.from_record(record)['task']
    card = next(row for row in inbox.collect(task, record=record) if row['type'] == 'recovery_case')
    assert card['background'] == '需要目录来源'
    assert card['context']['report']['diagnosis']['symptom']['evidence_id'] == 'E-17'
    assert card['questions'][0]['preserve_choice_value']
    runtime = runtime_for()
    original = AsyncMock(return_value=dict(ok=True))
    monkeypatch.setattr(server, 'api_recovery_case_answer', original)
    body = dict(conversation_id='conversation', answers={'choice':'manual'}, choice_inputs={'manual':'核对已有清单'}, notes={'choice':'保留当前检查点'})
    await answer(host_for(runtime, tmp_path), request_for(body), card, body)
    sent = original.call_args.args[0]
    assert (sent.choice, sent.answer, sent.note) == ('manual','核对已有清单','保留当前检查点')
    case['handling'].update(owner='system', handling_state='blocked')
    save_case(record, case)
    assert not any(row['type'] == 'recovery_case' for row in inbox.collect(task, record=record))


@pytest.mark.asyncio
async def test_run_parameters_resolve_explicit_bundle_without_starting(tmp_path, monkeypatch):
    from frontend import interaction_choices as choices_api
    from tests.test_interaction_choices_api import FakeSrv, _spec
    from mewcode.orchestration.interaction_choices import prepare_choices
    srv = FakeSrv(tmp_path)
    spec = _spec()
    spec['meta']['task_design'] = {'task_id': 'task'}
    spec = choices_api._spec_dict(choices_api._owned_spec(srv, spec, 'chat'))
    definitions = spec['goal_contract']['run_inputs']
    bundle = await prepare_choices(questions=definitions, context={}, allowed_roots=[str(tmp_path)], complete=AsyncMock(side_effect=ValueError('offline')), identity={'id': 'startup'})
    choices_api._cache_startup(srv, 'chat', spec, bundle, definitions)
    srv.SESSION.runtime_for = lambda cid: srv.runtime
    srv.runtime.conversation_id = 'chat'
    cached = next(iter(srv._interaction_choice_state['startup'].values()))
    card = inbox.collect(dict(id='task', conversation_id='chat', run_id=''), startup=[cached])[0]
    host = host_for(srv.runtime, tmp_path, srv=srv)
    host.startup_choices = lambda task, cid: [cached]
    body = dict(conversation_id='chat', answers={'sources': {'custom': ['explicit.csv']}})
    result = await answer(host, request_for(body), card, body)
    assert result['execution_started'] is False and srv.runtime.orchestration_run_inputs == {'sources': ['explicit.csv']}
    assert inbox.collect(dict(id='task', run_id=''), startup=[cached]) == []


@pytest.mark.asyncio
async def test_team_intervention_and_plan_use_original_commands(tmp_path, monkeypatch):
    runtime = runtime_for(run_id='tr-fixture')
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    command = Mock(return_value={'ok': True})
    host = host_for(runtime, tmp_path)
    host.team_host = lambda body, project: SimpleNamespace(command=command)
    task = dict(id='team:fixture', run_id='tr-fixture', conversation_id='conversation')
    for kind, identity in [('team_intervention', 'intervention_id'), ('team_plan', 'proposal_id')]:
        cards = inbox.collect(task, team_items=[dict(type=kind, source_id='source', title='确认变更', source={identity: 'source'}, basis={'epoch': 2})])
        body = dict(conversation_id='conversation', action='decline')
        await answer(host, request_for(body), cards[0], body)
        run, operation, args = command.call_args.args
        assert run == task['run_id'] and operation == ('dismiss_intervention' if kind == 'team_intervention' else 'dismiss_plan')
        assert args[identity] == 'source' and args['control_epoch'] == 2


def test_revision_draft_and_mandatory_gate_projection():
    task = dict(id='task', run_id='run')
    record = dict(workbench_revision_drafts={'draft': dict(id='draft', status='pending', digest='digest', base_version=1, summary='改用指定来源')})
    card = inbox.collect(task, record=record)[0]
    assert card['type'] == 'revision_draft' and card['context']['digest'] == 'digest'
    mandatory = inbox.make('ask_user', task, 'must-answer', title='确认', questions=[dict(requires_user_answer=True)],
                          gate=dict(reversible=True, kind='business_choice', deadline=999))
    assert mandatory['mandatory'] and mandatory['deadline'] is None


@pytest.mark.asyncio
async def test_reviewed_permission_producer_persists_identity_before_emit(tmp_path):
    from mewcode.agent import PermissionRequest
    from mewcode.experts.reviewed_tools import invoke_reviewed_tool
    from mewcode.tools.base import ToolResult
    store = store_for(tmp_path, selected='classic')
    runtime = runtime_for()
    received = []
    async def execute(call):
        pending = PermissionRequest(tool_name='Bash', description='检查', tool_id=call.tool_id,
                                    future=asyncio.get_running_loop().create_future())
        yield pending
        received.append(await pending.future)
        yield (ToolResult('用户拒绝，未执行', is_error=True), 0, None)
    async def emit(event):
        if event['type'] == 'permission':
            runtime.permission_future = runtime._perm_future
            persisted = store.load(RUN_ID)['node_states']['work']['pending_permission']
            assert persisted['request_id'] == event['request_id']
            await dispatch(SimpleNamespace(EXPERT_RUNS=store), 'permission',
                           dict(conversation_id='conversation', request_id=event['request_id'], decision='deny'), runtime=runtime)
    await invoke_reviewed_tool(SimpleNamespace(_execute_tool=execute), 'Bash', {}, session=runtime,
                               store=store, run_id=RUN_ID, node_id='work', emit=emit)
    assert received == [PermissionResponse.DENY]
    assert 'pending_permission' not in store.load(RUN_ID)['node_states']['work']
