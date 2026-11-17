"""Current host state wins over obsolete summaries, references and receipts."""
import copy
import json

import pytest

from mewcode.experts.continuation import checkpoint_prompt, turn_guidance
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.workflow_context import current_requirements, latest_submission
from mewcode.kernel.briefs import brief
from mewcode.tools.submit_verdict import SubmitVerdictParams
from tests.test_workflow_context import receipt, runtime


def stage_for(ctx, **changes):
    return dict(node_id='audit', node_attempt=1, epoch=0, input_revision=ctx.binding['input_revision'],
                status='running', tools={}, **changes)


def test_untyped_old_rejection_is_evidence_not_a_new_instruction():
    registry, _, _, ctx = runtime()
    stage = stage_for(ctx)
    stage['tools']['1'] = receipt('SubmitVerdict', {'verdict': 'NEEDS_ATTENTION'}, error=True,
                                output='OBSOLETE: ReadRunArchive and ReadAuthorizations for every old decision')
    before = copy.deepcopy(stage)
    assert 'OBSOLETE' not in latest_submission(stage)['next_action']
    ctx.observe(stage, current_requirements(registry))
    assert ctx.snapshot['submission']['status'] == 'rejected'
    assert 'OBSOLETE' not in ctx.snapshot['next_action']
    assert 'OBSOLETE' not in checkpoint_prompt(stage, node_id='audit', requirements=current_requirements(registry))
    assert stage == before and registry.get('SubmitVerdict')._holder.read() is None


@pytest.mark.asyncio
async def test_typed_old_reference_error_is_recomputed_against_current_requirements():
    registry, _, _, ctx = runtime()
    tool = registry.get('SubmitVerdict')
    tool.required_decisions = {'retired-decision': {'answer': 'old'}}
    params = SubmitVerdictParams(verdict='NEEDS_ATTENTION')
    result = await tool.execute(params)
    stage = stage_for(ctx)
    stage['tools']['1'] = receipt('SubmitVerdict', params.model_dump(), error=True,
                                output=result.output, observation=result.host_observation)
    tool.required_decisions = {}
    ctx.observe(stage, current_requirements(registry))
    feedback = ctx.snapshot['submission']
    assert feedback['status'] == 'rejected'  # Current references alone cannot accept it.
    assert 'retired-decision' not in json.dumps(feedback)
    assert 'ReadAuthorizations' not in feedback['next_action']
    assert 'missing_decision_ids' not in feedback
    assert tool._holder.read() is None


@pytest.mark.asyncio
async def test_only_changed_source_is_requested_and_accepted_submission_stops_old_corrections():
    registry, _, _, ctx = runtime()
    tool = registry.get('SubmitVerdict')
    tool.required_sources = {'unchanged': 'v1', 'changed': 'v2'}
    draft = SubmitVerdictParams(verdict='NEEDS_ATTENTION',
                                reviewed_sources={'unchanged': 'v1', 'changed': 'v1'})
    rejected = await tool.execute(draft)
    assert rejected.host_observation['submission']['mismatched_sources'] == {'changed': 'v2'}
    assert 'unchanged' not in rejected.output
    stage = stage_for(ctx)
    stage['tools']['1'] = receipt('SubmitVerdict', draft.model_dump(), error=True,
                                output=rejected.output, observation=rejected.host_observation)
    ctx.observe(stage, current_requirements(registry))
    stage['workflow_context'] = ctx.saved()
    # The stored summary is deliberately stale. A later accepted receipt wins.
    stage['workflow_context']['snapshot']['next_action'] = 'OBSOLETE_NEXT_ACTION'
    stage['workflow_context']['snapshot']['waiting_for'] = 'user'
    accepted = await tool.execute(draft.model_copy(update={'reviewed_sources': dict(tool.required_sources)}))
    stage['tools']['2'] = receipt('SubmitVerdict', {'verdict': 'NEEDS_ATTENTION'}, index=2,
                                observation=accepted.host_observation)
    stage['handoff'] = {'open_items': ['OBSOLETE_WORK']}
    prompt = checkpoint_prompt(stage, node_id='audit', requirements=current_requirements(registry))
    assert 'OBSOLETE_NEXT_ACTION' not in prompt
    assert '"waiting_for": "host"' in prompt
    assert 'submission_source_revision_mismatch' not in prompt
    assert turn_guidance(stage, {}) is None
    ctx.observe(stage, current_requirements(registry))
    assert not ctx.snapshot['next_items']
    assert tool._holder.read()['verdict'] == 'NEEDS_ATTENTION'


