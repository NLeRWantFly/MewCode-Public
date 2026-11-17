"""General mutations index changed history while keeping callback state detached."""
import copy

import pytest

from mewcode.kernel import events
from mewcode.kernel.sqlite_state import RunDatabase
from tests.test_tool_performance import RUN, assert_views_match, running_store


def test_unrelated_mutation_does_not_scan_unchanged_history(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    store.append_sequenced_events(RUN, [dict(seq=i, type='expert_step', label=str(i))
        for i in range(1, 80)], return_snapshot=False)
    before = store.history_page(RUN, limit=100)['items']
    original = RunDatabase._model_signatures
    def checked(self, db, space, model):
        assert space != 'history', 'unchanged history was scanned'
        return original(self, db, space, model)
    monkeypatch.setattr(RunDatabase, '_model_signatures', checked)
    store.mutate(RUN, lambda row: row.update(status_reason='same execution, new explanation'))
    assert store.history_page(RUN, limit=100)['items'] == before
    assert_views_match(tmp_path)


def test_changed_event_is_reindexed_without_erasing_archived_events(tmp_path):
    store = running_store(tmp_path)
    store.append_sequenced_events(RUN, [dict(seq=i, type='expert_step', label=str(i))
        for i in range(1, 5)], return_snapshot=False)
    def update(row):
        row['events'] = row['events'][-1:]
        row['events'][0]['label'] = 'corrected label'
    store.mutate(RUN, update)
    history = store.history_page(RUN, limit=100)['items']
    assert [row['seq'] for row in history] == [1, 2, 3, 4]
    assert [row['label'] for row in history] == ['1', '2', '3', 'corrected label']
    assert RunDatabase(tmp_path, RUN).read('public')['events'][0]['label'] == 'corrected label'


def test_general_callback_alias_never_becomes_committed_cache(tmp_path):
    store = running_store(tmp_path)
    saved = {'messages': [{'content': 'saved'}]}
    retained = []
    def update(row):
        retained.append(row)
        row['node_states']['executor'].update(saved)
    returned = store.mutate(RUN, update)
    committed = RunDatabase(tmp_path, RUN).load()
    saved['messages'][0]['content'] = 'callback alias'
    returned['status'] = 'completed'
    retained[0]['status_reason'] = 'callback changed after return'
    assert events.EventJournal(tmp_path, RUN)._sql_load() == committed
    assert RunDatabase(tmp_path, RUN).load() == committed
    # A subsequent real mutation must compare with committed data, not assume
    # the callback-owned live object still equals the database.
    store.mutate(RUN, lambda row: row.update(status_reason='next checkpoint'))
    saved_again = RunDatabase(tmp_path, RUN).load()
    assert saved_again['node_states']['executor']['messages'][0]['content'] == 'callback alias'
    assert_views_match(tmp_path)


def test_general_mutations_keep_a_detached_warm_journal_cache(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('general mutation evicted the warm committed state')
    monkeypatch.setattr(RunDatabase, 'load', forbidden)
    store.mutate(RUN, lambda row: row.update(status_reason='first'))
    first = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    assert first is not store._live[RUN]
    store._live[RUN]['status_reason'] = 'callback alias'
    assert first['status_reason'] == 'first'
    store.mutate(RUN, lambda row: row.update(status_reason='second'))
    second = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    assert second['status_reason'] == 'second' and second is not first
    assert first['status_reason'] == 'first'


def test_general_mutation_failure_restores_live_cache_views_and_history(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    store.append_sequenced_events(RUN, [dict(seq=1, type='expert_step', label='saved')], return_snapshot=False)
    before = RunDatabase(tmp_path, RUN).load()
    live, models = store._live[RUN], store._read_model_cache[RUN]
    def fail(*args, **kwargs):
        raise OSError('injected history transaction failure')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    with pytest.raises(OSError, match='injected'):
        store.mutate(RUN, lambda row: row['events'][0].update(label='not saved'))
    assert store._live[RUN] is live and store._read_model_cache[RUN] is models
    assert events.EventJournal(tmp_path, RUN)._sql_load() == before
    assert RunDatabase(tmp_path, RUN).load() == before
    assert store.history_page(RUN)['items'][0]['label'] == 'saved'


@pytest.mark.parametrize('history', [[dict(type='expert_step', label='legacy')],
    [dict(type='expert_step', seq=2, label='two'), dict(type='expert_step', seq=1, label='one')],
    [dict(type='expert_step', seq=1, label='one'), dict(type='expert_step', seq=1, label='duplicate')]])
def test_unsequenced_compatibility_event_keeps_full_projection(tmp_path, history):
    store = running_store(tmp_path)
    store.mutate(RUN, lambda row: row['events'].extend(history))
    store.mutate(RUN, lambda row: row.update(status_reason='still readable'))
    assert_views_match(tmp_path)
