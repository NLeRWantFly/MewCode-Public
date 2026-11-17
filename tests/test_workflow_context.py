"""Workflow instructions survive recovery without growing every turn's prefix."""
import copy
import json
from pathlib import Path

import pytest

from mewcode.conversation import ConversationManager
from mewcode.experts.workflow_context import (
    MARKER, MAX_DELTAS, WorkingContext, ReadWorkflowContext, ReadWorkflowContextParams,
    build_workflow_skill, current_requirements, latest_submission, project_working_context, save_skill_package,
)
from mewcode.experts.recovery_diagnostics import measure_segment, read_result_fingerprint
from mewcode.experts.recovery_diagnosis import diagnose_segment
from mewcode.experts.recovery_state import progress_marker
from mewcode.tools import ToolRegistry
from mewcode.tools.submit_verdict import SubmitVerdictTool, SubmitVerdictParams


def runtime():
    registry = ToolRegistry()
    registry.register(SubmitVerdictTool())
    record = {'task': '核验固定产物', 'locked_flow': {'nodes': [
        {'node_id': 'read-a', 'role': 'executor', 'label': '独立识别 A', 'responsibility': '独立读取'},
        {'node_id': 'read-b', 'role': 'executor', 'label': '独立识别 B', 'responsibility': '另一独立读取'},
        {'node_id': 'audit', 'role': 'auditor', 'label': '核验', 'responsibility': '核对原文并提交证据'}],
        'edges': [{'from_node': 'read-a', 'to_node': 'audit'}]},
        'node_states': {'read-b': {'messages': ['PRIVATE_PEER_RESULT'], 'handoff': {'summary': 'PRIVATE_PEER_RESULT'}}}}
    package = build_workflow_skill(record, 'audit', 'auditor', registry)
    binding = {'node_id': 'audit', 'input_revision': 'input-a', 'node_attempt': 1,
               'epoch': 0, 'skill_version': package['version']}
    return registry, record, package, WorkingContext(package, binding)


def receipt(name, args, *, index=1, output='observed', observation=None, error=False):
    return {'tool_id': str(index), 'node_attempt': 1, 'epoch': 0, 'name': name, 'args': args,
            'status': 'failed' if error else 'completed', 'is_error': error,
            'declared_at': 100 + index, 'started_at': 100 + index, 'completed_at': 100.5 + index,
            'output': output, 'result_sha256': read_result_fingerprint(output),
            'host_observation': observation}


def test_package_identity_separates_progress_and_tool_versions_and_excludes_peer_evidence(tmp_path):
    registry, record, package, ctx = runtime()
    record['node_states']['audit'] = {'status': 'completed', 'workflow_context': {'revision': 40}}
    assert build_workflow_skill(record, 'audit', 'auditor', registry)['version'] == package['version']
    assert 'PRIVATE_PEER_RESULT' not in json.dumps(package)
    assert 'SubmitVerdict' in package['sections']['tools']
    assert 'ReadWorkflowContext' in package['sections']['tools']
    registry.unregister('SubmitVerdict')
    changed = build_workflow_skill(record, 'audit', 'auditor', registry)
    assert changed['version'] != package['version']
    assert 'SubmitVerdict' not in changed['sections']['tools']
    path = Path(save_skill_package(package, tmp_path))
    from mewcode.skills.skill_validate import validate_skill
    assert validate_skill(path.parent)[0]
    assert json.loads((path.parent / 'references' / 'plan.json').read_text(encoding='utf-8')) == package['sections']['plan']
    assert path.read_text(encoding='utf-8') == package['markdown']


