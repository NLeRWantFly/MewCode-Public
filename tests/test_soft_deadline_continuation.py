import copy
from dataclasses import asdict, replace
import hashlib
import time

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.recovery_state import observed_progress_marker, work_progress
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.briefs import begin_stage_attempt
from mewcode.kernel.collaboration_faults import execution_fault
from mewcode.kernel.work_continuation import retain_segment, segment_checkpoint
from mewcode.orchestration.collab_compile import compile_plan
from tests.test_collaboration_mode import CATALOG, PROFILE, REVIEWER, RUN_ID, delivery, flow
from tests.test_productive_work_continuation import checkpoint_fixture


def yielded(kind='soft_deadline'):
    return ExpertRoleResult(text='saved execution slice', status='budget_exhausted', error_kind=kind,
        billable_tokens=1200, handoff={'status': 'needs_attention', 'open_items': ['finish work']})


@pytest.mark.parametrize('kind', ['soft_deadline', 'max_iterations'])
def test_only_explicit_slice_limits_bypass_execution_faults(kind):
    assert not execution_fault(yielded(kind))
    assert execution_fault(yielded('')) == 'run_deadline'
    assert execution_fault(yielded('run_deadline')) == 'run_deadline'
    assert execution_fault(replace(yielded(kind), handoff={'blocker_kind': 'permission_required'})) == 'permission_required'


def segment_fixture():
    record, attempt = checkpoint_fixture()
    stage = record['node_states']['work']
    outcome = yielded()
    stage.update(status=outcome.status, handoff=outcome.handoff)
    checkpoint = retain_segment(record, attempt['id'], asdict(outcome))
    assert checkpoint
    return record, attempt


def test_yield_resumes_same_dispatch_without_new_lineage_budget_or_acceptance():
    record, attempt = segment_fixture()
    value = record['collaboration']
    before = copy.deepcopy(value['lineage_budget'])
    assert ledger.resume_dispatch(record, 'work') is None
    resumed = ledger.resume_dispatch(record, 'work', resume_segment=True)
    assert resumed['id'] == attempt['id'] and resumed['number'] == 1
    assert value['lineage_budget'] == before and not value['certificates']
    assert len(value['attempts']) == 1 and not value.get('recovery_gates')
    assert ledger.resume_dispatch(record, 'work', resume_segment=True) is None


@pytest.mark.parametrize('boundary', ['pause', 'unknown', 'permission', 'question', 'epoch', 'inputs',
                                      'delivery', 'running_tool', 'held', 'run_attempt'])
def test_changed_or_unsettled_boundary_cannot_resume_a_slice(boundary):
    record, attempt = segment_fixture()
    stage = record['node_states']['work']
    if boundary == 'pause':
        record['status'] = 'paused'
    elif boundary == 'unknown':
        record['kernel_calls'] = {'call': {'node_id': 'work', 'state': 'unknown'}}
    elif boundary == 'permission':
        stage['pending_permission'] = {'tool_id': 'call'}
    elif boundary == 'question':
        stage['pending_interaction'] = {'status': 'waiting'}
    elif boundary == 'epoch':
        record['collaboration']['coordinator_epoch'] += 1
    elif boundary == 'inputs':
        stage['input_revision'] = 'changed'
    elif boundary == 'delivery':
        attempt['delivery']['text'] = 'changed'
    elif boundary == 'running_tool':
        stage['tools']['call'] = {'name': 'Bash', 'status': 'running'}
    elif boundary == 'held':
        record['collaboration']['contracts']['work']['held'] = True
    else:
        attempt['run_attempt'] = 2
    assert ledger.resume_dispatch(record, 'work', resume_segment=True) is None


