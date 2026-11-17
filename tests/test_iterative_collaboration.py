"""Progressive delivery through the real scheduler, journal and read models."""
import copy
import json
from pathlib import Path

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import Acceptance
from mewcode.kernel.collaboration_recovery import open_gate
from mewcode.orchestration.collab_patch import compare_reports, validate_decision
from mewcode.orchestration.task_view import from_record
from tests.test_collaboration_mode import RUN_ID, PROFILE, EXECUTOR, REVIEWER, delivery, flow
from tests.test_collaboration_integration import orchestrator, run


def retry_store(tmp_path, spec):
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    store.create(run_id=RUN_ID, conversation_id='conversation', task='交付测试结果', attachments=[],
                 profile=PROFILE.as_dict(), strategy='stable', flow=spec)
    store.mutate(RUN_ID, lambda record: record.update(role_models={'roles': {
        'verifier': REVIEWER.as_dict(), 'reviewer': REVIEWER.as_dict(),
        'executor': EXECUTOR.as_dict(), 'planner': EXECUTOR.as_dict()}}), durable=True)
    return store


def store_for(tmp_path):
    return retry_store(tmp_path, flow())


def checked_flow(*checks):
    raw = flow().as_dict()
    raw['meta']['collaboration_spec']['document']['entries'].append({
        'id': 'work', 'kind': 'node', 'body': '准备供下游使用的结果',
        'data': {'contract': {'checks': list(checks)}}})
    return ExpertFlowSpec.from_value(raw)


@pytest.mark.asyncio
async def test_default_flow_reviews_once_after_all_nodes_and_keeps_draft_findings(tmp_path):
    store = store_for(tmp_path)
    executed, reviewed = [], []
    async def execute(node, *_):
        executed.append(node.node_id)
        return delivery(node.node_id)
    async def review(model, payload, context):
        reviewed.append(payload['contract']['id'])
        assert executed == ['work', 'deliver']
        assert {row['node_id'] for row in payload['contract']['quality_scope']} == {'work', 'deliver'}
        return {'passed': False, 'evidence': ['初稿可使用，说明文字还需完善'], 'reason': '说明文字还需完善'}
    async def coordinate(*_):
        pytest.fail('ordinary quality must not invoke a planning model')
    result = await run(orchestrator(store, tmp_path, execute, review, coordinate), store)
    assert result.status == 'completed' and '初稿已完成' in result.final_summary
    assert reviewed == ['final']
    value = store.load(RUN_ID)['collaboration']
    assert not value.get('recovery_gates')
    assert set(value['integrations']) == {'final'}
    assert value['integrations']['final']['report']['passed'] is False
    assert all(ledger.valid_certificate(value, key) for key in ('work', 'deliver'))
    assert all(cert['quality_passed'] is False for cert in value['certificates'].values())
    store.transition(RUN_ID, result.status, reason=result.final_summary)
    view = from_record(store.load(RUN_ID))
    assert view['task']['quality_status'] == 'needs_revision'
    assert view['plan'][0]['steps'][0]['detail']['checks'][0]['deferred']
    assert {row['status'] for row in view['deliverables']} == {'可用初稿'}


