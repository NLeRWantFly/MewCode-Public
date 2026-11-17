import copy
from types import SimpleNamespace

import pytest

from mewcode.workflow_core.activity import observe_node_detail
from mewcode.workflow_core.projection import node_detail
from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.state import fresh, start_attempt
from tests.workflow_core.test_runtime import graph


def test_conversation_details_expose_current_tools_and_wait_target_without_private_context():
    state = fresh(graph(['source', 'consumer']))
    attempt = start_attempt(state, 'consumer', {}, [])
    attempt.update(status='suspended', waiting_on='question',
        messages=[{'role': 'system', 'content': 'private system prompt'},
                  {'role': 'assistant', 'reasoning_content': 'private reasoning'}],
        pending_tools={
            'old': dict(name='Bash', arguments={'command': 'previous execution'}, status='completed'),
            'read': dict(name='ReadFile', arguments={'file_path': 'input.txt'}, status='completed'),
            'current': dict(name='WaitForNodeReply', arguments={'request_id': 'question'}, status='queued',
                            queued_at=attempt['started_at'] + 1)},
        diagnostics=[dict(kind='tool_result', tool_id='read', name='ReadFile', output='public result')])
    state['node_requests'] = {'question': dict(id='question', node_id='source', sender_node_id='consumer',
        kind='revise', status='running', message='补充资料')}
    record = dict(workflow_core=state)
    original = copy.deepcopy(record)
    detail = node_detail(record, 'consumer')
    shown = detail['attempts'][0]
    assert set(shown['tools']) == {'read', 'current'}
    assert shown['tools']['read']['arguments'] == {'file_path': 'input.txt'}
    assert shown['waiting_request']['label'] == 'source'
    assert shown['waiting_request']['message'] == '补充资料'
    assert 'private' not in str(detail)
    assert record == original


@pytest.mark.asyncio
async def test_thinking_phases_are_coalesced_and_saved_at_checkpoints_with_live_attempt_binding():
    state = fresh(graph(['work']))
    attempt = start_attempt(state, 'work', {}, [])
    backend = MemoryBackend(state)
    core = Core(backend, None)
    core.observe(attempt['id'], 'work')
    core.tasks[attempt['id']] = SimpleNamespace(done=lambda: False)
    for _ in range(20):
        await core.publish(attempt['id'], 'activity', activity='reasoning')
    assert backend.read() == state
    await core.publish(attempt['id'], 'activity', activity='tool_running', tool='ReadFile', tool_id='read')
    await core.publish(attempt['id'], 'checkpoint', messages=[], pending_tools={})
    record = dict(workflow_core=backend.read())
    shown = node_detail(record, 'work')['attempts'][0]
    assert [row['phase'] for row in shown['phase_events']] == ['reasoning', 'tool_running']
    await core.publish(attempt['id'], 'checkpoint', messages=[], pending_tools={})
    assert backend.read()['attempts'][attempt['id']]['phase_events'] == shown['phase_events']
    await core.publish(attempt['id'], 'activity', activity='waiting_model')
    detail = observe_node_detail(node_detail(record, 'work'), live=core.activity_snapshot())
    assert detail['attempts'][0]['execution']['phase'] == 'waiting_model'
