import copy
import json
from dataclasses import asdict

import pytest

from mewcode.agent import Agent
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager, Message, ToolResultBlock, ToolUseBlock
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, TextDelta
from mewcode.tools.read_audit_artifact import ReadAuditArtifact
from mewcode.experts.recovery_context import probe_restores_step
from mewcode.experts.recovery_case import CaseRound, apply_event, cases, open_case
from mewcode.experts.recovery_case import method_fingerprint
from mewcode.experts.root_cause import validate_plan
from mewcode.experts.problem_dossier import build_dossier
from mewcode.experts.waits import BranchWaitPaused
from tests.test_recovery_case_e2e import RUN, harness, option, plan, run
from tests.test_visual_authorization_usage import setup, reserve
from mewcode.experts.visual_authorization_usage import authorization_account


def images_conversation():
    conversation = ConversationManager()
    conversation.add_user_message('Inspect the representative pages')
    for i in range(12):
        identity = f'page-{i}'
        conversation.history += [Message('assistant', '', tool_uses=[ToolUseBlock(identity, 'ReadAuditArtifact', {})]),
            Message('user', '', tool_results=[ToolResultBlock(identity, f'page {i}', content_blocks=[
                {'type': 'image', 'mime_type': 'image/png', 'data': f'pixels-{i}'}])])]
    for i in range(5):
        conversation.history.append(Message('user', '', tool_results=[ToolResultBlock(f'screen-{i}', 'screenshot',
            content_blocks=[{'type': 'image', 'mime_type': 'image/png', 'data': f'screen-{i}'}])]))
    return conversation


def tool_images(conversation):
    return [block for message in conversation.history for result in message.tool_results
            for block in result.content_blocks if block['type'] == 'image']


def test_image_view_preserves_twelve_evidence_pages_and_keeps_only_three_screenshots():
    conversation = images_conversation()
    before = copy.deepcopy(conversation.history)
    sent = conversation.prune_tool_result_images()
    assert len(tool_images(sent)) == 15
    assert [image['data'] for image in tool_images(sent)[:12]] == [f'pixels-{i}' for i in range(12)]
    assert len(tool_images(conversation)) == 17 and conversation.history == before
    sent.history[-1].tool_results[0].content_blocks.clear()
    assert conversation.history == before
    assert len(tool_images(conversation.prune_tool_result_images(keep_last=0))) == 12


def test_host_image_marker_preserves_evidence_after_tool_call_compaction(tmp_path):
    from PIL import Image
    import io
    data = io.BytesIO()
    Image.new('RGB', (20, 20)).save(data, format='PNG')
    result = ReadAuditArtifact.image(data.getvalue(), 'actual evidence')
    conversation = ConversationManager()
    conversation.history = [Message('user', '', tool_results=[ToolResultBlock(str(i), result.output,
        content_blocks=copy.deepcopy(result.content_blocks))]) for i in range(12)]
    assert len(tool_images(conversation.prune_tool_result_images(keep_last=0))) == 12


@pytest.mark.asyncio
@pytest.mark.parametrize('headless', [False, True])
async def test_both_agent_request_paths_send_all_evidence_without_modifying_history(tmp_path, headless):
    class Client(LLMClient):
        async def stream(self, conversation, system='', tools=None):
            seen = tool_images(conversation)
            assert len(seen) == 15
            assert sum(image['data'].startswith('pixels-') for image in seen) == 12
            yield TextDelta('observed representative pages')
            yield StreamEnd('end_turn', input_tokens=100, output_tokens=10)
    conversation = images_conversation()
    before = copy.deepcopy(conversation.history)
    agent = Agent(Client(), ToolRegistry(), 'anthropic', work_dir=str(tmp_path), max_iterations=2)
    if headless:
        await agent.run_to_completion('Observe the representative pages', conversation)
    else:
        _ = [event async for event in agent.run(conversation)]
    assert len(tool_images(conversation)) == 17
    # Agent adds ordinary prompt/answer messages, but original image blocks survive.
    actual = {r.tool_use_id: r.content_blocks for m in conversation.history for r in m.tool_results}
    assert actual == {r.tool_use_id: r.content_blocks for m in before for r in m.tool_results}


