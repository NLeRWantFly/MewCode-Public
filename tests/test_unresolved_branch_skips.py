"""Regression for downstream stages marked inactive while their parent waits."""
from __future__ import annotations

import copy
import json
import asyncio
from types import SimpleNamespace

import pytest

from mewcode.expert_runs import ExpertRunStore, SNAPSHOT_VERSION
from mewcode.experts.branch_state import reconcile_unresolved_branch_skips
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.node_ledger import ledger_for_run


def checkpoint():
    flow = ExpertFlowSpec.from_value({
        'flow_id': 'unresolved-branch', 'label': 'Unresolved branch', 'finalizer_node_id': 'done',
        'nodes': [{'node_id': key, 'role': 'executor' if key != 'done' else 'manager', 'label': key}
                  for key in ('work', 'dependent', 'independent', 'done')],
        'edges': [{'from': 'work', 'to': 'dependent'}, {'from': 'dependent', 'to': 'done'},
                  {'from': 'independent', 'to': 'done'}],
    })
    return {
        'version': SNAPSHOT_VERSION, 'run_id': 'a' * 32, 'conversation_id': 'branch-reconcile',
        'status': 'paused', 'stage_cursor': 'work', 'locked_flow': flow.as_dict(),
        'node_states': {
            'work': {'status': 'needs_attention', 'handoff': {'status': 'needs_attention'},
                     'command_receipts': [{'request_id': 'unknown', 'status': 'outcome_unknown'}]},
            'dependent': {'status': 'skipped', 'reason': 'branch_not_taken', 'passed': False, 'attempt': 3},
            'independent': {'status': 'completed', 'text': 'verified independent output'},
            'done': {'status': 'skipped', 'reason': 'branch_not_taken', 'passed': False},
        },
        'events': [{'type': 'expert_flow_state', 'phase': 'branch_paused', 'node_id': 'work'},
                   {'type': 'expert_node_completed', 'node_id': 'dependent',
                    'status': 'skipped', 'reason': 'branch_not_taken'}],
        'assistance_receipts': {'prior': {'next_step': 'fail'}},
    }


def test_reconcile_only_unexecuted_descendants_and_preserve_every_execution_fact():
    record = checkpoint()
    original = copy.deepcopy(record)
    ledger_for_run(record).record('work', 'blocker', {'kind': 'effect_unknown', 'request_id': 'unknown'})
    before_ledger = copy.deepcopy(record['node_ledgers'])
    assert reconcile_unresolved_branch_skips(record) == ['dependent', 'done']
    assert record['node_states']['dependent']['status'] == 'pending'
    assert record['node_states']['dependent']['blocked_by'] == ['work']
    assert record['node_states']['dependent']['branch_skip_history'][0]['attempt'] == 3
    assert record['node_states']['work'] == original['node_states']['work']
    assert record['node_states']['independent'] == original['node_states']['independent']
    assert record['events'] == original['events']
    assert record['locked_flow'] == original['locked_flow']
    assert record['assistance_receipts'] == original['assistance_receipts']
    assert record['node_ledgers'] == before_ledger
    assert reconcile_unresolved_branch_skips(record) == []


@pytest.mark.parametrize('evidence', [
    {'passed': True}, {'node_attempt': 1}, {'started_at': 123},
    {'tools': {'x': {'status': 'outcome_unknown'}}}, {'messages': [{'role': 'assistant', 'content': 'work'}]},
    {'handoff': {'status': 'completed'}}, {'artifacts': [{'path': 'output.txt'}]},
    {'command_receipts': [{'request_id': 'x', 'status': 'outcome_unknown'}]},
    {'pending_permission': {'tool_id': 'x'}}, {'pending_interaction': {'request_id': 'x'}},
    {'input_tokens': 123}, {'output_tokens': 456},
])
def test_unexpected_skip_with_execution_evidence_is_never_reset(evidence):
    record = checkpoint()
    record['node_states']['dependent'].update(evidence)
    before = copy.deepcopy(record['node_states']['dependent'])
    assert 'dependent' not in reconcile_unresolved_branch_skips(record)
    assert record['node_states']['dependent'] == before


@pytest.mark.parametrize('bucket', ['pipeline_runs', 'review_queues', 'review_queue_refs', 'command_receipts', 'node_expansions'])
def test_execution_in_another_bucket_is_preserved(bucket):
    record = checkpoint()
    record[bucket] = {'dependent': {'existing': True}}
    assert 'dependent' not in reconcile_unresolved_branch_skips(record)
    assert record[bucket] == {'dependent': {'existing': True}}


