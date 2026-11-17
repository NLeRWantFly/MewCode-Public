"""Continuous recovery scenarios; deterministic providers, real scheduler/store."""
import copy

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore, compact_loop_state
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.loops import LoopContract, LoopWatchdog
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.tools.submit_loop_decision import SubmitLoopDecisionTool, SubmitLoopDecisionParams
from mewcode.tools.submit_verdict import VerdictHolder
from tests.test_generic_loops import report, spec_for


def chain_flow(*, team=True):
    raw = spec_for().as_dict()
    ids = ['prepare', 'retrieve', 'extract', 'analyze', 'rewrite', 'check', 'done']
    raw['nodes'] = [{'node_id': node_id, 'label': node_id, 'goal': node_id,
                     'kind': 'decision' if node_id == 'check' else 'reduce' if node_id == 'done' else 'implement'}
                    for node_id in ids]
    raw['edges'] = [{'from': left, 'to': right} for left, right in zip(ids, ids[1:])]
    raw['edges'] += [{'from': 'check', 'to': 'retrieve', 'condition': 'loop'}]
    raw['meta']['loops'] = [{**raw['meta']['loops'][0], 'entry_node_id': 'retrieve',
                            'node_ids': ids[1:-1], 'control_mode': 'team' if team else 'strict'}]
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    return flow, LoopContract.from_value(flow.meta['loops'][0])


async def execute(flow, contract, states, runner, events=None, **kwargs):
    async def emit(event):
        if events is not None:
            events.append(copy.deepcopy(event))
    return await run_bounded_loop(contract=contract, flow=flow, saved=states, run_node=runner,
        emit=emit, incoming=[], policy_for=lambda _: ExpertExecutionPolicy('stable', False, 0), **kwargs)


def legacy_poisoned_run(tmp_path):
    flow, contract = chain_flow(team=False)
    store = ExpertRunStore(tmp_path)
    run_id = 'd' * 32
    store.create(run_id=run_id, conversation_id='team-test', task='repair current item', attachments=[],
                 profile={}, strategy='stable', flow=flow)
    state = dict(status='blocked', reason='Downstream refused stale budget handoff', error_kind='node_failure',
        failed_node_id='rewrite', round=1, item_round=1, round_open=True, tokens=379166, elapsed=1010.5,
        stalled=0, history=[], scores=None, contract_hash=contract.contract_hash, interrupted_nodes=['extract'],
        round_results={
            'retrieve': dict(status='completed', text='verified source list', handoff={'artifacts': [{'path': 'sources.md'}]}),
            'extract': dict(status='budget_exhausted', text='partial quotations; budget_exhausted', billable_tokens=168015),
            'analyze': dict(status='completed', text='refused to analyze because upstream stopped'),
            'rewrite': dict(status='needs_attention', text='missing analysis', handoff={'status': 'blocked'}),
        })
    snapshot = store.mutate(run_id, lambda s: s.update(status='blocked', loop_states={'improve': state},
        node_states={node_id: copy.deepcopy(value) for node_id, value in state['round_results'].items()}))
    return store, run_id, flow, contract, snapshot


def grant(store, run_id, snapshot, mode='team', command='user-team'):
    return store.set_loop_control_mode(run_id, mode=mode, expected_updated_at=snapshot['updated_at'],
        command_id=command, reason='User asks for controllable team execution without cumulative hard caps')


@pytest.mark.asyncio
async def test_actual_failure_chain_recovers_earliest_incomplete_and_survives_reload(tmp_path):
    store, run_id, flow, contract, before = legacy_poisoned_run(tmp_path)
    changed = grant(store, run_id, before)
    assert changed['status'] == 'blocked' and changed['attempt'] == before['attempt']
    assert changed['locked_flow'] == before['locked_flow']
    assert changed['node_states'] == before['node_states']
    assert changed['loop_states']['improve']['round_results'] == before['loop_states']['improve']['round_results']
    reloaded = ExpertRunStore(tmp_path)
    resumed = reloaded.resume(run_id, strategy='stable')
    state = resumed['loop_states']['improve']
    assert list(state['round_results']) == ['retrieve']
    assert state['tokens'] == 379166 and state['elapsed'] == 1010.5 and state['round'] == 1
    assert state['contract_hash'] == contract.contract_hash
    assert resumed['stage_cursor'] == 'extract'
    assert resumed['node_states']['analyze']['status'] == 'pending'
    assert set(state['recovery_history'][0]['results']) == {'extract', 'analyze', 'rewrite'}
    assert 'recovery_history' not in compact_loop_state(state)
    calls = []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        assert policy.token_budget is None
        assert '首轮没有返修单是正常情况' in prompt
        if node.node_id == 'extract':
            assert policy.resume_messages and incoming[0]['text'] == 'verified source list'
        else:
            assert not policy.resume_messages
            assert incoming[0]['status'] == 'completed' and 'budget_exhausted' not in incoming[0]['text']
        return ExpertRoleResult(text='fresh ' + node.node_id, billable_tokens=11,
            loop_decision=report(policy.loop_context) if node.node_id == 'check' else None)
    _, done = await execute(flow, contract, resumed['loop_states'], runner)
    assert done.status == 'completed' and calls == ['extract', 'analyze', 'rewrite', 'check']
    assert resumed['loop_states']['improve']['tokens'] == 379210
    # Reloading a completed loop restores it without more model calls.
    _, restored = await execute(flow, contract, resumed['loop_states'], runner)
    assert restored.restored and calls == ['extract', 'analyze', 'rewrite', 'check']