def test_long_context_is_bounded_and_normal_turns_keep_prefix():
    registry, record, package, ctx = runtime()
    conversation = ConversationManager()
    conversation.add_user_message('CURRENT_REQUIRED_CONSTRAINT')
    stage = {'node_attempt': 1, 'input_revision': 'input-a', 'tools': {}}
    maximum = 0
    for number in range(1, 141):
        stage['tools'][str(number)] = receipt('WriteFile', {'path': 'result.txt'}, index=number)
        stage['tools'][str(number)]['effect_sha256'] = f'{number:064x}'
        ctx.observe(stage)
        prefix = [(m.role, m.content) for m in conversation.history]
        previous_coalesces = ctx.coalesces
        ctx.sync(conversation)
        if number > 1 and ctx.coalesces == previous_coalesces:
            assert [(m.role, m.content) for m in conversation.history[:len(prefix)]] == prefix
        messages = [m for m in conversation.history if m.context_kind == MARKER]
        assert len(messages) <= MAX_DELTAS + 1
        maximum = max(maximum, sum(len(m.content) for m in messages))
        assert conversation.history[0].content == 'CURRENT_REQUIRED_CONSTRAINT'
        # Saving a summary has no effect on host business progress.
        stage['workflow_context'] = ctx.saved()
        assert progress_marker({'node_states': {'audit': stage}}, 'audit')['submitted_items'] == []
    assert maximum < 13000
    assert ctx.coalesces < 35
    assert ctx.snapshot['progress']['artifact_versions'] == 140
    before = copy.deepcopy(ctx.saved())
    candidate = ConversationManager()
    ctx.restore(candidate)
    assert ctx.saved() == before  # compaction candidate restoration is pure
    assert ctx.snapshot['next_action'] in candidate.history[-1].content


@pytest.mark.asyncio
async def test_rejected_extra_references_survive_reads_restart_and_correct_submission():
    registry, record, package, ctx = runtime()
    tool = registry.get('SubmitVerdict')
    draft = SubmitVerdictParams(verdict='PASS', reviewed_decisions={
        'old-' + str(i): {'applicability': 'original_scope_only', 'reason': 'Old scope'} for i in range(5)})
    rejected = await tool.execute(draft)
    assert rejected.is_error
    feedback = rejected.host_observation['submission']
    assert feedback['category'] == 'reference_mismatch'
    assert feedback['missing_decision_ids'] == []
    assert len(feedback['unknown_decision_ids']) == 5
    assert 'ReadAuthorizations' not in feedback['next_action']
    stage = {'node_attempt': 1, 'epoch': 0, 'input_revision': 'input-a', 'status': 'budget_exhausted',
             'started_at': 100, 'completed_at': 200, 'tools': {}}
    snapshot = {'node_states': {'audit': stage}}
    stage['slice_progress_baseline'] = progress_marker(snapshot, 'audit')
    stage['tools']['1'] = receipt('SubmitVerdict', draft.model_dump(), error=True,
        observation=rejected.host_observation, output=rejected.output)
    for i in range(2, 32):
        stage['tools'][str(i)] = receipt('ReadRunArchive', {'tool_id': '1', 'offset': 0}, index=i, output='same draft')
    ctx.observe(stage, current_requirements(registry))
    assert ctx.snapshot['waiting_for'] == 'agent'
    assert ctx.snapshot['submission']['tool_id'] == '1'
    assert ctx.snapshot['progress']['accepted_submissions'] == 0
    health = measure_segment(snapshot, 'audit', terminal_status='budget_exhausted')
    assert health['effective_calls'] == 0
    assert health['repeated_reads'] == 29
    diagnosis = diagnose_segment(health)
    assert diagnosis['primary_kind'] == 'protocol_conflict'
    assert diagnosis['recommended_action'] == 'reconcile_protocol'
    restarted = WorkingContext(package, ctx.binding, saved=ctx.saved())
    restarted.observe(stage, current_requirements(registry))
    reader = ReadWorkflowContext(restarted)
    response = await reader.execute(ReadWorkflowContextParams(section='submission'))
    stored = json.loads(json.loads(response.output)['content'])
    assert stored['draft'] == draft.model_dump()
    assert stored['feedback']['code'] == 'submission_reference_mismatch'
    corrected = await tool.execute(draft.model_copy(update={'reviewed_decisions': {}}))
    assert not corrected.is_error
    stage['tools']['32'] = receipt('SubmitVerdict', {'verdict': 'PASS'}, index=32,
                                  observation=corrected.host_observation)
    restarted.observe(stage)
    assert latest_submission(stage)['status'] == 'accepted'
    assert restarted.snapshot['progress']['accepted_submissions'] == 1
    assert restarted.snapshot['progress']['verified_items'] is None


