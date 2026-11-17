"""A terminal receipt is reusable evidence, never a new execution receipt."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import Agent
from mewcode.experts.command_receipts import observe_requests, request_receipts
from mewcode.kernel.events import digest


def cached_agent(state):
    original = {
        'call_id': 'source-call', 'node_id': 'env', 'input_revision': 'old-input',
        'state': state, 'invocation': {'tool': 'Bash', 'arguments': {'command': 'python probe.py'}},
        'evidence': [{'executor_accepted': True, 'tool_id': 'original-tool'}],
        'result': {'output': 'old environment', 'is_error': state == 'failed', 'content_blocks': [],
                   'host_observation': {'command_execution': {
                       'status': 'exited', 'command_started': True, 'exit_code': 0 if state == 'succeeded' else 1,
                       'stdout': 'old environment', 'stderr': ''}}},
    }
    agent = Agent.__new__(Agent)
    agent._safety_call_ref = lambda *args: 'test'
    agent._prepare_rollback_evidence = lambda *args: None
    agent.kernel_gateway = SimpleNamespace(prepare=lambda tc: copy.deepcopy(original))
    tool = SimpleNamespace(category='command', run=AsyncMock())
    call = SimpleNamespace(tool_name='Bash', arguments={'command': 'python probe.py'}, tool_id='new-tool')
    return agent, tool, call, original


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['succeeded', 'failed'])
async def test_cached_command_is_attributed_without_new_execution(state):
    agent, tool, call, original = cached_agent(state)
    before = copy.deepcopy(original)
    result = await agent._run_recorded_tool(tool, {}, call)
    tool.run.assert_not_awaited()
    assert original == before
    assert result.is_error == (state == 'failed')
    assert result.output.startswith('KERNEL_RECEIPT_REUSED:')
    assert result.output.endswith('old environment')
    host = result.host_observation
    assert host['reused'] is True
    assert host['command_execution']['command_started'] is False
    assert host['command_execution']['exit_code'] is None
    source = host['kernel_receipt_reuse']
    assert source['call_id'] == 'source-call'
    assert source['source_tool_id'] == 'original-tool'
    assert source['result_sha256'] == digest(original['result'])
    assert source['command_execution']['command_started'] is True
    assert source['command_execution']['exit_code'] == (0 if state == 'succeeded' else 1)
    assert 'stdout' not in source['command_execution']


@pytest.mark.asyncio
async def test_cached_success_cannot_satisfy_a_new_execution_request():
    agent, tool, call, original = cached_agent('succeeded')
    result = await agent._run_recorded_tool(tool, {}, call)
    requests = request_receipts('env', 'current-input', [{'command': 'python probe.py'}], status='outcome_unknown')
    rows = observe_requests(requests, [{'name': 'Bash', 'tool_id': 'new-tool',
        'args': call.arguments, 'output': result.output, 'is_error': result.is_error,
        'status': 'completed', 'host_observation': result.host_observation}])
    assert rows[0]['status'] == 'not_started'
    assert rows[0]['command_started'] is False
    assert rows[0]['exit_code'] is None
    assert rows[0]['successful'] is False
    tool.run.assert_not_awaited()


def test_reused_write_cannot_create_a_new_successful_write_receipt(tmp_path):
    from mewcode.orchestration.process_files import capture_process_file
    target = tmp_path / 'result.txt'
    target.write_text('changed independently since the original write')
    sandbox = SimpleNamespace(check=lambda *args, **kwargs: (True, ''))
    assert capture_process_file(tool_name='WriteFile', arguments={'file_path': str(target)},
        is_error=False, work_dir=str(tmp_path), sandbox=sandbox, node_id='env',
        tool_id='new-tool', member_id='', attempt=7,
        host_observation={'reused': True, 'kernel_receipt_reuse': {'call_id': 'source-call'}}) is None


def test_event_projection_keeps_reuse_source_without_old_stdout():
    from mewcode.experts.command_receipts import compact_execution_observation
    host = {'reused': True, 'kernel_receipt_reuse': {'call_id': 'source-call',
        'state': 'succeeded', 'source_tool_id': 'original-tool', 'result_sha256': 'a' * 64,
        'command_execution': {'status': 'exited', 'command_started': True, 'exit_code': 0}},
        'command_execution': {'status': 'not_started', 'command_started': False, 'exit_code': None,
                              'stdout': None, 'stderr': None}}
    value = compact_execution_observation(host)
    assert value['reused'] is True
    assert value['kernel_receipt_reuse'] == host['kernel_receipt_reuse']
    assert value['command_execution'] == {'status': 'not_started', 'command_started': False, 'exit_code': None}
    value['kernel_receipt_reuse']['state'] = 'changed'
    assert host['kernel_receipt_reuse']['state'] == 'succeeded'
    assert compact_execution_observation(None) is None
    assert compact_execution_observation({'command_execution': {
        'status': 'exited', 'command_started': True, 'exit_code': 0, 'stdout': 'not for compact event'}}) == {
            'command_execution': {'status': 'exited', 'command_started': True, 'exit_code': 0}}


@pytest.mark.asyncio
async def test_formal_execution_evidence_preserves_reuse_attribution():
    import json
    from mewcode.kernel.execution_evidence import execution_evidence
    from tests.test_execution_evidence import recorded_delivery
    agent, tool, call, original = cached_agent('succeeded')
    result = await agent._run_recorded_tool(tool, {}, call)
    record, attempt = recorded_delivery()
    record['node_states']['work']['tools']['receipt'].update(
        name='Bash', host_observation=result.host_observation)
    before = copy.deepcopy(record)
    evidence = execution_evidence(record, attempt)
    assert evidence['complete'] is True  # Complete attribution, not a business pass.
    receipt = evidence['tools'][0]
    assert receipt['execution_state'] == 'reused_receipt'
    assert receipt['command_started'] is False
    assert receipt['host_observation']['kernel_receipt_reuse']['source_tool_id'] == 'original-tool'
    assert receipt['host_observation']['kernel_receipt_reuse']['result_sha256'] == digest(original['result'])
    assert 'old environment' not in json.dumps(evidence)
    assert record == before
    tool.run.assert_not_awaited()
