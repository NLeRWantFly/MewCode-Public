"""Restored inputs, resolved incidents and one owner for loop slices."""
import asyncio
import copy
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest

from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.blocking_policy import record_node_preflight
from mewcode.kernel.escalation import decide_and_record, ensure_owners, used_levels


@pytest.mark.parametrize('failure', ['', 'probe', 'manifest', 'unrelated', 'acceptance', 'legacy_unproven'])
def test_preflight_closes_only_matching_dispatch_incident(failure):
    manifest = {'approved': True, 'nodes': {'check': {}}, 'workspace': 'project'}
    record = {'node_states': {'check': {}}, 'authorization_manifest': manifest}
    facts = ledger_for_run(record)
    fault = facts.record('check', 'blocker', {'kind': 'environment_fault',
        'ref': 'collaboration-fault:dispatch' if failure != 'unrelated' else 'tool:command',
        'fault_origin': None if failure == 'legacy_unproven' else 'execution' if failure == 'acceptance' else 'node_preflight'})
    # The old host consumed all rungs, then reserved an escalation.
    for index in range(4):
        choice = decide_and_record(record, 'check', trigger='environment_fault', identity=str(index))
    record['node_states']['check']['pending_recovery_decision'] = {'epoch': 0, 'decision': asdict(choice)}
    before = copy.deepcopy(facts.view('check').history)
    findings = [{'severity': 'error', 'code': 'missing_executable'}] if failure == 'probe' else []
    record_node_preflight(record, 'check', findings, {} if failure == 'manifest' else manifest)
    view = facts.view('check')
    assert view.history[:len(before)] == before
    if failure:
        assert fault.fact_id in {f.fact_id for f in view.active_blockers}
        assert record['node_states']['check']['pending_recovery_decision']
        assert 'replan_node' in used_levels(view)
    else:
        ensure_owners(record)
        assert not facts.view('check').active_blockers
        assert not record['node_states']['check'].get('pending_recovery_decision')
        assert not used_levels(view)
        assert decide_and_record(record, 'check', trigger='segment_exhausted', identity='later').level == 'retry_same_method'


def test_clearing_one_incident_does_not_replenish_unresolved_incident():
    record = {'node_states': {'check': {}}, 'authorization_manifest': {'approved': True, 'nodes': {'check': {}}}}
    facts = ledger_for_run(record)
    facts.record('check', 'blocker', {'kind': 'environment_fault', 'ref': 'collaboration-fault:dispatch', 'fault_origin': 'node_preflight'})
    other = facts.record('check', 'blocker', {'kind': 'capability_missing', 'ref': 'tool:command'})
    for index in range(3):
        decide_and_record(record, 'check', trigger='failure', identity=str(index))
    record_node_preflight(record, 'check', [], record['authorization_manifest'])
    assert [fact.fact_id for fact in facts.view('check').active_blockers] == [other.fact_id]
    assert decide_and_record(record, 'check', trigger='failure', identity='later').action == 'ask_human'


@pytest.mark.parametrize('intervening', [False, True])
def test_legacy_preflight_requires_uninterrupted_host_provenance(intervening):
    record = {'node_states': {'check': {}}, 'authorization_manifest': {'approved': True, 'nodes': {'check': {}}}}
    facts = ledger_for_run(record)
    fault = facts.record('check', 'blocker', {'kind': 'environment_fault', 'ref': 'collaboration-fault:old'})
    record['events'] = [{'type': 'expert_constraint_blocked', 'node_id': 'check',
        'server_time': fault.created_at - 2, 'text': '当前节点环境预检未通过：[]'}]
    if intervening:
        record['events'].append({'type': 'expert_tool_start', 'node_id': 'check',
            'server_time': fault.created_at - 1})
    for index in range(3):
        decide_and_record(record, 'check', trigger='failure', identity=str(index))
    for row in record['node_ledgers']['check']['facts']:
        row['payload'].pop('recovery_blocker_ids', None)
    record_node_preflight(record, 'check', [], record['authorization_manifest'])
    assert bool(facts.view('check').active_blockers) is intervening
    assert bool(used_levels(facts.view('check'))) is intervening


