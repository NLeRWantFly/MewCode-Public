from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from mewcode.agent import Agent
from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.permissions import PermissionMode
from mewcode.permissions.policy import build_permission_checker
from mewcode.tools import ToolRegistry
from mewcode.tools.base import TextDelta, ToolCallComplete, StreamEnd
from mewcode.tools.write_file import WriteFile
from mewcode.workflow_core.runtime import Core
from mewcode.workflow_core.store import ensure
from mewcode.workflow_core.web import AgentExecutor, envelope, run_chat
from tests.test_agent import MockLLMClient
from tests.workflow_core.test_runtime import graph


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['first_event', 'reasoning', 'output'])
async def test_workflow_keeps_slow_model_request_instead_of_legacy_silence_retry(tmp_path, monkeypatch, phase):
    from mewcode import client, expert_web, llm_watchdog
    from mewcode.tools.base import ThinkingDelta

    original_watch = llm_watchdog.watch_provider_stream
    monkeypatch.setattr(llm_watchdog, 'watch_provider_stream', lambda stream: original_watch(
        stream, first_timeout=.01, reasoning_timeout=.01, idle_timeout=.01))
    monkeypatch.setattr(client, 'LLM_REQUEST_TIMEOUT', .02)
    monkeypatch.setattr('mewcode.agent._stream_retry_delay', lambda *_: 0)
    builder = offline_builder(tmp_path, [])
    calls = []

    def build(*args):
        agent, extra, runtime = builder(*args)
        agent.provider_watchdog = True  # The legacy role builder enables this.
        original = agent.client.stream
        async def delayed(*values, **options):
            calls.append(phase)
            if len(calls) >= 2:
                if phase == 'reasoning':
                    yield ThinkingDelta('正在核对')
                elif phase == 'output':
                    yield TextDelta('已核对，')
                await asyncio.sleep(.06)
            async for event in original(*values, **options):
                yield event
        agent.client.stream = delayed
        return agent, extra, runtime

    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    rid = 'a' * 32
    flow = graph(['work'])
    store.create(run_id=rid, conversation_id='chat', task='OCR', attachments=[], profile={},
                 strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)), None, store, rid,
        {'executor': dict(provider_key='offline', model_id='offline', effort='max')}, AsyncMock())
    result = await Core(backend, executor).run()
    assert result['status'] == 'completed'
    assert len(calls) == 2  # Initial write, then the original slow response; no resubmission.
    attempt = result['attempts'][result['nodes']['work']['attempt_id']]
    assert sum(row.get('name') == 'WriteFile' for row in attempt['diagnostics']) == 1
    assert (tmp_path / 'work.txt').read_text(encoding='utf-8') == 'OCR OUTPUT'


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['error', 'exception'])
async def test_ended_model_without_delivery_blocks_only_its_branch(tmp_path, monkeypatch, ending):
    from mewcode import expert_web
    from mewcode.agent import ErrorEvent, StreamText, TurnComplete, LoopComplete

    invoked = []
    builder = offline_builder(tmp_path, invoked)
    def build(*args):
        agent, extra, runtime = builder(*args)
        if args[-1].execution_node_id == 'work':
            async def interrupted(conversation):
                conversation.add_assistant_message('已保存第185页，剩余部分尚未交付。')
                yield StreamText('已保存第185页，剩余部分尚未交付。')
                if ending == 'error':
                    yield ErrorEvent(message='API error (400): invalid model', kind='provider_error')
                elif ending == 'exception':
                    raise RuntimeError('API error (400): invalid model')
                elif ending == 'empty_completion':
                    yield TurnComplete(turn=1)
                    yield LoopComplete(total_turns=2)
            agent.run = interrupted
        return agent, extra, runtime
    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    flow = graph(['work', 'dependent', 'independent'], [('work', 'dependent', 'always')])
    rid = 'b' * 32
    store.create(run_id=rid, conversation_id='chat', task='OCR', attachments=[], profile={},
                 strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)), None, store, rid,
        {'executor': dict(provider_key='offline', model_id='offline', effort='max')}, AsyncMock())
    result = await Core(backend, executor).run()
    assert result['status'] == 'blocked'
    assert result['nodes']['work']['status'] == 'blocked'
    assert '尚未' in result['nodes']['work']['reason']
    assert result['nodes']['dependent']['status'] == 'pending'
    assert result['nodes']['independent']['status'] == 'completed'
    assert invoked == [('executor', 'work'), ('executor', 'independent')]
    attempt = result['attempts'][result['nodes']['work']['attempt_id']]
    assert '第185页' in str(attempt['messages'])
    assert not attempt.get('delivery_id')


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['error', 'exception', 'missing_completion', 'empty_completion'])
async def test_disconnected_agent_continues_saved_conversation_and_delivers(tmp_path, monkeypatch, ending):
    from mewcode import expert_web
    from mewcode.agent import ErrorEvent, StreamText, TurnComplete, LoopComplete

    invoked = []
    builder = offline_builder(tmp_path, invoked)
    def build(*args):
        agent, extra, runtime = builder(*args)
        if len(invoked) == 1:
            async def interrupted(conversation):
                conversation.add_assistant_message('保存原进度，剩余待完成。')
                yield StreamText('保存原进度，剩余待完成。')
                if ending == 'exception':
                    raise RuntimeError('Provider disconnected')
                if ending == 'error':
                    yield ErrorEvent(message='Provider disconnected', kind='provider_error')
                elif ending == 'empty_completion':
                    yield TurnComplete(turn=1)
                    yield LoopComplete(total_turns=2)
            agent.run = interrupted
        else:
            original = agent.run
            async def resumed(conversation):
                assert any('保存原进度' in (m.content or '') for m in conversation.history)
                async for item in original(conversation):
                    yield item
            agent.run = resumed
        return agent, extra, runtime

    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    flow, rid = graph(['work']), 'f' * 32
    store.create(run_id=rid, conversation_id='chat', task='OCR', attachments=[], profile={},
                 strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)), None, store, rid,
        {'executor': dict(provider_key='offline', model_id='offline', effort='max')}, AsyncMock())
    result = await Core(backend, executor, retry_delay=0).run()
    assert result['status'] == 'completed'
    assert invoked == [('executor', 'work'), ('executor', 'work')]
    failed = next(a for a in result['attempts'].values() if a['status'] == 'blocked')
    assert 'Provider disconnected' in failed['result']['reason'] or '模型对话已结束' in failed['result']['reason']
    assert any('保存原进度' in str(m) for m in failed['messages'])
    assert (tmp_path / 'work.txt').read_text(encoding='utf-8') == 'OCR OUTPUT'


