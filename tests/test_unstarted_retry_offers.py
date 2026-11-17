import copy

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.calls import canonical_invocation, propose
from mewcode.kernel.collaboration_recovery import open_gate, resolve_gate, unstarted_retry_gates
from mewcode.kernel.run_views import build_design
from mewcode.kernel.sqlite_state import RunDatabase
from mewcode.expert_runs import ExpertRunStore
from mewcode.orchestration.collab_service import user_recovery
from mewcode.orchestration.task_view import collaboration_projection
from tests.test_collaboration_mode import CATALOG, RUN_ID, store_for


def stopped(record):
    value = record['collaboration']
    attempt = ledger.dispatch(value, 'work')
    value['attempts'][attempt['id']]['delivery'] = {'status': 'needs_attention',
        'handoff': {'status': 'needs_attention', 'blocker_kind': 'capability_missing'}}
    record.update(status='blocked', active_node_ids=[])
    record['node_states'].setdefault('work', {}).update(status='needs_attention', input_revision=attempt['dependencies'])
    call = propose(record, 'work', attempt['dependencies'], canonical_invocation('WriteFile',
        {'file_path': 'report.txt', 'content': 'report'}))
    call.update(state='failed', result={'output': 'guard blocked before writing', 'is_error': True,
        'host_observation': {'operation_blocked': True, 'phase': 'before_effect'}})


def test_offer_is_read_only_and_service_materializes_exact_user_gate(tmp_path):
    store = store_for(tmp_path)
    store.mutate(RUN_ID, stopped, durable=True)
    record = store.load(RUN_ID)
    before = copy.deepcopy(record)
    offer, = unstarted_retry_gates(record)
    assert record == before
    assert unstarted_retry_gates(record) == [offer]
    assert collaboration_projection(record)['recovery_gates'][0]['id'] == offer['id']
    kwargs = dict(gate_id=offer['id'], expected_version=offer['spec_version'], request_id='explicit-retry',
        action='retry', reason='Retry with the repaired guard', work_dir=tmp_path, catalog=CATALOG)
    result = user_recovery(store, record, **kwargs)
    assert result['collaboration']['recovery_gates'][offer['id']]['status'] == 'resolved'
    assert result['kernel_calls'] == before['kernel_calls']
    assert set(result['kernel_call_retry_authorizations']) == set(before['kernel_calls'])
    assert unstarted_retry_gates(result) == []
    # A repeated host request is idempotent, including the already resolved offer.
    assert user_recovery(store, result, **kwargs)['kernel_call_retry_authorizations'] == result['kernel_call_retry_authorizations']


@pytest.mark.parametrize('change', ['running', 'active', 'unknown', 'succeeded', 'after_effect',
    'stale_attempt', 'stale_input', 'permission', 'business', 'held', 'existing_gate'])
def test_offer_requires_current_stopped_technical_non_execution(tmp_path, change):
    store = store_for(tmp_path)
    record = store.load(RUN_ID)
    stopped(record)
    call = next(iter(record['kernel_calls'].values()))
    attempt = next(iter(record['collaboration']['attempts'].values()))
    if change == 'running':
        record['status'] = 'running'
    elif change == 'active':
        record['active_node_ids'] = ['work']
    elif change in {'unknown', 'succeeded'}:
        call['state'] = change
    elif change == 'after_effect':
        call['result']['host_observation']['phase'] = 'after_effect'
    elif change == 'stale_attempt':
        attempt['superseded'] = True
    elif change == 'stale_input':
        record['node_states']['work']['input_revision'] = 'old'
    elif change == 'permission':
        record['node_states']['work']['pending_permission'] = {'id': 'permission'}
    elif change == 'business':
        attempt['delivery']['handoff']['blocker_kind'] = 'user_decision'
    elif change == 'held':
        record['collaboration']['contracts']['work']['held'] = True
    else:
        open_gate(record, scope='contract:work', keys=['work'], label='work', reason='existing', kind='infrastructure')
    before = copy.deepcopy(record)
    assert unstarted_retry_gates(record) == []
    assert record == before


def test_changed_host_proof_rejects_offer_before_and_after_materialization(tmp_path):
    store = store_for(tmp_path)
    store.mutate(RUN_ID, stopped, durable=True)
    record = store.load(RUN_ID)
    offer, = unstarted_retry_gates(record)
    def change(saved):
        next(iter(saved['kernel_calls'].values()))['result']['output'] = 'changed host evidence'
    store.mutate(RUN_ID, change, durable=True)
    before = store.load(RUN_ID)
    with pytest.raises(ValueError, match='没有找到'):
        user_recovery(store, record, gate_id=offer['id'], expected_version=offer['spec_version'],
            request_id='stale', action='retry', reason='retry', work_dir=tmp_path, catalog=CATALOG)
    assert store.load(RUN_ID) == before
    open_gate(record, keys=offer['node_ids'], **{key: offer[key] for key in
        ('scope', 'label', 'reason', 'kind', 'execution_evidence_fingerprint')})
    change(record)
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match='证据已经变化'):
        resolve_gate(record, gate_id=offer['id'], expected_version=offer['spec_version'],
            request_id='stale-persisted', action='retry', reason='retry', actor='user')
    assert record == before


def test_startup_upgrades_legacy_card_without_changing_execution(tmp_path):
    store = store_for(tmp_path)
    store.mutate(RUN_ID, stopped, durable=True)
    before = store.load(RUN_ID)
    legacy = build_design(before)
    legacy['version'] = 1
    legacy['collaboration']['recovery_gates'] = []
    database = RunDatabase(store.root, RUN_ID)
    history = database.page(after=0, limit=100)
    database.publish_models(before, {'design': legacy,
        'aux': dict(database.read('aux'), static_inbox=[])})
    restored = ExpertRunStore(store.root, storage='sqlite')
    assert restored.read_design(RUN_ID) is None
    assert restored.refresh_public_summaries() == 1
    assert restored.load(RUN_ID) == before
    offer, = restored.read_design(RUN_ID)['collaboration']['recovery_gates']
    assert offer['id'] == unstarted_retry_gates(before)[0]['id']
    assert restored.read_workbench(RUN_ID)['static_inbox'][0]['context']['id'] == offer['id']
    assert database.page(after=0, limit=100) == history
    assert restored.refresh_public_summaries() == 0