@pytest.mark.asyncio
async def test_loop_slice_yields_without_consuming_inner_recovery():
    from tests.test_iteration_recovery import SlicedAgent
    from mewcode.conversation import ConversationManager
    from mewcode.experts.iteration_recovery import recover_iteration_stream
    agent, state = SlicedAgent(), {}
    controller = AsyncMock(side_effect=AssertionError('loop scheduler owns recovery'))
    events = [event async for event in recover_iteration_stream(agent, ConversationManager(),
        state=state, save=lambda: None, emit=AsyncMock(), ask=AsyncMock(), remaining=lambda: True,
        progress=lambda: {}, node_id='check', role='auditor', batch_handoff=True, controller=controller)]
    assert len(agent.calls) == 1 and events[-1].kind == 'max_iterations'
    assert not state.get('decisions') and not state.get('controller_record')


@pytest.mark.asyncio
async def test_stage_evidence_counts_once_but_indexes_and_claims_do_not():
    from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams
    from mewcode.experts.recovery_state import observed_progress_marker, advanced
    reader = ReadStageResult({'audit': {'handoff': {'inline_outputs': [{'name': 'issues', 'content': 'four fixes'}]}}})
    record = {'node_states': {'check': {'tools': {}}}}
    baseline = observed_progress_marker(record, 'check')
    for identity, node in [('index', ''), ('read', 'audit'), ('repeat', 'audit')]:
        result = await reader.execute(ReadStageResultParams(node_id=node))
        record['node_states']['check']['tools'][identity] = {'name': 'ReadStageResult',
            'args': {'node_id': node}, 'status': 'completed', 'is_error': False,
            'host_observation': result.host_observation}
        marker = observed_progress_marker(record, 'check')
        assert advanced(baseline, marker) is (identity == 'read')
        baseline = marker


def test_duplicate_loop_notification_commits_once(tmp_path, monkeypatch):
    from tests.test_tool_performance import RUN, running_store
    from tests.test_loop_checkpoint_performance import notification
    store = running_store(tmp_path)
    event = notification(tokens_used=10)
    store.record_loop_state(RUN, event)
    before = store.load(RUN)
    monkeypatch.setattr(store, '_commit', lambda *a, **k: pytest.fail('duplicate commit'))
    store.record_loop_state(RUN, {**copy.deepcopy(event), 'run_id': RUN})
    assert store.load(RUN) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['', 'hash', 'criteria', 'repair', 'not_reviewer', 'incomplete'])
