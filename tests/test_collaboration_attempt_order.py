"""Object serialization order must not choose a different business attempt."""
import copy
from dataclasses import asdict

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel import collaboration as ledger
from mewcode.kernel import collaboration_recovery as recovery
from mewcode.kernel.events import EventJournal, _SQL_CACHE
from mewcode.orchestration.collab_service import user_recovery
from tests.test_collaboration_mode import CATALOG, PROFILE, RUN_ID, core, delivery, flow, store_for
from tests.test_collaboration_integration import orchestrator
from tests.test_collaboration_recovery import run


def legacy_latest(value, key):
    return next((row for row in reversed(list(value['attempts'].values()))
                 if row['contract_id'] == key), None)


def two_attempts():
    value = core()
    first = ledger.dispatch(value, 'work')
    ledger.complete(value, first['id'], {'passed': False, 'checks': [], 'artifacts': []})
    second = ledger.dispatch(value, 'work')
    value['attempts'][second['id']].update(status='checking', delivery=asdict(delivery()),
        acceptance_fault={'execution_error': 'provider_error', 'passed': False})
    # These actual generated hashes reproduce the production ordering inversion.
    assert second['id'] < first['id']
    value['attempts'] = dict(sorted(value['attempts'].items()))
    return {'run_id': RUN_ID, 'status': 'blocked', 'active_node_ids': [], 'collaboration': value}


