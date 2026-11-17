"""Progress, not attempts: persistent recovery safe-point decisions."""
import copy
from dataclasses import asdict

import pytest

from mewcode.experts.recovery_boundary import RecoveryBoundary, host_progress
from mewcode.experts.recovery_case import CaseRound, advance, cases, method_fingerprint
from mewcode.experts.case_runtime import _save_investigation_request, answer_case
from mewcode.experts.waits import BranchWaitPaused
from tests.test_recovery_case_e2e import harness, option, run, RUN


def test_eight_checkpoints_with_progress_never_exhaust_default_quota():
    boundary = RecoveryBoundary()
    for index in range(8):
        row = boundary.check(identity=str(index), method='same incremental repair', seconds=500,
                             evidence_refs=[f'cleared:blocker-{index}'])
        assert row['decision'] == 'continue'
        boundary = RecoveryBoundary(boundary.state)  # simulated process restart
    assert len(boundary.state['checks']) == 8
    same = boundary.check(identity='7', method='same incremental repair', seconds=500,
                          evidence_refs=['cleared:blocker-7'])
    assert same == boundary.state['checks'][-1]
    assert boundary.state['active_seconds'] == 4000


def test_log_or_error_changes_are_not_progress_and_evidence_cannot_be_reused():
    assert host_progress([], [], operation_restored=False,
                         probe={'passed': True, 'evidence_refs': ['new-log'], 'error': 'different'}) == []
    boundary = RecoveryBoundary()
    assert boundary.check(identity='1', method='a', seconds=1, evidence_refs=['cleared:x'])['decision'] == 'continue'
    assert boundary.check(identity='2', method='b', seconds=1, evidence_refs=['cleared:x'])['decision'] == 'review'
    assert boundary.check(identity='3', method='c', seconds=1)['decision'] == 'converge'


def test_overdue_is_soft_success_wins_and_hard_gate_is_recorded():
    boundary = RecoveryBoundary()
    check = boundary.check(identity='slow', method='read', seconds=150, expected_seconds=100)
    assert check['trigger'] == 'overdue' and check['decision'] == 'review'
    assert boundary.check(identity='restored', method='read', seconds=800,
                          expected_seconds=100, restored=True)['decision'] == 'resolved'
    check = boundary.check(identity='limit', method='new', seconds=1, hard_reason='用户设定的时间已用完')
    assert check['trigger'] == 'hard_gate' and check['decision'] == 'converge'


def test_next_model_plan_is_review_and_must_not_repeat_without_evidence():
    selected = option(1)
    boundary = RecoveryBoundary()
    boundary.check(identity='1', method=method_fingerprint(selected['strategy']), seconds=1)
    assert boundary.assess_plan({}, selected)
    changed = option(2)
    assert boundary.assess_plan({'boundary_review': {'action': 'continue', 'reason': 'test a different precondition',
                                                   'next_step': 'check the project interpreter'}}, changed) is None


def test_legacy_max_rounds_is_not_an_execution_limit():
    case = {'case_id': 'old', 'status': 'open', 'rounds': [], 'events': [], 'max_rounds': 1}
    for index in range(8):
        row = asdict(CaseRound(index + 1, 'step', 'method', 'unclassified', verification={'result': 'unchanged'}))
        case = advance(case, {'kind': 'finish', 'identity': str(index), 'round': row})
        assert case['status'] == 'open'


@pytest.mark.asyncio
async def test_runtime_stops_repeated_no_progress_without_business_success(tmp_path):
    h = harness(tmp_path, outcomes=[{'passed': False, 'error': 'still blocked'}] * 3)
    with pytest.raises(BranchWaitPaused):
        await run(h)
    case = cases(h.store.load(RUN))[0]
    assert len(case['rounds']) == 2
    assert case['status'] == 'reported'
    assert case['boundary']['checks'][-1]['decision'] == 'converge'
    assert h.store.load(RUN)['node_states']['work']['status'] != 'completed'


@pytest.mark.asyncio
async def test_runtime_explicit_budget_stops_before_execution(tmp_path):
    from dataclasses import replace
    h = harness(tmp_path)
    h.policy = replace(h.policy, token_budget=0)
    with pytest.raises(BranchWaitPaused):
        await run(h)
    assert h.calls == []
    assert cases(h.store.load(RUN))[0]['status'] == 'reported'