def test_repeated_method_after_repair_is_accepted_and_history_is_retained(tmp_path):
    h = harness(tmp_path)
    record = h.store.load(RUN)
    case = open_case(record, 'work')
    selected = option(1)
    row = asdict(CaseRound(1, 'same', method_fingerprint(selected['strategy']), 'transient', verification={'result': 'unchanged'}))
    case = apply_event(record, case['case_id'], {'kind': 'finish', 'identity': 'first', 'round': row})
    candidate = plan({'case_id': case['case_id'], 'round': 2}, [selected])
    validate_plan(candidate, case, build_dossier(record, case))
    from mewcode.experts.recovery_analyst import select_option
    assert select_option(candidate, record, case)[0] == selected
    case = apply_event(record, case['case_id'], {'kind': 'finish', 'identity': 'second',
        'round': {**row, 'n': 2, 'verification': {'result': 'resolved'}}})
    assert case['status'] == 'resolved' and len(case['rounds']) == 2


def test_probe_must_measure_original_failed_operation_and_not_inventory():
    case = {'failed_step': {'tool': 'ReadAuditArtifact', 'arguments': {'file_path': 'book.pdf', 'page': 183, 'render_page': True}}}
    assert probe_restores_step(case, 'ReadAuditArtifact', case['failed_step']['arguments'])
    assert not probe_restores_step(case, 'ProbeCapabilities', {})
    assert not probe_restores_step(case, 'ReadFile', {'file_path': 'log.json'})
    assert not probe_restores_step(case, 'ReadAuditArtifact', {**case['failed_step']['arguments'], 'page': 1})


@pytest.mark.asyncio
async def test_successful_inventory_and_changing_error_text_do_not_resolve_or_count_as_progress(tmp_path):
    h = harness(tmp_path, outcomes=[{'passed': True, 'operation_restored': False, 'error': 'one'},
        {'passed': True, 'operation_restored': False, 'error': 'two'}])
    with pytest.raises(BranchWaitPaused): await run(h)
    case = cases(h.store.load(RUN))[0]
    assert len(case['rounds']) == 2 and case['status'] == 'reported'
    assert all(row['verification']['result'] == 'unchanged' for row in case['rounds'])


def test_unknown_call_is_estimated_and_next_affordable_call_can_start(tmp_path):
    h, grant, route, meter = setup(tmp_path, cap=10000)
    prior = reserve(meter, route, input_tokens=100, output_tokens=800)
    meter.start(prior['call_id'])
    meter.settle(prior['call_id'], outcome='interrupted')
    account = authorization_account(h.store.load(RUN), grant)
    assert account['estimated_used_tokens'] == 900 and account['remaining_tokens'] == 9100
    following = reserve(meter, route, input_tokens=100, output_tokens=800)
    meter.start(following['call_id'])
    meter.settle(following['call_id'], usage={'reported_total_tokens': 200})
    assert authorization_account(h.store.load(RUN), grant)['remaining_tokens'] == 8900
    meter.settle(prior['call_id'], usage={'reported_total_tokens': 300})
    account = authorization_account(h.store.load(RUN), grant)
    assert account['estimated_used_tokens'] == 0 and account['used_tokens'] == 500


@pytest.mark.asyncio
async def test_quota_shortage_generates_one_concrete_decision_without_log_rounds(tmp_path):
    quota = {'remaining_tokens': 0, 'known_used_tokens': 200, 'estimated_used_tokens': 900,
        'required_tokens': 1000, 'request': {'operation_class': 'external_send', 'scope': {
            'model_routes': ['test/vision'], 'content_classes': ['page_image'], 'cost_cap_tokens': 1000}}}
    h = harness(tmp_path, outcomes=[{'passed': False, 'operation_restored': False, 'authorization_accounting': quota}])
    with pytest.raises(BranchWaitPaused): await run(h)
    case = cases(h.store.load(RUN))[0]
    assert len(case['rounds']) == 1 and case['status'] == 'awaiting_user'
    assert case['pending']['kind'] == 'elevation'
    assert '1000 token' in case['pending']['question'] and '保守估算' in case['pending']['question']
    assert [o['value'] for o in case['pending']['options']] == ['approve', 'close']
    assert case['pending']['authority'][0]['scope']['cost_cap_tokens'] == 1000


