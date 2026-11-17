"""Active checkpoints preserve full recovery state without copying old attempts."""
import copy

import pytest

from mewcode.kernel import events, run_views
from mewcode.kernel.sqlite_state import RunDatabase
from tests.test_tool_performance import RUN, assert_views_match, running_store


def checkpoint(**extra):
    return dict(node_id='executor', member_id='', role='executor', status='running',
                messages=[{'role': 'assistant', 'content': 'retained context'}],
                usage={'input_tokens': 7, 'output_tokens': 3},
                safety_review_state={'approved': {'status': 'reviewed'}}, **extra)


@pytest.mark.parametrize('member_id', ['', 'reader'])
def test_running_stage_matches_ordinary_mutation_and_survives_reload(tmp_path, member_id):
    store = running_store(tmp_path)
    bucket = 'members' if member_id else 'node_states'
    key = 'executor::reader' if member_id else 'executor'
    if member_id:
        store.mutate(RUN, lambda row: row.setdefault('members', {}).update({key: {'status': 'running'}}))
    before = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    frozen = copy.deepcopy(before)
    saved = checkpoint(partial_text='new text', iteration_recovery={'slice_exhaustions': 4})
    saved['member_id'] = member_id
    expected = copy.deepcopy(before)
    from mewcode.expert_runs import aggregate_snapshot_usage, _normalize_snapshot, _compact_snapshot_events
    from mewcode.experts.node_ledger import sync_run_facts
    from mewcode.kernel.escalation import ensure_owners
    expected[bucket][key].update(copy.deepcopy(saved), attempt=expected.get('attempt'))
    expected.setdefault('usage', {})['total'] = aggregate_snapshot_usage(expected)
    sync_run_facts(expected)
    ensure_owners(expected)
    _normalize_snapshot(expected)
    _compact_snapshot_events(expected)
    store.record_running_stage(RUN, 'executor', saved, member_id=member_id)
    actual = assert_views_match(tmp_path)
    for field in ('updated_at', 'kernel_revision'):
        expected[field] = actual[field]
    assert actual == expected
    assert before == frozen
    saved['messages'][0]['content'] = 'caller mutation'
    store._live[RUN][bucket][key]['messages'].clear()
    assert events.EventJournal(tmp_path, RUN)._sql_load() == actual
    events._SQL_CACHE.clear()
    assert RunDatabase(tmp_path, RUN).load() == actual


def test_running_stage_never_copies_unrelated_context(tmp_path):
    store = running_store(tmp_path)
    store.mutate(RUN, lambda row: row['node_states'].update(
        other={'status': 'completed', 'messages': []}))
    class Uncopyable(list):
        def __deepcopy__(self, memo):
            raise AssertionError('copied unrelated context')
    for state in (events.EventJournal(tmp_path, RUN)._sql_load(detached=False), store._live[RUN]):
        state['kernel_attempt_archives'] = Uncopyable()
        state['node_states']['other']['messages'] = Uncopyable()
    store.record_running_stage(RUN, 'executor', checkpoint())
    assert_views_match(tmp_path)


def test_running_stage_commit_failure_does_not_publish_partial_state(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    before = RunDatabase(tmp_path, RUN).load()
    live, models = store._live[RUN], store._read_model_cache[RUN]
    def fail(*args, **kwargs):
        raise OSError('injected transaction failure')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    with pytest.raises(OSError, match='injected'):
        store.record_running_stage(RUN, 'executor', checkpoint())
    assert store._live[RUN] is live
    assert store._read_model_cache[RUN] is models
    assert events.EventJournal(tmp_path, RUN)._sql_load() == before
    assert RunDatabase(tmp_path, RUN).load() == before


def test_reviewed_call_reuses_views_but_unknown_effect_rebuilds_them(tmp_path, monkeypatch):
    from mewcode.kernel.calls import CallGateway
    from tests.test_tool_boundary_performance import tool_call
    store = running_store(tmp_path)
    gateway = CallGateway(store, RUN, 'executor', 'v1')
    call = gateway.prepare(tool_call())
    original = run_views.build
    builds = []
    def tracked(*args, **kwargs):
        builds.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(run_views, 'build', tracked)
    gateway.update(call['call_id'], 'reviewed')
    assert not builds
    gateway.update(call['call_id'], 'dispatched', evidence={'executor_accepted': True})
    gateway.update(call['call_id'], 'unknown')
    assert builds
    assert_views_match(tmp_path)


def test_stream_reuses_unchanged_node_display_and_keeps_exact_projection(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    store.mutate(RUN, lambda row: row['node_states'].update(other={
        'status': 'completed', 'partial_text': 'old result', 'output_chunks': [{'text': 'old chunk'}]}))
    store.flush_stream(RUN, 'executor', text='one', reasoning='', messages=[], return_snapshot=False)
    original = run_views._stream_display_events
    reused = []
    def checked(snapshot, previous, public):
        old = [event for event in public['events'] if event.get('node_id') == 'other']
        result = original(snapshot, previous, public)
        fresh = [event for event in result if event.get('node_id') == 'other']
        reused.append(all(a is b for a, b in zip(old, fresh, strict=True)))
        return result
    monkeypatch.setattr(run_views, '_stream_display_events', checked)
    store.flush_stream(RUN, 'executor', text='two', reasoning='', messages=[], return_snapshot=False)
    assert reused == [True]
    assert_views_match(tmp_path)


def test_stream_projection_recovers_aggregates_outside_cached_window(tmp_path):
    store = running_store(tmp_path)
    store.mutate(RUN, lambda row: row['node_states'].update({f'z{i:03}': {
        'status': 'completed', 'partial_text': 'result ' + str(i)} for i in range(65)}))
    for text in ('one', 'two'):
        store.flush_stream(RUN, 'executor', text=text, reasoning='', messages=[], return_snapshot=False)
        assert_views_match(tmp_path)


@pytest.mark.parametrize('status', ['paused', 'completed', 'failed'])
def test_active_checkpoint_rejects_terminal_transition(tmp_path, status):
    store = running_store(tmp_path)
    saved = checkpoint()
    saved['status'] = status
    with pytest.raises(ValueError, match='running stage'):
        store.record_running_stage(RUN, 'executor', saved)
