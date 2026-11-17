"""Explicit node retries must not deadlock on an unstarted technical helper."""
import copy
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.collaboration_recovery import open_gate, resolve_gate
from tests.test_collaboration_mode import RUN_ID, core, delivery
from tests.test_collaboration_recovery import store_for, orchestrator, run, decide


def request(record):
    stage = record.setdefault('node_states', {}).setdefault('work', {})
    pending = dict(request_id='technical-request', node_id='work', epoch=0, origin='node',
        kind='capability', status='pending', need='Write the retained report using the current guard',
        evidence_refs=['failed-write'], requested_tools=['WriteFile', 'ReadFile'], scope_requests=[])
    stage['assistance_request'] = pending
    stage['tools'] = {'failed-write': {'name': 'WriteFile', 'is_error': True, 'status': 'failed',
        'host_observation': {'operation_blocked': True, 'phase': 'before_effect'}}}
    facts = ledger_for_run(record)
    facts.record('work', 'assistance', {**pending, 'status': 'requested'}, source='node')
    facts.record('work', 'blocker', {'kind': 'capability_missing', 'ref': 'old-guard'})
    return pending


def blocked_record():
    value = core()
    attempt = ledger.dispatch(value, 'work')
    value['attempts'][attempt['id']]['delivery'] = asdict(ExpertRoleResult(
        text='awaiting assistance', status='needs_attention', error_kind='assistance_requested'))
    record = dict(run_id=RUN_ID, collaboration=value, status='blocked', node_states={})
    request(record)
    gate = open_gate(record, scope='contract:work', keys=['work'], label='retry', reason='host fixed', kind='infrastructure')
    return record, dict(gate_id=gate['id'], expected_version=gate['spec_version'],
        request_id='user-retry', action='retry', reason='Retry the original node after the repair', actor='user')


def test_explicit_retry_preserves_evidence_and_is_not_a_clearance():
    record, args = blocked_record()
    before = copy.deepcopy(record)
    decision = resolve_gate(record, **args)
    stage = record['node_states']['work']
    assert stage['assistance_request'] == {**before['node_states']['work']['assistance_request'],
        'status': 'superseded', 'superseded_by_decision': decision['id']}
    assert stage['tools'] == before['node_states']['work']['tools']
    assert not stage.get('assistance_receipts')
    assert ledger_for_run(record).view('work').active_blockers == ledger_for_run(before).view('work').active_blockers
    for key in ('contracts', 'entries', 'certificates', 'lineage_budget'):
        assert record['collaboration'].get(key) == before['collaboration'].get(key)
    once = copy.deepcopy(record)
    assert resolve_gate(record, **args) == decision and record == once


@pytest.mark.parametrize('boundary', ['permission', 'effect_verification', 'investigation', 'scope',
    'running', 'interrupted', 'checkpoint', 'helper', 'receipt', 'epoch', 'node', 'origin',
    'unproven', 'authority', 'business', 'pending_permission', 'question', 'unknown_effect', 'model_actor'])
def test_retry_cannot_cancel_authority_or_started_assistance(boundary):
    record, args = blocked_record()
    stage = record['node_states']['work']
    pending = stage['assistance_request']
    if boundary in {'permission', 'effect_verification', 'investigation'}:
        pending['kind'] = boundary
    elif boundary in {'running', 'interrupted'}:
        pending['status'] = boundary
    elif boundary == 'scope':
        pending['scope_requests'] = [{'operation_class': 'other', 'scope': {'tools': ['WriteFile']}}]
    elif boundary == 'checkpoint':
        stage['assistance_checkpoint'] = {'request_id': pending['request_id']}
    elif boundary == 'helper':
        record['node_states']['work__assist_' + pending['request_id'][:12]] = {}
    elif boundary == 'receipt':
        stage['assistance_receipts'] = [{'request_id': pending['request_id']}]
    elif boundary in {'epoch', 'node', 'origin'}:
        pending[{'node': 'node_id'}.get(boundary, boundary)] = 1 if boundary == 'epoch' else 'other'
    elif boundary == 'unproven':
        record['node_ledgers']['work']['facts'] = []
    elif boundary in {'authority', 'business'}:
        ledger_for_run(record).record('work', 'blocker', {'kind': 'permission' if boundary == 'authority' else 'user_input'})
    elif boundary == 'pending_permission':
        stage['pending_permission'] = {'operation': 'write'}
    elif boundary == 'question':
        record['pending_interaction'] = {'kind': 'ask_user'}
    elif boundary == 'unknown_effect':
        record['kernel_calls'] = {'call': {'node_id': 'work', 'state': 'unknown'}}
    elif boundary == 'model_actor':
        args['actor'] = 'coordinator'
    before = copy.deepcopy(stage)
    if boundary in {'unknown_effect', 'model_actor'}:
        whole = copy.deepcopy(record)
        with pytest.raises(ValueError):
            resolve_gate(record, **args)
        assert record == whole
    else:
        resolve_gate(record, **args)
    assert record['node_states']['work'] == before


@pytest.mark.asyncio
@pytest.mark.parametrize('still_blocked', [False, True])
async def test_retry_reaches_original_executor_without_replaying_helper(tmp_path, still_blocked):
    store = store_for(tmp_path)
    calls = []
    async def execute(node, model, prompt, policy, member):
        calls.append((node.node_id, model.as_dict()))
        if node.node_id == 'work' and len(calls) == 1:
            store.mutate(RUN_ID, request, durable=True)
            return ExpertRoleResult(text='old report guard failed', status='needs_attention', error_kind='assistance_requested')
        if node.node_id == 'work':
            assert store.read_turn_boundary(RUN_ID, 'work')['assistance_request']['status'] == 'superseded'
            if still_blocked:
                store.mutate(RUN_ID, request, durable=True)
                return ExpertRoleResult(text='current guard still denies', status='needs_attention', error_kind='assistance_requested')
        return delivery()
    host = orchestrator(store, tmp_path, execute, AsyncMock(return_value={'passed': True, 'evidence': ['verified']}))
    host.recovery_handler = AsyncMock(return_value={'action': 'human'})
    assert (await run(host, store)).status == 'blocked'
    before = copy.deepcopy(store.load(RUN_ID)['node_states']['work']['tools'])
    decide(store, tmp_path)
    host.recovery_handler = AsyncMock(return_value={'action': 'human'}) if still_blocked else AsyncMock(
        side_effect=AssertionError('unstarted helper must not consume explicit retry'))
    assert (await run(host, store)).status == ('blocked' if still_blocked else 'completed')
    assert [row[0] for row in calls] == (['work', 'work'] if still_blocked else ['work', 'work', 'deliver'])
    assert calls[0][1] == calls[1][1]
    assert host.recovery_handler.await_count == int(still_blocked)
    assert store.load(RUN_ID)['node_states']['work']['tools'] == before
