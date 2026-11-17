"""An interrupted read-only review retains ownership without replaying delivery."""
import asyncio
import copy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import Acceptance, paused_review_binding, review_binding
from mewcode.kernel.events import digest
from mewcode.orchestration.collab_runtime import CollaborationRuntime
from mewcode.orchestration.revisions import can_resume_confirmed_flow
from tests.test_collaboration_mode import CATALOG, EXECUTOR, PROFILE, REVIEWER, RUN_ID, core, delivery, flow
from tests.test_collaboration_integration import orchestrator, run as run_host


def paused_record(tmp_path, *, bound=False):
    value = core()
    attempt = ledger.dispatch(value, 'work')
    result = delivery()
    value['attempts'][attempt['id']].update(status='checking', delivery=asdict(result))
    context = dict(node_id='work', attempt_id=attempt['id'], acceptance_round=0,
                   executor_model=EXECUTOR.as_dict(), executor_role='executor',
                   policy=SimpleNamespace(input_revision=attempt['dependencies']))
    contract = value['contracts']['work']
    payload = dict(contract=contract, check=contract['checks'][0],
                   artifacts=Acceptance(tmp_path).freeze(result.handoff),
                   unverified_report=result.text, handoff=result.handoff)
    identity = digest(['acceptance', attempt['id'], 0, payload['check'], payload['artifacts']])
    binding = {**review_binding(context, payload['check']), 'call_id': identity,
               'handoff_hash': digest(result.handoff)}
    helper = binding['recovery_node_id']
    call = dict(kind='acceptance', status='dispatched', model=REVIEWER.as_dict(), run_attempt=1, wait_history=[])
    if bound:
        call['binding'] = binding
    value['calls'][identity] = call
    record = dict(run_id=RUN_ID, attempt=1, status='paused', locked_flow=flow().as_dict(),
        collaboration=value, node_states={'work': {'status': 'paused'}, helper: {
            'status': 'paused', 'role': 'auditor', 'input_revision': attempt['dependencies']}},
        stage_cursor=helper, role_models={'roles': {'verifier': REVIEWER.as_dict(),
            'executor': EXECUTOR.as_dict(), 'planner': EXECUTOR.as_dict()}})
    return record, identity, binding, payload, context


@pytest.mark.parametrize('bound', [False, True])
def test_paused_review_ownership_is_pure_and_requires_original_delivery(tmp_path, bound):
    record, identity, binding, _, _ = paused_record(tmp_path, bound=bound)
    before = copy.deepcopy(record)
    assert paused_review_binding(record, identity, record['collaboration']['calls'][identity]) == binding
    assert can_resume_confirmed_flow(record, flow())
    assert record == before


@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('change', ['content', 'delivery_status', 'call_id', 'role', 'input_revision', 'check',
                                  'round', 'superseded', 'missing_call', 'real_node', 'removed_parent'])
def test_unproven_review_never_hides_a_missing_business_node(tmp_path, bound, change):
    record, identity, binding, _, context = paused_record(tmp_path, bound=bound)
    value = record['collaboration']
    attempt = value['attempts'][context['attempt_id']]
    helper = binding['recovery_node_id']
    current = flow().as_dict()
    if change == 'content':
        attempt['delivery']['handoff']['inline_outputs'][0]['content'] = 'changed'
    elif change == 'delivery_status':
        attempt['delivery']['status'] = 'needs_attention'
    elif change == 'call_id':
        value['calls']['changed'] = value['calls'].pop(identity)
    elif change == 'role':
        record['node_states'][helper]['role'] = 'executor'
    elif change == 'input_revision':
        record['node_states'][helper]['input_revision'] = 'changed'
    elif change == 'check':
        value['contracts']['work']['checks'][0]['version'] += 1
    elif change == 'round':
        value['acceptance_rounds'] = {attempt['id']: 1}
    elif change == 'superseded':
        attempt['superseded'] = True
    elif change == 'missing_call':
        value['calls'].clear()
    elif change == 'real_node':
        record['locked_flow']['nodes'].append({**record['locked_flow']['nodes'][0], 'node_id': helper})
    else:
        current['nodes'] = [node for node in current['nodes'] if node['node_id'] != 'work']
        current['edges'] = []
    assert not can_resume_confirmed_flow(record, current)


@pytest.mark.parametrize('bound', [False, True])
def test_file_delivery_requires_a_saved_host_binding(tmp_path, bound):
    record, identity, binding, _, context = paused_record(tmp_path, bound=bound)
    handoff = record['collaboration']['attempts'][context['attempt_id']]['delivery']['handoff']
    handoff['artifacts'] = [{'path': 'not-read-or-recreated.txt'}]
    if bound:
        record['collaboration']['calls'][identity]['binding']['handoff_hash'] = digest(handoff)
    assert can_resume_confirmed_flow(record, flow()) is bound
    assert not (tmp_path / 'not-read-or-recreated.txt').exists()


