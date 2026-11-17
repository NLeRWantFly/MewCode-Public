"""Recovery metadata must not copy historical transcripts or relax authority."""
import copy

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel import events
from mewcode.kernel.preflight_binding import prepare_manifest
from mewcode.kernel.sqlite_state import RunDatabase
from mewcode.permissions.task_authorization import TaskAuthorization
from tests.test_tool_performance import RUN, assert_views_match, running_store


def authorization(path):
    return TaskAuthorization.from_user_scope({
        'write_directories': [str(path)], 'allowed_tools': ['WriteFile'],
    }, task_id='task')


def test_manifest_commit_matches_compiler_and_keeps_caller_values_detached(tmp_path):
    store = running_store(tmp_path)
    auth = authorization(tmp_path)
    before = store.load(RUN)
    expected = prepare_manifest(copy.deepcopy(before), before['locked_flow'], tmp_path, auth, 'task')
    result = store.prepare_authorization_manifest(RUN, workspace=tmp_path,
        task_authorization=auth, task_id='task')
    saved = assert_views_match(tmp_path)
    assert result == saved['authorization_manifest'] == expected
    result['nodes'].clear()
    assert store.load(RUN)['authorization_manifest'] == expected
    assert events.EventJournal(tmp_path, RUN)._sql_load()['authorization_manifest'] == expected


def test_repeated_preflight_and_flow_read_never_copy_archives_or_rewrite_state(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    store.prepare_authorization_manifest(RUN, workspace=tmp_path)
    class Historical(list):
        def __deepcopy__(self, memo):
            raise AssertionError('historical transcript was copied')
    live = store._live[RUN]
    live['kernel_attempt_archives'] = Historical()
    committed = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    committed['kernel_attempt_archives'] = Historical()
    def fail(*args, **kwargs):
        raise AssertionError('unchanged preflight wrote a new transaction')
    monkeypatch.setattr(RunDatabase, 'commit', fail)
    for _ in range(2):
        assert store.prepare_authorization_manifest(RUN, workspace=tmp_path) == live['authorization_manifest']
        flow = store.read_execution_flow(RUN)
        assert flow.as_dict() == live['locked_flow']
    assert store._live[RUN] is live


def test_changed_manifest_shares_history_but_revokes_removed_authority(tmp_path):
    store = running_store(tmp_path)
    store.prepare_authorization_manifest(RUN, workspace=tmp_path,
        task_authorization=authorization(tmp_path), task_id='task')
    before = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    frozen = copy.deepcopy(before)
    revoked = store.prepare_authorization_manifest(RUN, workspace=tmp_path, task_id='task')
    after = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    assert 'task_authorization' not in revoked and 'task_id' not in revoked
    assert before == frozen
    assert after['node_states'] is before['node_states']
    assert after['authorization_manifest'] is not revoked
    assert_views_match(tmp_path)


def test_failed_manifest_commit_preserves_all_caches_and_database(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    before = RunDatabase(tmp_path, RUN).load()
    live, models = store._live[RUN], store._read_model_cache[RUN]
    def fail(*args, **kwargs):
        raise OSError('manifest transaction failed')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    with pytest.raises(OSError, match='manifest transaction'):
        store.prepare_authorization_manifest(RUN, workspace=tmp_path)
    assert store._live[RUN] is live and store._read_model_cache[RUN] is models
    assert events.EventJournal(tmp_path, RUN)._sql_load() == before
    assert RunDatabase(tmp_path, RUN).load() == before


def test_unchanged_manifest_still_rejects_a_stale_writer(tmp_path):
    store = running_store(tmp_path)
    store.prepare_authorization_manifest(RUN, workspace=tmp_path)
    other = ExpertRunStore(tmp_path, storage='sqlite')
    other.mutate(RUN, lambda value: value.update(status_reason='new writer'))
    with pytest.raises(ValueError, match='stale'):
        store.prepare_authorization_manifest(RUN, workspace=tmp_path)


def test_legacy_manifest_uses_ordinary_durable_commit(tmp_path):
    store = ExpertRunStore(tmp_path)
    store.create(run_id=RUN, conversation_id='chat', task='restore', attachments=[],
                 profile={}, strategy='stable')
    result = store.prepare_authorization_manifest(RUN, workspace=tmp_path)
    assert events.EventJournal(tmp_path, RUN).load()['authorization_manifest'] == result


def test_resume_validation_failure_cannot_publish_partial_changes(tmp_path, monkeypatch):
    store = running_store(tmp_path, status='paused')
    live, models = store._live[RUN], store._read_model_cache[RUN]
    before = RunDatabase(tmp_path, RUN).load()
    def reject_late(candidate, strategy):
        assert candidate['status'] == 'running' and candidate['attempt'] == before['attempt'] + 1
        candidate['node_states']['executor']['messages'] = [{'content': 'uncommitted'}]
        raise ValueError('late resume validation')
    monkeypatch.setattr(store, '_start_attempt_inplace', reject_late)
    with pytest.raises(ValueError, match='late resume validation'):
        store.resume(RUN, strategy='stable')
    assert store._live[RUN] is live and store._read_model_cache[RUN] is models
    assert store.load(RUN) == before
    assert RunDatabase(tmp_path, RUN).load() == before