def offline_builder(tmp_path, invoked):
    def build(session, registry, role, model, policy):
        invoked.append((role, policy.execution_node_id))
        path = tmp_path / (policy.execution_node_id + '.txt')
        client = MockLLMClient([
            [TextDelta('材料已读，'), TextDelta('开始生成结果。'),
             ToolCallComplete('write', 'WriteFile', {'file_path': str(path), 'content': 'OCR OUTPUT'}),
             StreamEnd(stop_reason='tool_use', input_tokens=1, output_tokens=1)],
            [TextDelta('产物已经保存。'), StreamEnd(stop_reason='end_turn', input_tokens=1, output_tokens=1)]])
        tools = ToolRegistry()
        tools.register(WriteFile())
        agent = Agent(client=client, registry=tools, protocol='anthropic', permission_checker=build_permission_checker(
            str(tmp_path), mode=PermissionMode.ACCEPT_EDITS), work_dir=str(tmp_path))
        from mewcode.permissions.reviewer import SafetyReviewResult
        agent.safety_reviewer.review = AsyncMock(return_value=SafetyReviewResult(
            decision='allow', risk='low', reason='Offline fixture permits writes inside the test directory'))
        runtime = SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())
        return agent, None, runtime
    return build


def host_session(tmp_path):
    durable = SimpleNamespace(session_id='chat', meta=SimpleNamespace(dialogue_focus={}),
                              save_meta=lambda: None, append=lambda _: None)
    return SimpleNamespace(work_dir=str(tmp_path), conversation_id='chat', conversation=ConversationManager(),
                           session=durable, status_payload=lambda: {}, tasks_payload=lambda: {})


@pytest.mark.asyncio
async def test_real_agent_final_reply_delivers_without_handoff_and_keeps_immutable_files(tmp_path, monkeypatch):
    from mewcode import expert_web
    invoked = []
    monkeypatch.setattr(expert_web, '_build_role_agent', offline_builder(tmp_path, invoked))
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    flow = graph(['ocr', 'store'], [('ocr', 'store', 'always')])
    rid = 'd' * 32
    store.create(run_id=rid, conversation_id='chat', task='OCR', attachments=[], profile={},
                 strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)), None, store, rid,
        {'executor': dict(provider_key='offline', model_id='offline', effort='max')}, AsyncMock())
    result = await Core(backend, executor).run()
    assert result['status'] == 'completed', result['nodes']
    assert invoked == [('executor', 'ocr'), ('executor', 'store')]
    attempt = result['attempts'][result['nodes']['ocr']['attempt_id']]
    assert [p['text'] for p in attempt['progress']] == ['材料已读，开始生成结果。', '产物已经保存。']
    assert attempt['diagnostics'][0]['name'] == 'WriteFile'
    artifact = attempt['artifacts'][0]
    Path(artifact['path']).write_text('changed later', encoding='utf-8')
    assert Path(artifact['stored_path']).read_text(encoding='utf-8') == 'OCR OUTPUT'


