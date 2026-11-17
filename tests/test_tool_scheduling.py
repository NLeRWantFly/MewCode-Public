"""A dirty stream and buffered usage must not turn tool starts into full saves."""
import asyncio
import copy
import threading

import pytest

from mewcode.kernel import events, run_views
from mewcode.kernel.async_commit import durable_call
from mewcode.kernel.sqlite_state import RunDatabase
from tests.test_tool_performance import RUN, assert_views_match, receipt, running_store


def flush(store, **extra):
    return store.flush_stream(RUN, 'executor', text='new visible text', reasoning='private',
        messages=[{'role': 'assistant', 'content': 'new context'}], return_snapshot=False, **extra)


@pytest.mark.parametrize('member', ['', 'helper'])
def test_changed_stream_is_detached_and_views_match_full_rebuild(tmp_path, member):
    store = running_store(tmp_path)
    if member:
        store.mutate(RUN, lambda row: row['members'].update({'executor::helper': {'status': 'running'}}))
    store.append_sequenced_events(RUN, [receipt(1), receipt(2, 'expert_tool_result', output='ok')], return_snapshot=False)
    old = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    frozen = copy.deepcopy(old)
    messages, children = [{'role': 'user', 'content': 'context'}], {'child': {'usage': {'input_tokens': 5}}}
    store.flush_stream(RUN, 'executor', member_id=member, text='new', reasoning='private',
        messages=messages, children=children, safety_review_state={},
        stream_activity={'phase': 'generating', 'updated_at': 1}, return_snapshot=False)
    saved = assert_views_match(tmp_path)
    assert old == frozen
    assert saved['usage']['total']['input_tokens'] == 5
    messages[0]['content'] = 'caller mutation'
    children['child']['usage']['input_tokens'] = 999
    bucket, key = ('members', 'executor::helper') if member else ('node_states', 'executor')
    store._live[RUN][bucket][key]['messages'][0]['content'] = 'live mutation'
    assert events.EventJournal(tmp_path, RUN)._sql_load() == saved
    assert RunDatabase(tmp_path, RUN).load() == saved


def test_mixed_usage_and_tool_batch_uses_incremental_views(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    store.append_sequenced_events(RUN, [dict(seq=i, type='expert_step', label=str(i))
                                      for i in range(1, 65)], return_snapshot=False)
    original = run_views.build
    def forbidden(*args, **kwargs):
        raise AssertionError('usage fell back to a full view rebuild')
    monkeypatch.setattr(run_views, 'build', forbidden)
    store.append_sequenced_events(RUN, [receipt(65), dict(seq=66, type='expert_usage', input_tokens=3),
        receipt(67, 'expert_tool_start', started_at=1)], return_snapshot=False)
    monkeypatch.setattr(run_views, 'build', original)
    assert_views_match(tmp_path)


def test_dirty_stream_never_copies_unrelated_history(tmp_path):
    store = running_store(tmp_path)
    flush(store)
    class Uncopyable(list):
        def __deepcopy__(self, memo):
            raise AssertionError('copied unrelated historical messages')
    private = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    private['kernel_attempt_archives'] = Uncopyable()
    store._live[RUN]['kernel_attempt_archives'] = Uncopyable()
    flush(store, stream_activity={'phase': 'generating', 'updated_at': 2})
    store.append_sequenced_events(RUN, [dict(seq=1, type='expert_usage'), receipt(2)], return_snapshot=False)
    assert RunDatabase(tmp_path, RUN).load()['node_states']['executor']['stream_activity']['updated_at'] == 2


def test_failed_stream_commit_publishes_neither_cache_nor_views(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    before = RunDatabase(tmp_path, RUN).load()
    cached, live = store._read_model_cache[RUN], store._live[RUN]
    def fail(*args, **kwargs):
        raise OSError('injected transaction failure')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    with pytest.raises(OSError, match='injected'):
        flush(store, safety_review_state={'pending_call_ref': 'review'})
    assert RunDatabase(tmp_path, RUN).load() == before
    assert events.EventJournal(tmp_path, RUN)._sql_load() == before
    assert store._live[RUN] is live and store._read_model_cache[RUN] is cached


def test_stream_pending_review_still_creates_bound_blocker(tmp_path):
    from mewcode.experts.node_ledger import ledger_for_run
    store = running_store(tmp_path)
    flush(store, safety_review_state={'pending_call_ref': 'review'})
    saved = assert_views_match(tmp_path)
    assert any(row.payload['ref'] == 'review' and row.payload['kind'] == 'safety_review'
               for row in ledger_for_run(saved).view('executor').active_blockers)


def test_stream_cannot_mistake_callback_alias_for_durable_context(tmp_path):
    store = running_store(tmp_path)
    messages = [{'role': 'assistant', 'content': 'old'}]
    store.mutate(RUN, lambda row: row['node_states']['executor'].update(
        node_id='executor', member_id='', partial_text='text', partial_reasoning='', messages=messages))
    messages[0]['content'] = 'new'
    store.flush_stream(RUN, 'executor', text='text', reasoning='', messages=messages, return_snapshot=False)
    assert RunDatabase(tmp_path, RUN).load()['node_states']['executor']['messages'][0]['content'] == 'new'


def test_stream_preserves_existing_recovery_card_projection(tmp_path):
    store = running_store(tmp_path)
    decision = {'action': 'human', 'decision_id': 'recover', 'reason': 'review tool evidence'}
    store.mutate(RUN, lambda row: row['node_states']['executor'].update(
        scheduler_recovery={'decisions': [decision]}))
    store.append_sequenced_events(RUN, [dict(seq=1, type='recovery_case', node_id='executor',
                                            recovery_decision=decision)], return_snapshot=False)
    flush(store)
    assert_views_match(tmp_path)


@pytest.mark.asyncio
async def test_cancelled_queued_stream_cannot_overwrite_final_checkpoint(tmp_path):
    store = running_store(tmp_path)
    entered, release = threading.Event(), threading.Event()
    generation = [1]
    def hold():
        entered.set()
        release.wait(5)
    holding = asyncio.create_task(durable_call(hold))
    await asyncio.to_thread(entered.wait, 2)
    pending = asyncio.create_task(durable_call(flush, store, is_current=lambda: generation[0] == 1))
    await asyncio.sleep(0)
    generation[0] = 2
    pending.cancel()
    release.set()
    await holding
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert 'partial_text' not in RunDatabase(tmp_path, RUN).load()['node_states']['executor']