@pytest.mark.asyncio
async def test_missing_references_are_targeted_and_user_wait_is_never_invented():
    registry, record, package, ctx = runtime()
    tool = registry.get('SubmitVerdict')
    tool.required_decisions = {'required': {'answer_excerpt': 'Already answered'}}
    result = await tool.execute(SubmitVerdictParams(verdict='PASS'))
    assert result.is_error and 'ReadAuthorizations' in result.output
    assert result.host_observation['submission']['missing_decision_ids'] == ['required']
    stage = {'node_attempt': 1, 'tools': {'1': receipt('SubmitVerdict', {}, error=True,
                         observation=result.host_observation)}, 'pending_interaction': {'question': 'Real business choice'}}
    snapshot = project_working_context(stage, binding=ctx.binding)
    assert snapshot['waiting_for'] == 'user'
    health = {'submission_feedback': result.host_observation['submission']}
    assert diagnose_segment(health, blockers=[{'kind': 'permission'}])['hard_boundary']


@pytest.mark.asyncio
async def test_paging_large_escaped_payload_has_no_gaps_and_self_reads_do_not_invalidate_current():
    registry, record, package, ctx = runtime()
    package['sections']['stage']['large'] = '\\"\n' * 12000
    ctx.observe({'tools': {}})
    reader = ReadWorkflowContext(ctx)
    parts, offset, revisions = [], 0, set()
    while True:
        response = await reader.execute(ReadWorkflowContextParams(section='stage', offset=offset, limit=6000))
        assert len(response.output) <= 10000
        page = json.loads(response.output)
        assert page['offset'] == offset
        parts.append(page['content'])
        revisions.add(page['source_revision'])
        offset = page['next_offset']
        if offset >= page['total_chars']:
            break
    assert len(revisions) == 1
    assert json.loads(''.join(parts)) == package['sections']['stage']
    first = await reader.execute(ReadWorkflowContextParams())
    ctx.observe({'tools': {'1': receipt('ReadWorkflowContext', {'section': 'current'},
                                      observation=first.host_observation, output=first.output)}})
    second = await reader.execute(ReadWorkflowContextParams())
    assert json.loads(first.output)['source_revision'] == json.loads(second.output)['source_revision']


def test_correction_gate_has_a_bound_and_acceptance_stays_distinct():
    from mewcode.experts.completion import rejected_verdict_correction
    state = {}
    kwargs = {'input_revision': 'v1', 'node_attempt': 1}
    assert rejected_verdict_correction(None, {}, state, **kwargs)
    assert rejected_verdict_correction(None, {}, state, **kwargs)
    assert not rejected_verdict_correction(None, {}, state, **kwargs)
    assert not rejected_verdict_correction({'verdict': 'NEEDS_ATTENTION'}, {}, {}, **kwargs)
    assert not rejected_verdict_correction(None, {'p': {'node_attempt': 1, 'status': 'waiting_permission'}}, {}, **kwargs)


def test_current_handoff_receipt_updates_next_work_before_scheduler_completion():
    _, _, _, ctx = runtime()
    stage = {'node_attempt': 1, 'handoff': {'open_items': ['OLD_WORK']}, 'tools': {
        '1': receipt('SubmitHandoff', {'status': 'needs_attention', 'open_items': ['CURRENT_WORK']})}}
    ctx.observe(stage)
    assert ctx.snapshot['next_items'] == ['CURRENT_WORK']
    assert ctx.snapshot['next_action'] == 'CURRENT_WORK'
    assert ctx.snapshot['progress']['verified_items'] is None
    stage['pending_permission'] = {'tool_id': 'permission-id'}
    ctx.observe(stage)
    assert ctx.snapshot['waiting_for'] == 'user'


@pytest.mark.asyncio
async def test_quality_service_and_schema_failures_remain_separate():
    registry, record, package, ctx = runtime()
    tool = registry.get('SubmitVerdict')
    failed = await tool.execute(SubmitVerdictParams(verdict='PASS', issues=[{
        'description': 'Artifact is wrong', 'resolution': 'producer_fix'}]))
    assert failed.host_observation['submission']['category'] == 'quality_failure'
    assert tool._holder.read() is None
    def unavailable():
        raise OSError('storage not ready')
    tool.completion_guard = unavailable
    failed = await tool.execute(SubmitVerdictParams(verdict='PASS'))
    assert failed.host_observation['submission']['category'] == 'service_fault'
    stage = {'tools': {'1': receipt('SubmitVerdict', {}, error=True, observation=failed.host_observation)}}
    assert project_working_context(stage, binding=ctx.binding)['waiting_for'] == 'host'
    from mewcode.tools.base import parameter_error_result
    typed = parameter_error_result('SubmitVerdict', ValueError('checks must be objects'))
    assert typed.host_observation['submission']['code'] == 'argument_schema_error'
    assert typed.is_error


