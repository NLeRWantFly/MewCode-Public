import asyncio
import copy
import time

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.loops import LoopContract
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.orchestration.validate import validate_spec
from tests.test_generic_loops import report


def spec():
    return OrchestrationSpec.from_value({
        'spec_id': 'fusion', 'label': '独立识别并融合', 'finalizer_node_id': 'done',
        'nodes': [{'node_id': key, 'kind': kind, 'label': key, 'goal': '完整且可追溯',
                   'capabilities': {'command_execution': False, 'write_paths': []}}
                  for key, kind in [('start', 'implement'), ('a', 'implement'), ('b', 'implement'),
                                    ('c', 'implement'), ('fuse', 'implement'), ('audit', 'audit'), ('done', 'reduce')]],
        'edges': [{'from': a, 'to': b} for a, b in [('start', 'a'), ('start', 'b'), ('start', 'c'),
            ('a', 'fuse'), ('b', 'fuse'), ('c', 'fuse'), ('fuse', 'audit'), ('audit', 'done')]],
        'meta': {'loops': [{'loop_id': 'pages', 'entry_node_id': 'start', 'check_node_id': 'audit',
            'node_ids': ['start', 'a', 'b', 'c', 'fuse', 'audit'], 'exit_condition': '所有页面有逐项证据',
            'criteria': [{'criterion_id': 'REQ-01', 'acceptance_test': '覆盖与证据完整'}],
            'parallel_groups': [{'group_id': 'readers', 'node_ids': ['a', 'b', 'c'],
                'join_node_id': 'fuse', 'min_success': 1, 'max_concurrency': 3, 'branch_timeout_seconds': 1}]}]}})


async def execute(raw, runner, *, states=None, emit=None):
    validate_spec(raw)
    flow = to_expert_flow(raw)
    states = {} if states is None else states
    async def ignore(event): pass
    value = await run_bounded_loop(contract=LoopContract.from_value(flow.meta['loops'][0]), flow=flow,
        saved=states, run_node=runner, emit=emit or ignore,
        policy_for=lambda _: ExpertExecutionPolicy('stable', False, 0), incoming=[])
    return value, states


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [set(), {'a'}, {'a', 'b'}])
async def test_three_readers_overlap_and_join_all_valid_results(failed):
    started, finished, calls = set(), set(), []
    all_started = asyncio.Event()
    async def runner(node, incoming, policy, prompt):
        key = node.node_id
        calls.append(key)
        if key in {'a', 'b', 'c'}:
            started.add(key)
            if len(started) == 3: all_started.set()
            await asyncio.wait_for(all_started.wait(), .5)
            assert [row['from_node'] for row in incoming] == ['start']
            assert policy.parallel_readonly and 'Bash' in policy.deny_tools
            if key == 'c': await asyncio.sleep(.03)
            finished.add(key)
            if key in failed:
                return ExpertRoleResult(text='provider failed', status='failed', error_kind='provider_error', input_tokens=10)
        if key == 'fuse':
            assert finished == {'a', 'b', 'c'}
            assert {row['from_node'] for row in incoming if not row.get('parallel_group')} == {'a', 'b', 'c'} - failed
            receipt = next(row['parallel_group'] for row in incoming if row.get('parallel_group'))
            assert receipt['settled'] and len(receipt['branches']) == 3
            assert set(receipt['usable_node_ids']) == {'a', 'b', 'c'} - failed
            assert '综合所有有效成果' in prompt and '关键字段' in prompt
        return ExpertRoleResult(text='valid', input_tokens=10,
            loop_decision=report(policy.loop_context) if key == 'audit' else None)
    (local, result), states = await execute(spec(), runner)
    assert result.status == 'completed'
    assert calls.count('fuse') == calls.count('audit') == 1
    assert states['pages']['tokens'] == 60
    assert not states['pages']['active_nodes']
    assert all(local[key].status == 'failed' for key in failed)


