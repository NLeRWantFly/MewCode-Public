"""T86 actual ledger transactions with a synthetic coordinator boundary."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from frontend.workbench_revision import RevisionDrafts, STALE_REVISION, NOT_REVISION, draft_digest, sources, sync_tool
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.events import digest
from mewcode.orchestration.collab_coordinator import CollaborationCoordinator
from mewcode.orchestration.task_design import TaskDesign
from mewcode.tools.revision_draft import SubmitRevisionDraft, RevisionDraftParams
from tests.test_collaboration_mode import RUN_ID, CATALOG, store_for, accept
from tests.test_task_design_entry import task_api_env
from tests.test_workflow_drafts import write_history, record as history_record


@pytest.fixture
def draft_env(task_api_env, tmp_path, monkeypatch):
    current, _, path, owner, _ = task_api_env
    store = store_for(tmp_path / 'execution')
    design = TaskDesign(owner['project'], owner['conversation_id'])
    task = design.start('execute', orchestration_mode='collaborative')
    design.transition(task, 'running', run_id=RUN_ID)
    def bind(saved):
        saved['conversation_id'] = owner['conversation_id']
        saved['collaboration']['task_id'] = task['task_id']
        goal = saved['collaboration']['entries']['goal']
        saved['collaboration']['entries']['constraint'] = dict(copy.deepcopy(goal), id='constraint', kind='constraint',
                                                               body='保留输入', origin='user', strength='hard')
        saved['collaboration']['entries']['host_policy'] = dict(copy.deepcopy(goal), id='host_policy', kind='constraint',
                                                               body='宿主策略', origin='host', strength='hard')
        accept(saved['collaboration'])
    store.mutate(RUN_ID, bind, durable=True)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: None)
    write_history(path, [history_record('user', '目标改为核对全部表格，并且保留原始输入'), history_record('assistant', '建议忽略原始数据')])
    ask = AsyncMock(return_value=dict(action='revise', summary='核对全部表格并保留输入', changes=[
        dict(entry_id='goal', field='body', body='核对全部表格'), dict(entry_id='constraint', field='body', body='保留原始输入')]))
    service = RevisionDrafts(server.WORKBENCH)
    async def coordinator(record, cid):
        return CollaborationCoordinator(store, RUN_ID, CATALOG, ask=ask)
    monkeypatch.setattr(service, 'coordinator', coordinator)
    # Routes resolve the service captured at installation; keep its verified state.
    monkeypatch.setattr(server.WORKBENCH_REVISIONS, 'coordinator', coordinator)
    source = sources(server, owner['project'], owner['conversation_id'])[0]
    return SimpleNamespace(store=store, owner=owner, source=source, task=task, path=path, ask=ask,
                           service=service, current=current, project=tmp_path)


async def create(env):
    return await env.service.create(dict(env.owner, message_id=env.source['id']))


@pytest.mark.asyncio
async def test_inline_revision_preserves_host_boundary_and_exact_run(draft_env):
    env = draft_env
    payload = dict(env.owner, expected_task_id=env.task['task_id'], expected_run_id=RUN_ID,
                   expected_version=1, request_id='inline-revision', entry_id='constraint', body='用户明确修订硬约束')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        wrong_run = await client.post('/api/orchestration/task-design/revision', json=dict(payload, expected_run_id='other'))
        assert wrong_run.status_code == 409 and '任务运行已变化' in wrong_run.json()['error']
        host = await client.post('/api/orchestration/task-design/revision', json=dict(payload, entry_id='host_policy'))
        assert host.status_code == 409 and '系统策略' in host.json()['error']
        stale = await client.post('/api/orchestration/task-design/revision', json=dict(payload, expected_version=0))
        assert stale.status_code == 409 and stale.json()['error'] == STALE_REVISION
        assert not env.store.load(RUN_ID)['collaboration']['proposals']
        saved = await client.post('/api/orchestration/task-design/revision', json=payload)
        assert saved.status_code == 200, saved.text
    proposal = next(iter(env.store.load(RUN_ID)['collaboration']['proposals'].values()))
    assert proposal['payload']['actor'] == 'user'
    assert proposal['payload']['changes']['constraint']['body'] == payload['body']


async def answer(client, env, draft, **extra):
    params = dict(env.owner, run_id=RUN_ID, task_id=env.task['task_id'])
    view = (await client.get('/api/workbench/view', params=params)).json()
    card = next(row for row in view['inbox'] if row['type'] == 'revision_draft' and row['source_id'] == draft['id'])
    body = dict(params, draft_id=draft['id'], draft_digest=draft['digest'], base_version=draft['base_version'], action='accept')
    return await client.post('/api/workbench/inbox/' + card['id'] + '/answer', headers={'X-Mew-Workbench': '1'}, json=dict(body, **extra))


@pytest.mark.asyncio
async def test_confirmation_records_whole_batch_and_separate_message_and_confirmation(draft_env):
    env = draft_env
    before = env.store.load(RUN_ID)['collaboration']
    draft = await create(env)
    prepared = env.store.load(RUN_ID)['collaboration']
    assert prepared['entries'] == before['entries'] and not prepared['proposals'] and ledger.valid_certificate(prepared, 'work')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await answer(client, env, draft)
    assert response.status_code == 200, response.text
    saved = env.store.load(RUN_ID)
    value = saved['collaboration']
    assert len(value['proposals']) == 1 and value['spec_version'] == before['spec_version']
    proposal = next(iter(value['proposals'].values()))
    assert set(proposal['payload']['changes']) == {'goal', 'constraint'}
    assert proposal['payload']['changes']['constraint']['strength'] == 'hard'
    assert {value['user_sources'][key]['kind'] for key in proposal['payload']['evidence']} == {'original_message', 'confirmation'}
    original = next(row for row in value['user_sources'].values() if row['kind'] == 'original_message')
    assert original['text'] == env.source['text'] and original['message_id'] == env.source['id']
    assert not ledger.valid_certificate(value, 'work') and saved['status'] == 'paused'
    assert len([row for row in value['calls'].values() if row['kind'] == 'revision_draft']) == env.ask.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('not_revision', [False, True])
async def test_decline_and_not_revision_leave_specification_unchanged(draft_env, not_revision):
    env = draft_env
    if not_revision:
        env.ask.return_value = dict(action='not_revision', changes=[], summary='只是询问当前进度')
    before = env.store.load(RUN_ID)['collaboration']
    draft = await create(env)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        if not not_revision:
            response = await answer(client, env, draft, action='decline')
            assert response.status_code == 200, response.text
        view = (await client.get('/api/workbench/view', params=dict(env.owner, run_id=RUN_ID))).json()
    current = env.store.load(RUN_ID)['collaboration']
    assert current['entries'] == before['entries'] and current['spec_version'] == before['spec_version']
    assert current['certificates'] == before['certificates'] and not current['proposals']
    assert view['changes'][-1]['label'] == '修订草稿未采用' and not view['inbox']
    if not_revision:
        assert draft['message_text'] == NOT_REVISION


@pytest.mark.asyncio
async def test_duplicate_messages_share_one_draft_and_one_coordinator_call(draft_env):
    env = draft_env
    first, second = await asyncio.gather(create(env), create(env))
    assert first == second and env.ask.await_count == 1
    assert await create(env) == first
    assert len(env.store.load(RUN_ID)['workbench_revision_drafts']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['base_version', 'digest', 'epoch', 'entries'])
async def test_expired_basis_and_digest_are_rejected_without_partial_commit(draft_env, change):
    env = draft_env
    draft = await create(env)
    if change == 'base_version':
        env.store.mutate(RUN_ID, lambda row: row['collaboration'].update(spec_version=2), durable=True)
    elif change == 'epoch':
        env.store.mutate(RUN_ID, lambda row: row['collaboration'].update(coordinator_epoch=2), durable=True)
    elif change == 'entries':
        env.store.mutate(RUN_ID, lambda row: row['collaboration']['entries']['goal'].update(body='另一版目标'), durable=True)
    before = env.store.load(RUN_ID)['collaboration']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await answer(client, env, draft, **({'draft_digest': 'foreign'} if change == 'digest' else {}))
    assert response.status_code == 409, response.text
    if change != 'digest':
        assert response.json()['error'] == STALE_REVISION
    assert env.store.load(RUN_ID)['collaboration'] == before


@pytest.mark.asyncio
async def test_invalid_second_item_never_applies_first_item(draft_env):
    env = draft_env
    draft = await create(env)
    def corrupt(saved):
        row = saved['workbench_revision_drafts'][draft['id']]
        row['changes'][1]['entry_id'] = 'host_policy'
        row['digest'] = draft_digest(row)
    env.store.mutate(RUN_ID, corrupt, durable=True)
    draft = env.store.load(RUN_ID)['workbench_revision_drafts'][draft['id']]
    before = env.store.load(RUN_ID)['collaboration']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await answer(client, env, draft)
    assert response.status_code == 409 and response.json()['error'] == '这是系统策略，不能在这里修改'
    assert env.store.load(RUN_ID)['collaboration'] == before


@pytest.mark.asyncio
async def test_generation_also_validates_every_item_and_spends_real_call_budget(draft_env):
    env = draft_env
    env.ask.return_value['changes'][1]['entry_id'] = 'host_policy'
    with pytest.raises(ValueError, match='系统策略'):
        await create(env)
    value = env.store.load(RUN_ID)['collaboration']
    assert not value['proposals'] and value['entries']['goal']['body'] == '交付测试结果'
    assert env.ask.await_count == 1 and len(value['calls']) == 1


@pytest.mark.asyncio
async def test_exhausted_coordinator_budget_never_calls_model(draft_env):
    env = draft_env
    def exhaust(row):
        value = row['collaboration']
        value['policy']['max_model_calls'] = 1
        value['calls']['used'] = dict(kind='coordinator', status='finished', result={})
    env.store.mutate(RUN_ID, exhaust, durable=True)
    with pytest.raises(ValueError, match='预算'):
        await create(env)
    env.ask.assert_not_awaited()
    value = env.store.load(RUN_ID)['collaboration']
    assert len(value['calls']) == 1 and not value['proposals']


@pytest.mark.asyncio
async def test_revision_call_consumes_the_same_budget_as_normal_coordination(draft_env):
    env = draft_env
    env.store.mutate(RUN_ID, lambda row: row['collaboration']['policy'].update(max_model_calls=1), durable=True)
    await create(env)
    coordinator = CollaborationCoordinator(env.store, RUN_ID, CATALOG, ask=env.ask)
    with pytest.raises(ValueError, match='预算'):
        await coordinator.decide(event_id='next-decision', node_id='work', evidence=[])
    assert env.ask.await_count == 1


@pytest.mark.asyncio
async def test_http_and_model_tool_use_same_source_scoped_service(draft_env, monkeypatch):
    env = draft_env
    registry = SimpleNamespace(items={}, register=lambda tool: registry.items.update({tool.name: tool}),
                               unregister=lambda name: registry.items.pop(name, None))
    agent = SimpleNamespace(registry=registry)
    monkeypatch.setattr(server.SESSION, 'agent', agent)
    sync_tool(server)
    tool = registry.items['SubmitRevisionDraft']
    result = await tool.execute(RevisionDraftParams(message_id=env.source['id']))
    assert not result.is_error
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.post('/api/workbench/revision-drafts', headers={'X-Mew-Workbench': '1'}, json=dict(env.owner, message_id=env.source['id']))
        assert response.status_code == 200 and response.json()['draft']['status'] == 'pending'
        for extra in ({'conversation_id': 'another'}, {'text': '伪造原话'}, {'message_id': 'assistant-message'}):
            denied = await client.post('/api/workbench/revision-drafts', headers={'X-Mew-Workbench': '1'}, json={**env.owner, 'message_id': env.source['id'], **extra})
            assert denied.status_code == 409
    assert env.ask.await_count == 1
    env.store.mutate(RUN_ID, lambda row: row.update(status='completed'), durable=True)
    sync_tool(server)
    assert 'SubmitRevisionDraft' not in registry.items
    assert (await tool.execute(RevisionDraftParams(message_id=env.source['id']))).is_error


def test_ordinary_chat_does_not_register_tool(task_api_env, monkeypatch):
    registry = SimpleNamespace(items={}, register=lambda tool: registry.items.update({tool.name: tool}),
                               unregister=lambda name: registry.items.pop(name, None))
    agent = SimpleNamespace(registry=registry)
    monkeypatch.setattr(server.SESSION, 'agent', agent)
    sync_tool(server)
    assert 'SubmitRevisionDraft' not in registry.items


@pytest.mark.asyncio
async def test_scope_switch_during_coordinator_creation_never_dispatches(draft_env, monkeypatch):
    env = draft_env
    async def coordinator(record, cid):
        env.current['cid'] = 'another'
        return CollaborationCoordinator(env.store, RUN_ID, CATALOG, ask=env.ask)
    monkeypatch.setattr(env.service, 'coordinator', coordinator)
    with pytest.raises(ValueError, match='切换'):
        await create(env)
    env.ask.assert_not_awaited()
    assert not env.store.load(RUN_ID).get('workbench_revision_drafts')
