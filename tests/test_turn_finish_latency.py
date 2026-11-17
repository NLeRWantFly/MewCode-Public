"""Turn receipts keep recovery semantics without touching archived context."""
import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import ErrorEvent, StreamText, TurnComplete
from mewcode.conversation import ConversationManager
from mewcode.experts.iteration_recovery import recover_iteration_stream
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.kernel import events
from mewcode.kernel.escalation import RecoveryDecision
from mewcode.kernel.sqlite_state import RunDatabase
from tests.test_iteration_recovery import SlicedAgent
from tests.test_tool_performance import RUN, assert_views_match, running_store


@pytest.mark.asyncio
@pytest.mark.parametrize('pending', [False, True])
async def test_async_replan_check_is_awaited_only_at_completed_turn(pending):
    agent = SlicedAgent(failures=0)
    called = asyncio.Event()
    async def requested():
        await asyncio.sleep(0)
        called.set()
        return pending
    controller = AsyncMock(return_value=RecoveryDecision(action='assist', reason='assist'))
    stream = recover_iteration_stream(agent, ConversationManager(), state={},
        save=lambda: None, emit=AsyncMock(), ask=AsyncMock(), remaining=lambda: True,
        progress=lambda: None, node_id='executor', role='executor',
        requested=requested, controller=controller)
    assert isinstance(await anext(stream), TurnComplete)
    assert not called.is_set()
    result = [event async for event in stream]
    assert called.is_set() and agent.closed == 1
    assert isinstance(result[-1], ErrorEvent if pending else StreamText)
    assert controller.await_count == int(pending)


@pytest.mark.asyncio
async def test_cancellation_during_replan_read_closes_stream_without_controller():
    agent, entered = SlicedAgent(failures=0), asyncio.Event()
    async def requested():
        entered.set()
        await asyncio.Event().wait()
    controller = AsyncMock()
    async def consume():
        return [event async for event in recover_iteration_stream(agent, ConversationManager(),
            state={}, save=lambda: None, emit=AsyncMock(), ask=AsyncMock(),
            remaining=lambda: True, progress=lambda: None, node_id='executor', role='executor',
            requested=requested, controller=controller)]
    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.closed == 1 and agent.max_iterations == 12
    controller.assert_not_called()


def response(store, *, turn=1, attempt=2, history_size=3):
    store.record_model_response(RUN, 'executor', turn=turn,
        node_attempt=attempt, history_size=history_size)


def test_response_matches_existing_fact_sync_and_all_views(tmp_path, monkeypatch):
    monkeypatch.setattr('mewcode.expert_runs.utc_now', lambda: '2026-10-27T00:00:00+00:00')
    monkeypatch.setattr('mewcode.experts.node_ledger.time.time', lambda: 100.)
    old = running_store(tmp_path/'before')
    new = running_store(tmp_path/'after')
    def add_blockers(row):
        ledger = ledger_for_run(row)
        for kind in ('provider_error', 'missing_authorization', 'unknown_effect'):
            ledger.record('executor', 'blocker', {'kind': kind}, identity=kind)
    for store in (old, new):
        store.mutate(RUN, add_blockers)
    monkeypatch.setattr('mewcode.experts.node_ledger.time.time', lambda: 101.)
    old.mutate(RUN, lambda row: ledger_for_run(row).record('executor', 'receipt',
        {'model_response_completed': True, 'turn': 1}, identity=['model_response', 2, 1, 3]),
        return_snapshot=False)
    response(new)
    actual = assert_views_match(tmp_path/'after')
    assert actual == RunDatabase(tmp_path/'before', RUN).load()
    assert {fact.payload['kind'] for fact in ledger_for_run(actual).view('executor').active_blockers} == {
        'missing_authorization', 'unknown_effect'}
    assert new._live[RUN] == actual


