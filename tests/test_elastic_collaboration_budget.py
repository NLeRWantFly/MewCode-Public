"""Elastic allocation uses one conserved root pool, not invented grants."""
import copy
import json

import pytest

from mewcode.kernel import collaboration as ledger, lineage_budget, collaboration_recovery as recovery
from mewcode.kernel import collaboration_graph as graph
from mewcode.kernel.collaboration_work_budget import expansion_capacity
from mewcode.orchestration.task_view import _budget_summary
from tests.test_collaboration_mode import RUN_ID, core, store_for, flow
from tests.test_decomposition_budget import fresh
from tests.test_autonomous_publication import publish_graph


@pytest.mark.parametrize('kind', lineage_budget.KINDS)
def test_elastic_uses_shared_pool_and_keeps_root_cap_atomic(kind):
    value = core()
    value['policy']['lineage_budget_mode'] = 'elastic'
    value['lineage_budget']['limits']['lineage'][kind] = 1
    value['lineage_budget']['limits']['root'][kind] = 4
    lineage_budget.reserve(value, 'first', lineage_id='work', kind=kind, amount=3)
    lineage_budget.settle(value, 'first')
    lineage_budget.reserve(value, 'sibling', lineage_id='other', kind=kind)
    before = copy.deepcopy(value)
    with pytest.raises(lineage_budget.BudgetExhausted) as error:
        lineage_budget.reserve(value, 'too-many', lineage_id='work', kind=kind)
    assert error.value.scope == 'root' and value == before
    assert value['lineage_budget']['lineages']['work']['used'][kind] == 3
    assert not value['lineage_budget']['grants']


def test_split_reserves_whole_cost_from_shared_pool(tmp_path):
    record, attempt, fragment = fresh(tmp_path, 1)
    value = ledger.state(record)
    value['policy']['lineage_budget_mode'] = 'elastic'
    value['lineage_budget']['limits']['root']['attempts'] = 4
    before = copy.deepcopy(value)
    capacity = expansion_capacity(value, 'work')
    assert capacity['allocation_mode'] == 'elastic'
    assert capacity['available_attempts'] == 3 and capacity['max_children'] == 2
    assert capacity['advisory_lineage_remaining'] == 0 and value == before
    item = graph.prepare(record, fragment, publication_id='publication-one', attempt_id=attempt['id'])
    publish_graph(record)
    value = ledger.state(record)
    assert len(item['attempt_reservations']) == 3
    assert value['lineage_budget']['root']['reserved']['attempts'] == 3
    assert not value['lineage_budget']['grants']
    row = _budget_summary(value, 'work')[0]
    assert row['allocation_mode'] == 'elastic' and row['limit'] is None
    assert row['nominal_limit'] == 1 and row['shared_remaining'] == 0


def test_elastic_retries_past_local_count_but_not_task_total():
    value = core()
    value['policy']['lineage_budget_mode'] = 'elastic'
    value['lineage_budget']['limits']['root']['attempts'] = 5
    value['lineage_budget']['limits']['lineage']['attempts'] = 1
    for _ in range(5):
        attempt = ledger.dispatch(value, 'work')
        ledger.mark_started(value, attempt['id'])
        ledger.complete(value, attempt['id'], {'passed': False})
    assert len(ledger.failed_attempts(value, 'work')) == 5
    assert not ledger.retry_exhausted(value, 'work')
    with pytest.raises(lineage_budget.BudgetExhausted) as error:
        ledger.dispatch(value, 'work')
    assert error.value.scope == 'root'
    assert len(value['attempts']) == 5


def _gate_record(scope='lineage'):
    value = core()
    value['lineage_budget']['limits']['lineage']['attempts'] = 1
    attempt = ledger.dispatch(value, 'work')
    ledger.mark_started(value, attempt['id'])
    ledger.complete(value, attempt['id'], {'passed': False})
    record = {'run_id': RUN_ID, 'collaboration': value, 'status': 'blocked', 'kernel_calls': {}}
    gate = recovery.open_gate(record, scope='contract:work', keys=['work'], label='额度不足', reason='本地额度不足',
        kind='lineage_budget_exhausted', budget=dict(kind='attempts', scope=scope, lineage_id='work', requested=3, available=0))
    return record, gate