@pytest.mark.asyncio
async def test_team_runs_past_old_caps_unknown_and_temporary_regression_with_targeted_repairs():
    flow, contract = chain_flow()
    states = {'improve': dict(round=5, item_round=5, tokens=9_000_000, elapsed=10000)}
    calls, reviews = [], []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        assert policy.token_budget is None and policy.timeout_seconds > 0
        if node.node_id != 'check':
            return ExpertRoleResult(text='artifact ' + node.node_id, billable_tokens=100)
        state = ['UNKNOWN', 'PARTIAL', 'FAIL', 'PARTIAL', 'PARTIAL', 'PASS'][len(reviews)]
        decision = report(policy.loop_context, state)
        decision['decision'] = 'EXIT' if state == 'PASS' else 'CONTINUE'
        decision['checks'][0]['evidence'] = 'new independently located evidence ' + str(len(reviews))
        decision['repair_node_ids'] = ['rewrite'] if state != 'PASS' else []
        reviews.append(decision)
        return ExpertRoleResult(text='review', loop_decision=decision)
    _, done = await execute(flow, contract, states, runner)
    assert done.status == 'completed' and len(reviews) == 6
    assert calls.count('retrieve') == calls.count('extract') == calls.count('analyze') == 1
    assert calls.count('rewrite') == 6
    assert states['improve']['round'] == 11 and states['improve']['elapsed'] >= 10000
    assert states['improve']['tokens'] > 9_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize('status,kind', [('budget_exhausted', ''), ('stalled', '')])
async def test_one_automatic_checkpoint_continuation_never_dispatches_partial_downstream(status, kind):
    flow, contract = chain_flow()
    calls, events, states = [], [], {}
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == 'extract' and calls.count('extract') == 1:
            await policy.usage_callback(70)
            return ExpertRoleResult(status=status, error_kind=kind, text='partial', billable_tokens=70)
        if node.node_id == 'extract':
            assert policy.resume_messages
        if incoming:
            assert incoming[0]['status'] == 'completed'
        return ExpertRoleResult(text='done ' + node.node_id, billable_tokens=3,
            loop_decision=report(policy.loop_context) if node.node_id == 'check' else None)
    _, done = await execute(flow, contract, states, runner, events)
    assert done.status == 'completed' and calls == ['retrieve', 'extract', 'extract', 'analyze', 'rewrite', 'check']
    assert states['improve']['tokens'] == 85
    assert len(states['improve']['recovery_history']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['provider_error', 'provider_interrupted', 'effect_unknown', 'permission_denied', 'awaiting_input'])
async def test_configuration_permissions_unknown_effects_and_questions_are_not_auto_retried(kind):
    flow, contract = chain_flow()
    calls, states = [], {}
    async def runner(node, *args):
        calls.append(node.node_id)
        return ExpertRoleResult(status='needs_attention', error_kind=kind, text='actual actionable cause')
    _, result = await execute(flow, contract, states, runner)
    assert result.error_kind == kind and result.failed_node_id == 'retrieve' and calls == ['retrieve']
    await execute(flow, contract, states, runner)
    assert calls == ['retrieve']  # Opening/reloading does not imply Continue.


@pytest.mark.asyncio
async def test_strict_partial_is_not_handed_downstream_and_location_is_real_node():
    flow, contract = chain_flow(team=False)
    calls = []
    async def runner(node, *args):
        calls.append(node.node_id)
        return ExpertRoleResult(status='budget_exhausted', text='partial', billable_tokens=contract.token_budget)
    _, result = await execute(flow, contract, {}, runner)
    assert calls == ['retrieve'] and result.failed_node_id == 'retrieve'


