"""Durable recovery cards, real preparation HTTP and stop/preparation races."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from frontend import interaction_choices as choices_api, server
from mewcode.experts.case_runtime import CaseCoordinator, answer_case
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.recovery_case import apply_event, get_case, open_case, save_case
from mewcode.experts.waits import BranchWaitPaused
from tests.test_interaction_choices_api import FakeSrv
from tests.test_prepared_interactions import current


def checkpoint(tmp_path, *, status='paused'):
    flow, store, run_id, _ = current(tmp_path, [])
    identity = {}
    def seed(record):
        case = open_case(record, 'work')
        case = apply_event(record, case['case_id'], {'kind': 'wait', 'identity': 'waiting',
            'pending': {'kind': 'incident', 'round': 0, 'options': [{'value': 'close'}]}})
        stage = record['node_states']['work']
        stage.update(status=status, pending_recovery_case={'case_id': case['case_id'], 'round': 0})
        ledger_for_run(record).set_phase('work', 'waiting_human' if status == 'paused' else 'running', reason='case_decision')
        record.update(status='running', active_node_ids=[])
        identity['case_id'] = case['case_id']
    store.mutate(run_id, seed)
    return flow, store, run_id, identity['case_id']


def endpoint(monkeypatch, tmp_path, store, *, busy=True, runtime=None):
    runtime = runtime or SimpleNamespace(_case_waiters={}, conversation_id='choices-chat', work_dir=str(tmp_path))
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server, 'SESSION', SimpleNamespace(
        runtime_for=lambda _cid: runtime, chat_busy=lambda _cid: busy))
    monkeypatch.setattr(server, '_capability_executor_scope_matches', lambda _body: True)
    monkeypatch.setattr(server, 'WS_EVENTS', SimpleNamespace(send_json=AsyncMock()))
    return runtime


def close_body(tmp_path, run_id, case_id, **kwargs):
    return server.RecoveryCaseAnswerBody(conversation_id='choices-chat', project=str(tmp_path),
        run_id=run_id, case_id=case_id, round=0, choice='close', **kwargs)


@pytest.mark.asyncio
async def test_busy_chat_can_stop_durable_wait_without_in_memory_waiter(monkeypatch, tmp_path):
    _flow, store, run_id, case_id = checkpoint(tmp_path)
    endpoint(monkeypatch, tmp_path, store)
    response = await server.api_recovery_case_answer(close_body(tmp_path, run_id, case_id))
    assert response.status_code == 200, response.body
    after = store.load(run_id)
    assert get_case(after, case_id)['closed_by_user'] is True
    assert after['node_states']['work']['status'] == 'paused'
    assert not get_case(after, case_id)['pending'] and not after.get('authorizations', {}).get('entries')
    again = await server.api_recovery_case_answer(close_body(tmp_path, run_id, case_id))
    assert again.status_code == 200 and json.loads(again.body)['already_stopped']
    assert store.load(run_id) == after


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['running', 'completed'])
async def test_running_or_completed_node_is_not_an_answerable_checkpoint(monkeypatch, tmp_path, status):
    _flow, store, run_id, case_id = checkpoint(tmp_path, status=status)
    endpoint(monkeypatch, tmp_path, store)
    before = store.load(run_id)
    response = await server.api_recovery_case_answer(close_body(tmp_path, run_id, case_id))
    assert response.status_code == 409
    assert json.loads(response.body)['snapshot']['run_id'] == run_id
    assert store.load(run_id) == before


@pytest.mark.asyncio
async def test_old_case_preparation_http_persists_ledger_without_ask_user_pending(monkeypatch, tmp_path):
    _flow, store, run_id, case_id = checkpoint(tmp_path)
    srv = FakeSrv(tmp_path)
    srv.SESSION.conversation_id = 'choices-chat'
    srv.runtime.conversation_id = 'choices-chat'
    srv.EXPERT_RUNS = store
    calls = []
    async def complete(_client, **kwargs):
        calls.append(kwargs['user_text'])
        return json.dumps({'searches': [], 'questions': []})
    monkeypatch.setattr(choices_api, 'collect_model_text', complete)
    app = FastAPI()
    choices_api.install(app, srv)
    payload = {'conversation_id': 'choices-chat', 'run_id': run_id,
               'node_id': 'work', 'request_id': case_id, 'case_id': case_id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        first = await client.post(choices_api._ROUTE, json=payload)
        assert first.status_code == 200, first.text
        bundle = first.json()['choice_bundle']
        assert bundle['status'] == 'ready' and bundle['questions'][0]['choices']
        saved = get_case(store.load(run_id), case_id)
        assert saved['pending']['choice_bundle']['id'] == bundle['id']
        assert 'pending_interaction' not in store.load(run_id)['node_states']['work']
        count = len(calls)
        second = await client.post(choices_api._ROUTE, json=payload)
        assert second.status_code == 200 and len(calls) == count
        assert second.json()['choice_bundle']['id'] == bundle['id']
    endpoint(monkeypatch, tmp_path, store)
    response = await server.api_recovery_case_answer(close_body(tmp_path, run_id, case_id,
        choice_bundle_id=bundle['id'], choice_version=bundle['version']))
    assert response.status_code == 200, response.body


@pytest.mark.asyncio
async def test_stop_during_readonly_preparation_cancels_and_cannot_resurrect(monkeypatch, tmp_path):
    _flow, store, run_id, case_id = checkpoint(tmp_path)
    runtime = endpoint(monkeypatch, tmp_path, store)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def prepare(**kwargs):
        old = copy.deepcopy(kwargs['case'])
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            # A provider may return an old result during cancellation cleanup.
            return old
    runtime.recovery_choice_preparer = prepare
    events = []
    async def emit(event):
        events.append(event)
    coordinator = CaseCoordinator(store=store, run_id=run_id, session=runtime, emit=emit)
    task = asyncio.create_task(coordinator.pause(get_case(store.load(run_id), case_id)))
    await asyncio.wait_for(entered.wait(), timeout=3)
    assert case_id in runtime._case_waiters
    response = await server.api_recovery_case_answer(close_body(tmp_path, run_id, case_id))
    assert response.status_code == 200, response.body
    with pytest.raises(BranchWaitPaused):
        await asyncio.wait_for(task, timeout=3)
    assert cancelled.is_set() and not runtime._case_waiters
    saved = get_case(store.load(run_id), case_id)
    assert saved['closed_by_user'] and saved['pending'] == {} and saved['rounds'] == []
    last_case = [event for event in events if event.get('type') == 'recovery_case'][-1]
    assert last_case['case']['closed_by_user'] and last_case['phase'] == '已停止'


@pytest.mark.asyncio
async def test_legacy_close_only_card_can_defer_without_binding_or_stop(monkeypatch, tmp_path):
    _flow, store, run_id, case_id = checkpoint(tmp_path)
    endpoint(monkeypatch, tmp_path, store)
    body = close_body(tmp_path, run_id, case_id).model_copy(update={'choice': 'defer'})
    response = await server.api_recovery_case_answer(body)
    assert response.status_code == 200, response.body
    saved = get_case(store.load(run_id), case_id)
    assert saved['status'] == 'awaiting_user' and saved['pending']
    assert not saved.get('closed_by_user') and saved['rounds'] == []
    assert json.loads(response.body)['resume_required'] is False


@pytest.mark.asyncio
async def test_late_preparation_http_does_not_overwrite_a_stop(monkeypatch, tmp_path):
    _flow, store, run_id, case_id = checkpoint(tmp_path)
    srv = FakeSrv(tmp_path)
    srv.SESSION.conversation_id = 'choices-chat'
    srv.EXPERT_RUNS = store
    called = False
    async def complete(_client, **kwargs):
        nonlocal called
        if not called:
            called = True
            store.mutate(run_id, lambda record: answer_case(record, case_id, round=0, choice='close'))
        return json.dumps({'searches': [], 'questions': []})
    monkeypatch.setattr(choices_api, 'collect_model_text', complete)
    app = FastAPI()
    choices_api.install(app, srv)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(choices_api._ROUTE, json={'conversation_id': 'choices-chat',
            'run_id': run_id, 'node_id': 'work', 'request_id': case_id, 'case_id': case_id})
        assert response.status_code == 409
    saved = get_case(store.load(run_id), case_id)
    assert saved['closed_by_user'] and not saved['pending']


@pytest.mark.asyncio
async def test_closed_case_never_waits_for_another_answer(tmp_path):
    _flow, store, run_id, case_id = checkpoint(tmp_path)
    store.mutate(run_id, lambda record: answer_case(record, case_id, round=0, choice='close'))
    runtime = SimpleNamespace(_case_waiters={}, recovery_choice_preparer=AsyncMock())
    async def emit(_event):
        pass
    coordinator = CaseCoordinator(store=store, run_id=run_id, session=runtime, emit=emit)
    with pytest.raises(BranchWaitPaused):
        await asyncio.wait_for(coordinator.pause(get_case(store.load(run_id), case_id)), timeout=3)
    assert not runtime._case_waiters and runtime.recovery_choice_preparer.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('retired', ['completed', 'superseded'])
async def test_pause_of_retired_case_never_reopens_current_node(tmp_path, retired):
    _flow, store, run_id, case_id = checkpoint(tmp_path)
    old = get_case(store.load(run_id), case_id)
    def retire(record):
        if retired == 'completed':
            record['node_states']['work']['status'] = 'completed'
        else:
            apply_event(record, case_id, {'kind': 'supersede', 'identity': 'new-checkpoint'})
    store.mutate(run_id, retire)
    before = store.load(run_id)
    runtime = SimpleNamespace(_case_waiters={}, recovery_choice_preparer=AsyncMock())
    async def emit(_event):
        pytest.fail('A retired case was published as an active checkpoint')
    coordinator = CaseCoordinator(store=store, run_id=run_id, session=runtime, emit=emit)
    with pytest.raises(BranchWaitPaused):
        await coordinator.pause(old)
    assert store.load(run_id) == before and not runtime._case_waiters
    assert runtime.recovery_choice_preparer.await_count == 0


@pytest.mark.asyncio
async def test_old_publish_is_history_only_and_cannot_replace_new_case(tmp_path):
    _flow, store, run_id, case_id = checkpoint(tmp_path)
    old = get_case(store.load(run_id), case_id)
    def replace(record):
        apply_event(record, case_id, {'kind': 'supersede', 'identity': 'new-case'})
        open_case(record, 'work', identity='current-case')
    store.mutate(run_id, replace)
    stage = copy.deepcopy(store.load(run_id)['node_states']['work'])
    events = []
    async def emit(event):
        events.append(event)
    coordinator = CaseCoordinator(store=store, run_id=run_id, emit=emit)
    await coordinator.publish(old, '已汇报')
    assert store.load(run_id)['node_states']['work'] == stage
    assert events[-1]['archived'] and events[-1]['actionable'] is False