async def test_loop_control_delivery_requires_current_valid_submission(tmp_path, invalid):
    import json
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.kernel.acceptance import Acceptance
    from mewcode.orchestration.stage_sources import loop_decision_handoff
    context = {'contract_hash': 'frozen', 'criteria': [{'criterion_id': 'quality'}],
        'node_ids': ['repair', 'check'], 'reviewer_node_ids': ['check'], 'control_mode': 'team'}
    decision = {'contract_hash': 'frozen', 'decision': 'CONTINUE', 'repair_node_ids': ['repair'],
        'checks': [{'criterion_id': 'quality', 'state': 'FAIL', 'evidence': 'four defects', 'location': 'audit'}]}
    if invalid == 'hash':
        decision['contract_hash'] = 'stale'
    if invalid == 'criteria':
        decision['checks'] = []
    if invalid == 'repair':
        decision['repair_node_ids'] = ['outside']
    source = ExpertRoleResult(text='submitted', status='paused' if invalid == 'incomplete' else 'completed',
        loop_decision=decision)
    result = await loop_decision_handoff(source, 'repair' if invalid == 'not_reviewer' else 'check', context)
    if invalid:
        assert result is source and not result.handoff
        return
    assert not source.handoff
    assert json.loads(result.handoff['inline_outputs'][0]['content']) == decision
    acceptance = Acceptance(tmp_path)
    report = await acceptance.check({'checks': [{'id': 'quality', 'version': 1, 'kind': 'review'}]},
        result, context={'phase': 'handoff'})
    assert report['ready'] and not report['passed']
    # A control receipt cannot satisfy an independently required file.
    report = await acceptance.check({'checks': [{'id': 'file', 'version': 1,
        'kind': 'file_exists', 'path': 'required.txt'}]}, result, context={'phase': 'handoff'})
    assert not report['ready'] and not report['passed']


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_envelope', [False, True])
async def test_restored_audit_read_drives_real_loop_decision_and_repair(tmp_path, monkeypatch, legacy_envelope):
    from tests.test_directed_loop_repair import five_stage_spec
    from tests.test_generic_loops import report
    from tests.test_collaboration_mode import RUN_ID, delivery, flow
    from tests.test_iterative_collaboration import retry_store
    from tests.test_collaboration_integration import orchestrator, run
    from mewcode.experts.flow import ExpertFlowSpec
    from mewcode.orchestration.convert import to_expert_flow
    from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams
    from mewcode.tools.submit_loop_decision import SubmitLoopDecisionTool, SubmitLoopDecisionParams
    from mewcode.tools.submit_verdict import VerdictHolder
    raw = to_expert_flow(five_stage_spec()).as_dict()
    raw['meta'].update(flow().meta)
    raw['meta']['node_plan_briefs'] = {node['node_id']: {'known_inputs': ['goal'],
        'outputs': ['delivery'], 'acceptance': ['frozen checks']} for node in raw['nodes']}
    raw['meta']['loops'][0].update(control_mode='team', token_budget=None, timeout_seconds=None)
    selected = ExpertFlowSpec.from_value(raw)
    store = retry_store(tmp_path, selected)
    calls, receipts = [], []
    resumed = False
    async def runner(node, model, prompt, policy, *_):
        context = policy.loop_context or {}
        calls.append(node.node_id)
        if node.node_id == 'decide' and not resumed and (not legacy_envelope or calls.count('decide') == 2):
            raise asyncio.CancelledError()
        result = delivery('four audit fixes' if node.node_id == 'audit' and context['round'] == 1 else node.node_id)
        if node.node_id in {'audit', 'decide'}:
            decision = report(context, 'FAIL' if context['round'] == 1 else 'PASS')
            if decision['decision'] == 'CONTINUE':
                decision['repair_node_ids'] = ['repair']
            if node.node_id == 'decide':
                read = await ReadStageResult(policy.stage_inputs).execute(ReadStageResultParams(node_id='audit'))
                assert not read.is_error
                if context['round'] == 1:
                    assert 'four audit fixes' in read.output
                    assert policy.stage_inputs['audit']['loop_decision']['decision'] == 'CONTINUE'
                holder = VerdictHolder()
                submitted = await SubmitLoopDecisionTool(holder, context).execute(SubmitLoopDecisionParams(**decision))
                assert not submitted.is_error
                receipts.append(holder.read())
                # The real control node submits only SubmitLoopDecision. It
                # does not call SubmitHandoff or invent a separate artifact.
                result = replace(result, handoff=None)
            result = replace(result, loop_decision=decision)
        return result
    review = AsyncMock(return_value={'passed': True, 'evidence': ['checked fixed delivery']})
    from mewcode.orchestration import stage_sources
    normalize = stage_sources.loop_decision_handoff
    if legacy_envelope:
        async def old_host(result, *_):
            return result
        monkeypatch.setattr(stage_sources, 'loop_decision_handoff', old_host)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run(orchestrator(store, tmp_path, runner, review), store, selected), 240)
    assert calls == ['first', 'second', 'audit', 'decide'] + (['decide'] if legacy_envelope else [])
    before = store.load(RUN_ID)
    rejected = [copy.deepcopy(a) for a in before['collaboration']['attempts'].values()
        if a['contract_id'] == 'decide' and a['status'] == 'rejected']
    certificate = copy.deepcopy(before['collaboration']['certificates']['audit'])
    store.mutate(RUN_ID, lambda record: record.update(status='paused', active_node_ids=[]))
    store.resume(RUN_ID, strategy='stable')
    assert store.load(RUN_ID)['collaboration']['certificates']['audit'] == certificate
    monkeypatch.setattr(stage_sources, 'loop_decision_handoff', normalize)
    resumed = True
    result = await asyncio.wait_for(run(orchestrator(store, tmp_path, runner, review), store, selected), 180)
    assert result.status == 'completed', result.final_summary
    assert calls == ['first', 'second', 'audit', 'decide', 'decide', 'repair', 'audit', 'decide', 'done']
    assert [receipt['decision'] for receipt in receipts] == ['CONTINUE', 'EXIT']
    final = store.load(RUN_ID)['collaboration']
    assert not final.get('recovery_gates')
    if legacy_envelope:
        assert len(rejected) == 1
        assert final['attempts'][rejected[0]['id']] == rejected[0]
        assert any(a.get('control_delivery_rechecked_from') == rejected[0]['id']
            and a['status'] == 'accepted' and a['executor_model'] == rejected[0]['executor_model']
            for a in final['attempts'].values())


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['', 'inputs', 'round', 'model', 'permission', 'retry', 'rework', 'invalid_decision'])
async def test_control_recheck_preserves_original_and_rejects_changed_basis(tmp_path, change):
    from tests.test_collaboration_mode import core, EXECUTOR, REVIEWER
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.kernel import collaboration as ledger
    from mewcode.kernel.acceptance import Acceptance
    from mewcode.orchestration.stage_sources import retained_loop_decision
    value = core()
    context = {'contract_hash': 'frozen', 'criteria': [{'criterion_id': 'quality'}],
        'node_ids': ['work', 'repair'], 'reviewer_node_ids': ['work'], 'control_mode': 'team'}
    result = ExpertRoleResult(text='submitted', loop_decision={'contract_hash': 'frozen',
        'decision': 'CONTINUE', 'repair_node_ids': ['repair'], 'checks': [
            {'criterion_id': 'quality', 'state': 'FAIL', 'evidence': 'four defects', 'location': 'audit'}]},
        input_tokens=10, output_tokens=4, billable_tokens=14, iterations_used=1, active_seconds=3)
    identity = ledger.dispatch(value, 'work')['id']
    source = value['attempts'][identity]
    source.update(status='checking', delivery=asdict(result), executor_model=EXECUTOR.as_dict())
    acceptance = Acceptance(tmp_path)
    report = await acceptance.check(value['contracts']['work'], result, context={'phase': 'handoff'})
    assert report['reason'] == '交接缺少可回读的文件或内联产物'
    ledger.complete(value, identity, report)
    current = ledger.dispatch(value, 'work')
    if change == 'inputs':
        value['contracts']['work']['version'] += 1
    if change == 'round':
        value['contracts']['work']['iteration'] = {'round': 2}
    value['attempts'][current['id']]['dependencies'] = ledger.dependencies(value, 'work')
    if change == 'permission':
        source['report']['execution_error'] = 'missing_authorization'
    if change == 'retry':
        value['retry_generations'] = {'work': 1}
    if change == 'rework':
        value['rework_instructions'] = {'work': {'issues': ['new issue']}}
    if change == 'invalid_decision':
        source['delivery']['loop_decision']['contract_hash'] = 'stale'
    before = copy.deepcopy(source)
    retained = await retained_loop_decision(value, 'work', context,
        REVIEWER if change == 'model' else EXECUTOR, current['id'])
    assert source == before
    if change:
        assert retained is None
        return
    origin, repaired = retained
    assert origin['id'] == identity and repaired.restored
    assert repaired.loop_decision == result.loop_decision
    assert repaired.input_tokens == repaired.output_tokens == repaired.billable_tokens == 0
    fresh = await acceptance.check(value['contracts']['work'], repaired, context={'phase': 'handoff'})
    assert fresh['ready'] and not fresh['passed']