def test_legacy_retry_requires_direction_and_does_not_grant_authority(tmp_path):
    from tests.test_recovery_checkpoint_choices import checkpoint
    from mewcode.experts.recovery_case import get_case, save_case
    _, store, run_id, identity = checkpoint(tmp_path)
    record = store.load(run_id)
    case = get_case(record, identity)
    case['pending']['options'].append({'value': 'retry'})
    save_case(record, case)
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match='具体处理方案'):
        answer_case(record, identity, round=0, choice='retry')
    assert record == before
    updated = answer_case(record, identity, round=0, choice='retry', answer='核对已安装的另一个解释器')
    assert updated['status'] == 'open'
    assert updated['continuation_direction']
    assert not record.get('authorizations', {}).get('entries')


@pytest.mark.asyncio
async def test_runtime_eight_host_clearances_same_method_resume_original_node(tmp_path):
    from mewcode.experts.node_ledger import ledger_for_run
    h = harness(tmp_path, make_options=lambda n: [option(1)])
    blockers = []
    def seed(record):
        ledger = ledger_for_run(record)
        for i in range(8):
            blockers.append(ledger.record('work', 'blocker', {'kind': 'external_dependency', 'message': f'condition {i}'}).fact_id)
    h.store.mutate(RUN, seed)
    count = 0
    async def verify(node, selected):
        nonlocal count
        fact_id = blockers[count]
        h.store.mutate(RUN, lambda record: ledger_for_run(record).record('work', 'clearance',
            {'blocker_fact_id': fact_id, 'kind': 'host_verified'}, source='host'))
        count += 1
        return {'passed': count == 8, 'operation_restored': count == 8,
                'evidence_refs': ['original-step'] if count == 8 else []}
    h.coordinator.probe = verify
    result = await run(h)
    record = h.store.load(RUN)
    case = cases(record)[0]
    assert result['action'] == 'continue_checkpoint'
    assert count == len(case['rounds']) == len(case['boundary']['checks']) == 8
    assert all(check['decision'] == 'continue' for check in case['boundary']['checks'][:-1])
    assert case['boundary']['checks'][-1]['decision'] == 'resolved'
    assert record['node_states']['work']['status'] == 'running'
    assert not any(event['case']['status'] in {'reported', 'awaiting_user'}
                   for event in h.events if event['type'] == 'recovery_case')


@pytest.mark.asyncio
async def test_internal_investigation_updates_report_without_user_choice_or_permission(tmp_path):
    h = harness(tmp_path, root='capability_absent')
    with pytest.raises(BranchWaitPaused):
        await run(h)
    case = cases(h.store.load(RUN))[0]
    before = len(h.calls)
    assert case['handling']['owner'] == 'system' and not case['pending']
    h.store.mutate(RUN, lambda record: _save_investigation_request(record, case, {},
        identity='host:readonly-investigation', instruction='查找项目里已经安装的解释器', note=''))
    with pytest.raises(BranchWaitPaused):
        await run(h)
    record = h.store.load(RUN)
    updated = cases(record)[0]
    assert updated['status'] == 'reported'
    assert len(h.calls) > before
    assert all(call[2].recovery_analysis and call[2].parallel_readonly for call in h.calls[before:])
    assert not record.get('authorizations', {}).get('entries')
    assert record['node_states']['work']['status'] != 'completed'
    assert not updated.get('investigation_requested')


