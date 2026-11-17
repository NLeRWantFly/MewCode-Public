"""A user retry can recheck proven non-execution, never replay real effects."""
import copy
from types import SimpleNamespace

import pytest

from mewcode.agent import Agent
from mewcode.experts.review_reports import WriteReviewReport
from mewcode.kernel.calls import CallGateway, authorize_unstarted_retries, canonical_invocation, propose, transition
from mewcode.kernel.collaboration_recovery import open_gate, resolve_gate
from mewcode.tools.write_file import WriteFile, Params
from tests.test_collaboration_mode import RUN_ID, store_for


def failed(record, *, node='work'):
    invocation = canonical_invocation('WriteFile', {'file_path': 'report.txt', 'content': 'report'})
    row = propose(record, node, 'v1', invocation)
    transition(row, 'reviewed')
    transition(row, 'dispatched', evidence={'executor_accepted': True})
    transition(row, 'failed')
    row['result'] = {'output': 'blocked by old host guard', 'is_error': True,
        'host_observation': {'operation_blocked': True, 'phase': 'before_effect'}}
    return row, invocation


def test_one_explicit_retry_has_its_own_receipt_and_keeps_original_proof():
    record = {}
    original, invocation = failed(record)
    old = copy.deepcopy(original)
    assert propose(record, 'work', 'v1', invocation) is original
    assert authorize_unstarted_retries(record, 'work', 'user-1') == [original['call_id']]
    retried = propose(record, 'work', 'v1', invocation)
    assert retried['call_id'] != original['call_id'] and retried['retry_of'] == original['call_id']
    assert original == old and retried['state'] == 'proposed'
    assert propose(record, 'work', 'v1', invocation) is retried
    transition(retried, 'reviewed')
    transition(retried, 'dispatched', evidence={'executor_accepted': True})
    transition(retried, 'succeeded')
    assert authorize_unstarted_retries(record, 'work', 'user-2') == []
    assert propose(record, 'work', 'v1', invocation) is retried
    assert len(record['kernel_calls']) == 2 and original == old


@pytest.mark.parametrize('boundary', ['no_observation', 'after_effect', 'success', 'false_flag',
    'command_started', 'command_unknown', 'legacy', 'adjudicated', 'foreign_node', 'malformed'])
def test_uncertain_successful_or_unproven_effects_never_gain_a_retry(boundary):
    record = {}
    original, invocation = failed(record)
    if boundary == 'no_observation':
        original['result'].pop('host_observation')
    elif boundary == 'malformed':
        original['result']['host_observation'] = 'not a host observation'
    elif boundary == 'after_effect':
        original['result']['host_observation']['phase'] = 'after_effect'
    elif boundary == 'success':
        original['state'] = 'succeeded'
    elif boundary == 'false_flag':
        original['result']['host_observation']['operation_blocked'] = False
    elif boundary in {'command_started', 'command_unknown'}:
        original['result']['host_observation']['command_execution'] = (
            {'command_started': True} if boundary == 'command_started' else {'status': 'unknown'})
    elif boundary == 'legacy':
        original['origin'] = 'legacy'
    elif boundary == 'adjudicated':
        original['user_adjudication'] = {'disposition': 'assume_applied'}
    assert authorize_unstarted_retries(record, 'other' if boundary == 'foreign_node' else 'work', 'user-1') == []
    assert propose(record, 'work', 'v1', invocation) is original


def test_failed_retry_is_not_an_automatic_retry_loop():
    record = {}
    original, invocation = failed(record)
    authorize_unstarted_retries(record, 'work', 'user-1')
    retried = propose(record, 'work', 'v1', invocation)
    retried.update(state='failed', result=copy.deepcopy(original['result']))
    for _ in range(3):
        assert propose(record, 'work', 'v1', invocation) is retried
    assert len(record['kernel_calls']) == 2
    authorize_unstarted_retries(record, 'work', 'user-2')
    assert propose(record, 'work', 'v1', invocation)['call_id'] not in {original['call_id'], retried['call_id']}


@pytest.mark.parametrize('change', ['source_hash', 'authority', 'input', 'unknown_effect'])
def test_retry_binding_cannot_cross_evidence_authority_or_unknown_effect(change):
    record = {}
    original, invocation = failed(record)
    authorize_unstarted_retries(record, 'work', 'user-1')
    grant = record['kernel_call_retry_authorizations'][original['call_id']]
    if change == 'source_hash':
        original['result']['output'] = 'different evidence'
    elif change == 'authority':
        grant['source'] = 'model'
    elif change == 'input':
        grant['input_revision'] = 'old'
    else:
        unknown = {**copy.deepcopy(original), 'call_id': 'unknown', 'state': 'unknown', 'input_revision': 'other'}
        record['kernel_calls']['unknown'] = unknown
    assert propose(record, 'work', 'v1', invocation) is (unknown if change == 'unknown_effect' else original)


@pytest.mark.asyncio
@pytest.mark.parametrize('protected_input', [False, True])
async def test_real_report_guard_and_gateway_recheck_after_user_gate(tmp_path, protected_input):
    store = store_for(tmp_path)
    path = tmp_path / 'report.txt'
    path.write_text('previous version', encoding='utf-8')
    sources = {'previous:work': {'node_id': 'previous:work', 'handoff': {'artifacts': [{'path': 'report.txt'}]}}}
    scope = {'outputs': ('report.txt',), 'inputs': ()}
    old_guard = WriteReviewReport(WriteFile(), work_dir=tmp_path, scope=scope, sources=sources)
    params = Params(file_path=str(path), content='new report')
    tc = SimpleNamespace(tool_name='WriteFile', arguments=params.model_dump(), tool_id='first')
    agent = Agent.__new__(Agent)
    agent._safety_call_ref = lambda *args: 'test'
    agent._prepare_rollback_evidence = lambda *args: None
    agent._review_recovery = SimpleNamespace(begin_effect=lambda *args: {}, finish_effect=lambda *args: None)
    agent.work_dir, agent.permission_checker = str(tmp_path), None
    agent.kernel_gateway = CallGateway(store, RUN_ID, 'work', 'v1')
    initial = await agent._run_recorded_tool(old_guard, params, tc)
    assert initial.is_error and path.read_text() == 'previous version'
    original_calls = store.load(RUN_ID)['kernel_calls']
    def retry(record):
        gate = open_gate(record, scope='contract:work', keys=['work'], label='retry', reason='guard repaired', kind='infrastructure')
        resolve_gate(record, gate_id=gate['id'], expected_version=gate['spec_version'], request_id='repair',
            action='retry', reason='Recheck the original write with current guards', actor='user')
    store.mutate(RUN_ID, retry, durable=True)
    if protected_input:
        sources['upstream'] = {'fixed_artifacts': [{'path': 'report.txt'}]}
    current_guard = WriteReviewReport(WriteFile(), work_dir=tmp_path, scope=scope, sources=sources, node_id='work')
    tc.tool_id = 'retry'
    result = await agent._run_recorded_tool(current_guard, params, tc)
    assert result.is_error is protected_input
    assert path.read_text() == ('previous version' if protected_input else 'new report')
    assert not (result.host_observation or {}).get('reused')
    repeated = await agent._run_recorded_tool(current_guard, params, tc)
    assert repeated.host_observation['reused']
    after = store.load(RUN_ID)['kernel_calls']
    assert len(after) == 2
    assert all(after[key] == row for key, row in original_calls.items())
