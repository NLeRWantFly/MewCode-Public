import copy
import json

import pytest

from mewcode.experts.node_ledger import ledger_for_run
from mewcode.kernel.briefs import archive_attempt
from mewcode.kernel.calls import propose, transition
from mewcode.kernel.collaboration_recovery import execution_recheck_gates
from mewcode.kernel.events import digest
from mewcode.kernel.execution_evidence import execution_evidence
from mewcode.kernel.reconcile import pre_dispatch_evidence, reconcile_pre_dispatch_effects
from mewcode.tools.read_run_archive import ReadRunArchive, ReadRunArchiveParams
from tests.test_execution_evidence import recorded_delivery


def stopped_call(name='WriteFile', *, archived=True):
    record, attempt = recorded_delivery()
    attempt['execution_evidence'] = execution_evidence(record, attempt)
    attempt['acceptance_input_evidence_hash'] = attempt['execution_evidence']['fingerprint']
    current = record['node_states']['work']
    args = ({'file_path': 'result.json', 'content': 'new'} if name == 'WriteFile' else
            {'file_path': 'result.json', 'old_string': 'old', 'new_string': 'new'})
    tool = {'name': name, 'tool_id': 'old-tool', 'args': args, 'epoch': 0, 'status': 'outcome_unknown'}
    old = {'node_id': 'work', 'input_revision': 'old-input', 'epoch': 0, 'tools': {'old-tool': tool}}
    record['node_states']['work'] = old
    archive = archive_attempt(record, 'work', force=True) if archived else None
    if archived:
        record['node_states']['work'] = current
    invocation = {'tool': name, 'arguments': copy.deepcopy(args)}
    if name == 'WriteFile':
        invocation['arguments'].update(encoding='utf-8', mode=None)
    call = propose(record, 'work', 'old-input', invocation)
    ledger_for_run(record).record('work', 'blocker', {'kind': 'unknown_effect', 'ref': 'old-tool'})
    return record, attempt, call, archive


@pytest.mark.parametrize('name', ['WriteFile', 'EditFile'])
def test_archived_proposal_clears_false_effect_without_rewriting_history_or_acceptance(name):
    record, attempt, call, archive = stopped_call(name)
    before = copy.deepcopy(record)
    assert len(reconcile_pre_dispatch_effects(record)) == 1
    assert ledger_for_run(record).view('work').active_blockers == ()
    assert record['kernel_attempt_archives'] == before['kernel_attempt_archives']
    assert record['kernel_calls'] == before['kernel_calls']
    assert attempt['report'] == before['collaboration']['attempts'][attempt['id']]['report']
    assert attempt['status'] == 'rejected'
    assert record['collaboration']['lineage_budget'] == before['collaboration']['lineage_budget']
    assert record['collaboration'] == before['collaboration']
    gate = execution_recheck_gates(record)[0]
    assert gate['kind'] == 'execution_evidence_available' and gate['status'] == 'waiting'
    evidence = execution_evidence(record, attempt)
    assert evidence['complete'] and evidence['fingerprint'] != attempt['acceptance_input_evidence_hash']
    fact = evidence['reconciled_effects'][0]
    assert fact['command_started'] is False and fact['effect'] == 'not_applied'
    assert fact['kernel_call_id'] == call['call_id'] and fact['archive_ids'] == [archive]
    assert 'new' not in fact and 'arguments' not in fact
    after = copy.deepcopy(record)
    assert reconcile_pre_dispatch_effects(record) == [] and record == after


@pytest.mark.parametrize('failure', ['dispatched', 'unknown', 'prior_dispatch', 'missing', 'wrong_input',
                                    'wrong_args', 'wrong_epoch', 'corrupt_archive', 'legacy', 'active', 'running'])
def test_uncertain_or_unbound_operation_is_never_cleared(failure):
    record, attempt, call, archive = stopped_call()
    if failure in {'dispatched', 'unknown', 'prior_dispatch'}:
        transition(call, 'reviewed')
        transition(call, 'dispatched', evidence={'executor_accepted': True, 'tool_id': 'old-tool'})
        if failure != 'dispatched':
            transition(call, 'unknown')
        if failure == 'prior_dispatch':
            transition(call, 'not_started', evidence={'host_verified': True, 'command_started': False})
            transition(call, 'proposed')
    elif failure == 'missing':
        record['kernel_calls'].clear()
    elif failure == 'wrong_input':
        call['input_revision'] = 'other-input'
    elif failure == 'wrong_args':
        call['invocation']['arguments']['content'] = 'different'
    elif failure == 'wrong_epoch':
        data = record['kernel_attempt_archives'][archive]['data']
        data['epoch'] = 2
        record['kernel_attempt_archives'][archive]['data_digest'] = digest(data)
    elif failure == 'corrupt_archive':
        record['kernel_attempt_archives'][archive]['data_digest'] = 'bad'
    elif failure == 'legacy':
        call['origin'] = 'legacy'
    elif failure == 'active':
        record['active_node_ids'] = ['work']
    else:
        record['status'] = 'running'
    before = copy.deepcopy(record)
    assert reconcile_pre_dispatch_effects(record) == []
    assert record == before