def sqlite_store(tmp_path, record=None):
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    store.create(run_id=RUN_ID, conversation_id='conversation', task='交付测试结果', attachments=[],
                 profile=PROFILE.as_dict(), strategy='stable', flow=flow())
    store.mutate(RUN_ID, lambda row: row.update(record or {'role_models': {'roles': {
        'verifier': REVIEWER.as_dict(), 'reviewer': REVIEWER.as_dict(),
        'executor': EXECUTOR.as_dict(), 'planner': EXECUTOR.as_dict()}}}), durable=True)
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['', 'same_attempt', 'unknown_effect', 'changed_model'])
async def test_legacy_paused_call_resumes_only_after_explicit_host_resume(tmp_path, blocked):
    record, identity, binding, payload, context = paused_record(tmp_path)
    if blocked != 'same_attempt':
        record['attempt'] = 2
    if blocked == 'unknown_effect':
        record['kernel_calls'] = {'unknown': {'node_id': binding['recovery_node_id'], 'state': 'unknown'}}
    if blocked == 'changed_model':
        record['collaboration']['calls'][identity]['model'] = EXECUTOR.as_dict()
    store = sqlite_store(tmp_path, record)
    review = AsyncMock(return_value={'passed': True, 'evidence': ['checked saved inline output']})
    runtime = CollaborationRuntime(store=store, run_id=RUN_ID, flow=flow(), catalog=CATALOG,
                                   work_dir=tmp_path, review=review)
    if blocked:
        before = store.load(RUN_ID)
        with pytest.raises(ValueError, match='前次验收调用结果未确认'):
            await runtime.review(payload, context)
        assert store.load(RUN_ID) == before and not review.called
    else:
        assert (await runtime.review(payload, context))['passed']
        assert review.call_args.args[2]['resume_acceptance'] is True
        saved = store.load(RUN_ID)['collaboration']
        assert list(saved['calls']) == [identity]
        assert saved['calls'][identity]['binding'] == binding
        assert saved['calls'][identity]['wait_history'][0]['source'] == 'persisted_paused_review_stage'
        attempt = saved['attempts'][context['attempt_id']]
        assert attempt.pop('acceptance_input_evidence_hash')
        assert saved['attempts'] == record['collaboration']['attempts']


@pytest.mark.asyncio
async def test_cancelled_acceptance_saves_wait_receipt_and_continues_same_delivery(tmp_path):
    store = sqlite_store(tmp_path)
    from tests.test_collaboration_mode import require_reviews
    require_reviews(store, 'work')
    waits = []
    async def review(model, payload, context):
        if context['node_id'] == 'work':
            waits.append(context['resume_acceptance'])
            if len(waits) == 1:
                call = next(row for row in store.load(RUN_ID)['collaboration']['calls'].values()
                            if row['kind'] == 'acceptance')
                assert call['status'] == 'dispatched' and call['binding']['node_id'] == 'work'
                helper = call['binding']['recovery_node_id']
                store.mutate(RUN_ID, lambda row: row['node_states'].update({helper: {
                    'status': 'paused', 'role': 'auditor', 'input_revision': context['policy'].input_revision}}), durable=True)
                raise asyncio.CancelledError()
        return {'passed': True, 'evidence': ['checked original delivery']}
    execute = AsyncMock(side_effect=lambda *args: delivery())
    host = orchestrator(store, tmp_path, execute, review)
    with pytest.raises(asyncio.CancelledError):
        await run_host(host, store)
    saved = store.load(RUN_ID)
    call_id, call = next((key, row) for key, row in saved['collaboration']['calls'].items() if row['kind'] == 'acceptance')
    assert call['status'] == 'waiting' and call['result']['execution_error'] == 'user_paused'
    assert call['result']['passed'] is False and not saved['collaboration']['certificates']
    assert waits == [False] and execute.await_count == 1
    store.transition(RUN_ID, 'paused')
    assert can_resume_confirmed_flow(store.load(RUN_ID), flow())
    store.resume(RUN_ID, strategy='stable')
    assert (await run_host(host, store)).status == 'completed'
    final = store.load(RUN_ID)['collaboration']
    assert waits == [False, True] and execute.await_count == 2  # work once, deliver once
    assert final['calls'][call_id]['status'] == 'finished'
    assert len(final['calls'][call_id]['wait_history']) == 1
    assert len(ledger.attempts_for(final, 'work')) == 1