def execution_flow(loop):
    if not loop:
        return flow()
    from tests.test_generic_loops import spec_for
    from mewcode.orchestration.convert import to_expert_flow
    raw = to_expert_flow(spec_for()).as_dict()
    raw['meta'].update(flow().meta)
    raw['meta']['node_plan_briefs'] = {row['node_id']: {
        'known_inputs': ['目标'], 'outputs': ['结果'], 'acceptance': ['结果符合目标']} for row in raw['nodes']}
    raw['meta']['loops'][0].update(control_mode='team', token_budget=None, timeout_seconds=None, max_iterations=None)
    return ExpertFlowSpec.from_value(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize('loop,denied,kind,slices', [(False, False, 'soft_deadline', 1),
    (True, False, 'soft_deadline', 3), (True, True, 'soft_deadline', 1), (True, False, 'max_iterations', 1),
    (True, True, 'completed', 0), (True, True, 'legacy_completed', 0)])
async def test_real_scheduler_continues_same_attempt_and_preserves_usage_and_denials(tmp_path, monkeypatch, loop, denied, kind, slices):
    selected = execution_flow(loop)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    store.create(run_id=RUN_ID, conversation_id='c', task='交付测试结果', attachments=[],
        profile=PROFILE.as_dict(), strategy='stable', flow=selected)
    def initialize(record):
        record['role_models'] = {'roles': {'verifier': REVIEWER.as_dict(), 'reviewer': REVIEWER.as_dict(),
            'executor': PROFILE.executor.as_dict(), 'planner': PROFILE.planner.as_dict()}}
        ledger.initialize(record, compile_plan(selected))
        for contract in record['collaboration']['contracts'].values():
            for check in contract['checks']:
                if check['kind'] == 'review':
                    check['blocking'] = True
        if denied:
            from mewcode.kernel.capabilities import declaration
            from mewcode.experts.node_ledger import ledger_for_run, project_node_view
            record['authorization_manifest'] = {'schema_version': 2, 'approved': True, 'workspace': str(tmp_path),
                'nodes': {'work': declaration({})}}
            ledger_for_run(record).record('work', 'blocker', {'kind': 'missing_authorization',
                'gate_kind': 'authorization_extension',
                'requested_call': {'tool': 'Bash', 'arguments': {'command': 'forbidden_command'}}},
                source='host', identity='denied')
            project_node_view(record, 'work')
    store.mutate(RUN_ID, initialize)
    original_manifest = copy.deepcopy(store.load(RUN_ID).get('authorization_manifest'))
    calls, reviewed, policies = [], [], []

    def event_sink(event):
        if event.get('type') == 'expert_node_started':
            store.mutate(RUN_ID, lambda record: begin_stage_attempt(record, event))
        store.append_event(RUN_ID, event)

    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        first = node.node_id == 'work' and calls.count('work') <= slices
        outcome = yielded(kind) if first else replace(delivery('final ' + node.node_id), billable_tokens=100)
        if kind == 'legacy_completed' and node.node_id == 'work':
            (tmp_path / 'accepted.txt').write_text('accepted OCR result', encoding='utf-8')
            outcome = replace(outcome, handoff={**outcome.handoff, 'artifacts': [{'path': 'accepted.txt'}]})
        if node.node_id == 'work':
            policies.append(policy)
        if node.node_id == 'check':
            from tests.test_generic_loops import report
            outcome = replace(outcome, verdict={'verdict': 'PASS'}, loop_decision=report(policy.loop_context))
        def save(record):
            stage = record['node_states'][node.node_id]
            stage['slice_progress_baseline'] = observed_progress_marker(record, node.node_id)
            stamp = time.time()
            stage.update(status=outcome.status, handoff=copy.deepcopy(outcome.handoff), input_revision=policy.input_revision,
                handoff_input_revision=policy.input_revision, started_at=stamp - 1, completed_at=stamp + 1,
                safety_review_state={}, messages=[{'role': 'assistant', 'content': outcome.text}])
            stage.setdefault('tools', {})['read-' + str(len(calls))] = {
                'name': 'ReadFile', 'args': {'file_path': 'result-' + str(len(calls))},
                'result_sha256': hashlib.sha256(outcome.text.encode()).hexdigest(), 'output': outcome.text,
                'status': 'completed', 'is_error': False, 'node_attempt': stage['node_attempt'],
                'attempt': record['attempt'], 'epoch': stage.get('epoch', 0),
                'started_at': stamp, 'completed_at': stamp + .1}
        store.mutate(RUN_ID, save)
        # The web runner returns raw ledger blockers even after completing a
        # delivery. Exercise that boundary instead of omitting work_progress.
        return replace(outcome, work_progress=work_progress(store.load(RUN_ID), node.node_id))

    async def reviewer(model, payload, context):
        reviewed.append(context['node_id'])
        if context['node_id'] == 'work':
            assert calls.count('work') == slices + 1
            assert payload['host_execution_evidence']['complete']
        return {'passed': True, 'evidence': ['checked final delivery']}

    orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, run_store=store,
        work_dir=tmp_path, archive_root=tmp_path / 'archive', collaboration_review=reviewer, event_sink=event_sink)
    async def run():
        return await orchestrator.run('交付测试结果', PROFILE, CATALOG, strategy='stable', run_id=RUN_ID,
            flow=selected, checkpoint=store.load(RUN_ID))

    if kind == 'legacy_completed':
        # Reproduce the previous host's raw-blocker projection, then resume
        # through the public store API with the actual frozen delivery.
        with monkeypatch.context() as old_host:
            old_host.setattr('mewcode.kernel.work_continuation.segment_progress', work_progress)
            # Reproduce both historical projections. The current loop keeps
            # typed faults; the legacy host mislabeled every raw blocker as
            # awaiting_input, which is the migration this case exercises.
            old_host.setattr('mewcode.kernel.collaboration_faults.continuation_fault',
                            lambda blockers, original='': 'awaiting_input')
            result = await run()
        assert result.status == 'blocked'
        before = copy.deepcopy(store.load(RUN_ID))
        assert calls == ['prepare', 'work']
        assert before['loop_states']['improve']['tokens'] == 100
        assert_restore_boundaries(before, selected, tmp_path)
        store.mutate(RUN_ID, lambda record: record.update(status='blocked', active_node_ids=[]))
        resumed = store.resume(RUN_ID, strategy='stable')
        restored = resumed['loop_states']['improve']
        assert restored['round_results']['work']['restored']
        assert restored['recovery_history'][-1]['reason'] == 'accepted_delivery_restored'
        assert restored['tokens'] == before['loop_states']['improve']['tokens']
        assert restored['elapsed'] == before['loop_states']['improve']['elapsed']
        assert resumed['collaboration']['certificates'] == before['collaboration']['certificates']
        assert resumed['node_ledgers'] == before['node_ledgers']
        result = await run()
        assert calls == ['prepare', 'work', 'check', 'done']
    else:
        result = await run()
    assert result.status == 'completed', result.final_summary
    record = store.load(RUN_ID)
    value = record['collaboration']
    attempts = ledger.attempts_for(value, 'work')
    assert len(attempts) == 1 and attempts[0]['status'] == 'accepted'
    assert calls.count('work') == slices + 1 and all(policy.resume_messages for policy in policies[1:])
    assert not value.get('recovery_gates') and not value['lineage_budget']['grants']
    assert record.get('authorization_manifest') == original_manifest
    if denied:
        from mewcode.kernel.capabilities import call_gap
        assert call_gap(original_manifest, 'work', 'Bash', {'command': 'forbidden_command'})
        assert any(row.get('payload', {}).get('kind') == 'missing_authorization'
                   for row in record['node_states']['work']['ledger_view']['active_blockers'])
    if loop:
        assert record['loop_states']['improve']['tokens'] == 1200 * slices + 200
        assert not record['loop_states']['improve'].get('controller_record')
    assert 'work' in reviewed