def test_cancelled_tool_start_is_not_dispatch_and_unrelated_permission_stays():
    record, _, call, _ = stopped_call(archived=False)
    stage = record['node_states']['work']
    tool = stage['tools']['old-tool']
    tool['status'] = 'running'
    proof = pre_dispatch_evidence(record, 'work', stage, 'old-tool', tool)
    assert proof['command_started'] is False
    ledger_for_run(record).record('work', 'blocker', {'kind': 'permission', 'ref': 'other'})
    assert reconcile_pre_dispatch_effects(record)
    assert stage['tools']['old-tool']['status'] == 'not_executed'
    assert [f.payload['kind'] for f in ledger_for_run(record).view('work').active_blockers] == ['permission']


def test_superseded_reconciliation_is_removed_from_cached_acceptance_evidence():
    record, attempt, _, _ = stopped_call()
    reconcile_pre_dispatch_effects(record)
    attempt['execution_evidence'] = execution_evidence(record, attempt)
    ledger = ledger_for_run(record)
    clearance = next(fact for fact in ledger.view('work').history if fact.kind == 'clearance')
    replacement = ledger.record('work', 'maintenance', {'reason': 'withdraw incorrect clearance'})
    ledger.supersede(clearance.fact_id, replacement.fact_id)
    evidence = execution_evidence(record, attempt)
    assert 'reconciled_effects' not in evidence
    assert ledger.view('work').active_blockers
    assert evidence['fingerprint'] != attempt['execution_evidence']['fingerprint']


def test_new_host_evidence_replaces_coordination_wait_in_existing_recovery_path(tmp_path):
    from mewcode.kernel.collaboration_recovery import open_gate
    from mewcode.orchestration.task_view import collaboration_projection
    from mewcode.orchestration.collab_service import user_recovery
    from tests.test_collaboration_recovery import store_for, RUN_ID
    record, attempt, _, _ = stopped_call()
    old = open_gate(record, scope='contract:work', keys=['work'], label='Work',
                    reason='Unresolved old call', kind='coordination_wait')
    assert execution_recheck_gates(record) == []
    reconcile_pre_dispatch_effects(record)
    before = copy.deepcopy(record)
    shown = collaboration_projection(record)['recovery_gates']
    assert record == before and len(shown) == 1
    assert shown[0]['kind'] == 'execution_evidence_available'
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda saved: saved.update(record), durable=True)
    user_recovery(store, store.load(RUN_ID), gate_id=shown[0]['id'], expected_version=shown[0]['spec_version'],
        request_id='recheck', action='recheck_execution_evidence', reason='New host evidence',
        work_dir=tmp_path, catalog={})
    saved = store.load(RUN_ID)['collaboration']
    assert saved['recovery_gates'][old['id']]['status'] == 'superseded'
    assert saved['attempts'][attempt['id']]['status'] == 'checking'
    assert saved['attempts'][attempt['id']]['report'] == attempt['report']
    assert saved['lineage_budget'] == before['collaboration']['lineage_budget']


@pytest.mark.asyncio
async def test_verifier_reads_bound_source_tool_by_id_and_cannot_read_other_nodes():
    record, _, _, archive = stopped_call()
    record['node_states']['other'] = {'tools': {'private': {'output': 'not authorized'}}}
    archive_attempt(record, 'other', force=True)
    ordinary = ReadRunArchive(lambda: record, 'verifier')
    assert (await ordinary.execute(ReadRunArchiveParams(tool_id='old-tool'))).is_error
    bound = ReadRunArchive(lambda: record, 'verifier', source_node_id='work')
    index = json.loads((await bound.execute(ReadRunArchiveParams())).output)
    assert [row['archive_id'] for row in index['archives']] == [archive]
    chunks, offset = [], 0
    while True:
        page = json.loads((await bound.execute(ReadRunArchiveParams(tool_id='old-tool', offset=offset, limit=100))).output)
        chunks.append(page['content'])
        if page['complete']:
            break
        offset = page['next_offset']
    receipt = json.loads(''.join(chunks))['receipts'][0]
    assert receipt['archive_id'] == archive and receipt['receipt']['args']['content'] == 'new'
    assert (await bound.execute(ReadRunArchiveParams(tool_id='private'))).is_error
    record['node_states']['work']['tools']['recent'] = {'name': 'ReadRecoveryContext', 'output': 'real output'}
    result = await bound.execute(ReadRunArchiveParams(tool_id='recent'))
    assert 'real output' in json.loads(result.output)['content']


def test_startup_repairs_false_blocker_and_keeps_run_stopped(tmp_path):
    from tests.test_collaboration_recovery import store_for, RUN_ID
    record, _, _, _ = stopped_call()
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda saved: saved.update(record), durable=True)
    store.recover_stale_runs()
    saved = store.load(RUN_ID)
    assert saved['status'] == 'paused' and saved['active_node_ids'] == []
    assert ledger_for_run(saved).view('work').active_blockers == ()
    assert execution_recheck_gates(saved)[0]['kind'] == 'execution_evidence_available'
