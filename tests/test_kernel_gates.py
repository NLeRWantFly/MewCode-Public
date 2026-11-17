import asyncio
from types import SimpleNamespace

import pytest

from mewcode.agent import AskUserRequest
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.waits import wait_for_human, wait_for_interaction
from mewcode.kernel.gates import GateKind, classify


def setup(tmp_path):
    store = ExpertRunStore(tmp_path / 'runs')
    run_id = 'a' * 32
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store.create(run_id=run_id, conversation_id='fixture', task='fixture', attachments=[], profile={}, strategy='stable')
    store.mutate(run_id, lambda record: record.update(authorization_manifest={
        'schema_version': 2, 'workspace': str(workspace), 'nodes': {'N03': {}}, 'scope_digest': 'fixture', 'approved': True}), durable=True)
    return store, run_id


def request(identity='choose'):
    return AskUserRequest(identity, 'AskUserQuestion', [{'name': 'choice', 'question': 'fixture?', 'options': [
        {'value': 'small', 'label': 'small', 'recommended': True}, {'value': 'large', 'label': 'large'}]}],
        asyncio.get_running_loop().create_future())


@pytest.mark.asyncio
async def test_reversible_timeout_has_snapshot_and_durable_distinct_selection(tmp_path, monkeypatch):
    monkeypatch.setenv('MEWCODE_EXPERT_HITL_SOFT_TIMEOUT', '.01')
    store, run_id = setup(tmp_path)
    events = []
    async def emit(value):
        events.append(value)
    answer = await wait_for_interaction(request(), kind='ask_user', session=SimpleNamespace(), emit=emit,
                                      store=store, run_id=run_id, stage_key='N03')
    assert answer == {'choice': 'small'}
    gate = store.load(run_id)['kernel_gates']['choose']
    assert gate['selection_source'] == 'timeout' and gate['snapshot_id']
    assert any(event['type'] == 'gate_timeout_selected' for event in events)
    assert gate['deadline'] is not None
    # Same scope reuses the saved answer without showing another question.
    events.clear()
    assert await wait_for_interaction(request('again'), kind='ask_user', session=SimpleNamespace(), emit=emit,
                                     store=store, run_id=run_id, stage_key='N03') == answer
    assert not events


@pytest.mark.asyncio
async def test_authorization_and_unknown_effect_wait_past_timeout(tmp_path):
    store, run_id = setup(tmp_path)
    for blocker, expected in [('missing_authorization', GateKind.AUTHORIZATION), ('unknown_effect', GateKind.IRREVERSIBLE)]:
        store.mutate(run_id, lambda record: ledger_for_run(record).record('N03', 'blocker',
            {'kind': blocker, 'reason': 'fixture'}, source='host', identity=blocker), durable=True)
        assert classify(store.load(run_id), 'N03') == expected
        item = request(blocker)
        task = asyncio.create_task(wait_for_human(item.future, kind='ask_user', emit=lambda event: asyncio.sleep(0),
            store=store, run_id=run_id, stage_key='N03', request=item, soft_timeout=.01, heartbeat=.005))
        await asyncio.sleep(.04)
        assert not task.done()
        item.future.set_result({'choice': 'small'})
        assert await task == {'choice': 'small'}


@pytest.mark.asyncio
async def test_snapshot_failure_keeps_waiting_without_auto_selection(tmp_path, monkeypatch):
    from mewcode.kernel import gates
    store, run_id = setup(tmp_path)
    async def fail(*args, **kwargs):
        raise OSError('fixture snapshot failed')
    monkeypatch.setattr(gates, 'snapshot_before_timeout', fail)
    item = request()
    task = asyncio.create_task(wait_for_human(item.future, kind='ask_user', emit=lambda event: asyncio.sleep(0),
        store=store, run_id=run_id, stage_key='N03', wait_id='choose', request=item, soft_timeout=.01, heartbeat=.005))
    await asyncio.sleep(.04)
    assert not task.done()
    assert store.load(run_id)['kernel_gates']['choose']['status'] == 'snapshot_failed'
    item.future.set_result({'choice': 'large'})
    assert await task == {'choice': 'large'}


@pytest.mark.asyncio
async def test_simultaneous_choices_share_one_question_and_keep_separate_answers(tmp_path):
    store, run_id = setup(tmp_path)
    first, second = request('first'), request('second')
    session, visible = SimpleNamespace(), []
    async def emit(event):
        if event['type'] == 'ask_user':
            visible.append(event)
            display = session._ask_user_request
            assert len(display.questions) == 2
            display.future.set_result({display.questions[0]['name']: 'small', display.questions[1]['name']: 'large'})
    results = await asyncio.gather(*[wait_for_interaction(item, kind='ask_user', session=session, emit=emit,
        store=store, run_id=run_id, stage_key=key) for item, key in [(first, 'N03'), (second, 'N04')]])
    assert results == [{'choice': 'small'}, {'choice': 'large'}]
    assert len(visible) == 1