def assert_restore_boundaries(before, selected, tmp_path):
    from mewcode.kernel.work_continuation import restore_completed_loop_delivery
    from mewcode.orchestration.loops import LoopContract
    loop = LoopContract.from_value(selected.meta['loops'][0])
    boundaries = ['unknown', 'permission', 'question', 'safety_review', 'epoch', 'inputs',
                  'delivery', 'running_tool', 'held', 'round', 'contract', 'artifact', 'snapshot', 'source']
    for boundary in boundaries:
        record = copy.deepcopy(before)
        record['active_node_ids'] = []
        stage = record['node_states']['work']
        state = record['loop_states']['improve']
        cert = record['collaboration']['certificates']['work']
        file = None
        if boundary == 'unknown':
            record['kernel_calls'] = {'call': {'node_id': 'work', 'state': 'unknown'}}
        elif boundary == 'permission':
            stage['pending_permission'] = {'tool_id': 'call'}
        elif boundary == 'question':
            stage['pending_interaction'] = {'status': 'pending'}
        elif boundary == 'safety_review':
            stage['safety_review_state'] = {'pending_call_ref': 'call'}
        elif boundary == 'epoch':
            record['collaboration']['coordinator_epoch'] += 1
        elif boundary == 'inputs':
            stage['input_revision'] = 'changed'
        elif boundary == 'delivery':
            state['round_results']['work']['text'] = 'changed'
        elif boundary == 'running_tool':
            stage['tools']['call'] = {'name': 'Bash', 'status': 'running'}
        elif boundary == 'held':
            record['collaboration']['contracts']['work']['held'] = True
        elif boundary == 'round':
            state['round'] += 1
        elif boundary == 'contract':
            state['contract_hash'] = 'changed'
        elif boundary == 'source':
            stage['ledger_view']['active_blockers'][0]['source'] = 'model'
        else:
            artifact = next(row for row in cert['artifacts'] if row['kind'] == 'file')
            file = tmp_path / artifact['path' if boundary == 'artifact' else 'snapshot_path']
            content = file.read_bytes()
            file.write_bytes(b'changed')
        unchanged = copy.deepcopy(record)
        try:
            assert not restore_completed_loop_delivery(record, state, loop), boundary
            assert record == unchanged, boundary
        finally:
            if file:
                file.write_bytes(content)