@pytest.mark.parametrize('kind', ['artifact', 'receipt', 'handoff', 'answer'])
def test_execution_ledger_is_preserved(kind):
    record = checkpoint()
    ledger_for_run(record).record('dependent', kind, {'existing': True})
    assert 'dependent' not in reconcile_unresolved_branch_skips(record)


def test_genuine_branch_selection_and_explicit_fallback_remain_skipped():
    record = checkpoint()
    record['fallback_resolutions'] = {'work': {'inactive_alternatives': ['dependent']}}
    assert 'dependent' not in reconcile_unresolved_branch_skips(record)
    record = checkpoint()
    record['node_states']['work']['status'] = 'completed'
    assert reconcile_unresolved_branch_skips(record) == []
    record = checkpoint()
    record['events'] = []
    assert reconcile_unresolved_branch_skips(record) == []


def test_unstarted_loop_repairs_are_pending_but_executed_loop_branches_remain_inactive():
    record = checkpoint()
    record['locked_flow'].setdefault('meta', {})['loops'] = [{'loop_id': 'L', 'entry_node_id': 'dependent',
        'node_ids': ['dependent', 'repair'], 'repair_node_ids': ['repair']}]
    record['node_states']['repair'] = {'status': 'skipped', 'reason': 'branch_not_taken'}
    started = copy.deepcopy(record)
    started['loop_states'] = {'parent/L': {'round': 1, 'status': 'completed'}}
    assert reconcile_unresolved_branch_skips(record) == ['dependent', 'done', 'repair']
    assert 'repair' not in reconcile_unresolved_branch_skips(started)
    assert started['node_states']['repair']['status'] == 'skipped'


def test_store_read_projects_corrected_progress_without_rewriting_the_original_file(tmp_path):
    record = checkpoint()
    path = tmp_path / (record['run_id'] + '.json')
    path.write_text(json.dumps(record), encoding='utf-8')
    original_bytes = path.read_bytes()
    store = ExpertRunStore(tmp_path)
    loaded = store.load(record['run_id'])
    assert loaded['node_states']['dependent']['status'] == 'pending'
    assert loaded['node_states']['done']['status'] == 'pending'
    assert loaded['node_states']['independent']['status'] == 'completed'
    assert path.read_bytes() == original_bytes
    public = store.public_for_conversation(record['conversation_id'])[0]
    assert public['node_states']['dependent']['status'] == 'pending'
    assert path.read_bytes() == original_bytes
    loaded_again = ExpertRunStore(tmp_path).public_for_conversation(record['conversation_id'])[0]
    assert loaded_again['node_states']['dependent']['status'] == 'pending'
    assert loaded_again['events'] == public['events']


def test_reconciled_downstream_can_receive_the_existing_scoped_repair_patch():
    from mewcode.experts.assistance import AssistanceRequest
    from mewcode.experts.maintenance_adoption import adopt_assistance_patch
    record = checkpoint()
    record['node_states']['work']['command_receipts'] = []
    reconcile_unresolved_branch_skips(record)
    ledger_for_run(record).record('work', 'artifact', {'path': 'verified-input.txt'})
    request = AssistanceRequest('fix', 'work', 0, 'diagnosis', 'repair missing capability', (), (), 'controller')
    adopt_assistance_patch(record, request, {'nodes': [
        {'node_id': 'dependent', 'tools': ['ReadFile'], 'responsibility': '核对已有产物及原始输入。'},
    ]})
    dependent = next(n for n in record['locked_flow']['nodes'] if n['node_id'] == 'dependent')
    assert dependent['tools'] == ['ReadFile']
    assert record['node_states']['dependent']['status'] == 'pending'
    assert any(f.kind == 'artifact' and f.payload.get('path') == 'verified-input.txt'
               for f in ledger_for_run(record).view('work').history)
    assert record['node_states']['work']['status'] != 'completed'


@pytest.mark.parametrize('status', ['in_flight', 'outcome_unknown', 'effect_unknown', 'effect_partial'])
def test_patch_cannot_archive_unknown_original_effects(status):
    from mewcode.experts.assistance import AssistanceRequest
    from mewcode.experts.maintenance_adoption import adopt_assistance_patch
    record = checkpoint()
    record['node_states']['work']['command_receipts'][0]['status'] = status
    reconcile_unresolved_branch_skips(record)
    original = copy.deepcopy(record)
    request = AssistanceRequest('fix', 'work', 0, 'diagnosis', 'repair', (), (), 'controller')
    with pytest.raises(ValueError, match='原操作效果'):
        adopt_assistance_patch(record, request, {'nodes': [{'node_id': 'dependent', 'tools': ['ReadFile']}]})
    assert record == original


