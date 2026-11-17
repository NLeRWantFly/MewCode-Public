"""Display reads reuse only the committed revision and never expose cache aliases."""
import copy

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel.sqlite_state import RunDatabase
from tests.test_tool_performance import RUN, running_store


def test_warm_display_avoids_database_tree_reads_and_is_detached(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    expected = store.read_workbench(RUN)
    def forbidden(*args, **kwargs):
        raise AssertionError('display reparsed the committed projection')
    monkeypatch.setattr(RunDatabase, '_read', forbidden)
    result = store.read_workbench(RUN)
    assert result == expected
    result['_workbench_view']['task']['status'] = 'caller changed'
    result['conversation_id'] = 'another conversation'
    assert store.read_workbench(RUN) == expected


def test_external_revision_uses_database_instead_of_stale_cache(tmp_path):
    store = running_store(tmp_path)
    before = store.read_workbench(RUN)
    other = ExpertRunStore(tmp_path, storage='sqlite')
    other.mutate(RUN, lambda row: row.update(status='paused', status_reason='new decision'))
    expected = other.read_workbench(RUN)
    assert before != expected
    assert store.read_workbench(RUN) == expected
    store._read_model_cache.clear()
    assert store.read_workbench(RUN) == expected


def test_failed_commit_cannot_publish_uncommitted_display(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    before = copy.deepcopy(store.read_workbench(RUN))
    def fail(*args, **kwargs):
        raise OSError('injected projection failure')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    with pytest.raises(OSError, match='injected'):
        store.mutate(RUN, lambda row: row.update(status='paused'))
    assert store.read_workbench(RUN) == before


def test_read_transaction_keeps_card_and_bindings_consistent_during_commit(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    expected = store.read_workbench(RUN)
    other = ExpertRunStore(tmp_path, storage='sqlite')
    original = store._read_model_cache
    class ConcurrentCache(dict):
        def get(self, key, default=None):
            other.mutate(RUN, lambda row: row.update(status='paused', conversation_id='changed'))
            return other._read_model_cache.get(key, default)
    store._read_model_cache = ConcurrentCache(original)
    # Revision was read before the competing commit; both tree reads must use
    # that same SQLite snapshot instead of mixing old bindings with a new card.
    assert store.read_workbench(RUN) == expected
