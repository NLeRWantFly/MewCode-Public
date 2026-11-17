import asyncio
import copy
import json
from dataclasses import replace

import pytest

from mewcode.experts.adaptive_replan import AdaptiveReplanCoordinator, context_for, validate_proposal
from mewcode.expert_group import ExpertExecutionPolicy
from mewcode.tools.base import StreamEnd, TextDelta
from tests.test_scheduler_recovery import controller, decide


def setup(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, events = controller(tmp_path, monkeypatch)
    store.mutate(handler.run_id, lambda v: v['node_states']['extract'].update(
        input_revision='r1', tools={'read': {'tool_id': 'r', 'name': 'ReadFile',
        'status': 'completed', 'is_error': True, 'output': 'Expected file digest, got text digest'}}))
    policy = ExpertExecutionPolicy('stable', False, 0, token_budget=50000, timeout_seconds=60)
    node = flow.node_map['extract']
    context = context_for(store.load(handler.run_id), node, policy)
    primary.value = {'action': 'adapt_method', 'contract_hash': context['contract_hash'],
        'input_revision': 'r1', 'diagnosis': 'The digest fields have different definitions',
        'evidence_refs': ['r'], 'method_steps': ['Read raw and normalized digest fields separately'],
        'expected_observation': 'Both original digest checks match their declared scope',
        'stop_condition': 'Source changed or evidence is missing'}
    coordinator = AdaptiveReplanCoordinator(store=store, run_id=handler.run_id, primary=primary)
    return handler, flow, store, primary, jev, coordinator, node, policy, context


@pytest.mark.asyncio
async def test_primary_consultation_no_jev_no_tools_no_chat_leak(tmp_path, monkeypatch):
    handler, _, store, primary, jev, coordinator, node, policy, _ = setup(tmp_path, monkeypatch)
    charges = []
    result = await coordinator.consult(node=node, policy=policy, error_kind='segment_timeout', charge=charges.append)
    assert result['source'] == 'primary_method_consultation' and not jev.calls
    assert charges == [130] and primary.max_output_tokens == 8192
    messages, system, tools, limit = primary.calls[0]
    assert len(messages) == 1 and tools == [] and limit == 1024
    assert '已有产物已落盘' not in messages[0].content
    episode = store.load(handler.run_id)['adaptive_replans'][result['episode_id']]
    assert episode['status'] == 'proposed' and 'verified' not in episode
    with pytest.raises(ValueError, match='already consulted'):
        await coordinator.consult(node=node, policy=policy, error_kind='segment_timeout', charge=charges.append)
    assert len(primary.calls) == 1


@pytest.mark.parametrize('change', [{'grant_tools': ['Bash']}, {'input_revision': 'old'},
    {'evidence_refs': ['invented']}, {'method_steps': []}, {'model': 'another'}])
def test_invalid_or_expanding_proposals_fail(tmp_path, monkeypatch, change):
    *_, primary, jev, coordinator, node, policy, context = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        validate_proposal({**primary.value, **change}, context)


@pytest.mark.asyncio
async def test_unknown_provider_outcome_retains_reservation_no_retry(tmp_path, monkeypatch):
    handler, _, store, primary, _, coordinator, node, policy, _ = setup(tmp_path, monkeypatch)
    async def interrupted(*args, **kwargs):
        yield TextDelta('{')
        raise ConnectionError('not certain')
    primary.stream = interrupted
    charges = []
    with pytest.raises(ConnectionError):
        await coordinator.consult(node=node, policy=policy, error_kind='segment_timeout', charge=charges.append)
    episode = next(iter(store.load(handler.run_id)['adaptive_replans'].values()))
    assert episode['status'] == 'advisory_unknown'
    assert charges == [episode['reserved_tokens']]
    other = AdaptiveReplanCoordinator(store=store, run_id=handler.run_id, primary=primary)
    with pytest.raises(ValueError):
        await other.consult(node=node, policy=policy, error_kind='segment_timeout', charge=charges.append)
    assert len(charges) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['permission', 'unknown', 'budget'])
async def test_real_boundaries_stop_before_model(tmp_path, monkeypatch, boundary):
    handler, _, store, primary, _, coordinator, node, policy, _ = setup(tmp_path, monkeypatch)
    if boundary == 'permission':
        store.mutate(handler.run_id, lambda v: v['node_states']['extract'].update(pending_permission={'tool_id': 'p'}))
    elif boundary == 'unknown':
        store.mutate(handler.run_id, lambda v: v['node_states']['extract']['tools'].update(
            write={'name': 'Bash', 'status': 'outcome_unknown'}))
    else:
        policy = replace(policy, token_budget=100)
    with pytest.raises(ValueError):
        await coordinator.consult(node=node, policy=policy, error_kind='segment_timeout', charge=lambda n: None)
    assert not primary.calls


@pytest.mark.asyncio
async def test_revision_changes_during_consultation_reject_application(tmp_path, monkeypatch):
    handler, _, store, primary, _, coordinator, node, policy, _ = setup(tmp_path, monkeypatch)
    async def changing(*args, **kwargs):
        store.mutate(handler.run_id, lambda v: v['node_states']['extract'].update(input_revision='r2'))
        yield TextDelta(json.dumps(primary.value))
        yield StreamEnd('end_turn', input_tokens=100, output_tokens=20)
    primary.stream = changing
    charges = []
    with pytest.raises(ValueError, match='No current'):
        await coordinator.consult(node=node, policy=policy, error_kind='segment_timeout', charge=charges.append)
    assert charges == [120]
    assert next(iter(store.load(handler.run_id)['adaptive_replans'].values()))['status'] == 'stale'