@pytest.mark.asyncio
async def test_eight_partial_artifacts_under_one_blocker_are_material_progress(tmp_path):
    from mewcode.experts.node_ledger import ledger_for_run
    h = harness(tmp_path, make_options=lambda n: [option(1)])
    blocker = []
    h.store.mutate(RUN, lambda r: blocker.append(ledger_for_run(r).record(
        'work', 'blocker', {'kind': 'external_dependency', 'message': 'all books required'}).fact_id))
    analyze = h.coordinator.analyst.analyze
    goal = {'target': 'Build all books', 'stop_when': 'Original step passes', 'milestones': [
        {'id': str(i), 'label': f'book {i}', 'evidence': f'file:book-{i}.json', 'expected_seconds': 100}
        for i in range(8)]}
    async def with_goal(*args, **kwargs):
        p = await analyze(*args, **kwargs)
        p['diagnosis'] = {'goal': goal}
        return p
    h.coordinator.analyst.analyze = with_goal
    dispatch = h.coordinator.executor.dispatch
    count = 0
    async def write_artifact(helper, prompt, policy):
        nonlocal count
        if not policy.recovery_analysis:
            (tmp_path / f'book-{count}.json').write_text('{"chapters": [1]}')
            count += 1
            h.store.mutate(RUN, lambda r: r['node_states'].setdefault(helper.node_id, {}).update(status='completed', handoff={'status': 'completed'}))
        return await dispatch(helper, prompt, policy)
    h.coordinator.executor.dispatch = write_artifact
    async def verify(node, selected):
        if count == 8:
            h.store.mutate(RUN, lambda r: ledger_for_run(r).record('work', 'clearance',
                {'blocker_fact_id': blocker[0]}, source='host'))
        return {'passed': count == 8, 'operation_restored': count == 8, 'evidence_refs': ['all-books']}
    h.coordinator.probe = verify
    assert (await run(h))['action'] == 'continue_checkpoint'
    c = cases(h.store.load(RUN))[0]
    assert len(c['rounds']) == 8
    assert all(any(ref.startswith('artifact:') for ref in row['evidence_refs']) for row in c['boundary']['checks'])


def test_milestone_files_ignore_outside_empty_and_repeated_artifacts(tmp_path):
    from mewcode.experts.recovery_boundary import milestone_files, milestone_progress
    outside = tmp_path.parent / 'outside.json'
    outside.write_text('private')
    (tmp_path / 'empty.json').write_text('')
    goal = {'milestones': [{'id': 'outside', 'evidence': 'file:../outside.json'},
                          {'id': 'empty', 'evidence': 'file:empty.json'},
                          {'id': 'book', 'evidence': 'file:book.json'}]}
    before = milestone_files(goal, tmp_path)
    assert before == {}
    (tmp_path / 'book.json').write_text('complete')
    after = milestone_files(goal, tmp_path)
    refs = milestone_progress(before, after)
    assert len(refs) == 1 and 'book.json' in refs[0]
    boundary = RecoveryBoundary()
    assert boundary.check(identity='1', method='write', seconds=1, evidence_refs=refs)['decision'] == 'continue'
    (tmp_path / 'book.json').write_text('changed again')
    duplicate = milestone_progress(after, milestone_files(goal, tmp_path))
    assert boundary.check(identity='2', method='write', seconds=1, evidence_refs=duplicate)['decision'] == 'converge'


@pytest.mark.asyncio
async def test_cancel_active_readonly_analysis_via_host_endpoint(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace
    from frontend import server
    from tests.test_recovery_checkpoint_choices import endpoint, close_body
    h = harness(tmp_path, root='capability_absent', session=SimpleNamespace())
    # A standalone coordinator without a session first produces a durable card.
    h.coordinator.session = None
    with pytest.raises(BranchWaitPaused):
        await run(h)
    c = cases(h.store.load(RUN))[0]
    h.store.mutate(RUN, lambda r: _save_investigation_request(r, c, {},
        identity='host:readonly-investigation', instruction='检查宿主保存的具体故障', note=''))
    h.store.mutate(RUN, lambda r: r.update(conversation_id='choices-chat'))
    runtime = endpoint(monkeypatch, tmp_path, h.store)
    h.coordinator.session = runtime
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    async def slow(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    h.coordinator.analyst.analyze = slow
    task = asyncio.create_task(run(h))
    await asyncio.wait_for(entered.wait(), 2)
    body = close_body(tmp_path, RUN, c['case_id']).model_copy(update={'round': len(c['rounds'])})
    response = await server.api_recovery_case_answer(body)
    assert response.status_code == 200, response.body
    with pytest.raises(BranchWaitPaused):
        await asyncio.wait_for(task, 2)
    assert cancelled.is_set()
    after = cases(h.store.load(RUN))[0]
    assert after['closed_by_user'] and not after['pending']
    assert not runtime._case_investigations