@pytest.mark.parametrize('pending', [
    {'pending_permission': {'tool_id': 'p'}},
    {'pending_interaction': {'request_id': 'q', 'tool': 'AskUserQuestion', 'questions': []}},
])
def test_patch_cannot_archive_permission_or_unanswered_question(pending):
    from mewcode.experts.assistance import AssistanceRequest
    from mewcode.experts.maintenance_adoption import adopt_assistance_patch
    record = checkpoint()
    record['node_states']['work']['command_receipts'] = []
    record['node_states']['work'].update(pending)
    reconcile_unresolved_branch_skips(record)
    original = copy.deepcopy(record)
    request = AssistanceRequest('fix', 'work', 0, 'diagnosis', 'repair', (), (), 'controller')
    with pytest.raises(ValueError, match='原问题'):
        adopt_assistance_patch(record, request, {'nodes': [{'node_id': 'dependent', 'tools': ['ReadFile']}]})
    assert record == original


@pytest.mark.parametrize('nodes', [[], [{'node_id': 'dependent'}], [None]])
def test_empty_or_malformed_patch_cannot_buy_a_new_epoch(nodes):
    from mewcode.experts.assistance import AssistanceRequest
    from mewcode.experts.maintenance_adoption import adopt_assistance_patch
    record = checkpoint()
    record['node_states']['work']['command_receipts'] = []
    reconcile_unresolved_branch_skips(record)
    original = copy.deepcopy(record)
    request = AssistanceRequest('fix', 'work', 0, 'diagnosis', 'repair', (), (), 'controller')
    with pytest.raises(ValueError):
        adopt_assistance_patch(record, request, {'nodes': nodes})
    assert record == original


@pytest.mark.asyncio
@pytest.mark.parametrize('patch', [{'nodes': []}, {'nodes': [None]},
                                  {'nodes': [{'node_id': 'work', 'model': {'model_id': 'unapproved'}}]}])
async def test_rejected_patch_retains_the_helper_receipt_usage_and_original_checkpoint(tmp_path, patch):
    from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
    from mewcode.experts.assistance import AssistanceCoordinator, AssistanceRequest, RunContext
    from unittest.mock import AsyncMock

    record = checkpoint()
    store = ExpertRunStore(tmp_path / 'runs')
    flow = ExpertFlowSpec.from_value(record['locked_flow'])
    store.create(run_id=record['run_id'], conversation_id='rejected-patch', task='task', attachments=[],
        profile={}, strategy='stable', flow=flow)
    store.mutate(record['run_id'], lambda r: r['node_states'].setdefault('work', {}).update(
        status='needs_attention', handoff={'status': 'needs_attention', 'blocker_kind': 'capability_missing'},
        messages=[{'role': 'assistant', 'content': '原节点证据'}]))
    original = store.load(record['run_id'])

    async def dispatch(helper, prompt, policy):
        policy.assistance_holder.update(next_step='rebase', patch=patch, notes='change method')
        return ExpertRoleResult('done', status='completed', billable_tokens=234)

    coordinator = AssistanceCoordinator(store=store, run_id=record['run_id'], dispatch=dispatch,
        emit=AsyncMock(), before_resume=AsyncMock(), task='task', work_dir=str(tmp_path), model={})
    request = AssistanceRequest('rejected-patch', 'work', 0, 'diagnosis', 'repair', (), (), 'controller')
    receipt = await coordinator.run(request, RunContext(flow.node_map['work'], ExpertExecutionPolicy('stable', False, 0)))
    after = store.load(record['run_id'])
    stage = after['node_states']['work']
    assert receipt.next_step == 'ask_human'
    assert '修订未接纳' in receipt.notes
    assert stage['assistance_receipts'][-1]['next_step'] == 'ask_human'
    assert stage['assistance_usage'][request.request_id] == 234
    assert stage['messages'] == original['node_states']['work']['messages']
    assert stage.get('epoch', 0) == original['node_states']['work'].get('epoch', 0)
    assert stage['status'] != 'completed'
    assert after['locked_flow'] == original['locked_flow']