@pytest.mark.asyncio
async def test_agent_activity_tracks_stream_tools_retries_and_delivery(tmp_path, monkeypatch):
    from mewcode import expert_web
    from mewcode.agent import (ThinkingText, StreamText, StreamRetryEvent, ToolUseEvent,
                              ToolExecutionStartEvent, ToolResultEvent, TurnComplete, LoopComplete)
    builder = offline_builder(tmp_path, [])
    def build(*args):
        agent, extra, runtime = builder(*args)
        async def stream(_conversation):
            yield ThinkingText('PRIVATE REASONING')
            yield ThinkingText('MORE PRIVATE REASONING')
            yield StreamText('正在核对材料。')
            yield ToolUseEvent('ReadFile', 'a', {'private_argument': 'hidden'})
            yield ToolUseEvent('Bash', 'b', {})
            yield ToolExecutionStartEvent('ReadFile', 'a')
            yield ToolExecutionStartEvent('Bash', 'b')
            yield ToolResultEvent('a', 'ReadFile', 'PRIVATE OUTPUT', True, .1)
            yield ToolResultEvent('b', 'Bash', 'outcome unknown', False, .1,
                                  host_observation={'command_execution': {'status': 'outcome_unknown'}})
            yield TurnComplete(1)
            yield StreamRetryEvent('provider unavailable', 1, 4, wait=2)
            yield StreamText('核对完成，疑点已保留。')
            yield LoopComplete(2)
        agent.run = stream
        return agent, extra, runtime
    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    rid, flow, events = 'c' * 32, graph(['work']), []
    store.create(run_id=rid, conversation_id='chat', task='activity', attachments=[], profile={},
                 strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)), None, store, rid,
        {'executor': dict(provider_key='offline', model_id='offline', effort='max')}, AsyncMock())
    result = await Core(backend, executor, emit=events.append).run()
    activities = [e['execution'] for e in events if e['type'] == 'workflow_activity']
    phases = [e['phase'] for e in activities]
    assert phases.count('reasoning') == 1
    assert {'starting', 'waiting_model', 'reasoning', 'output', 'tool_queued', 'tool_running',
            'tool_unknown', 'retrying', 'finishing'} <= set(phases)
    assert any(e['tools'] == ['ReadFile', 'Bash'] for e in activities)
    failed = next(e for e in activities if e['outcome'] == 'failed')
    assert failed['phase'] == 'tool_running' and failed['tools'] == ['Bash']
    assert 'PRIVATE' not in str(activities) and 'private_argument' not in str(activities)
    assert result['nodes']['work']['status'] == 'completed'


@pytest.mark.asyncio
async def test_agent_recovers_unknown_tool_inside_node_and_delivers(tmp_path, monkeypatch):
    from mewcode import expert_web
    from mewcode.workflow_core.state import start_attempt
    invoked, conversations = [], []
    builder = offline_builder(tmp_path, invoked)
    def capture(*args):
        built = builder(*args)
        conversations.append(built[2].conversation)
        return built
    monkeypatch.setattr(expert_web, '_build_role_agent', capture)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    flow = graph(['deploy', 'ocr'], [('deploy', 'ocr', 'always')])
    rid = 'e' * 32
    store.create(run_id=rid, conversation_id='chat', task='OCR', attachments=[], profile={},
                 strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    def interrupted(state):
        attempt = start_attempt(state, 'deploy', {}, [])
        attempt['messages'] = [{'role': 'user', 'content': 'Earlier deployment task'}]
        attempt['pending_tools'] = {'clone': {'name': 'RemoteSSH', 'status': 'running',
                                            'arguments': {'command': 'git clone example'}}}
    backend.change(interrupted)
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)), None, store, rid,
        {'executor': dict(provider_key='offline', model_id='offline', effort='max')}, AsyncMock())
    result = await Core(backend, executor).run()
    assert result['status'] == 'completed'
    assert invoked == [('executor', 'deploy'), ('executor', 'ocr')]
    attempt = result['attempts'][result['nodes']['deploy']['attempt_id']]
    assert attempt['pending_tools']['clone']['status'] == 'unknown'
    from mewcode.expert_runs import serialize_messages
    messages = serialize_messages(conversations[0].history)
    assert messages[0]['content'] == 'Earlier deployment task'
    assert '在当前节点内检查进程、文件或服务状态' in messages[1]['content']
    assert '不要直接重放' in messages[1]['content']


