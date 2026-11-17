"""A run-bound direction reaches the worker without cancelling its active call."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from mewcode.conversation import ConversationManager
from mewcode.orchestration.interventions import deliver_interventions
from tests.test_orchestration_takeover import setup


@pytest.mark.asyncio
async def test_supplement_keeps_active_call_and_is_delivered_once(monkeypatch):
    from frontend import server
    controller, runtime, store, events = setup()
    controller.active = asyncio.create_task(asyncio.Event().wait())
    messages = []
    runtime.session = SimpleNamespace(append=messages.append)
    runtime.conversation = ConversationManager()
    before = copy.deepcopy(store.value['node_states'])
    try:
        result = await server._accept_expert_supplement(runtime, 'pivot-1', '换方法，小验证后继续', 'run-a')
        assert result['new'] and result['receipt']['kind'] == 'supplement'
        assert not (await server._accept_expert_supplement(runtime, 'pivot-1', '换方法，小验证后继续', 'run-a'))['new']
        assert len(messages) == 1 and not runtime.conversation.history
        assert not controller.active.done() and not controller.active.cancelling()
        assert controller.phase == 'flow' and controller.pending == []
        assert runtime.expert_control_action == 'pause'
        assert store.value['node_states'] == before
        worker = ConversationManager()
        assert deliver_interventions(store.value, worker, node_id='work', node_attempt=9) == ['pivot-1']
        assert deliver_interventions(store.value, worker, node_id='work', node_attempt=9) == []
        assert store.value['intervention_receipts'][0]['delivered_to'] == [
            {'node_id': 'work', 'node_attempt': 9, 'member_id': ''}]
    finally:
        controller.active.cancel()
        await asyncio.gather(controller.active, return_exceptions=True)


@pytest.mark.parametrize('change', ['foreign_run', 'foreign_chat', 'paused', 'closed', 'takeover'])
def test_supplement_rejects_wrong_or_inactive_target(change):
    controller, runtime, store, events = setup()
    expected = 'run-a'
    if change == 'foreign_run':
        expected = 'run-b'
    elif change == 'foreign_chat':
        store.value['conversation_id'] = 'chat-b'
    elif change == 'paused':
        store.value['status'] = 'paused'
    elif change == 'closed':
        controller.accepting = False
    else:
        controller.phase = 'pausing'
    with pytest.raises((ValueError, RuntimeError)):
        controller.supplement('pivot-1', '换方法', expected_run_id=expected)
    assert not store.value.get('interventions')


@pytest.mark.asyncio
async def test_api_supplement_requires_explicit_header_run_and_orchestration(monkeypatch):
    from frontend import server
    controller, runtime, store, events = setup()
    runtime.execution_kind = 'orchestration'
    runtime.session = SimpleNamespace(append=lambda message: None)
    monkeypatch.setattr(server.SESSION, '_active_queue_runtime', lambda identity: runtime)
    body = server.OrchestrationInterventionBody(conversation_id='chat-a', queue_id='pivot-1',
        text='新增适配节点继续', mode='supplement', expected_run_id='run-a')
    request = SimpleNamespace(headers={})
    assert (await server.api_orchestration_intervention(request, body)).status_code == 403
    request.headers = {'x-mew-chat-queue': '1'}
    body.expected_run_id = 'run-b'
    assert (await server.api_orchestration_intervention(request, body)).status_code == 409
    body.expected_run_id = 'run-a'
    runtime.execution_kind = 'chat'
    assert (await server.api_orchestration_intervention(request, body)).status_code == 409
    runtime.execution_kind = 'orchestration'
    body.attachments = ['file']
    assert (await server.api_orchestration_intervention(request, body)).status_code == 409
    body.attachments = []
    response = await server.api_orchestration_intervention(request, body)
    assert response.status_code == 200
    assert json.loads(response.body)['receipt']['kind'] == 'supplement'
    assert server.OrchestrationInterventionBody(conversation_id='chat-a', queue_id='other').mode == 'takeover'
