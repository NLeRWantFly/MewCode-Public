"""Restoring accepted work must not copy unrelated execution history."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mewcode.experts.primary_repair import record_primary_verification
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import Acceptance
from mewcode.kernel.escalation import KernelScheduler
from mewcode.kernel.work_continuation import segment_progress
from mewcode.orchestration.collab_runtime import CollaborationRuntime
from tests.test_collaboration_mode import RUN_ID, passing_report, store_for


class History(list):
    def __deepcopy__(self, memo):
        raise AssertionError('unrelated historical data was copied')


def accepted_store(tmp_path):
    store = store_for(tmp_path)
    path = tmp_path / 'report.txt'
    path.write_text('actual report', encoding='utf-8')
    acceptance = Acceptance(tmp_path)
    artifacts = acceptance.freeze({'artifacts': [{'path': 'report.txt'}]})
    def seed(record):
        value = ledger.state(record)
        record['node_states'].setdefault('work', {})
        record['node_states'].setdefault('deliver', {})
        for key in ('work', 'deliver'):
            attempt = ledger.dispatch(value, key)
            report = passing_report(value, key)
            report['artifacts'] = artifacts
            ledger.complete(value, attempt['id'], report)
    store.mutate(RUN_ID, seed, durable=True)
    runtime = object.__new__(CollaborationRuntime)
    runtime.store, runtime.run_id, runtime.acceptance = store, RUN_ID, acceptance
    runtime.workspaces = SimpleNamespace(current_commit=Mock(return_value=True))
    return store, runtime, path


def test_scoped_scheduler_reads_match_existing_checks_without_history_copies(tmp_path, monkeypatch):
    store, runtime, _ = accepted_store(tmp_path)
    value = store.read_collaboration(RUN_ID)
    expected = store.read_segment_progress(RUN_ID, 'work')
    live = store._live[RUN_ID]
    live['kernel_attempt_archives'] = History()
    live['collaboration']['timeline'] = History()
    for attempt in live['collaboration']['attempts'].values():
        attempt['delivery'] = {'activity_records': History()}
    for stage in live['node_states'].values():
        stage['messages'] = History()
    monkeypatch.setattr(store, 'load', lambda *a: pytest.fail('full state read'))
    monkeypatch.setattr(store, 'mutate', lambda *a, **k: pytest.fail('read-only check wrote state'))
    assert runtime.read('contracts', 'policy') == {key: value[key] for key in ('contracts', 'policy')}
    assert runtime.accepted('work') and runtime.accepted('deliver')
    assert store.read_segment_progress(RUN_ID, 'work') == expected
    assert not store.read_current_certificate(RUN_ID, 'missing')
    record_primary_verification(store, RUN_ID, {'node_id': 'work', 'status': 'completed'})


def test_scoped_results_are_detached_and_follow_new_dependency_state(tmp_path):
    store, _, _ = accepted_store(tmp_path)
    snapshot = store.load(RUN_ID)
    metadata = store.read_collaboration_fields(RUN_ID, 'contracts', 'unknown')
    metadata['contracts']['work']['goal'] = 'caller changed'
    certificate = store.read_current_certificate(RUN_ID, 'work')
    certificate['certificate']['artifacts'].clear()
    fields = store.read_stage_fields(RUN_ID, 'work', 'status', 'tools')
    fields['tools'] = {'fake': {}}
    progress = store.read_segment_progress(RUN_ID, 'work')
    progress['after']['submitted_items'].append('fake')
    assert store.load(RUN_ID) == snapshot
    store.mutate_collaboration(RUN_ID, lambda value: value['contracts']['work'].update(goal='new requirement'))
    assert store.read_current_certificate(RUN_ID, 'work') is None
    assert store.read_current_certificate(RUN_ID, 'deliver') is None


@pytest.mark.parametrize('change', ['physical_file', 'upstream', 'workspace', 'manual_provenance'])
def test_certificate_projection_preserves_invalidation_checks(tmp_path, change):
    store, runtime, path = accepted_store(tmp_path)
    target = 'work'
    if change == 'physical_file':
        path.write_text('changed outside the host', encoding='utf-8')
    elif change == 'upstream':
        store.mutate_collaboration(RUN_ID, lambda value: value['certificates']['work'].update(invalidated=True))
        target = 'deliver'
    elif change == 'workspace':
        def bind(value):
            value['attempts'][value['certificates']['work']['attempt_id']]['workspace'] = {'commit': 'old'}
        store.mutate_collaboration(RUN_ID, bind)
        runtime.workspaces.current_commit.return_value = False
    else:
        store.mutate_collaboration(RUN_ID, lambda value: value['certificates']['work'].update(
            accepted_by='user', human_decision_id='not-a-real-decision'))
    assert not runtime.accepted(target)
    assert not ledger.valid_certificate(store.read_collaboration(RUN_ID), target)


def test_progress_projection_keeps_authority_blockers_and_saved_evidence(tmp_path):
    store, _, _ = accepted_store(tmp_path)
    from mewcode.experts.node_ledger import ledger_for_run
    def seed(record):
        stage = record['node_states']['work']
        stage.update(slice_progress_baseline={'review_complete': 0}, primary_repairs=[{'status': 'waiting'}])
        ledger_for_run(record).record('work', 'blocker', {'kind': 'permission', 'ref': 'denied-write'})
        stage['tools'] = {'read': {'name': 'ReadFile', 'status': 'completed', 'args': {'file_path': 'report.txt'},
                                  'result_sha256': '1' * 64}}
    store.mutate(RUN_ID, seed)
    snapshot = store.load(RUN_ID)
    result = store.read_segment_progress(RUN_ID, 'work')
    assert result == segment_progress(snapshot, 'work')
    assert result['continuation_blockers'][0]['kind'] == 'permission'
    assert result['after']['observed_sources']
    assert store.load(RUN_ID) == snapshot


@pytest.mark.parametrize('status', ['completed', 'failed'])
def test_primary_verification_still_records_only_current_bound_repairs(tmp_path, status):
    store = store_for(tmp_path)
    def seed(record):
        record['node_states'].setdefault('work', {}).update(input_revision='now', primary_repairs=[
            {'status': 'ready_to_verify', 'input_revision': 'now'},
            {'status': 'ready_to_verify', 'input_revision': 'old'}])
    store.mutate(RUN_ID, seed)
    record_primary_verification(store, RUN_ID, {'node_id': 'work', 'status': status, 'error_kind': 'error'})
    repairs = store.read_stage_fields(RUN_ID, 'work', 'primary_repairs')['primary_repairs']
    assert repairs[0]['status'] == ('verified' if status == 'completed' else 'verification_failed')
    assert repairs[1] == {'status': 'ready_to_verify', 'input_revision': 'old'}


@pytest.mark.asyncio
async def test_verification_callback_does_not_block_the_event_loop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def verify(*args):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr('mewcode.experts.primary_repair.record_primary_verification', verify)
    scheduler = object.__new__(KernelScheduler)
    scheduler.store, scheduler.run_id = None, RUN_ID
    task = asyncio.create_task(scheduler.record_result({'status': 'completed'}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.wait_for(asyncio.sleep(0), 0.2)
        assert not task.done()
    finally:
        release.set()
    await task
