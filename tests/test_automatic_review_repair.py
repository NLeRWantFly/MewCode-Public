"""A failed intermediate audit repairs its producers before opening a pass edge."""
from dataclasses import replace

import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from tests.test_collaboration_integration import orchestrator, run
from tests.test_collaboration_mode import RUN_ID, delivery, flow
from tests.test_iterative_collaboration import retry_store


def review_flow(*, failure_branch=False):
    raw = flow().as_dict()
    raw['nodes'].insert(1, {**raw['nodes'][0], 'node_id': 'audit', 'role': 'auditor', 'label': '必要条件复核'})
    raw['meta']['node_plan_briefs']['audit'] = {'acceptance': ['核对原产物并提交结构化结论']}
    raw['edges'] = [{'from_node': 'work', 'to_node': 'audit'},
                    {'from_node': 'audit', 'to_node': 'deliver', 'condition': 'pass'}]
    if failure_branch:
        raw['nodes'].insert(2, {**raw['nodes'][0], 'node_id': 'fallback'})
        raw['meta']['node_plan_briefs']['fallback'] = {'acceptance': ['按既定失败分支处理']}
        raw['edges'].extend([{'from_node': 'audit', 'to_node': 'fallback', 'condition': 'fail'},
                             {'from_node': 'fallback', 'to_node': 'deliver'}])
    return ExpertFlowSpec.from_value(raw)


def audit_result(passed, *, owner='work'):
    return replace(delivery('复核通过' if passed else '具体修订项'), verdict={
        'verdict': 'PASS' if passed else 'NEEDS_ATTENTION', 'evidence': [{'summary': '核对原产物'}],
        'issues': [] if passed else [{'issue_id': 'R-1', 'node_id': owner, 'file': '',
            'description': '修正原产物中的缺项', 'acceptance_test': '回读确认缺项已补齐', 'origin': 'preexisting',
            'resolution': 'producer_fix', 'blocking': True}]})


@pytest.mark.asyncio
async def test_failed_review_repairs_and_rechecks_automatically(tmp_path):
    spec = review_flow()
    store = retry_store(tmp_path, spec)
    invoked = []
    async def execute(node, model, prompt, policy, *_):
        invoked.append(node.node_id)
        if node.node_id == 'audit':
            return audit_result(invoked.count('work') == 2)
        if node.node_id == 'work' and invoked.count('work') == 2:
            assert '修正原产物中的缺项' in prompt
            assert 'previous:work' in policy.stage_inputs
        if node.node_id == 'deliver':
            assert invoked.count('audit') == 2
        return delivery(node.node_id)
    async def review(*_):
        return {'passed': True, 'evidence': ['固定版本核对']}
    result = await run(orchestrator(store, tmp_path, execute, review), store, spec)
    assert result.status == 'completed', result.final_summary
    assert invoked == ['work', 'audit', 'work', 'audit', 'deliver']
    value = store.read_collaboration(RUN_ID)
    assert [a['delivery']['verdict']['verdict'] for a in ledger.attempts_for(value, 'audit')] == ['NEEDS_ATTENTION', 'PASS']
    assert not value.get('recovery_gates')


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['work', 'unknown'])
async def test_review_with_no_progress_stops_with_actual_findings_and_survives_resume(tmp_path, owner):
    spec = review_flow()
    store = retry_store(tmp_path, spec)
    invoked = []
    async def execute(node, *_):
        invoked.append(node.node_id)
        return audit_result(False, owner=owner) if node.node_id == 'audit' else delivery('same bytes')
    async def review(*_):
        pytest.fail('downstream integration must not run')
    host = orchestrator(store, tmp_path, execute, review)
    result = await run(host, store, spec)
    assert result.status == 'blocked'
    assert '修正原产物中的缺项' in result.final_summary
    assert '仍有依赖未就绪' not in result.final_summary
    assert ('停止重复执行' if owner == 'work' else '未能将修订项定位') in result.final_summary
    assert invoked == (['work', 'audit'] * 2 if owner == 'work' else ['work', 'audit'])
    before = list(invoked)
    again = await run(host, store, spec)
    assert again.status == 'blocked' and invoked == before
    assert not store.read_collaboration(RUN_ID).get('recovery_gates')


