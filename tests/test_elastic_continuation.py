"""Execution can correct its protocol and leave a repeated-read loop without new authority."""
import copy
import hashlib
import json

import pytest

from mewcode.experts.completion import rejected_handoff_correction
from mewcode.experts.continuation import turn_guidance
from mewcode.tools.submit_handoff import HandoffHolder, SubmitHandoff, SubmitHandoffParams


def stage_with_reads(*, changed=False, paged=False):
    tools = {}
    for i in range(8):
        tools[str(i)] = {'tool_id': str(i), 'name': 'ReadFile', 'node_attempt': 1,
            'epoch': 0, 'declared_at': i + 1, 'status': 'completed', 'is_error': False,
            'args': {'file_path': f'input-{i % 4}.json', 'offset': i if paged else 0},
            'result_sha256': hashlib.sha256(str(i if changed else i % 4).encode()).hexdigest()}
    return {'tools': tools, 'node_id': 'work', 'node_attempt': 1, 'epoch': 0, 'input_revision': 'v1'}


def test_identical_reads_trigger_adjustment_once_across_restart():
    stage, state = stage_with_reads(), {}
    before = copy.deepcopy(stage)
    guidance = turn_guidance(stage, state)
    assert guidance['kind'] == 'adjust_execution' and '最小工作批次' in guidance['prompt']
    assert '不批准被拒绝操作' in guidance['prompt']
    assert stage == before
    assert turn_guidance(stage, json.loads(json.dumps(state))) is None


@pytest.mark.parametrize('change', ['changed', 'paged', 'write', 'pending', 'epoch', 'attempt'])
def test_new_evidence_or_unsettled_operation_does_not_trigger_read_loop(change):
    stage = stage_with_reads(changed=change == 'changed', paged=change == 'paged')
    if change == 'write':
        stage['tools']['4']['name'] = 'WriteFile'
    if change == 'pending':
        stage['tools']['7']['status'] = 'outcome_unknown'
    if change == 'epoch':
        stage['epoch'] = 1
    if change == 'attempt':
        stage['node_attempt'] = 2
    assert turn_guidance(stage, {}) is None


def rejected():
    return {'bad': {'tool_id': 'bad', 'name': 'SubmitHandoff', 'node_attempt': 1, 'epoch': 0,
        'declared_at': 10, 'status': 'failed', 'is_error': True,
        'args': {'blocker_kind': 'internal_work'}, 'output': 'invalid open_items; private result contents'}}


def test_failed_handoff_gets_local_correction_without_treating_it_as_delivery():
    tools, state = rejected(), {}
    stage = {'tools': tools, 'node_attempt': 1, 'epoch': 0, 'input_revision': 'v1'}
    guidance = turn_guidance(stage, state)
    assert guidance['kind'] == 'correct_handoff'
    assert 'private result contents' not in guidance['prompt']
    assert turn_guidance(stage, state) is None
    assert tools['bad']['is_error'] is True


def test_end_of_reply_corrections_are_bounded_across_slices_and_never_replace_a_gate():
    tools, state = rejected(), {}
    for _ in range(2):
        assert rejected_handoff_correction(None, tools, state, input_revision='v1', node_attempt=1)
        state = json.loads(json.dumps(state))
    assert not rejected_handoff_correction(None, tools, state, input_revision='v1', node_attempt=1)
    assert not rejected_handoff_correction({'status': 'blocked', 'question': 'Choose scope'}, tools, {},
                                           input_revision='v1', node_attempt=1)
    tools['pending'] = {'node_attempt': 1, 'status': 'outcome_unknown'}
    assert not rejected_handoff_correction(None, tools, {}, input_revision='v1', node_attempt=1)


@pytest.mark.asyncio
async def test_handoff_schema_exposes_correctable_checkpoint_and_rejects_false_completion():
    from pydantic import ValidationError
    holder = HandoffHolder()
    tool = SubmitHandoff(holder)
    schema = tool.get_schema()['input_schema']
    assert '$ref' not in json.dumps(schema) and '$defs' not in schema
    fields = schema['properties']
    assert 'internal_work' in fields['blocker_kind']['enum']
    assert fields['inline_outputs']['items']['properties']['content']['type'] == 'string'
    invalid = {'summary': 'inputs ready', 'blocker_kind': 'internal_work', 'blockers': ['slice ended']}
    with pytest.raises(ValidationError, match='open_items'):
        SubmitHandoffParams.model_validate(invalid)
    assert holder.read() is None
    corrected = {**invalid, 'status': 'needs_attention', 'blockers': [], 'open_items': ['Process the first batch']}
    assert not (await tool.execute(SubmitHandoffParams.model_validate(corrected))).is_error
    assert holder.read()['status'] == 'needs_attention'
    with pytest.raises(ValidationError):
        SubmitHandoffParams.model_validate({**corrected, 'status': 'completed'})


@pytest.mark.asyncio
async def test_real_agent_repairs_rejected_submission_instead_of_ending_with_prose(tmp_path):
    from mewcode.agent import Agent, ToolUseEvent, ToolResultEvent, PermissionRequest
    from mewcode.conversation import ConversationManager
    from mewcode.tools import ToolRegistry
    from mewcode.tools.base import TextDelta, ToolCallComplete, StreamEnd
    from tests.test_agent import MockLLMClient

    invalid = {'summary': 'input inspected', 'blocker_kind': 'internal_work', 'blockers': ['slice ended']}
    corrected = {**invalid, 'status': 'needs_attention', 'blockers': [], 'open_items': ['Process first batch']}
    client = MockLLMClient([
        [ToolCallComplete('bad', 'SubmitHandoff', invalid), StreamEnd('end_turn', input_tokens=1, output_tokens=1)],
        [TextDelta('I will continue later.'), StreamEnd('end_turn', input_tokens=1, output_tokens=1)],
        [ToolCallComplete('good', 'SubmitHandoff', corrected), StreamEnd('end_turn', input_tokens=1, output_tokens=1)],
        [TextDelta('Checkpoint recorded.'), StreamEnd('end_turn', input_tokens=1, output_tokens=1)],
    ])
    holder, registry = HandoffHolder(), ToolRegistry()
    registry.register(SubmitHandoff(holder))
    agent = Agent(client, registry, 'anthropic', work_dir=str(tmp_path))
    conversation = ConversationManager()
    conversation.add_user_message('Process inputs; retain progress across execution slices.')
    state, tools, asks = {}, {}, []

    async def gate(conversation, response):
        prompt = rejected_handoff_correction(holder.read(), tools, state, input_revision='v1', node_attempt=1)
        if prompt:
            conversation.add_system_reminder(prompt)
        return 'continue' if prompt else 'complete'

    agent.completion_gate = gate
    async for event in agent.run(conversation):
        if isinstance(event, ToolUseEvent):
            tools[event.tool_id] = {'name': event.tool_name, 'node_attempt': 1,
                'declared_at': len(tools) + 1, 'status': 'queued'}
        if isinstance(event, ToolResultEvent):
            tools[event.tool_id].update(status='failed' if event.is_error else 'completed', is_error=event.is_error)
        if isinstance(event, PermissionRequest):
            asks.append(event)
    assert not asks and client._call_index == 4
    assert tools['bad']['is_error'] and not tools['good']['is_error']
    assert holder.read()['open_items'] == ['Process first batch']
    assert holder.read()['status'] == 'needs_attention'
    assert state['attempts'] == 1