@pytest.mark.asyncio
async def test_required_file_repair_runs_directly_and_retains_both_versions(tmp_path):
    spec = checked_flow({'id': 'usable', 'version': 1, 'kind': 'schema', 'path': 'result.json',
        'schema': {'type': 'object', 'properties': {'ready': {'const': True}}, 'required': ['ready']}},
        {'id': 'quality', 'version': 1, 'kind': 'review', 'criteria': ['报告措辞完整']})
    store = retry_store(tmp_path, spec)
    executed, reviewed = [], []
    async def execute(node, model, prompt, policy, *_):
        executed.append(node.node_id)
        assert Path(policy.work_dir) == tmp_path
        assert not policy.candidate_context
        if node.node_id != 'work':
            assert json.loads((tmp_path / 'result.json').read_text())['ready']
            return delivery()
        ready = executed.count('work') > 1
        if ready:
            assert 'prior_failed_checks' in prompt
            from mewcode.orchestration.task_context import build_task_context
            context = build_task_context(store.read_collaboration(RUN_ID), 'work')
            assert [row['id'] for row in context['objective']['prior_failed_checks']['checks']] == ['usable']
        (tmp_path / 'result.json').write_text(json.dumps({'ready': ready}), encoding='utf-8')
        return ExpertRoleResult(text='已生成报告', handoff={'status': 'completed', 'artifacts': [{'path': 'result.json'}]})
    async def review(model, payload, context):
        reviewed.append(payload['contract']['id'])
        return {'passed': True, 'evidence': ['核对固定产物']}
    async def coordinate(*_):
        pytest.fail('a known failed prerequisite goes straight to its executor')
    result = await run(orchestrator(store, tmp_path, execute, review, coordinate), store, spec)
    assert result.status == 'completed', result.final_summary
    assert executed == ['work', 'work', 'deliver'] and reviewed == ['final']
    attempts = ledger.attempts_for(store.load(RUN_ID)['collaboration'], 'work')
    assert len(attempts) == 2 and not any(row.get('patch') for row in attempts)
    snapshots = [tmp_path / row['report']['artifacts'][0]['snapshot_path'] for row in attempts]
    assert [json.loads(path.read_text())['ready'] for path in snapshots] == [False, True]


@pytest.mark.asyncio
async def test_explicit_required_review_still_blocks_consumer(tmp_path):
    spec = checked_flow({'id': 'usable', 'version': 1, 'kind': 'review', 'blocking': True,
                         'criteria': ['必须先确认下游能使用输入']})
    store = retry_store(tmp_path, spec)
    executed = []
    async def execute(node, *_):
        executed.append(node.node_id)
        return delivery()
    async def review(model, payload, context):
        return {'passed': payload['contract']['id'] != 'work' or executed.count('work') > 1,
                'evidence': ['确认输入兼容性']}
    result = await run(orchestrator(store, tmp_path, execute, review), store, spec)
    assert result.status == 'completed' and executed == ['work', 'work', 'deliver']


@pytest.mark.asyncio
async def test_actionable_final_feedback_repairs_producer_without_coordinator(tmp_path):
    store = store_for(tmp_path)
    executed, reviews = [], []
    async def execute(node, model, prompt, *_):
        executed.append(node.node_id)
        if node.node_id == 'work' and executed.count('work') > 1:
            assert '修正第二行拼写' in prompt
        return delivery(node.node_id + str(executed.count(node.node_id)))
    async def review(model, payload, context):
        reviews.append(payload['contract']['id'])
        return {'passed': len(reviews) > 1, 'evidence': ['核对内容'],
                'issues': [] if len(reviews) > 1 else [{'node_id': 'work', 'description': '修正第二行拼写',
                                                       'acceptance_test': '回读第二行', 'resolution': 'producer_fix'}]}
    async def coordinate(*_):
        pytest.fail('concrete feedback needs no extra planning pass')
    result = await run(orchestrator(store, tmp_path, execute, review, coordinate), store)
    assert result.status == 'completed'
    assert executed == ['work', 'deliver', 'work', 'deliver'] and reviews == ['final', 'final']
    assert not store.load(RUN_ID)['collaboration'].get('recovery_gates')