def test_stop_case_then_explicit_node_retry_starts_fresh_epoch_and_keeps_case_history(tmp_path):
    from mewcode.experts.case_runtime import answer_case
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.experts.recovery_case import active_case
    h = harness(tmp_path)
    def prepare(record):
        case = open_case(record, 'work')
        case = apply_event(record, case['case_id'], {'kind': 'report', 'identity': 'report',
            'report': {'reason': 'stopped', 'options': [{'value': 'close'}]}})
        answer_case(record, case['case_id'], round=0, choice='close')
        record['status'] = 'paused'
        record['active_node_ids'] = []
    h.store.mutate(RUN, prepare)
    prior = h.store.load(RUN)
    old = cases(prior, 'work')[0]
    resumed = h.store.resume(RUN, strategy='stable', retry_node_id='work')
    assert ledger_for_run(resumed).view('work').epoch == 1
    assert active_case(resumed, 'work') is None
    saved = cases(resumed, 'work')[0]
    assert saved['case_id'] == old['case_id'] and saved['closed_by_user'] and saved['status'] == 'superseded'
    assert resumed['acceptance_rechecks'][-1]['previous']['work']['status'] == 'paused'


def test_explicit_retry_does_not_clear_unknown_write_effects(tmp_path):
    from mewcode.experts.case_runtime import answer_case
    from mewcode.experts.node_ledger import ledger_for_run
    h = harness(tmp_path)
    def prepare(record):
        case = open_case(record, 'work')
        apply_event(record, case['case_id'], {'kind': 'report', 'identity': 'report',
            'report': {'reason': 'stopped', 'options': [{'value': 'close'}]}})
        answer_case(record, case['case_id'], round=0, choice='close')
        ledger_for_run(record).record('work', 'receipt', {
            'request_id': 'write-unknown', 'status': 'outcome_unknown', 'effect_class': 'write'})
        record.update(status='paused', active_node_ids=[])
    h.store.mutate(RUN, prepare)
    before = h.store.load(RUN)
    with pytest.raises(ValueError, match='效果未知'):
        h.store.resume(RUN, strategy='stable', retry_node_id='work')
    assert h.store.load(RUN) == before


def test_unknown_read_can_retry_but_unknown_write_and_live_read_still_block():
    from mewcode.experts.recovery_context import tool_effect_pending
    from mewcode.experts.recovery_state import blockers
    assert not tool_effect_pending({'name': 'InspectVisualEvidence', 'status': 'outcome_unknown'})
    assert tool_effect_pending({'name': 'InspectVisualEvidence', 'status': 'running'})
    assert tool_effect_pending({'name': 'WriteFile', 'status': 'outcome_unknown'})
    assert tool_effect_pending({'name': 'Bash', 'status': 'outcome_unknown'})
    assert not blockers({'tools': {'read': {'name': 'InspectVisualEvidence', 'status': 'outcome_unknown'}}})
    assert blockers({'tools': {'write': {'name': 'WriteFile', 'status': 'outcome_unknown'}}})


def test_archiving_old_node_does_not_refund_legacy_visual_usage(tmp_path):
    h, grant, _, _ = setup(tmp_path)
    receipt = {'name': 'InspectVisualEvidence', 'host_observation': {'visual_call': {
        'authorization': {'auth_id': grant['auth_id']}, 'usage': {'reported_total_tokens': 150}}}}
    record = h.store.load(RUN)
    record['node_states']['work']['tools'] = {'old': receipt}
    original = authorization_account(record, grant)
    record['acceptance_rechecks'] = [{'previous': {'work': copy.deepcopy(record['node_states']['work'])}}]
    record['node_states']['work']['tools'] = {}
    assert authorization_account(record, grant)['used_tokens'] == original['used_tokens'] == 150
