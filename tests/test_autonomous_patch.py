"""Candidate repair exercises the real scheduler, fixed checks and publication."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.agent import Agent
from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.calls import CallGateway
from mewcode.tools.base import ToolResult
from tests.test_autonomous_context_runtime import retry_flow, retry_store, run_flow
from tests.test_collaboration_mode import RUN_ID, delivery, EXECUTOR


def patch_flow():
    raw = retry_flow(4).as_dict()
    raw['nodes'][0]['capabilities'] = {'write_paths': ['report.json']}
    raw['nodes'][1]['capabilities'] = {}
    raw['meta']['collaboration_spec']['document']['entries'].append({
        'id': 'work', 'kind': 'node', 'body': '合计与行数', 'data': {'contract': {'checks': [
            {'id': 'total', 'version': 1, 'kind': 'schema', 'path': 'report.json',
             'schema': {'type': 'object', 'properties': {'total': {'const': 3}}, 'required': ['total']}},
            {'id': 'rows', 'version': 1, 'kind': 'schema', 'path': 'report.json',
             'schema': {'type': 'object', 'properties': {'rows': {'const': 2}}, 'required': ['rows']}},
            {'id': 'meaning', 'version': 1, 'kind': 'review', 'criteria': ['固定表格满足原要求']},
        ]}}})
    return ExpertFlowSpec.from_value(raw)


def decision(payload):
    identity = payload['context']['identity']
    return {'action': 'propose_patch', 'reason': '合计未通过', 'evidence_refs': ['check:total'],
            'basis_hash': identity['basis_hash'], 'target_refs': [f"work@{identity['contract_version']}"],
            'expected_improvement': '合计修正且行数及原语义保持通过', 'verification_refs': ['total', 'rows'],
            'instructions': '只修正合计，保留行数'}


def recorded_publisher(store, project, calls):
    async def publish(changes, context):
        for relative, content in changes.items():
            target = project / relative
            async def write(_params, **_kwargs):
                calls.append(relative)
                target.write_bytes(content)
                return ToolResult('published')
            tool = SimpleNamespace(category='write', run=write)
            tc = SimpleNamespace(tool_name='WriteFile', tool_id='publish-' + relative,
                                 arguments={'file_path': str(target), 'content_hash': context['candidate_hash']})
            agent = Agent.__new__(Agent)
            agent._safety_call_ref = lambda *args: 'host-test-boundary'
            agent._prepare_rollback_evidence = lambda *args: None
            agent._review_recovery = SimpleNamespace(begin_effect=lambda *args: {}, finish_effect=lambda *args: None)
            agent.work_dir = str(project)
            agent.permission_checker = None
            agent.kernel_gateway = CallGateway(store, RUN_ID, 'work__patch_publish', context['candidate_hash'])
            result = await agent._run_recorded_tool(tool, {}, tc)
            assert not result.is_error
        return {'host_verified': True}
    return publish


@pytest.mark.asyncio
@pytest.mark.parametrize('candidate,outcome,crash', [({'total': 0, 'rows': 2}, 'plateau', False),
    ({'total': 3, 'rows': 1}, 'regression', False), ({'total': 3, 'rows': 2}, 'complete', False),
    ({'total': 3, 'rows': 2}, 'complete', True)])
async def test_legacy_candidate_publication_keeps_regression_and_receipt_guards(tmp_path, monkeypatch, candidate, outcome, crash):
    from dataclasses import asdict
    from mewcode.kernel.acceptance import Acceptance
    from mewcode.orchestration.collab_runtime import CollaborationRuntime
    from mewcode.expert_group import derive_execution_policy
    from tests.test_collaboration_mode import CATALOG
    project = tmp_path / 'project'
    project.mkdir()
    flow = patch_flow()
    store = retry_store(tmp_path, flow)
    original = {'total': 0, 'rows': 2}
    (project / 'report.json').write_text(json.dumps(original), encoding='utf-8')
    baseline_delivery = ExpertRoleResult(text='固定基线', handoff={'status': 'completed', 'artifacts': [{'path': 'report.json'}]})
    async def review(*_):
        return {'passed': True, 'evidence': ['独立核对固定证据']}
    report = await Acceptance(project, review=review).check(store.read_collaboration(RUN_ID)['contracts']['work'],
        baseline_delivery, context={})
    def seed(value):
        initial = ledger.dispatch(value, 'work')
        value['attempts'][initial['id']]['delivery'] = asdict(baseline_delivery)
        ledger.complete(value, initial['id'], report)
        return initial['id']
    baseline_id = store.mutate_collaboration(RUN_ID, seed)
    published, directories = [], []
    runtime = CollaborationRuntime(store=store, run_id=RUN_ID, flow=flow, catalog=CATALOG,
        work_dir=project, review=review, publish=recorded_publisher(store, project, published))
    current = runtime.read()
    proposal = {'baseline_id': baseline_id, 'decision': {'basis_hash': ledger.dependencies(current, 'work'),
        'target_refs': ['work@1'], 'verification_refs': ['total', 'rows'], 'expected_improvement': '修正合计并保留行数',
        'evidence_refs': ['check:total'], 'instructions': '修正合计'}}
    async def runner(node, model, prompt, policy, member=None):
        directory = Path(policy.work_dir)
        directories.append(directory)
        assert directory != project and policy.candidate_context['collaboration_patch']
        assert policy.expansion_holder is None
        assert json.loads((project / 'report.json').read_text()) == original
        (directory / 'report.json').write_text(json.dumps(candidate), encoding='utf-8')
        return ExpertRoleResult(text='候选表格', handoff={'status': 'completed', 'artifacts': [{'path': 'report.json'}]})
    async def execute():
        return await runtime.execute(flow.node_map['work'], EXECUTOR, '', derive_execution_policy(False), None,
            runner, patch_proposal=proposal)
    if crash:
        original_mutate, injected = store.mutate, []
        def interrupt_commit(run_id, callback, **kwargs):
            if callback.__name__ == 'commit' and not injected:
                injected.append(True)
                raise OSError('simulated interruption after receipted file publication')
            return original_mutate(run_id, callback, **kwargs)
        monkeypatch.setattr(store, 'mutate', interrupt_commit)
        with pytest.raises(OSError, match='simulated interruption'):
            await execute()
        assert published == ['report.json']
        assert not ledger.valid_certificate(runtime.read(), 'work')
    result = await execute()
    value = runtime.read()
    patched = next(row for row in value['attempts'].values() if row.get('patch'))
    assert patched['report']['improvement']['outcome'] == outcome
    assert value['lineage_budget']['root']['used']['revisions'] == 1
    assert len(directories) == 1
    assert json.loads((project / 'report.json').read_text()) == (candidate if outcome == 'complete' else original)
    assert patched['patch']['status'] == ('published' if outcome == 'complete' else 'rejected')
    assert published == (['report.json'] if outcome == 'complete' else [])
    assert ledger.valid_certificate(value, 'work') is (outcome == 'complete')
    assert result.status == ('completed' if outcome == 'complete' else 'needs_attention')


def test_patch_rejects_wrong_basis_invented_checks_or_missing_improvement(tmp_path):
    from mewcode.orchestration.collab_patch import validate_decision
    store = retry_store(tmp_path, patch_flow())
    value = ledger.state(store.load(RUN_ID))
    valid = {'basis_hash': ledger.dependencies(value, 'work'), 'target_refs': ['work@1'],
             'verification_refs': ['total'], 'expected_improvement': '合计通过',
             'evidence_refs': ['total'], 'instructions': '修正合计'}
    for change in ({'basis_hash': 'old'}, {'target_refs': ['deliver@1']},
                   {'verification_refs': ['invented']}, {'expected_improvement': ''}):
        with pytest.raises(ValueError, match='绑定当前合同'):
            validate_decision(value, 'work', {**valid, **change})


def test_candidate_checker_preserves_source_denials_and_rejects_escape(tmp_path):
    from mewcode.experts.audit_workspace import AuditWorkspace
    from mewcode.kernel.shadow_policy import bind_collaboration_candidate
    from mewcode.permissions import PermissionMode
    from mewcode.permissions.checker import Decision
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.tools.write_file import WriteFile
    project = tmp_path / 'project'
    project.mkdir()
    transaction = AuditWorkspace(project, project / '.mewcode/collaboration-candidates', control_archive=True)
    calls = []
    def source_check(tool, args):
        calls.append(args)
        return Decision('deny', '原文件不允许写入', source='fixture_policy')
    checker = build_permission_checker(transaction.candidate, mode=PermissionMode.AUTO)
    bind_collaboration_candidate(checker, SimpleNamespace(check=source_check), {
        'source': str(project), 'candidate': str(transaction.candidate), 'baseline': str(transaction.baseline)})
    assert checker.check(WriteFile(), {'file_path': str(transaction.candidate / 'report.json')}).effect == 'deny'
    assert calls[-1]['file_path'] == str(project / 'report.json')
    assert checker.check(WriteFile(), {'file_path': str(project / 'report.json')}).source == 'candidate_scope'
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('has_coordinator', [False, True])
async def test_file_repair_runs_in_original_scope_without_candidate_or_planner(tmp_path, has_coordinator):
    project = tmp_path / 'project'
    project.mkdir()
    flow = patch_flow()
    store = retry_store(tmp_path, flow)
    calls = []
    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id != 'work':
            return delivery('已整合')
        assert Path(policy.work_dir) == project and not policy.candidate_context
        (project / 'report.json').write_text(json.dumps({'total': 3 if calls.count('work') > 1 else 0, 'rows': 2}), encoding='utf-8')
        return ExpertRoleResult(text='已保存表格', handoff={'status': 'completed', 'artifacts': [{'path': 'report.json'}]})
    async def review(*args):
        return {'passed': True, 'evidence': ['独立语义通过']}
    async def coordinate(*args):
        pytest.fail('普通文件返修不调用协调者')
    host = ExpertGroupOrchestrator(runner=None, node_runner=runner, run_store=store, work_dir=project,
        archive_root=tmp_path / 'archive', collaboration_review=review,
        collaboration_coordinate=coordinate if has_coordinator else None,
        event_sink=lambda event: store.append_event(RUN_ID, event))
    result = await run_flow(host, flow, store)
    assert result.status == 'completed' and calls == ['work', 'work', 'deliver']
    value = store.read_collaboration(RUN_ID)
    assert not any(row.get('patch') for row in value['attempts'].values())
    baseline = ledger.attempts_for(value, 'work')[0]['report']['artifacts'][0]
    assert json.loads((project / baseline['snapshot_path']).read_text()) == {'total': 0, 'rows': 2}