@pytest.mark.asyncio
async def test_existing_quality_rejection_can_be_reused_without_reexecuting_work(tmp_path):
    store = store_for(tmp_path)
    (tmp_path / 'result.txt').write_text('已完成的初稿', encoding='utf-8')
    value = store.load(RUN_ID)['collaboration']
    result = delivery()
    result.handoff['artifacts'] = [{'path': 'result.txt'}]
    async def reject(*_):
        return {'passed': False, 'evidence': ['一句文字需修订'], 'reason': '一句文字需修订'}
    report = await Acceptance(tmp_path, review=reject).check(value['contracts']['work'], result, context={})
    # A pre-upgrade report has no independent readiness outcome.
    report.pop('ready'); report.pop('artifacts_unchanged')
    from dataclasses import asdict
    saved = []
    def prepare(record):
        current = record['collaboration']
        current['contracts']['work']['bound_dependencies'] = []
        attempt = ledger.dispatch(current, 'work')
        current['attempts'][attempt['id']]['delivery'] = asdict(result)
        ledger.complete(current, attempt['id'], report)
        saved.append(attempt['id'])
        open_gate(record, scope='contract:work', keys=['work'], label='准备',
                  reason='候选修订必须绑定当前合同、证据、明确改善及现有检查', kind='coordination_wait')
    store.mutate(RUN_ID, prepare, durable=True)
    executed = []
    async def execute(node, *_):
        executed.append(node.node_id)
        return delivery()
    async def review(*_):
        return {'passed': True, 'evidence': ['集中复核']}
    result = await run(orchestrator(store, tmp_path, execute, review), store)
    assert result.status == 'completed' and executed == ['deliver']
    value = store.load(RUN_ID)['collaboration']
    assert value['attempts'][saved[0]]['report'] == report
    assert value['attempts'][saved[0]]['status'] == 'rejected'
    assert all(gate['status'] == 'superseded' for gate in value['recovery_gates'].values())
    assert value['certificates']['work']['quality_passed'] is False


def test_partial_candidate_improvement_is_publishable_without_fabricating_pass():
    contract = {'checks': [{'id': 'a', 'version': 1}, {'id': 'b', 'version': 1}]}
    def report(flags):
        return {'passed': all(flags), 'artifacts': [], 'checks': [
            {'id': key, 'version': 1, 'passed': passed, 'evidence': {'observed': True}}
            for key, passed in zip(('a', 'b'), flags)]}
    improved = compare_reports(contract, report([False, False]), report([True, False]), ['a'])
    assert improved['publishable'] is True and improved['passed'] is False
    regressed = compare_reports(contract, report([False, True]), report([True, False]), ['a'])
    assert regressed['publishable'] is False


def test_versioned_check_reference_normalizes_only_matching_version(tmp_path):
    value = store_for(tmp_path).load(RUN_ID)['collaboration']
    value['contracts']['work']['capabilities'] = {}
    decision = {'basis_hash': ledger.dependencies(value, 'work'), 'target_refs': ['work@1'],
                'verification_refs': ['work_acceptance@1'], 'expected_improvement': '修正文字',
                'evidence_refs': ['report'], 'instructions': '只修正文字'}
    assert validate_decision(value, 'work', decision)['verification_refs'] == ['work_acceptance']
    with pytest.raises(ValueError):
        validate_decision(value, 'work', {**decision, 'verification_refs': ['work_acceptance@2']})


