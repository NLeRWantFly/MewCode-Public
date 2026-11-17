from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.sandbox import network_cleanup, srt_runtime
from mewcode.sandbox.cleanup_evidence import SCHEMA, BACKEND
from mewcode.sandbox.lpac_runtime import encode_request
from mewcode.sandbox.transaction_registry import TransactionRegistry


@pytest.fixture
def cleanup_case(tmp_path, monkeypatch):
    class Lock:
        def acquire(self): return True
        def close(self): pass
    monkeypatch.setattr(srt_runtime, '_RunnerLock', lambda *_: Lock())
    shared = tmp_path / 'shared'; shared.mkdir()
    monkeypatch.setattr(srt_runtime, '_network_shared_root', lambda: shared)
    original = tmp_path / 'original'; original.mkdir()
    transaction = tmp_path / 'stage-bounded'; (transaction / 'workspace').mkdir(parents=True)
    (transaction / 'cleanup').mkdir()
    registry = TransactionRegistry(tmp_path / 'registry')
    record = registry.begin(original, task_id='synthetic-task', policy_revision='unchanged')
    request_id = '0123456789abcdef0123456789abcdef'
    envelope = {'request_id': request_id, 'original_workspace': str(original),
                'request': {'workspace': str(transaction / 'workspace')}}
    frozen = encode_request(envelope)
    digest = hashlib.sha256(frozen).hexdigest()
    (transaction / 'cleanup/request.bin').write_bytes(frozen)
    (transaction / 'cleanup/receipt.json').write_text('ORIGINAL_FAILURE_EVIDENCE', encoding='utf-8')
    registry.update(record['id'], 'cleanup_unknown', transaction=str(transaction),
        request_id=request_id, request_sha256=digest,
        native_receipt={'backend': 'mewcode-windows-srt', 'cleanup_confirmed': False,
                        'status': 'spawn_failed', 'command_started': True})
    (shared / 'cleanup-unconfirmed.json').write_text(json.dumps({'request_id': request_id}))
    monkeypatch.setattr(network_cleanup, '_helper', lambda: Path('fixed-host-recovery.exe'))
    calls = []

    def recover(args, *, input, **kwargs):
        calls.append((args, input))
        assert input == frozen
        (transaction / 'cleanup/recovery-receipt.json').write_text(json.dumps({
            'schema': SCHEMA, 'backend': BACKEND, 'request_id': request_id, 'request_sha256': digest,
            'completed': True, 'job_cleanup_ok': True, 'profile_cleanup_ok': True, 'acl_cleanup_ok': True}),
            encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=b'{}')
    monkeypatch.setattr(network_cleanup.subprocess, 'run', recover)
    return registry, record['id'], transaction, shared, calls


def test_cleanup_recovers_only_the_bound_candidate_without_asserting_execution_success(cleanup_case):
    registry, identity, transaction, shared, calls = cleanup_case
    result = network_cleanup.recover_cleanup(registry)
    assert result['cleanup_confirmed'] and result['state'] == 'rejected'
    assert result['command_replayed'] is False
    assert len(calls) == 1
    assert not (shared / 'cleanup-unconfirmed.json').exists()
    assert not registry.pending()
    assert (transaction / 'cleanup/receipt.json').read_text(encoding='utf-8') == 'ORIGINAL_FAILURE_EVIDENCE'
    assert registry.get(identity)['native_receipt']['status'] == 'spawn_failed'


def test_frozen_request_mismatch_never_invokes_recovery_or_clears_the_marker(cleanup_case):
    registry, _, transaction, shared, calls = cleanup_case
    (transaction / 'cleanup/request.bin').write_bytes(b'changed')
    with pytest.raises(RuntimeError, match='哈希不匹配'):
        network_cleanup.recover_cleanup(registry)
    assert calls == []
    assert (shared / 'cleanup-unconfirmed.json').exists()


def test_interrupted_publication_remains_pending_after_only_cleanup(cleanup_case):
    registry, identity, transaction, _, _ = cleanup_case
    (transaction / 'publication').mkdir()
    (transaction / 'publication/plan.json').write_text('{}')
    result = network_cleanup.recover_cleanup(registry)
    assert result['state'] == 'recovery_required'
    assert registry.pending()[0]['id'] == identity
    assert registry.pending()[0]['native_receipt']['status'] == 'spawn_failed'
