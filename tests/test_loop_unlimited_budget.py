import copy

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.orchestration.loops import LoopWatchdog
from tests.test_generic_loops import report
from tests.test_loop_parameter_recovery import saved_case


def budget_case(tmp_path):
    runs, run_id, flow, contract, _ = saved_case(tmp_path)
    def prepare(snapshot):
        state = snapshot['loop_states']['improve']
        state.update(tokens=320920, error_kind='budget_exhausted', reason='达到循环 Token 或时间预算',
                     round_results={'work': dict(status='budget_exhausted', text='Preserved partial artifact', billable_tokens=320920)},
                     interrupted_nodes=['work'])
    before = runs.mutate(run_id, prepare)
    return runs, run_id, flow, contract, before


def disable(runs, run_id, before, command_id='user-unlimited'):
    return runs.disable_loop_token_limit(run_id, loop_id='improve', expected_updated_at=before['updated_at'],
                                        command_id=command_id, reason='User explicitly requests no Token limit')


@pytest.mark.asyncio
async def test_unlimited_is_explicit_durable_and_does_not_reset_or_execute(tmp_path):
    runs, run_id, flow, contract, before = budget_case(tmp_path)
    changed = disable(runs, run_id, before)
    state = changed['loop_states']['improve']
    assert changed['status'] == 'blocked' and changed['attempt'] == before['attempt']
    assert state['tokens'] == 320920 and state['elapsed'] == 9.5 and state['round'] == 1
    assert state['round_results'] == before['loop_states']['improve']['round_results']
    assert state['contract_hash'] == contract.contract_hash and changed['locked_flow'] == before['locked_flow']
    assert LoopWatchdog(contract, state).token_limit is None
    assert runs.load(run_id)['loop_states']['improve']['loop_resource_policy'] == state['loop_resource_policy']
    assert runs.public(run_id)['loop_states']['improve']['loop_resource_policy']['limit'] is None
    assert 'loop_budget_changes' not in runs.public(run_id)
    repeat = disable(runs, run_id, before)  # Response loss does not require a new grant.
    assert len(repeat['loop_budget_changes']) == 1 and repeat['loop_states']['improve']['budget_resume_pending']
    resumed = runs.resume(run_id, strategy='stable')
    calls = []
    async def emit(event):
        pass
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        assert policy.token_budget is None and policy.timeout_seconds < contract.timeout_seconds
        assert policy.loop_context['effective_token_budget'] is None
        if node.node_id == 'work':
            assert policy.resume_messages
            return ExpertRoleResult(text='Completed artifact after resuming partial work', billable_tokens=0)
        assert incoming[0]['text'] == 'Completed artifact after resuming partial work'
        return ExpertRoleResult(text='Fresh independent acceptance', billable_tokens=6000000,
                                loop_decision=report(policy.loop_context))
    _, result = await run_bounded_loop(contract=contract, flow=flow, saved=resumed['loop_states'], run_node=runner,
        emit=emit, policy_for=lambda _: ExpertExecutionPolicy('stable', False, 0), incoming=[])
    assert result.status == 'completed' and calls == ['work', 'check']
    assert resumed['loop_states']['improve']['tokens'] == 6320920


@pytest.mark.parametrize('change', ['time', 'stalled', 'acceptance', 'provider'])
def test_disabling_tokens_does_not_clear_other_stop_reasons(tmp_path, change):
    runs, run_id, flow, contract, before = budget_case(tmp_path)
    def block(snapshot):
        state = snapshot['loop_states']['improve']
        if change == 'time':
            state['elapsed'] = contract.timeout_seconds
        elif change == 'stalled':
            state['stalled'] = contract.max_stalled_rounds
        else:
            state['error_kind'] = 'acceptance_blocked' if change == 'acceptance' else 'provider_error'
    before = runs.mutate(run_id, block)
    disable(runs, run_id, before)
    with pytest.raises(ValueError, match='未启动新调用'):
        runs.resume(run_id, strategy='stable')
    resumed = runs.load(run_id)['loop_states']['improve']
    assert resumed['status'] == 'blocked'
    assert resumed['tokens'] == 320920 and resumed['round_results'] == before['loop_states']['improve']['round_results']


@pytest.mark.parametrize('change', ['running', 'active_node', 'active_interval', 'stale', 'contract'])
def test_grant_checks_stopped_state_revision_and_contract(tmp_path, change):
    runs, run_id, flow, contract, before = budget_case(tmp_path)
    def alter(snapshot):
        if change == 'running':
            snapshot['status'] = 'running'
        elif change == 'active_node':
            snapshot['active_node_ids'] = ['work']
        elif change == 'active_interval':
            snapshot['loop_states']['improve']['active_at'] = 123
        elif change == 'contract':
            snapshot['loop_states']['improve']['contract_hash'] = 'invalid'
    current = runs.mutate(run_id, alter)
    if change == 'active_node':
        # Stopped-snapshot normalization clears stale active IDs. Simulate an
        # in-flight cached update to exercise the mutation-time guard itself.
        runs._cached(run_id)['active_node_ids'] = ['work']
    with pytest.raises(ValueError):
        disable(runs, run_id, before if change == 'stale' else current)
    after = runs.load(run_id)
    assert not after.get('loop_budget_changes') and 'loop_resource_policy' not in after['loop_states']['improve']


def test_default_limit_and_invalid_overrides_remain_closed(tmp_path):
    runs, run_id, flow, contract, before = budget_case(tmp_path)
    state = before['loop_states']['improve']
    assert LoopWatchdog(contract, state).remaining() == '达到循环 Token 预算'
    for override in ({'limit': None}, {'limit': None, 'contract_hash': 'wrong', 'authorized_by': 'user', 'change_id': 'x'}):
        with pytest.raises(ValueError, match='授权覆盖无效'):
            LoopWatchdog(contract, {**state, 'loop_resource_policy': override})