def test_collaboration_updates_preserve_other_state_and_skip_noop_commit(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    before = store.load(RUN_ID)
    monkeypatch.setattr('mewcode.experts.node_ledger.sync_run_facts', lambda *_: pytest.fail('unrelated node ledger rebuild'))
    returned = store.mutate_collaboration(RUN_ID, lambda value: value['policy'])
    returned['max_attempts'] = 999
    assert store.load(RUN_ID) == before
    store.mutate_collaboration(RUN_ID, lambda value: ledger.event(value, 'test_note', reason='只改编排记录'))
    after = store.load(RUN_ID)
    assert after['node_states'] == before['node_states'] and after['node_ledgers'] == before['node_ledgers']
    assert after['collaboration']['policy'] == before['collaboration']['policy']
    assert after['kernel_revision'] > before['kernel_revision']


@pytest.mark.asyncio
async def test_empty_completion_does_not_make_a_handoff_ready(tmp_path):
    contract = store_for(tmp_path).load(RUN_ID)['collaboration']['contracts']['work']
    result = await Acceptance(tmp_path).check(contract, ExpertRoleResult(text='完成'), context={'phase': 'handoff'})
    assert not result['ready'] and '产物' in result['reason']


@pytest.mark.asyncio
@pytest.mark.parametrize('passed', [True, False])
async def test_explicit_final_audit_is_reused_and_does_not_relabel_findings(tmp_path, passed):
    from mewcode.experts.outcome import effective_run_status, acceptance_retry_required
    raw = flow().as_dict()
    raw['nodes'][-1]['role'] = 'auditor'
    spec = ExpertFlowSpec.from_value(raw)
    store = retry_store(tmp_path, spec)
    async def execute(node, model, prompt, policy, *_):
        result = delivery(node.node_id)
        if node.role == 'auditor':
            assert 'FINAL_REVIEW_SCOPE' in prompt and 'work_acceptance' in prompt
            assert 'work' in policy.stage_inputs
            result.verdict = {'verdict': 'PASS' if passed else 'NEEDS_ATTENTION',
                'evidence': [{'kind': 'read', 'summary': '已核对上游固定产物'}],
                'issues': [] if passed else [{'description': '术语可进一步精简'}]}
            result.handoff = None
        return result
    async def review(*_):
        pytest.fail('the explicit final audit already covered this fixed scope')
    result = await run(orchestrator(store, tmp_path, execute, review), store, spec)
    assert result.status == 'completed', result.final_summary
    store.transition(RUN_ID, result.status, reason=result.final_summary)
    saved = store.load(RUN_ID)
    assert saved['collaboration']['integrations']['final']['report']['passed'] is passed
    verdict = ledger.attempts_for(saved['collaboration'], 'deliver')[-1]['delivery']['verdict']
    assert verdict['verdict'] == ('PASS' if passed else 'NEEDS_ATTENTION')
    # The lightweight scheduler actor has no web stage writer. Project the
    # same structured receipt the web adapter persists for this outcome check.
    saved['node_states']['deliver'] = {'status': 'completed', 'verdict': verdict}
    assert effective_run_status(saved) == 'completed' and not acceptance_retry_required(saved)
    saved['collaboration']['certificates']['work']['invalidated'] = '要求已修订'
    assert not ledger.final_ready(saved['collaboration'])
    if not passed:
        assert effective_run_status(saved) == 'needs_attention'


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,ready', [('provider_error', False), ('acceptance_unavailable', False),
    ('model_stream_stalled', False), ('acceptance_format', True),
    ('permission_required', False), ('effect_unknown', False), ('safety_review_unavailable', False)])
async def test_optional_review_cannot_waive_model_availability_or_host_clearance(tmp_path, fault, ready):
    contract = {'checks': [{'id': 'quality', 'version': 1, 'kind': 'review'}]}
    async def review(*_):
        return {'passed': fault == 'provider_error', 'evidence': ['未完成复核'], 'execution_error': fault}
    report = await Acceptance(tmp_path, review=review).check(contract, delivery(), context={'phase': 'review'})
    assert report['ready'] is ready and report['passed'] is False
    assert report['checks'][0]['evidence']['execution_error'] == fault
    if not ready:
        from mewcode.kernel.acceptance import report_ready
        assert not report_ready(contract, {**report, 'ready': True, 'execution_error': ''})


@pytest.mark.asyncio
@pytest.mark.parametrize('blocking', [True, False])
@pytest.mark.parametrize('kind', ['file_exists', 'schema', 'test'])
async def test_handoff_checks_follow_downstream_necessity(tmp_path, blocking, kind):
    check = {'id': 'input', 'version': 1, 'kind': kind, 'blocking': blocking}
    result = delivery('已有可回读的主体内容')
    if kind == 'schema':
        (tmp_path / 'data.json').write_text('{"data": 1}', encoding='utf-8')
        result.handoff['artifacts'] = [{'path': 'data.json'}]
        check.update(path='data.json', schema={'required': ['downstream_field']})
    elif kind == 'file_exists':
        check['path'] = 'missing.txt'
    else:
        check['command'] = 'verify-required-input'
    async def command(*_):
        return {'receipt_id': 'actual-check', 'exit_code': 1, 'command_started': True}
    report = await Acceptance(tmp_path, command=command).check({'checks': [check]}, result,
                                                               context={'phase': 'handoff'})
    assert report['ready'] is not blocking
    assert report['passed'] is False and report['checks'][0]['passed'] is False


