"""Loop notifications preserve recovery semantics without copying run history."""
import copy

import pytest

from mewcode.kernel import events, run_views
from mewcode.kernel.sqlite_state import RunDatabase
from tests.test_tool_performance import RUN, assert_views_match, running_store, receipt


def notification(**fields):
    return dict(type='expert_flow_state', phase='loop', loop_id='pages',
                loop_state_key='pages', node_id='executor', loop_contract={'loop_id': 'pages'},
                loop_state=dict(status='running', round=1, active_node='executor',
                                round_results={'reader': {'status': 'completed', 'text': 'private context'}},
                                **fields))


def test_loop_checkpoint_keeps_resume_payload_and_views_without_aliases(tmp_path):
    store = running_store(tmp_path)
    old = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    frozen = copy.deepcopy(old)
    event = notification(tokens_used=12)
    store.record_loop_state(RUN, event)
    saved = assert_views_match(tmp_path)
    assert old == frozen
    assert saved['loop_states']['pages'] == event['loop_state']
    event['loop_state']['round_results']['reader']['text'] = 'caller mutation'
    store._live[RUN]['loop_states']['pages']['round_results']['reader']['text'] = 'live mutation'
    assert events.EventJournal(tmp_path, RUN)._sql_load() == saved
    assert RunDatabase(tmp_path, RUN).load() == saved
    # The persisted event history still contains the bounded loop display.
    store.append_sequenced_events(RUN, [dict(notification(tokens_used=12), seq=1)], return_snapshot=False)
    history = store.history_page(RUN)['items']
    assert history[-1]['loop_state']['round_results']['reader'] == {'status': 'completed'}


def test_warm_loop_checkpoint_skips_unrelated_history_and_full_projection(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    store.record_loop_state(RUN, notification(tokens_used=10))
    class Uncopyable(list):
        def __deepcopy__(self, memo):
            raise AssertionError('copied unrelated historical context')
    for state in (events.EventJournal(tmp_path, RUN)._sql_load(detached=False), store._live[RUN]):
        state['kernel_attempt_archives'] = Uncopyable()
    def forbidden(*args, **kwargs):
        raise AssertionError('rebuilt unchanged plan/contract views')
    original = run_views.build
    monkeypatch.setattr(run_views, 'build', forbidden)
    store.record_loop_state(RUN, notification(tokens_used=11))
    store.append_sequenced_events(RUN, [dict(notification(tokens_used=11), seq=1)], return_snapshot=False)
    monkeypatch.setattr(run_views, 'build', original)
    assert_views_match(tmp_path)


def test_failed_loop_checkpoint_leaves_every_publication_unchanged(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    store.record_loop_state(RUN, notification(tokens_used=1))
    before = RunDatabase(tmp_path, RUN).load()
    live, cache = store._live[RUN], store._read_model_cache[RUN]
    def fail(*args, **kwargs):
        raise OSError('injected transaction failure')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    with pytest.raises(OSError, match='injected'):
        store.record_loop_state(RUN, notification(tokens_used=2))
    assert RunDatabase(tmp_path, RUN).load() == before
    assert events.EventJournal(tmp_path, RUN)._sql_load() == before
    assert store._live[RUN] is live and store._read_model_cache[RUN] is cache


def test_loop_checkpoint_syncs_new_tool_progress_and_recovery_facts(tmp_path):
    store = running_store(tmp_path)
    store.append_sequenced_events(RUN, [receipt(1), receipt(2, 'expert_tool_result',
        output='observed', host_observation={'file_state': {'text_sha256': 'proof'}})], return_snapshot=False)
    store.record_loop_state(RUN, notification(tokens_used=3))
    saved = assert_views_match(tmp_path)
    from mewcode.experts.node_ledger import ledger_for_run
    assert ledger_for_run(saved).view('executor').last_progress is not None


@pytest.mark.parametrize('status', ['blocked', 'completed'])
def test_stopped_loop_is_durable_and_cold_reload_matches(tmp_path, status):
    store = running_store(tmp_path)
    event = notification()
    event['loop_state'].update(status=status, active_node='', reason='checkpoint boundary')
    store.record_loop_state(RUN, event)
    events._SQL_CACHE.clear()
    assert assert_views_match(tmp_path)['loop_states']['pages']['status'] == status


def test_loop_checkpoint_mirrors_adaptive_receipt_once_and_rejects_conflict(tmp_path):
    from mewcode.orchestration.token_window import POLICY_VERSION
    store = running_store(tmp_path)
    event = notification(loop_resource_policy={'mode': 'adaptive', 'limit': 100000},
        token_window_receipts=[dict(policy_version=POLICY_VERSION, segment_id='segment-1',
            ceiling=100000, increases_total_budget=False, changes_contract=False, spent_tokens=2)])
    store.record_loop_state(RUN, event)
    store.record_loop_state(RUN, event)
    before = assert_views_match(tmp_path)
    assert len(before['loop_budget_changes']) == 1
    event['loop_state']['token_window_receipts'][0]['spent_tokens'] = 3
    with pytest.raises(ValueError, match='conflicting'):
        store.record_loop_state(RUN, event)
    assert RunDatabase(tmp_path, RUN).load() == before
