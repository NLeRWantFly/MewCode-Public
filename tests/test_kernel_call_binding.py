"""Exercise actual writes after binding, plus restart and authorization changes."""
import copy
import json

import pytest

from mewcode.experts.authorization_ledger import record_authorization
from mewcode.kernel.capabilities import ManifestGuard, compile_manifest
from mewcode.kernel.escalation import authorization_choice_options, apply_authorization_choice
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.tools.write_file import WriteFile, Params
from tests.test_kernel_authorization_choice import Store, flow, blocker, WRITE_CALL


def pending_write(tmp_path):
    store = Store({'authorization_manifest': compile_manifest(flow(), workspace=str(tmp_path), approved=True)})
    guard = ManifestGuard(store, 'run', 'N01')
    assert guard.check('WriteFile', WRITE_CALL['arguments'])
    _, pending = authorization_choice_options(store.record, 'N01')
    apply_authorization_choice(store.record, node_id='N01', blocked=pending,
                               choice='allow:' + pending[0][0].fact_id, source_ref='answer')
    assert guard.check('WriteFile', WRITE_CALL['arguments']) is None
    return store


def test_exact_answer_does_not_grant_other_content_file_tool_or_node(tmp_path):
    store = pending_write(tmp_path)
    for tool, arguments in [('WriteFile', {**WRITE_CALL['arguments'], 'content': 'different'}),
                            ('WriteFile', {'file_path': 'kb/00_inventory/other.md', 'content': 'x'}),
                            ('EditFile', WRITE_CALL['arguments'])]:
        assert ManifestGuard(store, 'run', 'N01').check(tool, arguments)
    store.record['authorization_manifest']['nodes']['N02'] = None
    assert ManifestGuard(store, 'run', 'N02').check('WriteFile', WRITE_CALL['arguments'])


def test_revocation_reopens_a_visible_blocker_after_clearance(tmp_path):
    store = pending_write(tmp_path)
    assert blocker(store.record, 'N01') is None
    store.record['authorizations']['entries'][0]['revoked_at'] = 1
    # Revocation must work even if a caller neglected to bump its version.
    assert ManifestGuard(store, 'run', 'N01').check('WriteFile', WRITE_CALL['arguments'])
    assert blocker(store.record, 'N01') is not None


def test_directory_denial_overrides_an_earlier_exact_call_grant(tmp_path):
    store = pending_write(tmp_path)
    record_authorization(store.record, kind='denial', scope={
        'operation_classes': ['write_outside_workspace'], 'paths': [(tmp_path / 'kb').as_posix()]},
        quote='不再允许该目录', source_ref='deny-directory')
    assert ManifestGuard(store, 'run', 'N01').check('WriteFile', WRITE_CALL['arguments'])
    assert blocker(store.record, 'N01') is not None


def test_snapshot_relative_write_uses_the_same_identity_after_answer(tmp_path):
    original, snapshot = tmp_path / 'original', tmp_path / 'snapshot'
    original.mkdir()
    snapshot.mkdir()
    store = Store({'authorization_manifest': compile_manifest(flow(), workspace=str(original), approved=True)})
    guard = ManifestGuard(store, 'run', 'N01', execution_workspace=str(snapshot))
    assert guard.check('WriteFile', WRITE_CALL['arguments'])
    _, pending = authorization_choice_options(store.record, 'N01')
    apply_authorization_choice(store.record, node_id='N01', blocked=pending,
                               choice='allow:' + pending[0][0].fact_id, source_ref='snapshot-answer')
    assert guard.check('WriteFile', WRITE_CALL['arguments']) is None
    assert blocker(store.record, 'N01') is None
    assert guard.check('WriteFile', {'file_path': str(original / 'kb' / 'escape.md'), 'content': 'x'})


@pytest.mark.asyncio
async def test_three_inventory_files_are_written_and_restart_reuses_directory_grant(tmp_path):
    store = Store({'authorization_manifest': compile_manifest(flow(), workspace=str(tmp_path), approved=True)})
    record_authorization(store.record, kind='gate_answer', scope={
        'operation_classes': ['write_outside_workspace'], 'paths': [(tmp_path / 'kb').as_posix()]},
        quote='允许输出目录', source_ref='directory:1', host_verified=True)
    tool = WriteFile()
    outputs = {'book_inventory.json': '{"books": 4}', 'batch_manifest.json': '{"pages": [3]}',
               'README.md': '# Inventory'}
    for i, (name, content) in enumerate(outputs.items()):
        if i == 1:
            # The store is reconstructed from serialized state, as on restart.
            store = Store(json.loads(json.dumps(store.record)))
        path = tmp_path / 'kb' / '00_inventory' / name
        arguments = {'file_path': str(path), 'content': content}
        assert ManifestGuard(store, 'run', 'N01').check('WriteFile', arguments) is None
        result = await tool.execute(Params(**arguments))
        assert not result.is_error
        assert path.read_text(encoding='utf-8') == content
        assert authorization_choice_options(store.record, 'N01') == ([], [])
    assert ManifestGuard(store, 'run', 'N01').check('WriteFile', {
        'file_path': str(tmp_path / 'outside.md'), 'content': 'x'})
    assert not (tmp_path / 'outside.md').exists()


def test_task_scope_is_tool_specific_and_live_removal_revokes_binding(tmp_path):
    scope = TaskAuthorization.from_user_scope({'write_directories': [str(tmp_path / 'kb')],
        'allowed_tools': ['WriteFile'], 'ttl_seconds': 3600}, task_id='task-1')
    current = [scope]
    store = Store({'authorization_manifest': compile_manifest(flow(), workspace=str(tmp_path), approved=True)})
    guard = ManifestGuard(store, 'run', 'N01', task_authorization_reader=lambda: (current[0], 'task-1'))
    assert guard.check('WriteFile', WRITE_CALL['arguments']) is None
    assert guard.check('EditFile', WRITE_CALL['arguments'])
    current[0] = None
    assert guard.check('WriteFile', WRITE_CALL['arguments'])
    assert blocker(store.record, 'N01') is not None


def test_stale_answer_cannot_clear_a_replaced_fact(tmp_path):
    store = Store({'authorization_manifest': compile_manifest(flow(), workspace=str(tmp_path), approved=True)})
    guard = ManifestGuard(store, 'run', 'N01')
    guard.check('WriteFile', WRITE_CALL['arguments'])
    _, pending = authorization_choice_options(store.record, 'N01')
    from mewcode.experts.node_ledger import ledger_for_run
    ledger_for_run(store.record).record('N01', 'clearance', {
        'blocker_fact_id': pending[0][0].fact_id, 'clearance_type': 'explicit_permission_receipt'})
    before = copy.deepcopy(store.record.get('authorizations'))
    apply_authorization_choice(store.record, node_id='N01', blocked=pending,
                               choice='allow:' + pending[0][0].fact_id, source_ref='stale')
    assert store.record.get('authorizations') == before