@pytest.mark.asyncio
async def test_optional_test_environment_does_not_block_but_model_outage_does(tmp_path):
    contract = {'checks': [{'id': 'optional', 'version': 1, 'kind': 'test', 'blocking': False,
                           'command': 'optional-check'}]}
    async def unavailable(*_):
        raise OSError('optional test environment is absent')
    report = await Acceptance(tmp_path, command=unavailable).check(contract, delivery(), context={})
    assert report['ready'] and not report['passed']
    assert report['checks'][0]['evidence']['execution_error'] == 'environment_fault'
    async def denied(*_):
        return {'execution_error': 'permission_required'}
    report = await Acceptance(tmp_path, command=denied).check(contract, delivery(), context={})
    assert not report['ready']


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['execution', 'final'])
async def test_unavailable_selected_model_stops_and_resumes_same_work(tmp_path, scope):
    from mewcode.kernel.collaboration_recovery import resolve_gate
    from mewcode.orchestration.collab_service import projection
    store = store_for(tmp_path)
    executed, reviewed = [], []
    outage = True
    async def execute(node, model, *_):
        executed.append((node.node_id, model.as_dict()))
        if scope == 'execution' and outage:
            return ExpertRoleResult(text='指定模型当前不可用', status='failed', error_kind='provider_error')
        return delivery()
    async def review(model, *_):
        reviewed.append(model.as_dict())
        if scope == 'final' and outage:
            raise ConnectionError('selected verifier is unavailable')
        return {'passed': True, 'evidence': ['核对完成']}
    host = orchestrator(store, tmp_path, execute, review)
    result = await run(host, store)
    assert result.status == 'blocked'
    store.transition(RUN_ID, result.status, reason=result.final_summary)
    record = store.load(RUN_ID)
    gate = next(row for row in record['collaboration']['recovery_gates'].values() if row['status'] == 'waiting')
    assert gate['kind'] == 'infrastructure'
    assert not projection(record)['recovery_gates'][0]['manual_available']
    if scope == 'execution':
        assert [key for key, _ in executed] == ['work'] and not reviewed
        assert not ledger.valid_certificate(record['collaboration'], 'work')
    else:
        assert [key for key, _ in executed] == ['work', 'deliver']
        assert not ledger.final_ready(record['collaboration'])
    store.mutate(RUN_ID, lambda row: resolve_gate(row, gate_id=gate['id'], expected_version=1,
        request_id='service-restored', action='retry', reason='指定模型服务已恢复', actor='user'), durable=True)
    outage = False
    store.resume(RUN_ID, strategy='stable')
    assert (await run(host, store)).status == 'completed'
    assert [key for key, _ in executed] == (['work', 'work', 'deliver'] if scope == 'execution' else ['work', 'deliver'])
    assert all(model == EXECUTOR.as_dict() for _, model in executed)
    assert all(model == REVIEWER.as_dict() for model in reviewed)


@pytest.mark.asyncio
async def test_optional_review_format_failure_is_a_finding_not_model_outage(tmp_path):
    store = store_for(tmp_path)
    executed = []
    async def execute(node, *_):
        executed.append(node.node_id)
        return delivery()
    async def review(*_):
        return '{invalid json'
    result = await run(orchestrator(store, tmp_path, execute, review), store)
    assert result.status == 'completed' and executed == ['work', 'deliver']
    value = store.read_collaboration(RUN_ID)
    assert not value.get('recovery_gates')
    report = value['integrations']['final']['report']
    assert report['ready'] and not report['passed']
    assert report['checks'][0]['evidence']['execution_error'] == 'acceptance_format'