@pytest.mark.asyncio
async def test_all_failed_never_reaches_fusion_or_audit():
    calls = []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        return ExpertRoleResult(text='failure', status='failed' if node.node_id != 'start' else 'completed',
                                error_kind='provider_error' if node.node_id != 'start' else '')
    (_, result), states = await execute(spec(), runner)
    assert result.error_kind == 'parallel_quorum_missing'
    assert set(calls) == {'start', 'a', 'b', 'c'}
    assert states['pages']['parallel_settlements']['readers']['settled']


@pytest.mark.asyncio
async def test_slow_branch_outlives_legacy_timeout_and_is_included_in_fusion():
    finished = set()
    async def runner(node, incoming, policy, prompt):
        if node.node_id == 'b':
            await asyncio.sleep(1.1)  # Persisted legacy group timeout is 1 s.
        if node.node_id in {'a', 'b', 'c'}:
            finished.add(node.node_id)
        if node.node_id == 'fuse':
            assert finished == {'a', 'b', 'c'}
            assert {row['from_node'] for row in incoming} == {'a', 'b', 'c', '@parallel:readers'}
        return ExpertRoleResult(text='valid', loop_decision=report(policy.loop_context) if node.node_id == 'audit' else None)
    (local, result), _ = await execute(spec(), runner)
    assert result.status == 'completed'
    assert local['b'].status == 'completed' and not local['b'].error_kind


@pytest.mark.asyncio
async def test_parallel_branch_continues_slices_beyond_legacy_total_timeout():
    raw, slices = spec(), 0
    raw.meta['loops'][0]['control_mode'] = 'team'
    async def runner(node, incoming, policy, prompt):
        nonlocal slices
        if node.node_id == 'b':
            slices += 1
            await asyncio.sleep(.6)
            if slices == 1:
                return ExpertRoleResult(text='checkpoint saved', status='budget_exhausted',
                    error_kind='soft_deadline', input_tokens=10)
            assert policy.resume_messages
        if node.node_id == 'fuse':
            assert slices == 2
            assert next(row for row in incoming if row['from_node'] == 'b')['text'] == 'finished'
        return ExpertRoleResult(text='finished', input_tokens=10,
            loop_decision=report(policy.loop_context) if node.node_id == 'audit' else None)
    (local, result), states = await execute(raw, runner)
    assert result.status == 'completed' and local['b'].status == 'completed'
    assert states['pages']['tokens'] == 70


@pytest.mark.asyncio
async def test_explicit_cancel_preserves_completed_peers_and_resumes_slow_branch():
    states, calls, stopped = {}, [], set()
    waiting = asyncio.Event()
    resume = False
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id in {'a', 'b', 'c'}:
            if node.node_id == 'b' and not resume:
                waiting.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.add('b')
            if node.node_id == 'b' and resume:
                assert policy.resume_messages
        return ExpertRoleResult(text='saved', loop_decision=report(policy.loop_context) if node.node_id == 'audit' else None)
    task = asyncio.create_task(execute(spec(), runner, states=states))
    await asyncio.wait_for(waiting.wait(), 2)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped == {'b'} and not states['pages']['active_nodes']
    assert {'start', 'a', 'c'} <= states['pages']['round_results'].keys()
    assert 'b' not in states['pages']['round_results']
    resume = True
    (_, result), _ = await execute(spec(), runner, states=copy.deepcopy(states))
    assert result.status == 'completed'
    assert calls == ['start', 'a', 'b', 'c', 'b', 'fuse', 'audit']