@pytest.mark.asyncio
async def test_explicit_failure_branch_keeps_control_of_recovery(tmp_path):
    spec = review_flow(failure_branch=True)
    store = retry_store(tmp_path, spec)
    invoked = []
    async def execute(node, *_):
        invoked.append(node.node_id)
        return audit_result(False) if node.node_id == 'audit' else delivery()
    async def review(*_):
        return {'passed': True, 'evidence': ['完成失败分支']}
    result = await run(orchestrator(store, tmp_path, execute, review), store, spec)
    assert result.status == 'completed'
    assert invoked == ['work', 'audit', 'fallback', 'deliver']
    assert not store.read_collaboration(RUN_ID).get('integration_reworks')


@pytest.mark.asyncio
async def test_explicit_audit_retry_rechecks_without_repeating_accepted_producers(tmp_path):
    import copy
    spec = review_flow()
    store = retry_store(tmp_path, spec)
    invoked = []
    async def execute(node, *_):
        invoked.append(node.node_id)
        if node.node_id == 'audit':
            result = audit_result(invoked.count('audit') == 2)
            if result.verdict['issues']:
                result.verdict['issues'][0]['resolution'] = 'capability_gap'
            return result
        return delivery('original producer result')
    async def review(*_):
        return {'passed': True, 'evidence': ['checked current delivery']}
    host = orchestrator(store, tmp_path, execute, review)
    first = await run(host, store, spec)
    assert first.status == 'blocked' and invoked == ['work', 'audit']
    store.transition(RUN_ID, first.status)
    before = copy.deepcopy(store.read_collaboration(RUN_ID))
    # The web role runner normally persists the structured stage result.
    store.mutate(RUN_ID, lambda row: row.setdefault('node_states', {}).setdefault('audit', {}).update(
        status='completed', passed=False,
        verdict=ledger.attempts_for(before, 'audit')[-1]['delivery']['verdict']), durable=True)
    store.resume(RUN_ID, strategy='stable', retry_node_id='audit')
    current = store.read_collaboration(RUN_ID)
    assert current['certificates']['work'] == before['certificates']['work']
    assert not ledger.valid_certificate(current, 'audit')
    assert current['lineage_budget'] == before['lineage_budget']
    again = await run(host, store, spec)
    assert again.status == 'completed', again.final_summary
    assert invoked == ['work', 'audit', 'audit', 'deliver']


def test_multi_file_findings_resolve_exact_declared_outputs_without_using_grants():
    from mewcode.orchestration.review_feedback import issue_owners
    value = {'contracts': {
        'a': {'deliverables': ['runs/a.json：原始记录'], 'capabilities': {'write_paths': ['runs']}},
        'b': {'deliverables': ['runs/b.json'], 'capabilities': {'write_paths': ['runs']}},
        'ambiguous': {'deliverables': ['runs/b.json']},
    }, 'certificates': {'a': {'artifacts': [{'kind': 'file', 'path': 'runs\\actual.json'}]}}}
    assert issue_owners(value, ['a', 'b'], {'file': 'runs/a.json:7; runs/b.json；runs/actual.json'}) == ['a', 'b']
    assert issue_owners(value, ['a', 'b'], {'node_id': 'a', 'file': 'runs/b.json'}) == ['a']
    assert issue_owners(value, ['a', 'b'], {'file': 'runs/unknown.json; runs/../a.json'}) == []
    assert issue_owners(value, ['b', 'ambiguous'], {'file': 'runs/b.json'}) == []
    assert issue_owners(value, ['a'], {'node_id': 'b', 'file': 'runs/b.json'}) == []


@pytest.mark.asyncio
async def test_repair_round_limit_does_not_reset_when_artifacts_keep_changing(tmp_path):
    spec = review_flow()
    store = retry_store(tmp_path, spec)
    store.mutate(RUN_ID, lambda r: r['collaboration']['policy'].update(max_integration_reworks=1), durable=True)
    invoked = []
    async def execute(node, *_):
        invoked.append(node.node_id)
        return audit_result(False) if node.node_id == 'audit' else delivery(str(len(invoked)))
    async def review(*_):
        pytest.fail('must not bypass the failed audit')
    result = await run(orchestrator(store, tmp_path, execute, review), store, spec)
    assert result.status == 'blocked' and '达到现有上限' in result.final_summary
    assert invoked == ['work', 'audit', 'work', 'audit']