@pytest.mark.asyncio
async def test_ready_receipt_rejects_missing_checks_and_changed_artifacts(tmp_path):
    from mewcode.kernel.acceptance import report_ready
    contract = {'checks': [{'id': 'required', 'version': 1, 'kind': 'file_exists', 'path': 'result.txt'},
                          {'id': 'quality', 'version': 2, 'kind': 'review'}]}
    (tmp_path / 'result.txt').write_text('draft', encoding='utf-8')
    result = delivery()
    result.handoff['artifacts'] = [{'path': 'result.txt'}]
    acceptance = Acceptance(tmp_path)
    report = await acceptance.check(contract, result, context={'phase': 'handoff'})
    assert report_ready(contract, report) and not report['passed']
    for field, replacement in [('checks', report['checks'][1:]), ('artifacts', []),
                               ('artifacts_unchanged', False), ('execution_error', 'effect_unknown')]:
        assert not report_ready(contract, {**report, field: replacement})
    changed = copy.deepcopy(report)
    changed['checks'][0]['passed'] = False
    assert not report_ready(contract, changed)
    async def review(*_):
        (tmp_path / 'result.txt').write_text('changed', encoding='utf-8')
        return {'passed': True, 'evidence': ['read']}
    modified = await Acceptance(tmp_path, review=review).check(contract, result, context={'phase': 'review'})
    assert not modified['ready'] and not modified['passed']


def test_collaboration_commit_failure_rolls_back_memory_and_disk(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    before = store.load(RUN_ID)
    def fail(*_, **__):
        raise OSError('simulated commit failure')
    monkeypatch.setattr(store, '_commit', fail)
    with pytest.raises(OSError, match='commit failure'):
        store.mutate_collaboration(RUN_ID, lambda value: ledger.event(value, 'not_persisted'))
    assert store.load(RUN_ID) == before
    assert ExpertRunStore(store.root, storage='sqlite').load(RUN_ID)['collaboration'] == before['collaboration']


def test_collaboration_commit_does_not_copy_or_reproject_tool_history(tmp_path, monkeypatch):
    from mewcode.kernel.sqlite_state import RunDatabase
    import mewcode.kernel.run_views as views
    store = store_for(tmp_path)
    history = [{'type': 'historical', 'text': 'preserved'}]
    store.mutate(RUN_ID, lambda row: row.update(kernel_attempt_archives=history), durable=True)
    class Uncopyable(list):
        def __deepcopy__(self, memo):
            pytest.fail('orchestration copied unrelated execution history')
    store._live[RUN_ID]['kernel_attempt_archives'] = Uncopyable(history)
    build = views.build
    def observed(snapshot, **kwargs):
        assert kwargs.get('history_events') == []
        return build(snapshot, **kwargs)
    monkeypatch.setattr(views, 'build', observed)
    store.mutate_collaboration(RUN_ID, lambda value: ledger.event(value, 'small_progress'))
    saved = RunDatabase(store.root, RUN_ID).load()
    assert saved['kernel_attempt_archives'] == history
    assert saved['collaboration']['timeline'][-1]['kind'] == 'small_progress'


@pytest.mark.asyncio
async def test_invalid_coordination_response_is_repaired_internally_once(tmp_path):
    from mewcode.orchestration.collab_coordinator import CollaborationCoordinator
    from tests.test_collaboration_mode import CATALOG
    store = store_for(tmp_path)
    calls = []
    async def ask(model, payload, system):
        calls.append(payload)
        return {'action': 'invalid'} if len(calls) == 1 else {'action': 'retry', 'reason': '原范围修正', 'instructions': ['修改第一项']}
    coordinator = CollaborationCoordinator(store, RUN_ID, CATALOG, ask=ask)
    decision = await coordinator.decide(event_id='bad-format', node_id='work', evidence=['交接问题'])
    assert decision['action'] == 'retry' and decision['instructions'] == '修改第一项'
    assert len(calls) == 2 and calls[1]['format_error']
    assert await coordinator.decide(event_id='bad-format', node_id='work', evidence=['交接问题']) == decision
    assert len(calls) == 2 and not store.read_collaboration(RUN_ID).get('recovery_gates')