@pytest.mark.asyncio
async def test_continue_reopens_legacy_timeout_and_rebuilds_dependent_fusion():
    from mewcode.experts.loop_recovery import reopen_team_checkpoint
    raw, states, calls = spec(), {}, []
    raw.meta['loops'][0]['control_mode'] = 'team'
    before_fix = True
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == 'b':
            if before_fix:
                return ExpertRoleResult(text='legacy timeout', status='failed', error_kind='branch_timeout')
            assert policy.resume_messages
        return ExpertRoleResult(text='saved', input_tokens=10,
            loop_decision=report(policy.loop_context) if node.node_id == 'audit' else None)
    async def emit(event):
        saved = event.get('loop_state') or {}
        if before_fix and 'fuse' in saved.get('round_results', {}):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await execute(raw, runner, states=states, emit=emit)
    before = copy.deepcopy(states['pages'])
    flow = to_expert_flow(raw)
    loop = LoopContract.from_value(flow.meta['loops'][0])
    affected = reopen_team_checkpoint(flow, loop, states['pages'])
    assert affected == {'b', 'fuse', 'audit'}
    assert states['pages']['interrupted_nodes'] == ['b']
    assert states['pages']['tokens'] == before['tokens']
    assert states['pages']['elapsed'] == before['elapsed']
    for key in ('start', 'a', 'c'):
        assert states['pages']['round_results'][key] == before['round_results'][key]
    before_fix = False
    (local, result), _ = await execute(raw, runner, states=states, emit=emit)
    assert result.status == 'completed' and local['b'].status == 'completed'
    assert calls == ['start', 'a', 'b', 'c', 'fuse', 'b', 'fuse', 'audit']


@pytest.mark.asyncio
async def test_restart_keeps_settled_failure_and_success_without_rerunning_branches():
    states, calls = {}, []
    crash = True
    async def emit(event):
        row = (event.get('loop_state') or {}).get('parallel_settlements', {}).get('readers') or {}
        if crash and row.get('settled'): raise asyncio.CancelledError()
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == 'a': return ExpertRoleResult(text='offline', status='failed', error_kind='provider_error')
        return ExpertRoleResult(text='valid', loop_decision=report(policy.loop_context) if node.node_id == 'audit' else None)
    with pytest.raises(asyncio.CancelledError):
        await execute(spec(), runner, states=states, emit=emit)
    crash = False
    (_, result), resumed = await execute(spec(), runner, states=copy.deepcopy(states), emit=emit)
    assert result.status == 'completed'
    assert calls == ['start', 'a', 'b', 'c', 'fuse', 'audit']


@pytest.mark.asyncio
async def test_parallel_time_is_wall_time_and_token_reservations_are_disjoint():
    limits, all_started, start = [], asyncio.Event(), time.monotonic()
    raw = spec()
    raw.meta['loops'][0]['token_budget'] = 12_000
    async def runner(node, incoming, policy, prompt):
        if node.node_id in {'a', 'b', 'c'}:
            limits.append(policy.token_budget)
            if len(limits) == 3: all_started.set()
            await all_started.wait()
            await asyncio.sleep(.06)
            await policy.usage_callback(20)
        return ExpertRoleResult(text='valid', input_tokens=20,
            loop_decision=report(policy.loop_context) if node.node_id == 'audit' else None)
    (_, result), states = await execute(raw, runner)
    assert result.status == 'completed'
    assert sum(limits) <= 12_000 - 20
    assert states['pages']['tokens'] == 120
    assert states['pages']['elapsed'] < (time.monotonic() - start) * 1.3


@pytest.mark.asyncio
async def test_audit_repair_reuses_readers_and_rechecks_only_changed_fusion():
    raw, calls = spec(), []
    raw.meta['loops'][0].update(control_mode='team', requires_repair=True, repair_node_ids=['fuse'])
    async def runner(node, incoming, policy, prompt):
        calls.append((policy.loop_context['round'], node.node_id))
        if node.node_id == 'a':
            return ExpertRoleResult(text='offline', status='failed', error_kind='provider_error')
        decision = None
        if node.node_id == 'audit':
            decision = report(policy.loop_context, 'FAIL' if policy.loop_context['round'] == 1 else 'PASS')
            decision['repair_node_ids'] = ['fuse']
        if node.node_id == 'fuse' and policy.loop_context['round'] == 2:
            assert '上轮逐项证据' in prompt
            assert {row['from_node'] for row in incoming} == {'b', 'c', '@parallel:readers'}
        return ExpertRoleResult(text='page 7 region 2 corrected', loop_decision=decision)
    (_, result), states = await execute(raw, runner)
    assert result.status == 'completed'
    assert [node for _, node in calls] == ['start', 'a', 'b', 'c', 'fuse', 'audit', 'fuse', 'audit']
    assert states['pages']['round_results']['a']['status'] == 'failed'