def test_unstarted_loop_repair_node_accepts_method_patch_without_changing_graph_or_acceptance():
    from mewcode.experts.assistance import AssistanceRequest
    from mewcode.experts.maintenance_adoption import adopt_assistance_patch
    from mewcode.orchestration.convert import to_expert_flow
    from mewcode.orchestration.spec import OrchestrationSpec
    from tests.test_directed_loop_repair import five_stage_spec
    spec = five_stage_spec().as_dict()
    spec['nodes'].insert(0, {'node_id': 'prepare', 'kind': 'plan', 'label': 'prepare', 'goal': '准备原材料'})
    spec['edges'].insert(0, {'from': 'prepare', 'to': 'first'})
    flow = to_expert_flow(OrchestrationSpec.from_value(spec)).as_dict()
    record = {'locked_flow': flow, 'node_states': {'prepare': {'status': 'needs_attention'},
        'repair': {'status': 'pending'}}, 'node_ledgers': {}}
    before = copy.deepcopy(flow)
    request = AssistanceRequest('loop-fix', 'prepare', 0, 'diagnosis', 'repair method', (), (), 'controller')
    adopt_assistance_patch(record, request, {'nodes': [{'node_id': 'repair', 'tools': ['Bash'],
        'responsibility': '按审计清单补充核验已有页面。'}]})
    assert next(n for n in record['locked_flow']['nodes'] if n['node_id'] == 'repair')['tools'] == ['Bash']
    assert record['locked_flow']['edges'] == before['edges']
    assert record['locked_flow']['meta'] == before['meta']


@pytest.mark.asyncio
async def test_submission_resume_executes_the_original_stages_and_real_audit_repair_loop(tmp_path):
    from mewcode.expert_group import ExpertGroupOrchestrator, ExpertProfile, ExpertRoleResult
    from mewcode.agent import AskUserRequest
    from mewcode.experts.waits import wait_for_interaction
    from mewcode.model_registry import ModelRef
    from mewcode.orchestration.convert import to_expert_flow
    from mewcode.orchestration.spec import OrchestrationSpec
    from tests.test_directed_loop_repair import five_stage_spec
    from tests.test_generic_loops import report

    spec = five_stage_spec().as_dict()
    spec['nodes'].insert(0, {'node_id': 'prepare', 'kind': 'plan', 'label': 'prepare', 'goal': '调查输入'})
    spec['edges'].insert(0, {'from': 'prepare', 'to': 'first'})
    flow = to_expert_flow(OrchestrationSpec.from_value(spec))
    run_id = 'b' * 32
    store = ExpertRunStore(tmp_path / 'runs')
    store.create(run_id=run_id, conversation_id='submission-resume', task='task', attachments=[],
        profile={}, strategy='stable', flow=flow)
    valid_answer = False
    completed, calls, events = [], [], []
    session = SimpleNamespace()

    async def runner(node, _model, _prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == 'prepare':
            stage = store.load(run_id)['node_states'].get('prepare', {})
            pending = stage.get('pending_interaction')
            request = AskUserRequest('original-question', 'AskUserQuestion', [
                {'name': 'direction', 'message': '使用哪组已提供输入？', 'type': 'text'}],
                asyncio.get_running_loop().create_future())

            async def emit(event):
                if event.get('type') == 'ask_user':
                    request.future.set_result({'direction': 'provided inputs'} if valid_answer else {})

            await wait_for_interaction(request, kind='ask_user', session=session, emit=emit,
                store=store, run_id=run_id, stage_key='prepare', resumed=bool(pending))
        completed.append(node.node_id)
        decision, verdict = None, None
        if node.node_id in {'audit', 'decide'}:
            state = 'FAIL' if policy.loop_context['round'] == 1 else 'PASS'
            decision = report(policy.loop_context, state)
            if state == 'FAIL':
                decision['repair_node_ids'] = ['repair']
            verdict = {'verdict': 'PASS' if state == 'PASS' else 'NEEDS_ATTENTION'}
        return ExpertRoleResult('已核验：' + node.node_id, status='completed', loop_decision=decision, verdict=verdict)

    async def fallback(*_args):
        raise AssertionError('only the original graph runner owns execution')

    ref = ModelRef('test', 'test', '')
    orchestrator = ExpertGroupOrchestrator(runner=fallback, node_runner=runner, run_store=store,
        event_sink=lambda event: events.append(copy.deepcopy(event)), archive_root=tmp_path / 'archives')
    args = dict(task='task', profile=ExpertProfile(ref, ref, ref), catalog={}, strategy='stable', run_id=run_id, flow=flow)
    paused = await orchestrator.run(**args)
    snapshot = store.load(run_id)
    assert paused.status == 'paused'
    assert completed == []
    assert calls == ['prepare']
    assert snapshot['node_states']['prepare']['pending_interaction']['request_id'] == 'original-question'
    assert not any(s.get('status') in {'skipped', 'completed'} for s in snapshot['node_states'].values())
    valid_answer = True
    resumed = await orchestrator.run(**args, attempt=2, checkpoint=snapshot)
    assert resumed.status == 'completed'
    assert completed == ['prepare', 'first', 'second', 'audit', 'decide', 'repair', 'audit', 'decide', 'done']
    assert any(e.get('loop_state', {}).get('round') == 2 for e in events)
    assert not any(e.get('reason') == 'branch_not_taken' for e in events)
