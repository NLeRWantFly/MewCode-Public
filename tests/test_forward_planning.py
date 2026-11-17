"""Reassessment is factual; a changed route uses the actual reviewed graph path."""
import copy
import json

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.forward_planning import reassessment_context
from tests.test_autonomous_fragment import _sample_flow, _proposal, ALLOWED_TOOLS
from tests.test_autonomous_context_runtime import retry_store, orchestrate, run_flow, failing_work_reviewer
from tests.test_collaboration_mode import RUN_ID, accept, core, delivery, require_reviews


def failure(value, key, number, **extra):
    identity = f'{key}-failure-{number}'
    value['attempts'][identity] = dict(id=identity, contract_id=key, number=number,
                                     status='rejected', **extra)
    return identity


def test_reassessment_only_counts_this_contracts_unsuperseded_failures():
    value = core()
    failure(value, 'work', 1)
    failure(value, 'deliver', 1)
    failure(value, 'work', 2, superseded=True)
    assert reassessment_context(value, 'work') is None
    failure(value, 'work', 3)
    before = copy.deepcopy(value)
    context = reassessment_context(value, 'work')
    assert context['failed_attempt_count'] == 2
    assert context['recent_failed_attempt_ids'] == ['work-failure-1', 'work-failure-3']
    assert context['unstarted_successor_ids'] == []  # deliver has already started
    assert context['historical_failures_are_not_current_blockers']
    assert value == before


def test_reassessment_reuses_only_valid_inputs_and_bounds_future_projection():
    value = core()
    accept(value, 'work')
    for number in reversed(range(1, 8)):
        failure(value, 'deliver', number)
    for number in range(14):
        value['contracts'][f'future-{number}'] = {'depends_on': ['deliver']}
    context = reassessment_context(value, 'deliver')
    assert context['reusable_accepted_inputs'] == [{
        'contract_id': 'work', 'contract_version': value['certificates']['work']['contract_version'],
        'report_hash': value['certificates']['work']['report_hash']}]
    assert context['recent_failed_attempt_ids'] == [f'deliver-failure-{i}' for i in range(4, 8)]
    assert context['unstarted_successor_count'] == 14
    assert len(context['unstarted_successor_ids']) == 12
    value['certificates']['work']['invalidated'] = 'changed input'
    assert reassessment_context(value, 'deliver')['reusable_accepted_inputs'] == []


@pytest.mark.asyncio
async def test_repeated_failures_can_pivot_into_reviewed_children_and_preserve_history(tmp_path):
    raw = _sample_flow().as_dict()
    raw['nodes'][0]['failure_policy']['max_attempts'] = 6
    flow = ExpertFlowSpec.from_value(raw)
    store = retry_store(tmp_path, flow)
    require_reviews(store, 'work')  # This route cannot supply a usable downstream input.
    invoked, prior_failures, limits = [], [], []
    review = failing_work_reviewer(failures=2)

    async def runner(node, model, prompt, policy, member=None):
        invoked.append(node.node_id)
        if node.node_id != 'work':
            return delivery('适配后的成果 ' + node.node_id)
        count = invoked.count('work')
        if count < 3:
            return delivery(f'MARK-{count} 旧方法未完成')
        context = json.JSONDecoder().raw_decode(prompt.split('TASK_CONTEXT_JSON\n', 1)[1])[0]
        assert context['objective']['route_reassessment']['failed_attempt_count'] == 2
        assert context['objective']['route_reassessment']['unstarted_successor_ids'] == ['deliver']
        value = ledger.state(store.load(RUN_ID))
        prior_failures.extend(copy.deepcopy(row) for row in ledger.attempts_for(value, 'work')
                              if row['status'] == 'rejected')
        limits.append(copy.deepcopy(value['lineage_budget']['limits']))
        proposal = _proposal()
        proposal['reason'] = '旧路线两次失败；用最小验证及适配节点继续后续工作'
        proposal['nodes'][0].update(label='最小验证', task='用一个样例验证替代方法')
        proposal['nodes'][1].update(label='适配后续', task='复用已有产物并适配后续交付')
        policy.expansion_holder.set_allowed_tools(set(ALLOWED_TOOLS))
        validated = policy.expansion_holder.submit(proposal)
        return ExpertRoleResult(text='提出替代路线', status='node_expansion', node_expansion=validated.as_dict())

    async def coordinate(model, payload, system):
        if 'fragment' in payload:
            return {'passed': True, 'reason': '范围和最终验收保留', 'evidence': ['parent_contract', 'fragment']}
        return {'action': 'retry', 'instructions': '换方法并用一个最小验证继续', 'reason': '已有不同失败证据'}

    result = await run_flow(orchestrate(store, tmp_path, runner, review, coordinate), flow, store)
    assert result.status == 'completed', result.final_summary
    record = store.load(RUN_ID)
    value = ledger.state(record)
    assert value['plan_version'] == 2 and record['locked_flow']['version'] == 1
    assert len(prior_failures) == 2
    for old in prior_failures:
        assert value['attempts'][old['id']] == old
    assert value['lineage_budget']['limits'] == limits[0]
    assert value['lineage_budget']['lineages']['work']['used']['expansions'] == 1
    assert invoked.count('work') == 3 and invoked.count('deliver') == 1
    assert all(invoked.count(key) == 1 for key in value['contracts']['work']['join_children'])
    assert all(ledger.valid_certificate(value, key) for key in value['contracts'])
