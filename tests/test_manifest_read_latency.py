"""Capability checks retain live grant semantics without reading run history."""
import copy
import time
from dataclasses import replace

import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.authorization_ledger import record_authorization
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.kernel.capabilities import ManifestGuard, compile_manifest
from mewcode.permissions.task_authorization import TaskAuthorization
from tests.test_tool_performance import RUN, assert_views_match, running_store


def fixture(root):
    store = running_store(root)
    flow = ExpertFlowSpec.from_value({'flow_id': 'scope', 'finalizer_node_id': 'executor',
        'nodes': [{'node_id': 'executor', 'role': 'executor', 'capabilities': {}}], 'edges': []})
    store.mutate(RUN, lambda row: row.update(authorization_manifest=compile_manifest(
        flow, workspace=str(root), approved=True)))
    return store, ManifestGuard(store, RUN, 'executor')


@pytest.mark.parametrize('node_id', ['executor', 'executor::member'])
def test_unchanged_manifest_checks_do_not_copy_messages_or_write(tmp_path, monkeypatch, node_id):
    store, guard = fixture(tmp_path)
    guard = ManifestGuard(store, RUN, node_id)
    assert guard.check('ReadFile', {'file_path': 'one.txt'}) is None
    class HistoricalContext(list):
        def __deepcopy__(self, memo):
            raise AssertionError('capability check copied execution history')
    live = store._live[RUN]
    live['kernel_attempt_archives'] = HistoricalContext()
    live['node_states']['executor']['messages'] = HistoricalContext()
    def forbidden(*args, **kwargs):
        raise AssertionError('unchanged permission check performed a full transaction')
    monkeypatch.setattr(store, 'load', forbidden)
    monkeypatch.setattr(store, 'mutate', forbidden)
    before = copy.deepcopy(store.read_authorization_state(RUN, 'executor'))
    for tool in ('ReadFile', 'Grep', 'Glob', 'ReadStageResult', 'ProbeCapabilities'):
        assert guard.check(tool, {'file_path': 'one.txt'}) is None
    scoped = store.read_authorization_state(RUN, 'executor')
    assert scoped == before
    scoped['authorization_manifest']['approved'] = False
    scoped['node_ledgers']['executor']['facts'].clear()
    assert store.read_authorization_state(RUN, 'executor') == before


def test_failed_binding_commit_does_not_allow_the_call(tmp_path, monkeypatch):
    from mewcode.kernel.sqlite_state import RunDatabase
    store, guard = fixture(tmp_path)
    store.mutate(RUN, lambda row: record_authorization(row, kind='gate_answer',
        scope={'operation_classes': ['write_outside_workspace'], 'paths': [(tmp_path/'allowed').as_posix()]},
        quote='Allow this directory', source_ref='grant', host_verified=True))
    before = RunDatabase(tmp_path, RUN).load()
    def fail(*args, **kwargs):
        raise OSError('injected binding commit failure')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    with pytest.raises(OSError, match='binding commit'):
        guard.check('WriteFile', {'file_path': 'allowed/result.txt', 'content': 'result'})
    assert RunDatabase(tmp_path, RUN).load() == before
    assert store.load(RUN) == before


@pytest.mark.parametrize('change', ['revoke', 'expire', 'deny'])
def test_scoped_guard_rechecks_changed_grants_and_keeps_blockers(tmp_path, change):
    store, guard = fixture(tmp_path)
    arguments = {'file_path': 'allowed/result.txt', 'content': 'result'}
    assert guard.check('WriteFile', arguments)
    scope = {'operation_classes': ['write_outside_workspace'], 'paths': [(tmp_path/'allowed').as_posix()]}
    def grant(row):
        record_authorization(row, kind='gate_answer', scope=scope, quote='Allow this directory',
                             source_ref='grant', host_verified=True)
    store.mutate(RUN, grant)
    assert guard.check('WriteFile', arguments) is None
    saved = assert_views_match(tmp_path)
    assert saved['authorization_manifest']['call_bindings']
    assert not ledger_for_run(saved).view('executor').active_blockers
    def withdraw(row):
        if change == 'deny':
            record_authorization(row, kind='denial', scope=scope, quote='Do not write', source_ref='deny')
        else:
            row['authorizations']['entries'][0]['revoked_at' if change == 'revoke' else 'expires_at'] = time.time()-1
    store.mutate(RUN, withdraw)
    assert guard.check('WriteFile', arguments)
    saved = assert_views_match(tmp_path)
    assert not saved['authorization_manifest']['call_bindings']
    assert any(f.payload['kind'] == 'missing_authorization'
               for f in ledger_for_run(saved).view('executor').active_blockers)


@pytest.mark.parametrize('change', ['revoked', 'expired', 'different_task'])
def test_live_task_scope_is_rechecked_even_for_readonly_calls(tmp_path, change):
    store, _ = fixture(tmp_path)
    authorization = TaskAuthorization.from_user_scope({'write_directories': [str(tmp_path/'allowed')],
        'allowed_tools': ['WriteFile'], 'ttl_seconds': 60}, task_id='task')
    current = [authorization, 'task']
    guard = ManifestGuard(store, RUN, 'executor', task_authorization_reader=lambda: tuple(current))
    arguments = {'file_path': 'allowed/result.txt', 'content': 'result'}
    assert guard.check('WriteFile', arguments) is None
    assert store.read_authorization_state(RUN, 'executor')['authorization_manifest']['call_bindings']
    if change == 'revoked':
        current[0] = None
    elif change == 'expired':
        current[0] = replace(authorization, expires_at=time.time()-1)
    else:
        current[1] = 'another-task'
    assert guard.check('ReadFile', {'file_path': 'one.txt'}) is None
    assert not store.read_authorization_state(RUN, 'executor')['authorization_manifest']['call_bindings']
    assert guard.check('WriteFile', arguments)
    assert_views_match(tmp_path)