def test_resumed_evidence_includes_only_host_recorded_run_attempts_and_same_node_attempt():
    from mewcode.kernel.execution_evidence import execution_evidence
    from tests.test_execution_evidence import recorded_delivery
    record, attempt = recorded_delivery()
    record.update(status='running', attempt=2)
    assert begin_stage_attempt(record, {'node_id': 'work', 'node_attempt': 1, 'started_at': 20.0})
    stage = record['node_states']['work']
    stage.update(status='completed', completed_at=22.0)
    assert stage['started_at'] == 20.0 and stage['attempt_started_at'] == 10.0
    assert execution_evidence(record, attempt)['complete']
    stage['tools']['receipt']['attempt'] = 3
    assert not execution_evidence(record, attempt)['complete']
    stage['tools']['receipt']['attempt'] = 1
    stage['tools']['receipt']['node_attempt'] = 0
    assert not execution_evidence(record, attempt)['complete']


@pytest.mark.asyncio
@pytest.mark.parametrize('token_limit,expected_calls', [(5000, ['work', 'work', 'check']), (1500, ['work'])])
async def test_strict_parent_keeps_cumulative_budget_when_a_productive_slice_ends(token_limit, expected_calls):
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.experts.loop_runtime import run_bounded_loop
    from mewcode.orchestration.loops import compile_loops
    from mewcode.orchestration.convert import to_expert_flow
    from tests.test_generic_loops import spec_for, report
    raw = to_expert_flow(spec_for()).as_dict()
    raw['meta']['loops'][0]['token_budget'] = token_limit
    selected = ExpertFlowSpec.from_value(raw)
    _, contracts = compile_loops(selected.nodes, selected.edges, selected.meta['loops'])
    calls, states = [], {}
    async def emit(event):
        if event.get('loop_state'):
            states[event['loop_state_key']] = copy.deepcopy(event['loop_state'])
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if calls == ['work']:
            return replace(yielded(), work_progress={
                'before': {'review_complete': 0, 'review_recorded': 0, 'submitted_items': []},
                'after': {'review_complete': 0, 'review_recorded': 0, 'submitted_items': ['saved-item']}})
        assert policy.token_budget == token_limit - (1200 if node.node_id == 'work' else 1300)
        return replace(delivery(), billable_tokens=100,
                       loop_decision=report(policy.loop_context) if node.node_id == 'check' else None)
    _, result = await run_bounded_loop(contract=contracts[0], flow=selected, saved={}, run_node=runner,
        emit=emit, policy_for=lambda node: ExpertExecutionPolicy('stable', False, 0), incoming=[])
    assert calls == expected_calls
    assert (result.status == 'completed') is (token_limit == 5000)
    assert states['improve']['tokens'] == (1400 if token_limit == 5000 else 1200)


@pytest.mark.asyncio
async def test_restored_delivery_does_not_spend_the_loop_token_budget_twice():
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.experts.loop_runtime import run_bounded_loop
    from mewcode.orchestration.loops import compile_loops
    from mewcode.orchestration.convert import to_expert_flow
    from tests.test_generic_loops import spec_for, report
    raw = to_expert_flow(spec_for()).as_dict()
    raw['meta']['loops'][0]['token_budget'] = 5000
    selected = ExpertFlowSpec.from_value(raw)
    _, contracts = compile_loops(selected.nodes, selected.edges, selected.meta['loops'])
    calls, states = [], {}
    async def emit(event):
        if event.get('loop_state'):
            states[event['loop_state_key']] = copy.deepcopy(event['loop_state'])
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        assert policy.token_budget == 2500
        return replace(delivery(), restored=node.node_id == 'work',
            billable_tokens=2500 if node.node_id == 'work' else 100,
            loop_decision=report(policy.loop_context) if node.node_id == 'check' else None)
    _, result = await run_bounded_loop(contract=contracts[0], flow=selected, saved={'improve': {'tokens': 2500}},
        run_node=runner, emit=emit, policy_for=lambda node: ExpertExecutionPolicy('stable', False, 0), incoming=[])
    assert result.status == 'completed' and calls == ['work', 'check']
    assert states['improve']['tokens'] == 2600