def open_legacy_gate(record, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(recovery, 'latest_attempt', legacy_latest)
        return recovery.open_gate(record, scope='contract:work', keys=['work'],
            label='work', reason='verifier service unavailable', kind='infrastructure')


def test_cold_sqlite_read_keeps_gate_bound_to_latest_number(tmp_path):
    record = two_attempts()
    value = record['collaboration']
    gate = recovery.open_gate(record, scope='contract:work', keys=['work'],
        label='work', reason='verifier service unavailable', kind='infrastructure')
    journal = EventJournal(tmp_path, RUN_ID, storage='sqlite')
    journal.commit(record)
    _SQL_CACHE.clear()
    loaded = journal.load()['collaboration']
    assert legacy_latest(loaded, 'work')['number'] == 1
    assert recovery.latest_attempt(loaded, 'work')['number'] == 2
    assert [row['number'] for row in ledger.attempts_for(loaded, 'work')] == [1, 2]
    assert recovery.gate_current(loaded, gate)
    assert recovery.verifier_switch_attempt(loaded, gate)['number'] == 2
    assert loaded == value


def test_reconcile_proven_legacy_basis_requires_a_fresh_user_decision(monkeypatch):
    record = two_attempts()
    old = open_legacy_gate(record, monkeypatch)
    before = copy.deepcopy(record)
    assert not recovery.gate_current(record['collaboration'], old)
    repairs = recovery.reconcile_attempt_order_gates(record)
    assert len(repairs) == 1 and repairs[0]['previous_gate_id'] == old['id']
    value = record['collaboration']
    fresh = value['recovery_gates'][repairs[0]['gate_id']]
    assert value['recovery_gates'][old['id']]['status'] == 'superseded'
    assert fresh['status'] == 'waiting' and recovery.gate_current(value, fresh)
    assert record['status'] == before['status'] and record['active_node_ids'] == []
    assert {k: v for k, v in value.items() if k not in {'recovery_gates', 'timeline'}} == {
        k: v for k, v in before['collaboration'].items() if k not in {'recovery_gates', 'timeline'}}
    unchanged = copy.deepcopy(record)
    assert recovery.reconcile_attempt_order_gates(record) == []
    for gate_id, actor in ((old['id'], 'user'), (fresh['id'], 'assistant')):
        with pytest.raises(ValueError):
            recovery.resolve_gate(record, gate_id=gate_id, expected_version=1, request_id='no-bypass',
                action='retry', reason='not approved', actor=actor)
        assert record == unchanged


@pytest.mark.parametrize('change', ['running', 'active', 'unknown', 'held', 'epoch', 'old_attempt', 'kind'])
def test_reconcile_does_not_refresh_unrelated_staleness_or_active_work(monkeypatch, change):
    record = two_attempts()
    gate = open_legacy_gate(record, monkeypatch)
    value = record['collaboration']
    if change == 'running':
        record['status'] = 'running'
    elif change == 'active':
        record['active_node_ids'] = ['work']
    elif change == 'unknown':
        record['kernel_calls'] = {'call': {'node_id': 'work', 'state': 'unknown'}}
    elif change == 'held':
        value['contracts']['work']['held'] = True
    elif change == 'epoch':
        value['coordinator_epoch'] += 1
    elif change == 'old_attempt':
        legacy_latest(value, 'work')['report']['changed'] = True
    else:
        value['recovery_gates'][gate['id']]['kind'] = 'attempts_exhausted'
    before = copy.deepcopy(record)
    assert recovery.reconcile_attempt_order_gates(record) == []
    assert record == before


def test_reconcile_never_reopens_an_existing_superseded_gate(monkeypatch):
    record = two_attempts()
    recovery.open_gate(record, scope='contract:work', keys=['work'], label='work',
        reason='previously recorded', kind='infrastructure')
    open_legacy_gate(record, monkeypatch)
    before = copy.deepcopy(record)
    assert recovery.reconcile_attempt_order_gates(record) == []
    assert record == before


@pytest.mark.asyncio
async def test_restart_and_verifier_recovery_keep_second_delivery_without_reexecuting(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    value = core()
    first = ledger.dispatch(value, 'work')
    ledger.complete(value, first['id'], {'passed': False, 'checks': [], 'artifacts': []})
    store.mutate(RUN_ID, lambda record: record.update(collaboration=value), durable=True)
    invocations = []
    chosen = {'provider_key': 'other-verification', 'model_id': 'independent-verifier', 'effort': 'high'}
    catalog = {'models': CATALOG['models'] + [{**chosen, 'available': True, 'tools': True,
        'provider_brand': 'brand-c', 'efforts': ['high']}]}

    async def execute(node, *args):
        invocations.append(node.node_id)
        return delivery()

    async def review(model, payload, context):
        if payload['contract']['id'] == 'work' and model.provider_key != chosen['provider_key']:
            raise ConnectionError('INSUFFICIENT_BALANCE')
        return {'passed': True, 'evidence': ['independent checks']}

    assert (await run(orchestrator(store, tmp_path, execute, review), store)).status == 'blocked'
    def old_host_gate(record):
        value = record['collaboration']
        # The old host never created a number-ordered gate. Replace the new
        # host's test-generated gate to model that persisted legacy state.
        value['recovery_gates'] = {}
        value['attempts'] = dict(sorted(value['attempts'].items()))
        open_legacy_gate(record, monkeypatch)
    store.mutate(RUN_ID, old_host_gate, durable=True)
    before = store.load(RUN_ID)
    original_delivery = copy.deepcopy(recovery.latest_attempt(before['collaboration'], 'work')['delivery'])
    journal = EventJournal(store.root, RUN_ID)
    _, revision, proof = journal._scan()
    journal.migrate_sqlite(expected_revision=revision, expected_hash=proof)
    _SQL_CACHE.clear()
    restarted = ExpertRunStore(store.root)
    assert restarted.recover_stale_runs() == 0  # Repair is not a resume.
    saved = restarted.load(RUN_ID)
    gate = next(g for g in saved['collaboration']['recovery_gates'].values() if g['status'] == 'waiting')
    assert recovery.gate_current(saved['collaboration'], gate)
    assert recovery.verifier_switch_attempt(saved['collaboration'], gate)['number'] == 2
    for key in ('attempts', 'lineage_budget', 'certificates', 'human_decisions', 'acceptance_model_overrides'):
        assert saved['collaboration'].get(key) == before['collaboration'].get(key)
    assert restarted.recover_stale_runs() == 0
    assert restarted.load(RUN_ID)['kernel_revision'] == saved['kernel_revision']
    user_recovery(restarted, saved, gate_id=gate['id'], expected_version=1, request_id='explicit-verifier-choice',
        action='switch_verifier', reason='explicit test user choice', model=chosen, catalog=catalog, work_dir=tmp_path)
    assert recovery.latest_attempt(restarted.load(RUN_ID)['collaboration'], 'work')['delivery'] == original_delivery
    assert restarted.load(RUN_ID)['collaboration']['lineage_budget'] == saved['collaboration']['lineage_budget']
    restarted.resume(RUN_ID, strategy='stable')
    host = orchestrator(restarted, tmp_path, execute, review)
    result = await host.run('goal', PROFILE, catalog, strategy='stable', run_id=RUN_ID,
        flow=flow(), checkpoint=restarted.load(RUN_ID))
    assert result.status == 'completed'
    assert invocations == ['work', 'deliver']
