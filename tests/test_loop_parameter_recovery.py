import copy

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.experts.loop_runtime import run_bounded_loop
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.loops import LoopContract
from tests.test_acceptance_completion import old_false_completion
from tests.test_generic_loops import spec_for, report


ERROR = "API error (400): Error code: 400 - {'detail': 'Unsupported parameter: max_output_tokens'}"


def rejected():
    return dict(text=ERROR, status="failed", error_kind="provider_error", failed_node_id="check",
                input_tokens=0, output_tokens=0, billable_tokens=0, iterations_used=0,
                activity_records=[], verification_records=[], handoff={})


def saved_case(tmp_path, failure=None, kind="provider_error"):
    flow = to_expert_flow(spec_for())
    contract = LoopContract.from_value(flow.meta["loops"][0])
    state = dict(status="blocked", error_kind=kind, failed_node_id="check", reason=ERROR,
                 round=1, item_round=1, round_open=True, tokens=1300, elapsed=9.5,
                 stalled=0, history=[], scores=None, contract_hash=contract.contract_hash,
                 round_results={"work": dict(status="completed", text="immutable previous artifact", billable_tokens=1300),
                                "check": failure if failure is not None else rejected()})
    runs, run_id = old_false_completion(tmp_path)
    runs.mutate(run_id, lambda snapshot: snapshot.update(status="blocked", locked_flow=flow.as_dict(),
        loop_states={"improve": copy.deepcopy(state)},
        node_states={"prepare": {"status": "completed"}, "work": {"status": "completed"},
                     "check": {"status": "needs_attention", "request_error": ERROR}, "done": {"status": "pending"}}))
    return runs, run_id, flow, contract, state


@pytest.mark.asyncio
async def test_explicit_continue_retries_only_rejected_request_and_keeps_evidence(tmp_path):
    runs, run_id, flow, contract, before = saved_case(tmp_path)
    calls = []
    async def emit(event):
        pass
    async def runner(node, incoming, policy, prompt):
        calls.append((node.node_id, policy.resume_messages, policy.token_budget))
        assert incoming[0]['text'] == 'immutable previous artifact'
        return ExpertRoleResult(text='Independently verified', billable_tokens=12,
                                loop_decision=report(policy.loop_context))
    kwargs = dict(contract=contract, flow=flow, run_node=runner, emit=emit,
                  policy_for=lambda _: ExpertExecutionPolicy('stable', False, 0), incoming=[])
    # Merely loading/re-entering a blocked loop must never auto-retry it.
    _, blocked = await run_bounded_loop(saved=runs.load(run_id)['loop_states'], **kwargs)
    assert blocked.error_kind == 'provider_error' and calls == []
    resumed = runs.resume(run_id, strategy='stable')
    state = resumed['loop_states']['improve']
    assert state['round_results'] == {'work': before['round_results']['work']}
    assert state['tokens'] == 1300 and state['elapsed'] == 9.5 and state['round'] == 1
    assert state['request_rejection_history'][0]['result'] == before['round_results']['check']
    assert runs.load(run_id)['loop_states']['improve'] == state  # Durable, not only a UI reset.
    _, done = await run_bounded_loop(saved=resumed['loop_states'], **kwargs)
    state = resumed['loop_states']['improve']
    assert done.status == 'completed'
    assert calls == [('check', False, contract.token_budget - 1300)]
    assert state['tokens'] == 1312 and state['round'] == 1
    assert state['request_rejection_history'][0]['result']['text'] == ERROR


@pytest.mark.parametrize('change', [
    {'billable_tokens': None}, {'input_tokens': 1}, {'output_tokens': 1},
    {'iterations_used': 1}, {'activity_records': [{'kind': 'edit', 'path': 'candidate'}]},
    {'handoff': {'artifacts': [{'path': 'candidate'}]}},
    {'text': 'API error (500): provider unavailable'}, {'error_kind': 'effect_unknown'},
])
def test_uncertain_or_executed_failure_is_not_reopened(tmp_path, change):
    runs, run_id, _, _, before = saved_case(tmp_path, {**rejected(), **change})
    with pytest.raises(ValueError, match='未启动新调用'):
        runs.resume(run_id, strategy='stable')
    assert runs.load(run_id)['loop_states']['improve'] == before


@pytest.mark.parametrize('kind', ['budget_exhausted', 'acceptance_blocked', 'convergence_stalled'])
def test_other_loop_blocks_are_not_bypassed(tmp_path, kind):
    runs, run_id, _, _, before = saved_case(tmp_path, kind=kind)
    with pytest.raises(ValueError, match='未启动新调用'):
        runs.resume(run_id, strategy='stable')
    assert runs.load(run_id)['loop_states']['improve'] == before


def test_rejected_continue_does_not_mutate_cached_loop(tmp_path):
    runs, run_id, _, _, before = saved_case(tmp_path)
    runs.mutate(run_id, lambda s: s.update(pending_recovery_revision={'requires_approval': True}))
    with pytest.raises(ValueError, match='待批准'):
        runs.resume(run_id, strategy='stable')
    assert runs.load(run_id)['loop_states']['improve'] == before


@pytest.mark.asyncio
@pytest.mark.parametrize('resource', ['tokens', 'elapsed'])
async def test_explicit_retry_still_obeys_spent_budget(tmp_path, resource):
    runs, run_id, flow, contract, before = saved_case(tmp_path)
    spent = contract.token_budget if resource == 'tokens' else contract.timeout_seconds
    runs.mutate(run_id, lambda s: s['loop_states']['improve'].update({resource: spent}))
    resumed = runs.resume(run_id, strategy='stable')
    async def emit(event):
        pass
    async def runner(*args):
        pytest.fail('A spent budget must prevent a new request')
    _, result = await run_bounded_loop(contract=contract, flow=flow, saved=resumed['loop_states'],
        run_node=runner, emit=emit, policy_for=lambda _: ExpertExecutionPolicy('stable', False, 0), incoming=[])
    assert result.error_kind == 'budget_exhausted'
    assert resumed['loop_states']['improve'][resource] == spent