@pytest.mark.asyncio
@pytest.mark.parametrize('advancing, expected_calls', [(True, 5), (False, 2)])
async def test_review_batches_continue_only_with_durable_progress(advancing, expected_calls):
    flow, contract = chain_flow()
    calls = []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == 'check' and calls.count('check') < 5:
            return ExpertRoleResult(status='budget_exhausted', text='batch saved',
                review_progress={'advanced': advancing, 'complete': calls.count('check'), 'total': 5})
        return ExpertRoleResult(text='complete',
            loop_decision=report(policy.loop_context) if node.node_id == 'check' else None)
    _, result = await execute(flow, contract, {}, runner)
    assert calls.count('check') == expected_calls
    assert (result.status == 'completed') == advancing


@pytest.mark.asyncio
async def test_blocked_handoff_from_any_role_cannot_claim_completion():
    flow, contract = chain_flow()
    async def runner(node, *args):
        return ExpertRoleResult(status='completed', text='response ended',
                                handoff={'status': 'blocked', 'summary': 'Need an unavailable source'})
    _, result = await execute(flow, contract, {}, runner)
    assert result.status == 'needs_attention' and result.failed_node_id == 'retrieve'


@pytest.mark.asyncio
async def test_repeated_evidence_pauses_then_explicit_continue_can_recover(tmp_path):
    store, run_id, flow, contract, before = legacy_poisoned_run(tmp_path)
    grant(store, run_id, before)
    resumed = store.resume(run_id, strategy='stable')
    states = resumed['loop_states']
    calls = []
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        return ExpertRoleResult(text='work', loop_decision=report(policy.loop_context, 'FAIL') if node.node_id == 'check' else None)
    _, stopped = await execute(flow, contract, states, runner)
    assert stopped.error_kind == 'no_progress' and calls.count('check') == 3
    store.mutate(run_id, lambda s: s.update(status='blocked', loop_states=states))
    resumed_again = store.resume(run_id, strategy='stable', supplemental='New source supplied; verify it')
    assert resumed_again['loop_states']['improve']['status'] == 'running'
    assert resumed_again['loop_states']['improve']['stalled'] == 0
    assert resumed_again['loop_states']['improve']['tokens'] >= 379166


@pytest.mark.asyncio
async def test_team_decision_missing_evidence_is_repairable_but_unknown_is_never_pass():
    _, contract = chain_flow()
    context = {**contract.as_dict(), 'contract_hash': contract.contract_hash, 'effective_control_mode': 'team'}
    decision = report(context, 'UNKNOWN')
    decision['decision'] = 'CONTINUE'
    tool = SubmitLoopDecisionTool(VerdictHolder(), context)
    assert not (await tool.execute(SubmitLoopDecisionParams(**decision))).is_error
    for decision_name, question in [('EXIT', ''), ('BLOCKED', '')]:
        value = {**decision, 'decision': decision_name, 'question': question}
        assert (await SubmitLoopDecisionTool(VerdictHolder(), context).execute(SubmitLoopDecisionParams(**value))).is_error
    value = {**decision, 'decision': 'BLOCKED', 'question': 'Which authorized source should I use?'}
    assert not (await SubmitLoopDecisionTool(VerdictHolder(), context).execute(SubmitLoopDecisionParams(**value))).is_error


@pytest.mark.parametrize('change', ['stale', 'active', 'hash', 'running', 'mode'])
def test_policy_change_is_scoped_guarded_and_atomic(tmp_path, change):
    store, run_id, _, _, before = legacy_poisoned_run(tmp_path)
    if change == 'active':
        store._cached(run_id)['active_node_ids'] = ['extract']
    elif change == 'hash':
        store._cached(run_id)['loop_states']['improve']['contract_hash'] = 'wrong'
    elif change == 'running':
        store._cached(run_id)['status'] = 'running'
    elif change == 'stale':
        before['updated_at'] = 'stale'
    with pytest.raises(ValueError):
        grant(store, run_id, before, mode='invalid' if change == 'mode' else 'team')
    assert not store.load(run_id).get('loop_control_policy')


def test_user_continue_rejection_does_not_partially_reopen_any_cached_node(tmp_path):
    store, run_id, _, _, before = legacy_poisoned_run(tmp_path)
    grant(store, run_id, before)
    store.mutate(run_id, lambda s: s.update(pending_recovery_revision={'requires_approval': True}))
    before = store.load(run_id)
    with pytest.raises(ValueError, match='待批准'):
        store.resume(run_id, strategy='stable')
    assert store.load(run_id) == before