def test_protocol_recovery_uses_existing_checkpoint_budget():
    from mewcode.experts.recovery_actions import plan_recovery_actions
    diagnosis = diagnose_segment({'submission_feedback': {'status': 'rejected', 'tool_id': 't',
        'category': 'reference_mismatch', 'next_action': 'Correct the field'}})
    args = dict(diagnosis=diagnosis, allowed=['continue_checkpoint'], checkpoint_present=True, hard_boundary=False)
    assert plan_recovery_actions(**args)['actions'][0]['status'] == 'available'
    assert plan_recovery_actions(**dict(args, allowed=[]))['actions'][0]['status'] == 'unavailable'
    assert plan_recovery_actions(**dict(args, hard_boundary=True))['actions'][0]['status'] == 'blocked'


def test_waiting_display_requires_real_inbox_item():
    from mewcode.orchestration.task_view import align_execution_display
    view = {'task': {'run_id': 'r', 'execution_status': 'blocked', 'status': '等你处理'},
            'inbox_resolved': True, 'inbox': [], 'plan': [{'status': '等你处理',
                'steps': [{'id': 'a', 'status': '等你处理'}]}]}
    align_execution_display(view)
    assert view['task']['status'] == '已暂停'
    assert view['plan'][0]['steps'][0]['status'] == '已暂停'
    pending = copy.deepcopy(view)
    pending['task']['status'] = '等你处理'
    pending['plan'][0]['steps'][0]['status'] = '等你处理'
    pending['inbox'] = [{'type': 'permission', 'mandatory': True}]
    align_execution_display(pending)
    assert pending['task']['waiting_for'] == 'user'
    assert pending['task']['status'] == '等你处理'


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['anthropic', 'openai-compat'])
async def test_real_agent_restores_workflow_during_compaction_and_budget_preview(tmp_path, monkeypatch, protocol):
    from mewcode.agent import Agent, ErrorEvent
    from mewcode.client import LLMClient
    from mewcode.context.manager import CompactEvent
    from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
    registry, record, package, ctx = runtime()
    ctx.observe({'tools': {}, 'handoff': {'open_items': ['NEXT_EXACT_WORK']}})
    registry.register(ReadWorkflowContext(ctx))
    seen = []
    class Client(LLMClient):
        async def stream(self, conversation, system='', tools=None):
            content = '\n'.join(str(m.content) for m in conversation.history)
            seen.append(content)
            assert package['version'] in content
            assert 'NEXT_EXACT_WORK' in content
            if len(seen) == 1:
                yield ToolCallComplete('current', 'ReadWorkflowContext', {'section': 'current'})
            else:
                yield TextDelta('Resumed from the exact next work item')
            yield StreamEnd('end_turn', input_tokens=20, output_tokens=10)
    previewed = []
    async def compact(conversation, *args, **kwargs):
        if len(seen) == 1:
            prior = copy.deepcopy(ctx.saved())
            preview = ConversationManager()
            kwargs['restore_context'](preview)
            assert ctx.saved() == prior
            assert 'NEXT_EXACT_WORK' in '\n'.join(m.content for m in preview.history)
            previewed.append(True)
            conversation.history = [m for m in conversation.history if not m.transient]
            return CompactEvent(before_tokens=100)
    monkeypatch.setattr('mewcode.agent.auto_compact', compact)
    agent = Agent(Client(), registry, protocol, str(tmp_path), max_iterations=3)
    agent.workflow_context = ctx
    agent.activate_skill(package['name'], package['body'])
    conversation = ConversationManager()
    conversation.add_user_message('Continue under CURRENT_REQUIRED_CONSTRAINT')
    events = [event async for event in agent.run(conversation)]
    assert not [event for event in events if isinstance(event, ErrorEvent)]
    assert len(seen) == 2 and previewed
    assert len([m for m in conversation.history if m.context_kind == MARKER]) == 1
    assert all('CURRENT_REQUIRED_CONSTRAINT' in content for content in seen)