@pytest.mark.asyncio
async def test_scheduler_uses_one_ledgered_self_diagnosis_before_escalation(tmp_path, monkeypatch):
    handler, flow, store, primary, jev, _, node, policy, _ = setup(tmp_path, monkeypatch)
    handler.model_decisions = False
    result = await decide(handler, flow, policy=policy)
    assert result['source'] == 'recovery_controller'
    assert result['action'] == 'diagnose_path'
    assert result['controller']['action'] == 'self_diagnose'
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.escalation import action_count
    view = ledger_for_run(store.load(handler.run_id)).view(node.node_id)
    assert action_count(view, 'self_diagnose') == 1
    assert action_count(view, 'assist') == 0
    assert not primary.calls and not jev.calls


@pytest.mark.asyncio
async def test_concurrent_consultations_same_node_only_one_request(tmp_path, monkeypatch):
    handler, _, store, primary, _, coordinator, node, policy, _ = setup(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def waiting(*args, **kwargs):
        calls.append(1)
        entered.set()
        await release.wait()
        yield TextDelta(json.dumps(primary.value))
        yield StreamEnd('end_turn', input_tokens=100, output_tokens=20)
    primary.stream = waiting
    charges = []
    task = asyncio.create_task(coordinator.consult(node=node, policy=policy,
        error_kind='segment_timeout', charge=charges.append))
    await entered.wait()
    try:
        with pytest.raises(ValueError):
            await coordinator.consult(node=node, policy=policy, error_kind='segment_timeout', charge=charges.append)
    finally:
        release.set()
        await task
    assert len(calls) == 1 and charges == [120]


@pytest.mark.asyncio
async def test_cancellation_persists_unknown_request_and_charge(tmp_path, monkeypatch):
    handler, _, store, primary, _, coordinator, node, policy, _ = setup(tmp_path, monkeypatch)
    async def cancelled(*args, **kwargs):
        yield TextDelta('{')
        raise asyncio.CancelledError()
    primary.stream = cancelled
    charges = []
    with pytest.raises(asyncio.CancelledError):
        await coordinator.consult(node=node, policy=policy, error_kind='segment_timeout', charge=charges.append)
    episode = next(iter(store.load(handler.run_id)['adaptive_replans'].values()))
    assert episode['status'] == 'advisory_unknown'
    assert charges == [episode['reserved_tokens']]


@pytest.mark.asyncio
async def test_saved_proposal_reused_and_trial_receipt_not_business_acceptance(tmp_path, monkeypatch):
    handler, _, store, primary, _, coordinator, node, policy, _ = setup(tmp_path, monkeypatch)
    charges = []
    await coordinator.consult(node=node, policy=policy, error_kind='segment_timeout', charge=charges.append)
    restarted = AdaptiveReplanCoordinator(store=store, run_id=handler.run_id, primary=primary)
    decision = await restarted.prepare(node=node, policy=policy, error_kind='segment_timeout', charge=charges.append)
    restarted.dispatch_probe(decision['episode_id'])
    with pytest.raises(ValueError, match='consumed'):
        restarted.dispatch_probe(decision['episode_id'])
    assert len(primary.calls) == 1 and charges == [130]
    store.mutate(handler.run_id, lambda v: v.setdefault('review_queues', {}).update(
        reviewer={'repair_batches': {'batch': {'owner': node.node_id, 'status': 'submitted', 'item_ids': ['item']}}}))
    receipt = restarted.finish_probe(decision['episode_id'])
    assert receipt['status'] == 'applied' and receipt['business_acceptance'] is False
    assert restarted.finish_probe(decision['episode_id']) == receipt


def test_failed_tool_is_real_durable_evidence(tmp_path, monkeypatch):
    handler, _, store, _, _, _, node, policy, _ = setup(tmp_path, monkeypatch)
    store.mutate(handler.run_id, lambda v: v['node_states']['extract']['tools']['read'].update(status='failed'))
    capsule = context_for(store.load(handler.run_id), node, policy)
    assert capsule['evidence'][0]['is_error'] and capsule['evidence'][0]['status'] == 'failed'


@pytest.mark.asyncio
async def test_explicit_request_is_durable_and_evidence_bound(tmp_path, monkeypatch):
    from mewcode.tools.request_replan import RequestReplan, RequestReplanParams
    handler, _, store, _, _, _, node, _, _ = setup(tmp_path, monkeypatch)
    tool = RequestReplan(store, handler.run_id, node.node_id)
    invalid = await tool.execute(RequestReplanParams(reason='method assumption invalid', evidence_refs=['invented']))
    assert invalid.is_error
    params = RequestReplanParams(reason='hash scope differs', evidence_refs=['r'])
    assert not (await tool.execute(params)).is_error
    first = store.load(handler.run_id)['node_states'][node.node_id]['adaptive_request']
    assert not (await tool.execute(params)).is_error
    assert store.load(handler.run_id)['node_states'][node.node_id]['adaptive_request'] == first