def _enable(record, gate, **overrides):
    args = dict(gate_id=gate['id'], expected_version=1, request_id='elastic-choice', action='enable_elastic_budget',
                reason='用户选择在总任务预算内弹性分配', actor='user')
    args.update(overrides)
    return recovery.resolve_gate(record, **args)


def test_user_can_enable_elastic_without_rewriting_history_or_limits():
    record, gate = _gate_record()
    before = copy.deepcopy(record['collaboration'])
    decision = _enable(record, gate)
    value = record['collaboration']
    for key in ('attempts', 'certificates', 'contracts', 'entries', 'lineage_budget'):
        assert value[key] == before[key]
    assert decision['budget_policy'] == {'before': 'fixed', 'after': 'elastic', 'scope': 'run'}
    assert 'elastic_budget' in value['required_capabilities']
    assert _enable(record, gate) == decision
    restored = json.loads(json.dumps(record))
    assert lineage_budget.elastic(restored['collaboration'])
    lineage_budget.reserve(restored['collaboration'], 'new-work', lineage_id='work', kind='attempts', amount=3)


@pytest.mark.parametrize('other_kind', ['attempts_exhausted', 'lineage_budget_exhausted'])
def test_switch_retires_other_local_quota_gates_but_preserves_real_blockers(other_kind):
    record, gate = _gate_record()
    extra = {'budget': dict(kind='attempts', scope='lineage', lineage_id='deliver', requested=1, available=0)} if other_kind == 'lineage_budget_exhausted' else {}
    local = recovery.open_gate(record, scope='contract:deliver', keys=['deliver'], label='局部限制',
        reason='另一个分支的局部额度', kind=other_kind, **extra)
    root = recovery.open_gate(record, scope='task-budget', keys=['deliver'], label='总额度', reason='总额度',
        kind='lineage_budget_exhausted', budget=dict(kind='attempts', scope='root', lineage_id='deliver', requested=1, available=0))
    stalled = recovery.open_gate(record, scope='milestone:other', keys=['deliver'], label='无进展', reason='没有新的可行路线', kind='no_progress')
    decision = _enable(record, gate)
    gates = record['collaboration']['recovery_gates']
    assert decision['superseded_local_budget_gates'] == [local['id']]
    assert gates[local['id']]['status'] == 'superseded'
    assert gates[root['id']]['status'] == gates[stalled['id']]['status'] == 'waiting'


@pytest.mark.parametrize('failure', ['actor', 'unknown', 'root', 'amount', 'no-root-room', 'model'])
def test_elastic_recovery_cannot_bypass_other_boundaries(failure):
    record, gate = _gate_record('root' if failure == 'root' else 'lineage')
    args = {}
    if failure == 'actor': args['actor'] = 'model'
    if failure == 'amount': args['budget_amount'] = 100
    if failure == 'model': args['model'] = {'provider_key': 'other', 'model_id': 'other', 'effort': ''}
    if failure == 'unknown': record['kernel_calls']['unknown'] = {'node_id': 'deliver', 'state': 'unknown'}
    if failure == 'no-root-room': record['collaboration']['lineage_budget']['limits']['root']['attempts'] = 1
    before = copy.deepcopy(record)
    with pytest.raises(ValueError): _enable(record, gate, **args)
    assert record == before


def test_new_run_defaults_to_elastic_without_changing_compiled_identity(tmp_path):
    from mewcode.orchestration.collab_compile import compile_plan
    store = store_for(tmp_path)
    record = store.load(RUN_ID)
    assert lineage_budget.elastic(record['collaboration'])
    before = copy.deepcopy(record)
    ledger.initialize(record, compile_plan(flow()))
    assert record == before
    legacy = {'run_id': RUN_ID, 'collaboration': core()}
    before = copy.deepcopy(legacy)
    ledger.initialize(legacy, compile_plan(flow()))
    assert legacy == before and not lineage_budget.elastic(legacy['collaboration'])