def test_other_input_revision_cannot_reintroduce_old_work_or_errors():
    registry, _, _, ctx = runtime()
    stage = stage_for(ctx)
    stage.update(handoff={'open_items': ['OBSOLETE_WORK']}, handoff_input_revision='old-input')
    stage['tools']['1'] = dict(receipt('SubmitVerdict', {}, error=True,
                                     output='OBSOLETE_REJECTION'), input_revision='old-input')
    ctx.observe(stage, current_requirements(registry))
    assert ctx.snapshot['submission'] is None
    assert not ctx.snapshot['next_items']
    assert 'OBSOLETE' not in checkpoint_prompt(stage, node_id='audit')


def test_current_delivery_and_pending_request_override_old_ledger_and_summary():
    registry, _, _, ctx = runtime()
    stage = stage_for(ctx)
    stage.update(handoff={'open_items': ['CURRENT_WORK'], 'question': 'ANSWERED_QUESTION'},
                 ledger_view={'last_handoff': {'payload': {'open_items': ['OBSOLETE_WORK']}}},
                 workflow_context={'snapshot': {'next_action': 'OBSOLETE_ACTION'}})
    prompt = checkpoint_prompt(stage, node_id='audit')
    assert 'CURRENT_WORK' in prompt
    assert 'OBSOLETE' not in prompt and 'ANSWERED_QUESTION' not in prompt
    stage['pending_interaction'] = {'request_id': 'real', 'question': 'CURRENT_QUESTION'}
    prompt = checkpoint_prompt(stage, node_id='audit')
    assert 'CURRENT_QUESTION' in prompt and '"waiting_for": "user"' in prompt
    assert '"next_items": []' in prompt
    ctx.observe(stage, current_requirements(registry))
    assert ctx.snapshot['waiting_for'] == 'user'


def test_unpublished_current_handoff_is_used_but_published_host_state_wins():
    _, _, _, ctx = runtime()
    stage = stage_for(ctx)
    stage['handoff'] = {'open_items': ['OBSOLETE_WORK']}
    stage['tools']['1'] = receipt('SubmitHandoff', {'open_items': ['CURRENT_WORK']})
    assert 'CURRENT_WORK' in checkpoint_prompt(stage, node_id='audit')
    assert 'OBSOLETE_WORK' not in checkpoint_prompt(stage, node_id='audit')
    stage.update(status='completed', handoff={'open_items': ['HOST_RECONCILED_WORK']})
    ctx.observe(stage)
    assert ctx.snapshot['next_items'] == ['HOST_RECONCILED_WORK']


def test_resolved_failure_is_not_reintroduced_by_attempt_brief():
    record = {'node_states': {'audit': {}}, 'attempt': 2}
    ledger = ledger_for_run(record)
    failed = ledger.record('audit', 'receipt', {'request_id': 'request', 'status': 'failed', 'exit_code': 1})
    ledger.record('audit', 'receipt', {'request_id': 'request', 'status': 'completed', 'exit_code': 0})
    payload = json.loads(brief(record, 'audit'))
    assert payload['failed_methods'] == []
    assert any(fact.fact_id == failed.fact_id for fact in ledger.view('audit').history)


def test_quality_rejection_and_real_pending_request_are_not_cleared_by_reference_match():
    registry, _, _, ctx = runtime()
    stage = stage_for(ctx)
    stage['tools']['1'] = receipt('SubmitVerdict', {'verdict': 'PASS'}, error=True,
        observation={'submission': {'status': 'rejected', 'category': 'quality_failure',
                                     'next_action': 'CURRENT_QUALITY_FAILURE'}})
    ctx.observe(stage, current_requirements(registry))
    assert ctx.snapshot['submission']['category'] == 'quality_failure'
    assert ctx.snapshot['next_action'] == 'CURRENT_QUALITY_FAILURE'
    stage['pending_permission'] = {'tool_id': 'real-permission'}
    ctx.observe(stage, current_requirements(registry))
    assert ctx.snapshot['waiting_for'] == 'user'
    assert turn_guidance(stage, {}) is None


def test_source_reference_recheck_does_not_clear_required_review_snapshot_failure():
    registry, _, _, ctx = runtime()
    stage = stage_for(ctx)
    stage['tools']['1'] = receipt('SubmitVerdict', {'verdict': 'PASS'}, error=True,
        observation={'submission': {'status': 'rejected', 'category': 'reference_mismatch',
            'code': 'submission_snapshot_mismatch', 'next_action': 'Verify required review snapshot'}})
    ctx.observe(stage, current_requirements(registry))
    assert ctx.snapshot['submission']['code'] == 'submission_snapshot_mismatch'
    assert ctx.snapshot['submission']['status'] == 'rejected'
