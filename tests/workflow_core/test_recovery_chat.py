import asyncio
import copy

import httpx
import pytest

from frontend import server
from frontend import workflow_recovery
from mewcode.orchestration.chat_routing import set_dialogue_focus
from mewcode.expert_runs import ExpertRunStore
from mewcode.permissions import PermissionMode
from mewcode.workflow_core import web
from mewcode.workflow_core.runtime import Core, LIVE
from mewcode.workflow_core.state import command, start_attempt, finish_attempt
from mewcode.workflow_core.store import ensure
from mewcode.workflow_core.web import envelope
from tests.test_chat_task_routing import ingress  # noqa: F401 -- reuse actual WS ingress fixture
from tests.workflow_core.test_runtime import graph

REQUEST = '检查为什么卡住并继续'


def setup_run(env, monkeypatch, *, rid='d' * 32):
    env.owner.work_dir = env.runtime.work_dir
    env.owner.permission_mode = PermissionMode.AUTO
    env.runs = ExpertRunStore(env.runs.root / 'sqlite', storage='sqlite')
    monkeypatch.setattr(server, 'EXPERT_RUNS', env.runs)
    flow = graph(['source', 'work', 'finish'], [('source', 'work', 'always'), ('work', 'finish', 'always')])
    env.runs.create(run_id=rid, conversation_id=env.runtime.conversation_id, task='处理资料', attachments=[],
                    profile={}, strategy='stable', flow=envelope(flow))
    backend = ensure(env.runs, rid, flow)
    def prepare(state):
        source = start_attempt(state, 'source', {}, [])
        finish_attempt(state, source['id'], dict(status='completed', text='原始交付'))
        work = start_attempt(state, 'work', {}, [])
        work['messages'] = [dict(role='assistant', content='已有工作已落盘')]
        work['pending_tools'] = {'export': dict(name='Bash', status='unknown')}
        finish_attempt(state, work['id'], dict(status='blocked', reason='工具返回丢失', text=''))
        state['status'] = 'blocked'
    backend.change(prepare)
    set_dialogue_focus(env.runtime, 'orchestration', task='处理资料', run_id=rid)
    calls = []
    def executor(session, registry, store, run_id, profile, emit, content_blocks):
        async def execute(ctx):
            calls.append(ctx.node_id)
            saved = store.load(run_id)['host_execution_scope']
            assert saved['conversation_id'] == env.runtime.conversation_id and saved['task_id']
            if ctx.node_id == 'work':
                assert ctx.checkpoint['pending_tools']['export']['status'] == 'unknown'
                assert ctx.checkpoint['messages'][0]['content'] == '已有工作已落盘'
                assert ctx.recovery['request'] == REQUEST
            return dict(status='completed', text='接续完成')
        return execute
    monkeypatch.setattr(web, 'AgentExecutor', executor)
    return rid, backend, calls


@pytest.mark.asyncio
async def test_real_websocket_diagnoses_repairs_and_resumes_without_confirmation(ingress, monkeypatch):
    rid, backend, calls = setup_run(ingress, monkeypatch)
    before = backend.read()
    socket = ingress.Socket([dict(text=REQUEST, request_id='chat-repair')])
    await server.ws_endpoint(socket)
    final = backend.read()
    assert final['status'] == 'completed' and calls == ['work', 'finish']
    assert final['graph'] == before['graph']
    # Missing human-readable reports may be added for old attempts; their
    # original transcript, result and tool receipts must not change.
    for key, row in before['attempts'].items():
        assert {field: final['attempts'][key][field] for field in row} == row
        assert set(final['attempts'][key]) - set(row) <= {'report'}
    assert all(final['deliveries'][key] == row for key, row in before['deliveries'].items())
    assert not ingress.prompts  # No ordinary chat or planning detour.
    assert not any(item['type'] in {'permission', 'ask_user', 'human_oncall', 'error'} for item in socket.messages)
    assert any(item['type'] == 'workflow_recovery' and item['run_id'] == rid for item in socket.messages)
    assert any(item['type'] == 'expert_run_state' and item['status'] == 'running' for item in socket.messages)
    assert socket.messages[-1]['type'] == 'done'
    assert any(message.role == 'user' and message.content == REQUEST for message in ingress.runtime.conversation.history)