@pytest.mark.parametrize('fault', ['chain', 'overlap', 'command', 'quorum', 'outside', 'duplicate', 'escape'])
def test_invalid_parallel_contracts_are_rejected(fault):
    raw = spec().as_dict()
    group = raw['meta']['loops'][0]['parallel_groups'][0]
    if fault == 'chain':
        next(edge for edge in raw['edges'] if edge['to_node'] == 'b')['from_node'] = 'a'
    elif fault == 'overlap':
        for node in raw['nodes']:
            if node['node_id'] in {'a', 'b'}: node['capabilities']['write_paths'] = ['results']
    elif fault == 'command': raw['nodes'][1]['capabilities']['command_execution'] = True
    elif fault == 'quorum': group['min_success'] = 0
    elif fault == 'outside': group['join_node_id'] = 'done'
    elif fault == 'duplicate': group['node_ids'] = ['a', 'a']
    else: raw['nodes'][1]['capabilities']['write_paths'] = ['../other']
    with pytest.raises(ValueError): validate_spec(OrchestrationSpec.from_value(raw))


@pytest.mark.asyncio
async def test_collaboration_accepts_all_settled_join_without_promoting_failed_branch(tmp_path):
    from dataclasses import replace
    from tests.test_collaboration_mode import flow as basic, delivery, RUN_ID
    from tests.test_collaboration_integration import orchestrator, run
    from tests.test_iterative_collaboration import retry_store
    from mewcode.kernel import collaboration as ledger
    raw = to_expert_flow(spec())
    raw.meta.update(orchestration_mode='collaborative',
                    collaboration_spec=copy.deepcopy(basic().meta['collaboration_spec']))
    raw.meta['node_plan_briefs'] = {node.node_id: {'known_inputs': ['共同原始输入'],
        'outputs': ['结果'], 'acceptance': ['真实且完整']} for node in raw.nodes}
    # This exercises real durable Windows commits, not just model execution.
    raw.meta['loops'][0]['parallel_groups'][0]['branch_timeout_seconds'] = 180
    store = retry_store(tmp_path, raw)
    started, barrier = set(), asyncio.Event()
    async def runner(node, model, prompt, policy, *args):
        if node.node_id in {'a', 'b', 'c'}:
            started.add(node.node_id)
            if len(started) == 3: barrier.set()
            await asyncio.wait_for(barrier.wait(), 60)
            if node.node_id == 'a':
                return ExpertRoleResult(text='offline', status='failed', error_kind='provider_error')
        if node.node_id == 'fuse':
            assert set(policy.stage_inputs) == {'b', 'c', '@parallel:readers'}
        result = delivery(node.node_id)
        return replace(result, loop_decision=report(policy.loop_context)) if node.node_id == 'audit' else result
    async def review(model, payload, context):
        return {'passed': True, 'evidence': ['已核对交付']}
    async def coordinate(*args):
        pytest.fail('optional provider failure must not trigger global coordination')
    result = await run(orchestrator(store, tmp_path, runner, review, coordinate), store, raw)
    assert result.status == 'completed', result.final_summary
    value = store.load(RUN_ID)['collaboration']
    assert not ledger.valid_certificate(value, 'a')
    assert ledger.valid_certificate(value, 'fuse') and ledger.valid_certificate(value, 'done')
    # A changed branch is new evidence even if the earlier fusion omitted it.
    value['attempts'][ledger.attempts_for(value, 'a')[-1]['id']]['delivery']['text'] = 'new evidence'
    assert not ledger.valid_certificate(value, 'fuse')
