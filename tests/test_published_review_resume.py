"""Continue owns published investigations and their nested review stages."""
import copy
from dataclasses import asdict

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import review_binding
from mewcode.kernel.events import digest
from mewcode.orchestration.review_intervention import intervene
from mewcode.orchestration.revisions import can_resume_confirmed_flow
from tests.test_collaboration_mode import RUN_ID, delivery
from tests.test_review_intervention import intervention_fixture, investigation_plan


async def checkpoint(tmp_path):
    async def coordinate(model, payload, system):
        if 'intervention_plan' in payload:
            return {'passed': True, 'evidence': ['issue:R-1'], 'reason': 'Read-only evidence work.'}
        return investigation_plan()

    runtime, issues = intervention_fixture(tmp_path, coordinate)
    original = runtime.flow
    assert await intervene(runtime, original.node_map['audit'], issues) == 'scheduled'

    def pause_review(record):
        value = ledger.state(record)
        child = next(key for key in value['contracts'] if '::review-' in key)
        attempt = value['attempts'][ledger.dispatch(value, child)['id']]
        attempt.update(status='checking', delivery=asdict(delivery('Saved investigation result')))
        check = value['contracts'][child]['checks'][0]
        context = dict(node_id=child, attempt_id=attempt['id'], acceptance_round=0)
        binding = review_binding(context, check)
        identity = digest(['test-acceptance', binding])
        binding.update(call_id=identity, handoff_hash=digest(attempt['delivery']['handoff']))
        helper = binding['recovery_node_id']
        value['calls'][identity] = dict(kind='acceptance', status='dispatched', binding=binding)
        # Earlier failed reviews remain in history when this review is paused.
        historical_binding = review_binding({**context, 'acceptance_round': 1}, check)
        historical_binding.update(call_id='finished-review', handoff_hash=binding['handoff_hash'])
        historical = historical_binding['recovery_node_id']
        value['calls']['finished-review'] = dict(kind='acceptance', status='finished',
            binding=historical_binding, result={'execution_error': 'acceptance_format', 'passed': False})
        record.update(status='paused', stage_cursor=helper, active_node_ids=[])
        record['node_states'] = {
            'work': {'status': 'completed'}, child: {'status': 'paused'},
            historical: {'status': 'incomplete_audit', 'role': 'auditor', 'input_revision': attempt['dependencies']},
            helper: {'status': 'paused', 'role': 'auditor', 'input_revision': attempt['dependencies']}}

    runtime.store.mutate(RUN_ID, pause_review, durable=True)
    return runtime.store, original


@pytest.mark.asyncio
async def test_continue_preserves_published_work_and_paused_independent_acceptance(tmp_path):
    store, original = await checkpoint(tmp_path)
    before = store.load(RUN_ID)
    assert can_resume_confirmed_flow(before, original)
    assert before == store.load(RUN_ID)
    after = store.resume(RUN_ID, strategy='stable', flow=original)
    assert after['attempt'] == before['attempt'] + 1
    assert after['locked_flow'] == before['locked_flow']
    for key in ('runtime_flow', 'flow_versions', 'graph_publications', 'entries', 'contracts', 'attempts', 'certificates'):
        assert after['collaboration'][key] == before['collaboration'][key]


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['missing_publication', 'unpublished', 'changed_fragment', 'changed_version',
    'changed_runtime', 'changed_history', 'missing_binding', 'unknown_stage', 'changed_design', 'removed_parent',
    'unbound_history', 'wrong_call', 'wrong_delivery', 'wrong_role'])
async def test_continue_rejects_unproven_published_or_review_progress(tmp_path, change):
    store, original = await checkpoint(tmp_path)
    record = store.load(RUN_ID)
    value, current = record['collaboration'], original.as_dict()
    version = value['flow_versions'][-1]
    publication = value['graph_publications'][version['publication_id']]
    if change == 'missing_publication': value['graph_publications'].clear()
    elif change == 'unpublished': publication['status'] = 'prepared'
    elif change == 'changed_fragment': publication['fragment']['flow']['version'] += 1
    elif change == 'changed_version': publication['new_plan_version'] += 1
    elif change == 'changed_runtime': value['runtime_flow']['nodes'].pop()
    elif change == 'changed_history': version['flow']['nodes'].pop()
    elif change == 'missing_binding': value['calls'].clear()
    elif change == 'unknown_stage': record['node_states']['audit::review-unproven'] = {'status': 'completed'}
    elif change == 'changed_design': current['nodes'][0]['label'] = 'Another plan'
    elif change == 'removed_parent':
        current['nodes'] = [node for node in current['nodes'] if node['node_id'] != 'audit']
        current['edges'] = []
    else:
        call = value['calls']['finished-review']
        if change == 'unbound_history': call.pop('binding')
        elif change == 'wrong_call': call['binding']['call_id'] = 'not-the-call'
        elif change == 'wrong_delivery': call['binding']['handoff_hash'] = 'not-the-delivery'
        elif change == 'wrong_role': record['node_states'][call['binding']['recovery_node_id']]['role'] = 'executor'
    before = copy.deepcopy(record)
    assert not can_resume_confirmed_flow(record, current)
    assert record == before