@pytest.mark.asyncio
async def test_repeat_message_id_does_not_redeliver_or_renew_loop_limits(ingress, monkeypatch):
    _, backend, calls = setup_run(ingress, monkeypatch)
    frame = dict(text=REQUEST, request_id='repeat')
    socket = ingress.Socket([frame, frame])
    await server.ws_endpoint(socket)
    assert calls == ['work', 'finish']
    assert len(backend.read()['recovery_requests']) == 1
    assert len([m for m in ingress.runtime.conversation.history if m.role == 'user' and m.content == REQUEST]) == 1
    assert len([m for m in socket.messages if m['type'] == 'workflow_recovery']) == 1


@pytest.mark.asyncio
async def test_inspect_only_preserves_run_and_question_does_not_route(ingress, monkeypatch):
    _, backend, calls = setup_run(ingress, monkeypatch)
    before = backend.read()
    socket = ingress.Socket([dict(text='检查为什么卡住，先不要继续')])
    await server.ws_endpoint(socket)
    assert backend.read() == before and not calls
    assert any(item['type'] == 'workflow_recovery' and item['status'] == 'inspected' for item in socket.messages)
    assert socket.messages[-1]['type'] == 'done'


@pytest.mark.asyncio
async def test_workbench_selection_overrides_old_focus_but_rejects_foreign_conversation(ingress, monkeypatch):
    rid, backend, calls = setup_run(ingress, monkeypatch)
    set_dialogue_focus(ingress.runtime, 'orchestration', task='别的运行', run_id='a' * 32)
    context = dict(project=ingress.runtime.work_dir, conversation_id=ingress.runtime.conversation_id, run_id=rid)
    socket = ingress.Socket([dict(text=REQUEST, request_id='selected', workbench_context=context)])
    await server.ws_endpoint(socket)
    assert backend.read()['status'] == 'completed' and calls == ['work', 'finish']
    before = backend.read()
    wrong = dict(context, conversation_id='foreign')
    socket = ingress.Socket([dict(text=REQUEST, request_id='foreign', workbench_context=wrong)])
    await server.ws_endpoint(socket)
    assert backend.read() == before
    assert socket.messages[-1]['type'] == 'done' and socket.messages[-1]['rejected']


@pytest.mark.asyncio
async def test_running_intervention_reports_live_work_without_takeover_or_restart(ingress, monkeypatch):
    rid, backend, calls = setup_run(ingress, monkeypatch)
    backend.change(lambda state: command(state, dict(operation='resume', request_id='existing-start')))
    entered, finish = asyncio.Event(), asyncio.Event()
    async def execute(ctx):
        entered.set()
        await finish.wait()
        return dict(text='done')
    core = Core(backend, execute, run_id=rid)
    task = asyncio.create_task(core.run())
    ingress.runtime.task = task
    ingress.runtime.execution_kind = 'orchestration'
    try:
        await asyncio.wait_for(entered.wait(), 30)
        before = backend.read()['nodes']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test',
                                    headers={'X-Mew-Chat-Queue': '1'}) as client:
            response = await client.post('/api/orchestration/interventions', json=dict(
                conversation_id=ingress.runtime.conversation_id, queue_id='live-check', text=REQUEST,
                workbench_context=dict(conversation_id=ingress.runtime.conversation_id, run_id=rid)))
        assert response.status_code == 200, response.text
        assert response.json()['handled'] and response.json()['new']
        assert backend.read()['nodes'] == before
        assert LIVE[rid] is core and not task.done() and not calls
        assert not ingress.runtime.expert_takeover_controller
    finally:
        finish.set()
        await task


@pytest.mark.asyncio
async def test_pause_during_host_repair_wins_and_keeps_checkpoint(ingress, monkeypatch):
    rid, backend, calls = setup_run(ingress, monkeypatch)
    async def repairing(runtime, report):
        backend.change(lambda state: command(state, dict(operation='pause', request_id='user-pause')))
        return dict(action='already_ready')
    monkeypatch.setattr(workflow_recovery, 'repair_environment', repairing)
    original = copy.deepcopy(backend.read()['attempts'])
    socket = ingress.Socket([dict(text=REQUEST, request_id='repair')])
    await server.ws_endpoint(socket)
    assert not calls
    assert backend.read()['status'] == 'paused'
    assert backend.read()['attempts'] == original
    assert any('已暂停或停止' in item.get('text', '') for item in socket.messages)


@pytest.mark.asyncio
async def test_plan_mode_does_not_mutate_run(ingress, monkeypatch):
    _, backend, calls = setup_run(ingress, monkeypatch)
    ingress.owner.permission_mode = PermissionMode.PLAN
    before = backend.read()
    socket = ingress.Socket([dict(text=REQUEST, request_id='repair')])
    await server.ws_endpoint(socket)
    assert not calls and backend.read() == before
    assert socket.messages[-1]['rejected']