def test_policy_switch_back_restores_limits_without_clearing_usage(tmp_path):
    store, run_id, _, contract, before = legacy_poisoned_run(tmp_path)
    team = grant(store, run_id, before)
    assert len(store.checkpoint_archive.list(run_id)) == 1
    repeat = grant(store, run_id, before)
    assert len(repeat['loop_budget_changes']) == 1
    assert len(store.checkpoint_archive.list(run_id)) == 1
    strict = grant(store, run_id, repeat, mode='strict', command='user-strict')
    state = strict['loop_states']['improve']
    assert LoopWatchdog(contract, state).token_limit == contract.token_budget
    assert state['tokens'] == 379166 and state['elapsed'] == 1010.5
    assert len(store.checkpoint_archive.list(run_id)) == 2


@pytest.mark.asyncio
async def test_run_policy_api_requires_ownership_pause_and_cas_and_never_starts(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import httpx
    from frontend import server
    store, run_id, _, _, before = legacy_poisoned_run(tmp_path)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    session = SimpleNamespace(session=SimpleNamespace(session_id='team-test'), chat_busy=lambda _: False)
    monkeypatch.setattr(server, 'SESSION', session)
    publish = AsyncMock()
    monkeypatch.setattr(server, 'WS_EVENTS', SimpleNamespace(send_json=publish))
    body = {'conversation_id': 'team-test', 'expected_updated_at': before['updated_at'], 'mode': 'team', 'command_id': 'ui-change'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        url = f'/api/expert-runs/{run_id}/loop-control'
        assert (await client.post(url, json={**body, 'conversation_id': 'other'})).status_code == 409
        session.chat_busy = lambda _: True
        assert (await client.post(url, json=body)).status_code == 409
        session.chat_busy = lambda _: False
        assert (await client.post(url, json={**body, 'expected_updated_at': 'stale'})).status_code == 409
        response = await client.post(url, json=body)
        assert response.status_code == 200, response.text
        result = response.json()['snapshot']
        assert result['status'] == 'blocked' and result['attempt'] == before['attempt']
        assert result['loop_control_policy']['mode'] == 'team'
        assert publish.call_count == 1


def test_inactive_resource_hints_do_not_reject_team_graphs():
    _, contract = chain_flow()
    rebuilt = LoopContract.from_value({**contract.as_dict(), 'token_budget': 0,
                                       'timeout_seconds': None, 'max_iterations': 99999})
    assert LoopWatchdog(rebuilt).token_limit is None
    with pytest.raises(ValueError):
        LoopContract.from_value({**rebuilt.as_dict(), 'node_max_iterations': 0})


def nested_flow():
    flow, _ = chain_flow()
    raw = spec_for().as_dict()
    # Reuse the chain's normalized flow through its orchestration adapter.
    from mewcode.orchestration.convert import from_expert_flow
    raw = from_expert_flow(flow).as_dict()
    raw['nodes'] = [{**node, 'kind': 'decision'} if node['node_id'] == 'analyze' else node for node in raw['nodes']]
    raw['meta']['loops'].append({**raw['meta']['loops'][0], 'loop_id': 'inner',
        'entry_node_id': 'extract', 'check_node_id': 'analyze', 'node_ids': ['extract', 'analyze'],
        'reviewer_node_ids': ['analyze']})
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    return flow, LoopContract.from_value(flow.meta['loops'][0])


@pytest.mark.asyncio
async def test_targeted_repair_does_not_replay_unaffected_nested_team():
    flow, contract = nested_flow()
    calls, states = [], {}
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        decision = None
        if node.node_id == 'analyze':
            decision = report(policy.loop_context)
        elif node.node_id == 'check':
            decision = report(policy.loop_context, 'PARTIAL' if calls.count('check') == 1 else 'PASS')
            decision['repair_node_ids'] = ['rewrite']
        return ExpertRoleResult(text='done ' + node.node_id, billable_tokens=10, loop_decision=decision)
    _, done = await execute(flow, contract, states, runner)
    assert done.status == 'completed'
    assert calls == ['retrieve', 'extract', 'analyze', 'rewrite', 'check', 'rewrite', 'check']
    assert states['improve']['tokens'] == 70 and states['improve/1/inner']['tokens'] == 20


@pytest.mark.asyncio
async def test_nested_completed_checkpoint_is_invalidated_if_upstream_input_changed(tmp_path):
    flow, contract = nested_flow()
    store = ExpertRunStore(tmp_path)
    run_id = 'e' * 32
    store.create(run_id=run_id, conversation_id='nested', task='nested input repair', attachments=[],
                 profile={}, strategy='stable', flow=flow)
    states = {}
    async def first(node, incoming, policy, prompt):
        if node.node_id == 'rewrite':
            return ExpertRoleResult(status='needs_attention', text='need new source', error_kind='awaiting_input')
        return ExpertRoleResult(text='old ' + node.node_id, billable_tokens=10,
            loop_decision=report(policy.loop_context) if node.node_id == 'analyze' else None)
    await execute(flow, contract, states, first)
    # Emulate an older run that passed an incomplete source into a completed child.
    states['improve']['round_results']['retrieve']['status'] = 'budget_exhausted'
    tokens = states['improve']['tokens']
    child_tokens = states['improve/1/inner']['tokens']
    store.mutate(run_id, lambda s: s.update(status='blocked', loop_states=states))
    resumed = store.resume(run_id, strategy='stable')
    child = resumed['loop_states']['improve/1/inner']
    assert child['status'] == 'running' and child['round_results'] == {} and child['tokens'] == child_tokens
    calls = []
    async def second(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if incoming:
            assert incoming[0]['text'].startswith('new ')
        return ExpertRoleResult(text='new ' + node.node_id, billable_tokens=10,
            loop_decision=report(policy.loop_context) if node.node_id in {'check', 'analyze'} else None)
    _, done = await execute(flow, contract, resumed['loop_states'], second)
    assert done.status == 'completed' and calls == ['retrieve', 'extract', 'analyze', 'rewrite', 'check']
    assert resumed['loop_states']['improve']['tokens'] == tokens + 50
    assert resumed['loop_states']['improve/1/inner']['tokens'] == child_tokens + 20


def summary_edit_case(tmp_path, *, verified=False, criteria=False):
    store, run_id, flow, old_contract, before = legacy_poisoned_run(tmp_path)
    store.save_turn_checkpoint(run_id, turn_key='before-copy-edit')
    changed = copy.deepcopy(before['locked_flow'])
    changed['meta']['loops'][0]['exit_condition'] += '，按当前项逐项判断。'
    if criteria:
        changed['meta']['loops'][0]['criteria'][0]['acceptance_test'] = 'a materially different test'
    def revise(snapshot):
        snapshot['locked_flow'] = changed
        if verified:
            snapshot['loop_states']['improve']['reports'] = {'check': report({'contract_hash': old_contract.contract_hash})}
    before = store.mutate(run_id, revise)
    return store, run_id, old_contract, before


def test_legacy_unreviewed_exit_prose_edit_is_reconciled_without_losing_outputs(tmp_path):
    store, run_id, old_contract, before = summary_edit_case(tmp_path)
    changed = grant(store, run_id, before)
    state = changed['loop_states']['improve']
    current = LoopContract.from_value(changed['locked_flow']['meta']['loops'][0])
    assert state['contract_hash'] == current.contract_hash != old_contract.contract_hash
    assert state['round_results'] == before['loop_states']['improve']['round_results']
    assert state['tokens'] == 379166 and state['contract_rebindings'][0]['from_hash'] == old_contract.contract_hash
    resumed = store.resume(run_id, strategy='stable')
    assert resumed['stage_cursor'] == 'extract'
    assert list(resumed['loop_states']['improve']['round_results']) == ['retrieve']


@pytest.mark.parametrize('case', ['accepted', 'criteria', 'unknown_history'])
def test_summary_compatibility_never_transfers_acceptance_or_invents_history(tmp_path, case):
    store, run_id, _, before = summary_edit_case(tmp_path, verified=case == 'accepted', criteria=case == 'criteria')
    if case == 'unknown_history':
        before = store.mutate(run_id, lambda s: s['loop_states']['improve'].update(contract_hash='unattested'))
    with pytest.raises(ValueError):
        grant(store, run_id, before)
    assert store.load(run_id) == before


def test_exit_summary_compatibility_works_on_explicit_continue_without_policy_switch(tmp_path):
    store, run_id, _, before = summary_edit_case(tmp_path)
    # Existing explicit Token grant remains attested when only exit prose moves.
    state = before['loop_states']['improve']
    state['loop_control_policy'] = {'mode': 'team', 'authorized_by': 'user', 'change_id': 'previous-team',
                                    'contract_hash': state['contract_hash']}
    store.mutate(run_id, lambda s: s.update(loop_states={'improve': state}))
    resumed = store.resume(run_id, strategy='stable')
    assert resumed['stage_cursor'] == 'extract'
    assert resumed['loop_states']['improve']['tokens'] == 379166
