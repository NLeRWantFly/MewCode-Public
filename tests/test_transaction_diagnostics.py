"""Inspect stranded host transactions without replay, recovery or disclosure."""
import json

import pytest

from mewcode.sandbox.cleanup_evidence import BACKEND, SCHEMA
from mewcode.sandbox.transaction_diagnostics import transaction_summary, workspace_transactions
from mewcode.sandbox.transaction_registry import TransactionRegistry, workspace_command_block


def stranded(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    private = tmp_path / 'private'
    (private / 'cleanup').mkdir(parents=True)
    registry = TransactionRegistry(tmp_path / 'host')
    monkeypatch.setattr('mewcode.sandbox.transaction_registry.default_root', lambda: registry.root)
    record = registry.begin(workspace, task_id='task', policy_revision='policy')
    record = registry.update(record['id'], 'cleanup_unknown', transaction=str(private),
        publication_mode='discard', metadata_broker_required=False, request_id='b' * 32,
        request_sha256='c' * 64, reason='RuntimeError', secret='never-export-this')
    return workspace, registry, private, record


def test_inspection_exposes_exact_missing_receipt_without_mutating_or_exporting_private_fields(tmp_path, monkeypatch):
    workspace, registry, private, record = stranded(tmp_path, monkeypatch)
    other = tmp_path / 'other'
    other.mkdir()
    registry.begin(other, task_id='other-task', policy_revision='other-policy')
    before = registry.path.read_bytes()
    monkeypatch.setattr(registry, '_db', lambda: pytest.fail('read must not open a write/schema connection'))
    assert registry.pending(workspace)[0]['id'] == record['id']
    result = workspace_transactions(workspace)
    assert result['available'] and result['read_only'] and result['total'] == 1
    item = result['items'][0]
    assert item['id'] == record['id'] and item['state'] == 'cleanup_unknown'
    assert item['diagnostic_code'] == 'cleanup_evidence_missing'
    assert item['private_only'] and not item['execution_receipt_present']
    assert item['command_started'] is None and item['cleanup_confirmed'] is None
    assert '无需再让用户截图' in result['guidance']
    assert 'never-export-this' not in json.dumps(result)
    assert str(private) not in json.dumps(result)
    assert registry.path.read_bytes() == before and workspace_command_block(workspace)


@pytest.mark.parametrize('valid', [True, False])
def test_cleanup_proof_is_identity_checked_but_never_settles_transaction(tmp_path, monkeypatch, valid):
    workspace, registry, private, record = stranded(tmp_path, monkeypatch)
    proof = {'schema': SCHEMA, 'backend': BACKEND, 'request_id': record['request_id'],
             'request_sha256': record['request_sha256'] if valid else 'd' * 64,
             'completed': True, 'job_cleanup_ok': True, 'profile_cleanup_ok': True, 'acl_cleanup_ok': True}
    (private / 'cleanup/receipt.json').write_text(json.dumps(proof), encoding='utf-8')
    result = transaction_summary(record, task_id='new-task')
    assert result['current_task'] is False
    assert result['cleanup_evidence'] == ('verified' if valid else 'invalid_or_unreadable')
    assert result['cleanup_confirmed'] is None
    assert registry.pending(workspace)[0]['state'] == 'cleanup_unknown'
    assert workspace_command_block(workspace)


def test_inspection_does_not_create_missing_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr('mewcode.sandbox.transaction_registry.default_root', lambda: tmp_path / 'absent')
    result = workspace_transactions(tmp_path)
    assert result['available'] and result['total'] == 0
    assert not (tmp_path / 'absent').exists()


def test_unreadable_ledger_is_unknown_not_clear(tmp_path, monkeypatch):
    workspace, registry, _, _ = stranded(tmp_path, monkeypatch)
    registry.path.write_bytes(b'corrupt database')
    result = workspace_transactions(workspace)
    assert result['available'] is False and 'total' not in result
    assert workspace_command_block(workspace)
