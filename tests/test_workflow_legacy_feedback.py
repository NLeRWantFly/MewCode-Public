"""Old rejected drafts get current guidance without rewriting historical receipts."""
import copy
import json

import pytest

from mewcode.experts.continuation import turn_guidance
from mewcode.experts.recovery_diagnosis import diagnose_segment
from mewcode.experts.recovery_diagnostics import measure_segment
from mewcode.experts.workflow_context import (
    ReadWorkflowContext, ReadWorkflowContextParams, current_requirements, latest_submission,
)
from mewcode.tools.submit_verdict import SubmitVerdictParams
from tests.test_workflow_context import receipt, runtime


def legacy_stage(ctx):
    draft = SubmitVerdictParams(verdict='NEEDS_ATTENTION',
        reviewed_sources={'@model_receipts': 'old'},
        reviewed_decisions={f'old-{i}': {'applicability': 'original_scope_only', 'reason': 'old scope'}
                            for i in range(5)})
    stage = {'node_id': 'audit', 'input_revision': ctx.binding['input_revision'],
             'node_attempt': 1, 'epoch': 0, 'status': 'budget_exhausted',
             'started_at': 100, 'completed_at': 200, 'tools': {}}
    stage['tools']['1'] = receipt('SubmitVerdict', draft.model_dump(), error=True,
        output='用户答复核对不完整；继续翻查所有历史。')
    for number in range(2, 32):
        stage['tools'][str(number)] = receipt('ReadRunArchive', {'offset': number * 1000}, index=number)
    return stage, draft


@pytest.mark.asyncio
async def test_live_legacy_shape_revalidates_sources_and_extra_refs_without_submitting():
    registry, _, _, ctx = runtime()
    tool = registry.get('SubmitVerdict')
    tool.required_sources = {'@model_receipts': 'current'}
    stage, draft = legacy_stage(ctx)
    before = copy.deepcopy(stage)
    assert latest_submission(stage)['category'] == 'unclassified'
    ctx.observe(stage, current_requirements(registry))
    feedback = ctx.snapshot['submission']
    assert feedback['category'] == 'reference_mismatch'
    assert feedback['feedback_basis'] == 'current_reference_requirements'
    assert feedback['unknown_decision_ids'] == [f'old-{i}' for i in range(5)]
    assert feedback['missing_decision_ids'] == []
    assert 'reviewed_decisions 应为 {}' in ctx.snapshot['next_action']
    assert 'ReadStageResult' in ctx.snapshot['next_action']
    assert 'ReadAuthorizations' not in ctx.snapshot['next_action']
    assert '继续翻查所有历史' not in ctx.snapshot['next_action']
    assert ctx.snapshot['waiting_for'] == 'agent'
    assert stage == before and tool._holder.read() is None and tool._holder.submissions == []
    response = await ReadWorkflowContext(ctx).execute(ReadWorkflowContextParams(section='submission', limit=6000))
    submission = json.loads(json.loads(response.output)['content'])
    assert submission['draft'] == draft.model_dump()
    assert submission['receipt'] == before['tools']['1']['output']
    assert submission['feedback']['feedback_basis'] == 'current_reference_requirements'

    stage['workflow_context'] = ctx.saved()
    guidance = turn_guidance(stage, {})
    assert guidance['kind'] == 'correct_submission'
    assert 'reviewed_decisions 应为 {}' in guidance['prompt']
    diagnosis = diagnose_segment(measure_segment({'node_states': {'audit': stage}}, 'audit'))
    assert diagnosis['primary_kind'] == 'protocol_conflict'
    assert diagnosis['recommended_action'] == 'reconcile_protocol'
    # The real tool must still execute all validation. The projection cannot accept it.
    corrected = await tool.execute(draft.model_copy(update={
        'reviewed_decisions': {}, 'reviewed_sources': {'@model_receipts': 'current'}}))
    assert not corrected.is_error
    assert tool._holder.read()['verdict'] == 'NEEDS_ATTENTION'
    assert before['tools'] == stage['tools']


@pytest.mark.parametrize('field,value', [
    ('node_id', 'peer'), ('input_revision', 'changed'), ('node_attempt', 2), ('epoch', 1),
])
def test_saved_requirements_cannot_reclassify_a_different_stage_binding(field, value):
    registry, _, _, ctx = runtime()
    stage, _ = legacy_stage(ctx)
    ctx.observe(stage, current_requirements(registry))
    stage['workflow_context'] = ctx.saved()
    stage[field] = value
    feedback = latest_submission(stage)
    assert feedback is None or feedback['category'] == 'unclassified'


def test_legacy_prose_cannot_fabricate_missing_references_or_quality_acceptance():
    registry, _, _, ctx = runtime()
    stage, _ = legacy_stage(ctx)
    stage['tools']['1']['args']['reviewed_decisions'] = {}
    stage['tools']['1']['args']['reviewed_sources'] = {}
    ctx.observe(stage, current_requirements(registry))
    assert ctx.snapshot['submission']['category'] == 'unclassified'
    assert ctx.snapshot['submission']['status'] == 'rejected'
    assert registry.get('SubmitVerdict')._holder.read() is None
    stage['tools']['1']['host_observation'] = {'submission': {'status': 'rejected',
        'category': 'quality_failure', 'next_action': '修复实际质量缺陷。'}}
    stage['tools']['1']['args']['reviewed_decisions'] = {'old': {}}
    ctx.observe(stage, current_requirements(registry))
    assert ctx.snapshot['submission']['category'] == 'quality_failure'


def test_current_missing_answer_refs_are_targeted_and_real_user_wait_is_preserved():
    registry, _, _, ctx = runtime()
    tool = registry.get('SubmitVerdict')
    tool.required_decisions = {'required-now': {'answer': 'already recorded'}}
    stage, _ = legacy_stage(ctx)
    stage['pending_interaction'] = {'request_id': 'real-question', 'question': '请选择尚未决定的范围。'}
    ctx.observe(stage, current_requirements(registry))
    feedback = ctx.snapshot['submission']
    assert feedback['missing_decision_ids'] == ['required-now']
    assert '仅补充 missing_decision_ids' in feedback['next_action']
    assert ctx.snapshot['waiting_for'] == 'user'
    assert stage['pending_interaction']['request_id'] == 'real-question'


def test_reference_preview_never_clears_an_existing_verdict():
    registry, _, _, ctx = runtime()
    tool = registry.get('SubmitVerdict')
    tool._holder.submit({'verdict': 'NEEDS_ATTENTION', 'evidence': ['original']})
    _, draft = legacy_stage(ctx)
    original = copy.deepcopy(tool._holder)
    assert tool.reference_rejections(draft)
    assert tool._holder == original