@pytest.mark.asyncio
async def test_chat_resume_keeps_single_node_rerun_scope(tmp_path, monkeypatch):
    from mewcode import expert_web
    from mewcode.workflow_core.state import command
    invoked = []
    monkeypatch.setattr(expert_web, '_build_role_agent', offline_builder(tmp_path, invoked))
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    profile = {'executor': dict(provider_key='offline', model_id='offline', effort='max')}
    session = host_session(tmp_path)
    ws = SimpleNamespace(send_json=AsyncMock())
    flow = graph(['ocr', 'store'], [('ocr', 'store', 'always')])
    await run_chat(ws, 'OCR', session=session, registry_service=SimpleNamespace(resolve=lambda _: None),
        profile_store=SimpleNamespace(get=lambda _: profile), run_store=store, confirmed_new_flow=envelope(flow))
    rid = session.last_expert_run_id
    backend = ensure(store, rid)
    assert backend.read()['status'] == 'completed'
    backend.change(lambda s: command(s, dict(operation='rerun', node_id='ocr', request_id='rerun')))
    await run_chat(ws, 'OCR', session=session, registry_service=None, profile_store=None, run_store=store, resume_run_id=rid)
    assert invoked == [('executor', 'ocr'), ('executor', 'store'), ('executor', 'ocr')]
    assert backend.read()['nodes']['store']['status'] == 'paused'
    assert all(a.get('report') for a in backend.read()['attempts'].values())


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['classic', 'collaborative'])
async def test_task_design_start_keeps_its_claim_identity(tmp_path, monkeypatch, mode):
    from mewcode.orchestration.task_design import TaskDesign
    from mewcode.workflow_core import web
    service = TaskDesign(tmp_path, 'chat')
    created = service.start('execute', orchestration_mode=mode)
    ready = service.transition(created, 'ready', build_request={'id': 'build', 'state': 'saved', 'epoch': 1})
    claimed = service.claim_execution(ready)
    expected = service.execution_run_id(claimed)
    async def execute(_self, ctx):
        return dict(text='done')
    monkeypatch.setattr(web.AgentExecutor, '__call__', execute)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    profile = {'executor': dict(provider_key='offline', model_id='offline', effort='max')}
    session = host_session(tmp_path)
    await run_chat(SimpleNamespace(send_json=AsyncMock()), 'OCR', session=session,
        registry_service=SimpleNamespace(resolve=lambda _: None), profile_store=SimpleNamespace(get=lambda _: profile),
        run_store=store, confirmed_new_flow=envelope(graph(['work'])), task_design_dispatch=service.execution_token(claimed))
    assert service.read()['run_id'] == expected == session.last_expert_run_id
    assert store.load(expected)['workflow_core']['status'] == 'completed'


@pytest.mark.asyncio
async def test_native_start_and_resume_bind_identity_before_revision_commands(tmp_path, monkeypatch):
    from mewcode.expert_web import _expert_permission_checker
    from mewcode.orchestration.chat_routing import set_dialogue_focus, bind_task_permission_scope, dialogue_focus
    from mewcode.sandbox.lpac_runtime import NativeScope
    from mewcode.workflow_core import web
    from mewcode.workflow_core.state import command
    from tests.test_expert_task_identity import runtime_for
    session, checker = runtime_for(tmp_path)
    session.work_dir = str(tmp_path)
    session.status_payload = session.tasks_payload = lambda: {}
    revision = tmp_path / 'revision'
    revision.mkdir()
    identities = []
    async def execute(_self, ctx):
        child = _expert_permission_checker(session, str(revision))
        identities.append(NativeScope.from_checker(child).task_id)
        assert child.task_authorization is None
        assert child.policy_writable_roots == (revision.resolve(),)
        return dict(text='done')
    monkeypatch.setattr(web.AgentExecutor, '__call__', execute)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    profile = {'executor': dict(provider_key='offline', model_id='offline', effort='max')}
    await run_chat(SimpleNamespace(send_json=AsyncMock()), 'Original task', session=session,
        registry_service=SimpleNamespace(resolve=lambda _: None), profile_store=SimpleNamespace(get=lambda _: profile),
        run_store=store, confirmed_new_flow=envelope(graph(['work'])))
    rid = session.last_expert_run_id
    assert store.load(rid)['host_execution_scope']['task_id'] == identities[0]
    set_dialogue_focus(session, 'chat', task='Different chat')
    bind_task_permission_scope(session, task_id=dialogue_focus(session)['task_id'])
    assert checker.task_id != identities[0]
    ensure(store, rid).change(lambda s: command(s, dict(operation='rerun', node_id='work', request_id='again')))
    await run_chat(SimpleNamespace(send_json=AsyncMock()), 'Continue', session=session,
        registry_service=None, profile_store=None, run_store=store, resume_run_id=rid)
    assert identities == [identities[0], identities[0]]
    assert store.load(rid)['status'] == 'completed'