def test_duplicate_response_does_not_commit_copy_history_or_refresh_timestamp(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    response(store)
    before = store.load(RUN)
    class Uncopyable(list):
        def __deepcopy__(self, memo):
            raise AssertionError('copied archived context')
    for record in (store._live[RUN], events.EventJournal(tmp_path, RUN)._sql_load(detached=False)):
        record['kernel_attempt_archives'] = Uncopyable()
    def unexpected(*args, **kwargs):
        raise AssertionError('duplicate performed a transaction')
    monkeypatch.setattr(store, '_commit', unexpected)
    monkeypatch.setattr(store, 'mutate', unexpected)
    response(store)
    assert store._live[RUN]['kernel_revision'] == before['kernel_revision']
    assert store._live[RUN]['updated_at'] == before['updated_at']
    assert RunDatabase(tmp_path, RUN).load() == before


def test_new_response_does_not_copy_history_and_keeps_private_ownership(tmp_path):
    store = running_store(tmp_path)
    store.mutate(RUN, lambda row: row.update(kernel_attempt_archives=['old']))
    class Uncopyable(list):
        def __deepcopy__(self, memo):
            raise AssertionError('copied archived context')
    for record in (store._live[RUN], events.EventJournal(tmp_path, RUN)._sql_load(detached=False)):
        record['kernel_attempt_archives'] = Uncopyable(['old'])
    response(store)
    durable = assert_views_match(tmp_path)
    assert durable['kernel_attempt_archives'] == ['old']
    store._live[RUN]['node_ledgers']['executor']['facts'][-1]['payload']['modified'] = True
    store._live[RUN]['node_states']['executor']['ledger_view']['phase'] = 'changed'
    assert events.EventJournal(tmp_path, RUN)._sql_load(detached=False)['node_ledgers'] == durable['node_ledgers']
    assert RunDatabase(tmp_path, RUN).load() == durable


def test_new_response_rolls_back_when_database_write_fails(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    before = store.load(RUN)
    models = store._read_model_cache[RUN]
    def fail(*args, **kwargs):
        raise OSError('injected receipt write failure')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    with pytest.raises(OSError, match='injected'):
        response(store)
    assert store.load(RUN) == before
    assert events.EventJournal(tmp_path, RUN)._sql_load() == before
    assert RunDatabase(tmp_path, RUN).load() == before
    assert store._read_model_cache[RUN] is models


def test_response_identity_keeps_epoch_and_retries_distinct(tmp_path):
    store = running_store(tmp_path)
    response(store)
    first = copy.deepcopy(store._live[RUN]['node_ledgers'])
    response(store)
    assert store._live[RUN]['node_ledgers'] == first
    response(store, attempt=3)
    store.mutate(RUN, lambda row: ledger_for_run(row).record('executor', 'epoch_change',
        {'reason': 'user_direction', 'source_ref': 'new-direction'}, epoch=1, identity='new-direction'))
    response(store)
    facts = [fact for fact in ledger_for_run(store.load(RUN)).view('executor').history
             if fact.payload.get('model_response_completed')]
    assert len(facts) == 3 and [fact.epoch for fact in facts] == [0, 0, 1]


def test_duplicate_receipt_does_not_clear_a_later_provider_failure(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    monkeypatch.setattr('mewcode.experts.node_ledger.time.time', lambda: 100.)
    response(store)
    monkeypatch.setattr('mewcode.experts.node_ledger.time.time', lambda: 200.)
    store.mutate(RUN, lambda row: ledger_for_run(row).record('executor', 'blocker',
        {'kind': 'provider_error'}, identity='later-failure'))
    revision = store._live[RUN]['kernel_revision']
    response(store)
    view = ledger_for_run(store.load(RUN)).view('executor')
    assert [fact.payload['kind'] for fact in view.active_blockers] == ['provider_error']
    assert store._live[RUN]['kernel_revision'] == revision


def test_paused_response_uses_existing_durable_path(tmp_path):
    store = running_store(tmp_path, status='paused')
    response(store)
    assert len([fact for fact in ledger_for_run(assert_views_match(tmp_path)).view('executor').history
                if fact.payload.get('model_response_completed')]) == 1
