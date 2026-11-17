"""Offline recovery ladder: real store, analyst and assistance coordinator."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.assistance import AssistanceCoordinator
from mewcode.experts.authorization_ledger import covers, record_authorization, authorization_block
from mewcode.experts.capability_broker import BindingResult, binding_receipt, ALTERNATIVES
from mewcode.experts.case_runtime import CaseCoordinator, answer_case
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.recovery_case import cases, active_case, migrate_legacy
from mewcode.experts.waits import BranchWaitPaused
from tests.test_retry_memory import retry_flow

RUN = 'c' * 32
AUTH = {'operation_class': 'external_send', 'scope': {'model_routes': ['test/vision'],
    'content_classes': ['page_image'], 'cost_cap_tokens': 200000}}


def option(n=1, *, authority=False, vision=False):
    return {'option_id': f'method-{n}', 'method': f'方法 {n}',
        'strategy': {'kind': 'bind' if vision else 'probe', 'tools': ['ProbeCapabilities'], 'target': f'target-{n}'},
        'needs': {'capabilities': ['vision'] if vision else [], 'authority': [copy.deepcopy(AUTH)] if authority else [], 'user_input': []},
        'risk': 'low', 'reversible': True, 'changes_acceptance': False,
        'verification': {'probe': '核对实际宿主证据', 'expected_evidence': '工具回执', 'tool': 'ProbeCapabilities', 'arguments': {}}}


def plan(context, options, root='unclassified'):
    return {'case_id': context['case_id'], 'round': context['round'],
        'root_cause': {'class': root, 'summary': '宿主证据表明当前能力未准备好',
            'evidence_refs': ['dossier:' + context['case_id']], 'confidence': 'high'},
        'options': options, 'recommended_option_id': options[0]['option_id'], 'not_recommended': [],
        'boundary_review': {'action': 'continue', 'reason': '核对下一项具体前置条件',
                            'next_step': options[0]['strategy']['target'], 'evidence_refs': []}}


def harness(tmp_path, *, make_options=None, outcomes=None, root='unclassified', blocker=False, session=None, analyst_failure=False):
    flow = retry_flow()
    node = flow.node_map['work']
    store = ExpertRunStore(tmp_path / 'runs')
    store.create(run_id=RUN, conversation_id='conv', task='task', attachments=[], profile={}, strategy='stable', flow=flow)
    def prepare(record):
        record['recovery_case_enabled'] = True
        record['node_states'].setdefault('work', {}).update(status='needs_attention', input_revision='rev',
            messages=[{'role': 'user', 'content': 'original checkpoint'}])
        if blocker:
            ledger_for_run(record).record('work', 'blocker', {'kind': 'capability_missing', 'required_tools': ['InspectVisualEvidence']})
    store.mutate(RUN, prepare)
    events, calls = [], []
    async def dispatch(helper, prompt, policy):
        calls.append((helper.node_id, prompt, policy))
        assert prompt.startswith('【用户授权与表态')
        if policy.recovery_analysis:
            assert policy.parallel_readonly and policy.resume_messages and not policy.allow_delegation
            assert policy.max_iterations is None or policy.max_iterations > 0
            assert policy.timeout_seconds is None or policy.timeout_seconds > 0
            assert policy.token_budget is None or policy.token_budget > 0
            assert not {'Bash', 'WriteFile', 'EditFile', 'RunAgent'} & set(policy.node_tools)
            context = policy.recovery_analysis
            if analyst_failure:
                return ExpertRoleResult('no plan')
            options = make_options(context['round']) if make_options else [option(context['round'])]
            policy.recovery_analysis['holder'].update(plan(context, options, root))
            from mewcode.experts.recovery_analyst import ANALYSIS_CONTEXT_VERSION
            store.mutate(RUN, lambda r: r['node_states'].setdefault(helper.node_id, {}).update(
                analysis_context_version=ANALYSIS_CONTEXT_VERSION,
                messages=[{'role': 'assistant', 'content': 'saved analyst history'}]))
        else:
            assert '*' not in policy.node_tools
            policy.assistance_holder.update(next_step='resume', notes='tool evidence recorded', clearances=[])
        return ExpertRoleResult('done', handoff={'status': 'completed'}, billable_tokens=3)
    async def emit(event):
        events.append(copy.deepcopy(event))
    executor = AssistanceCoordinator(store=store, run_id=RUN, dispatch=dispatch, emit=emit,
        before_resume=AsyncMock(), task='task', work_dir=str(tmp_path), model={})
    def binding(node, selected):
        record = store.load(RUN)
        case = active_case(record, node.node_id)
        coverage = covers(record, {**AUTH, 'scope': {**AUTH['scope'], 'case_id': case['case_id'],
            'round': len(case['rounds']) + 1, 'node_ids': [node.node_id], 'epoch': case['epoch']}})
        if coverage.status != 'covered':
            return BindingResult('authority_required', authority_required=[copy.deepcopy(AUTH)])
        return BindingResult('bound', probe_success=True, resolver=lambda: (object(), {'supports_vision': True}))
    observations = iter(outcomes or [{'passed': True, 'evidence_refs': ['host:probe']}])
    async def probe(node, selected):
        outcome = next(observations)
        if blocker and outcome['passed']:
            store.mutate(RUN, lambda r: binding_receipt(r, node.node_id, binding(node, selected), request_id='probe'))
        # This fixture models a host replay of the failed operation, not a
        # capabilities query. Production probes classify that independently.
        return {**outcome, 'operation_restored': outcome.get('operation_restored', outcome['passed'])}
    coordinator = CaseCoordinator(store=store, run_id=RUN, emit=emit, executor=executor,
        capability_inventory=lambda _: {'models': [], 'models_complete': True, 'alternatives': ALTERNATIVES},
        capability_bind=binding, probe=probe, session=session)
    return SimpleNamespace(store=store, node=node, calls=calls, events=events, coordinator=coordinator,
        policy=ExpertExecutionPolicy('stable', False, 0), result=ExpertRoleResult('blocked', status='needs_attention', error_kind='capability_missing'))


async def run(h):
    return await h.coordinator.run(node=h.node, result=h.result, policy=h.policy)


@pytest.mark.asyncio
async def test_e1a_bound_authority_no_question_one_round_original_checkpoint(tmp_path):
    h = harness(tmp_path, make_options=lambda n: [option(n, authority=True, vision=True)], blocker=True)
    h.store.mutate(RUN, lambda r: ledger_for_run(r).record('work', 'maintenance', {'recovery_action': 'self_diagnose'}))
    h.store.mutate(RUN, lambda r: record_authorization(r, kind='gate_answer',
        scope={**AUTH['scope'], 'operation_classes': ['external_send']}, quote='confirmed typed scope', source_ref='gate:1', host_verified=True))
    result = await run(h)
    record = h.store.load(RUN)
    case = cases(record)[0]
    assert result['action'] == 'continue_checkpoint' and case['status'] == 'resolved' and len(case['rounds']) == 1
    assert not any(e['case']['status'] == 'awaiting_user' for e in h.events if e['type'] == 'recovery_case')
    assert record['node_states']['work']['messages'][0]['content'] == 'original checkpoint'
    assert not ledger_for_run(record).view('work').active_blockers
    assert record['node_states']['work']['status'] == 'running'
    assert case['phases'] == ['发现问题', '节点自救', '统计问题', '主模型分析', '执行', '核验', '已解决']


@pytest.mark.asyncio
async def test_e1b_one_elevation_then_approval_reuses_scope(tmp_path):
    h = harness(tmp_path, make_options=lambda n: [option(n, authority=True, vision=True), option(90)], blocker=True,
        outcomes=[{'passed': True, 'evidence_refs': ['host:probe']}]*2)
    with pytest.raises(BranchWaitPaused):
        await run(h)
    record = h.store.load(RUN)
    case = cases(record)[0]
    assert case['status'] == 'awaiting_user' and len(case['pending']['options']) == 3
    assert not any('__assist_' in c[0] for c in h.calls)
    h.store.mutate(RUN, lambda r: answer_case(r, case['case_id'], round=0, choice='approve'))
    await run(h)
    record = h.store.load(RUN)
    assert cases(record)[0]['status'] == 'resolved'
    assert covers(record, {**AUTH, 'scope': {**AUTH['scope'], 'node_ids': ['different']}}).status == 'covered'
    # Exercise another actual node, rather than proving reuse only via covers().
    from dataclasses import replace
    other = replace(h.node, node_id='done')
    h.store.mutate(RUN, lambda r: r['node_states'].setdefault('done', {}).update(status='needs_attention', input_revision='rev'))
    await h.coordinator.run(node=other, result=h.result, policy=h.policy)
    assert cases(h.store.load(RUN), 'done')[0]['status'] == 'resolved'
    assert len([e for e in h.events if e['type'] == 'recovery_case' and e['phase'] == '待你提权']) == 1


@pytest.mark.asyncio
async def test_e2_failed_method_then_new_method_same_analyst_session(tmp_path):
    h = harness(tmp_path, outcomes=[{'passed': False, 'error': 'temporary'}, {'passed': True, 'evidence_refs': ['probe']}])
    await run(h)
    rounds = cases(h.store.load(RUN))[0]['rounds']
    assert [r['verification']['result'] for r in rounds] == ['unchanged', 'resolved']
    assert rounds[0]['method_fingerprint'] != rounds[1]['method_fingerprint']
    analysts = [c for c in h.calls if c[2].recovery_analysis]
    assert len(analysts) == 2 and analysts[0][0] == analysts[1][0]
    assert '完整明细' in analysts[0][1] and '完整明细' not in analysts[1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ValueError('scope must be an object'), TimeoutError('analysis deadline')])
async def test_analysis_delivery_error_survives_round_and_report(tmp_path, error):
    h = harness(tmp_path)
    h.coordinator.capability_inventory = lambda _: {}
    async def failed(*args, **kwargs):
        raise error
    h.coordinator.analyst.analyze = failed
    with pytest.raises(BranchWaitPaused):
        await run(h)
    case = cases(h.store.load(RUN))[0]
    assert case['status'] == 'reported'
    assert case['rounds'][-1]['executor_receipt_ref'] is None
    assert case['report']['analysis_errors'] == [type(error).__name__ + ': ' + str(error)] * 2
    assert case['report']['analysis_errors'] == case['rounds'][-1]['verification']['analysis_errors']
    assert '尚未交付' in case['report']['reason'] if isinstance(error, TimeoutError) else '校验' in case['report']['reason']
    assert h.store.load(RUN)['node_states']['work']['status'] != 'completed'


@pytest.mark.asyncio
async def test_e3_errors_without_restoration_stay_system_owned(tmp_path):
    h = harness(tmp_path, outcomes=[{'passed': False, 'error': 'different-' + char} for char in 'abcde'])
    with pytest.raises(BranchWaitPaused):
        await run(h)
    case = cases(h.store.load(RUN))[0]
    assert case['status'] == 'reported' and len(case['rounds']) == 2
    assert case['handling']['owner'] == 'system' and not case['pending']
    with pytest.raises(ValueError, match='系统受阻'):
        h.store.resume(RUN, strategy='stable')
    assert len(cases(h.store.load(RUN))[0]['rounds']) == 2
    with pytest.raises(ValueError, match='系统事项没有待答选项'):
        h.store.mutate(RUN, lambda r: answer_case(r, case['case_id'], round=2, choice='manual', answer='核验另一个已配置的环境'))
    assert not h.store.load(RUN).get('authorizations', {}).get('entries')


@pytest.mark.asyncio
async def test_e4_two_same_errors_report_early(tmp_path):
    h = harness(tmp_path, outcomes=[{'passed': False, 'error': 'same'}] * 2)
    with pytest.raises(BranchWaitPaused):
        await run(h)
    case = cases(h.store.load(RUN))[0]
    assert len(case['rounds']) == 2 and case['report']['root_cause'] == 'repeat_without_progress'


@pytest.mark.asyncio
async def test_e5_fundamental_no_routes_one_round_no_executor(tmp_path):
    h = harness(tmp_path, root='capability_absent')
    with pytest.raises(BranchWaitPaused):
        await run(h)
    case = cases(h.store.load(RUN))[0]
    assert len(case['rounds']) == 1 and case['report']['root_cause'] == 'capability_absent'
    assert all(c[2].recovery_analysis for c in h.calls)
    assert case['report']['alternatives']


@pytest.mark.asyncio
async def test_e6_denial_selects_free_alternative_without_reasking(tmp_path):
    h = harness(tmp_path, make_options=lambda n: [option(n, authority=True), option(90)])
    with pytest.raises(BranchWaitPaused):
        await run(h)
    case = cases(h.store.load(RUN))[0]
    h.store.mutate(RUN, lambda r: answer_case(r, case['case_id'], round=0, choice='alternative'))
    await run(h)
    record = h.store.load(RUN)
    assert cases(record)[0]['rounds'][0]['option_id'] == 'method-90'
    assert any(r['kind'] == 'denial' for r in record['authorizations']['entries'])
    assert len([e for e in h.events if e.get('phase') == '待你提权']) == 1


def test_e7_sanitized_legacy_migrates_once_without_model_calls():
    record = json.loads((Path(__file__).parent / 'fixtures/run_n01_escalation_stall.json').read_text(encoding='utf-8'))
    case = migrate_legacy(record, 'N01')
    assert case['status'] == 'reported' and len(case['rounds']) == 3
    before = copy.deepcopy(record)
    assert migrate_legacy(record, 'N01') is None and record == before
    assert len(cases(record)) == 1 and case['report']['kind'] == 'incident'
    assert all(not r['verification'].get('result') == 'resolved' for r in case['rounds'])


@pytest.mark.asyncio
async def test_e8_statement_pinned_in_every_dispatch_never_grants(tmp_path):
    h = harness(tmp_path)
    h.store.mutate(RUN, lambda r: record_authorization(r, kind='user_statement', scope={},
        quote='你们可以自己决定用哪种方式识别', source_ref='message:11'))
    await run(h)
    record = h.store.load(RUN)
    assert not record['authorizations']['entries'][0]['enforceable']
    assert all('ST-01' in c[1] and '不形成工具权限' in c[1] for c in h.calls)
    assert covers(record, AUTH).status == 'uncovered'
    assert len(authorization_block(record)) <= 1200


@pytest.mark.asyncio
async def test_analyst_failure_uses_readonly_host_fallback_then_reports(tmp_path):
    h = harness(tmp_path, analyst_failure=True, outcomes=[{'passed': False, 'error': 'same'}])
    with pytest.raises(BranchWaitPaused):
        await run(h)
    case = cases(h.store.load(RUN))[0]
    assert len(case['rounds']) == 2 and all(r['verification']['analyst_failed'] for r in case['rounds'])
    executors = [c for c in h.calls if c[2].assistance_session]
    assert len(executors) == 1 and executors[0][2].parallel_readonly


@pytest.mark.asyncio
async def test_false_capability_absence_is_reasked_once_then_host_fallback(tmp_path):
    h = harness(tmp_path, root='capability_absent', outcomes=[{'passed': False, 'error': 'same'}])
    h.coordinator.capability_inventory = lambda _: {'models_complete': True,
        'models': [{'available': True, 'modalities': {'vision': True}}], 'alternatives': ALTERNATIVES}
    with pytest.raises(BranchWaitPaused):
        await run(h)
    analysts = [c for c in h.calls if c[2].recovery_analysis]
    assert len(analysts) == 4  # original + one re-ask in each of two rounds
    assert '能力清单存在可绑定路线' in analysts[1][1]
    c = cases(h.store.load(RUN))[0]
    assert len(c['rounds']) == 2 and c['report']['root_cause'] != 'capability_absent'
    assert all(r['verification']['analyst_failed'] for r in c['rounds'])


@pytest.mark.asyncio
async def test_material_answer_replans_with_full_dossier_before_executing(tmp_path):
    def options(n):
        o = option(n)
        if not h.store.load(RUN).get('case_inputs'):
            o['needs']['user_input'] = ['提供可访问的新文件地址']
        else:
            o['strategy']['target'] = 'new-file'
            o['verification'].update(tool='ReadFile', arguments={'file_path': 'new-file'})
        return [o]
    h = harness(tmp_path, make_options=options)
    with pytest.raises(BranchWaitPaused):
        await run(h)
    c = cases(h.store.load(RUN))[0]
    before = copy.deepcopy(h.store.load(RUN))
    with pytest.raises(ValueError, match='具体补充'):
        h.store.mutate(RUN, lambda r: answer_case(r, c['case_id'], round=0, choice='input', answer=' '))
    assert h.store.load(RUN) == before
    answer = '新文件地址和详细业务材料' * 200
    h.store.mutate(RUN, lambda r: answer_case(r, c['case_id'], round=0, choice='input', answer=answer))
    assert cases(h.store.load(RUN))[0]['plan'] == {}
    await run(h)
    from mewcode.experts.problem_dossier import build_dossier
    c = cases(h.store.load(RUN))[0]
    assert c['status'] == 'resolved' and c['plan']['options'][0]['verification']['arguments']['file_path'] == 'new-file'
    dossier = build_dossier(h.store.load(RUN), c)
    assert dossier['user_inputs'][0]['text'] == answer
    analysts = [call for call in h.calls if call[2].recovery_analysis]
    assert len(analysts) == 2 and analysts[0][0] == analysts[1][0]
    assert 'user_input_refs' in analysts[1][1] and '重新核对方案' in analysts[1][1]
    assert len([call for call in h.calls if call[2].assistance_session]) == 1


@pytest.mark.asyncio
async def test_same_answered_material_question_reports_instead_of_reopening_wait(tmp_path):
    def options(n):
        o = option(n)
        o['needs']['user_input'] = ['提供可访问的新文件地址']
        return [o]
    h = harness(tmp_path, make_options=options)
    with pytest.raises(BranchWaitPaused):
        await run(h)
    c = cases(h.store.load(RUN))[0]
    h.store.mutate(RUN, lambda r: answer_case(r, c['case_id'], round=0, choice='input', answer='new-file'))
    with pytest.raises(BranchWaitPaused):
        await run(h)
    c = cases(h.store.load(RUN))[0]
    assert c['status'] == 'reported' and c['pending']['kind'] == 'incident'
    assert '已保存你的补充' in c['report']['reason'] and len(h.calls) == 2
    assert not any(call[2].assistance_session for call in h.calls)


@pytest.mark.asyncio
async def test_live_answer_future_resumes_saved_plan_not_another_analysis(tmp_path):
    import asyncio
    session = SimpleNamespace()
    h = harness(tmp_path, make_options=lambda n: [option(n, authority=True), option(90)], session=session)
    task = asyncio.create_task(run(h))
    for _ in range(100):
        await asyncio.sleep(0)
        if getattr(session, '_case_waiters', None):
            break
    case_id, future = next(iter(session._case_waiters.items()))
    h.store.mutate(RUN, lambda r: answer_case(r, case_id, round=0, choice='approve'))
    future.set_result('approve')
    assert (await task)['action'] == 'continue_checkpoint'
    assert len([c for c in h.calls if c[2].recovery_analysis]) == 1
