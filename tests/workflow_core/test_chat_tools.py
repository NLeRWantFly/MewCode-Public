import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.permissions import PermissionMode
from mewcode.tools import ToolRegistry
from mewcode.workflow_core import goal_host
from mewcode.workflow_core.chat_tools import (
    BlockParams, ChatWorkflow, ControlParams, ReadParams, RunParams, SaveParams,
    DispatchParams as UpdateParams, install,
)
from mewcode.workflow_core.recipes import RecipeStore
from mewcode.workflow_core.store import ensure
from tests.workflow_core.test_exploration import MODEL, item


def host(tmp_path, monkeypatch):
    monkeypatch.setattr(goal_host, 'SESSIONS', {})
    monkeypatch.setattr(goal_host, 'TASKS', {})
    monkeypatch.setattr(goal_host, 'CHANGES', {})
    runtime = SimpleNamespace(work_dir=str(tmp_path), conversation_id='chat',
        session=SimpleNamespace(meta=SimpleNamespace(dialogue_focus=dict(
            kind='chat', task_id='current-task', task='行测章节'))),
        agent=SimpleNamespace(permission_checker=SimpleNamespace(task_id='current-task'),
            permission_mode=PermissionMode.ACCEPT_EDITS, registry=ToolRegistry()),
        slot=SimpleNamespace(key=MODEL['provider_key']), conversation=ConversationManager(),
        task=asyncio.get_running_loop().create_future(), cancel_requested=False,
        owner=SimpleNamespace(_continue_conversation_queue=Mock()))
    srv = SimpleNamespace(EXPERT_RUNS=ExpertRunStore(tmp_path/'runs', storage='sqlite'),
        MODEL_REGISTRY=SimpleNamespace(resolve=Mock()),
        SESSION=SimpleNamespace(runtime_for=lambda cid: runtime),
        WS_EVENTS=SimpleNamespace(send_json=AsyncMock()))
    return srv, runtime, ChatWorkflow(srv, runtime)


def update_params(*items, request_id='first', **kwargs):
    return UpdateParams(request_id=request_id, intent='execute', items=list(items), **kwargs)


@pytest.mark.asyncio
async def test_plain_chat_is_lazy_and_proposal_is_not_execution(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    install(srv, runtime)
    assert srv.EXPERT_RUNS.list_for_conversation('chat') == []
    assert runtime.agent.registry.get('UpdateWorkflowTasks') is not None
    for tool in runtime.agent.registry.list_tools():
        tool.get_schema()
        assert tool.consumes_runtime_slot is False
    proposal = await bridge.dispatch(UpdateParams(request_id='proposal', intent='propose', items=[item('a')]))
    assert proposal['execution_started'] is False
    assert len(proposal['workflow']['nodes']) == 1
    assert not goal_host.TASKS
    assert srv.EXPERT_RUNS.list_for_conversation('chat') == []
    runtime.agent.permission_mode = PermissionMode.PLAN
    with pytest.raises(ValueError, match='仅允许设计'):
        await bridge.dispatch(update_params(item('a')))


@pytest.mark.asyncio
async def test_chat_eight_way_fanout_incremental_successor_and_no_duplicate_owner(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    calls, active, peak = [], set(), 0
    started, release, successor = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def execute(ctx):
        nonlocal peak
        calls.append(ctx.node_id)
        active.add(ctx.node_id)
        peak = max(peak, len(active))
        if len(active) == 8:
            started.set()
        if ctx.node_id == 'fast':
            await started.wait()
        elif ctx.node_id == 'next':
            assert len(active) == 8
            successor.set()
        else:
            await release.wait()
        active.remove(ctx.node_id)
        return dict(text=ctx.node_id + '交付')
    monkeypatch.setattr(goal_host, 'AgentExecutor', Mock(return_value=execute))
    request = update_params(item('fast'), *(item('slow' + str(i)) for i in range(7)), concurrency=8)
    first = await bridge.dispatch(request)
    rid = first['run_id']
    record = srv.EXPERT_RUNS.load(rid)
    assert record['chat_origin'] and not record['parent_run_id']
    assert record['host_execution_scope']['task_id'] == 'current-task'
    assert await bridge.dispatch(request) == first
    await asyncio.wait_for(started.wait(), 10)
    await bridge.dispatch(update_params(item('next', ['fast']), request_id='more'))
    result = await asyncio.wait_for(bridge.wait(ReadParams(node_ids=['next'])), 10)
    assert result['tasks'][0]['status'] == 'completed'
    assert successor.is_set() and not release.is_set()
    release.set()
    result = await asyncio.wait_for(bridge.wait(ReadParams()), 10)
    assert result['status'] == 'completed' and len(calls) == 9 and peak == 8
    assert set(calls) == {item['id'] for item in record['workflow_core']['graph']['nodes']} | {'next'}
    await bridge.dispatch(update_params(item('after', ['next']), request_id='after'))
    result = await asyncio.wait_for(bridge.wait(ReadParams()), 10)
    assert result['status'] == 'completed' and calls.count('fast') == 1
    assert len(calls) == 10


@pytest.mark.asyncio
async def test_default_two_lanes_and_background_completion_releases_owner(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    runtime.task = None
    release, started = asyncio.Event(), asyncio.Event()
    active, calls, peak = set(), [], 0

    async def execute(ctx):
        nonlocal peak
        active.add(ctx.node_id)
        calls.append(ctx.node_id)
        peak = max(peak, len(active))
        if len(active) == 2:
            started.set()
        await release.wait()
        active.remove(ctx.node_id)
        return dict(text='交付')

    monkeypatch.setattr(goal_host, 'AgentExecutor', Mock(return_value=execute))
    result = await bridge.dispatch(update_params(*(item('part' + str(i)) for i in range(5))))
    await asyncio.wait_for(started.wait(), 10)
    assert len(calls) == 2
    assert bridge.backend().read()['graph']['concurrency'] == 2
    release.set()
    await asyncio.wait_for(goal_host.TASKS[result['run_id']], 60)
    await asyncio.sleep(0)
    assert peak == 2 and len(calls) == 5 and not runtime._goal_run_tasks
    runtime.owner._continue_conversation_queue.assert_called_once_with(runtime)


@pytest.mark.asyncio
async def test_live_concurrency_reduction_drains_without_cancelling(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    releases = {str(i): asyncio.Event() for i in range(6)}
    started, active, cancelled = [], set(), []

    async def execute(ctx):
        scheduler = goal_host.SESSIONS[bridge.identity()].goal_runtime_scheduler
        async with scheduler.slot(kind='model'):
            started.append(ctx.node_id)
            active.add(ctx.node_id)
            try:
                await releases[ctx.node_id].wait()
            except asyncio.CancelledError:
                cancelled.append(ctx.node_id)
                raise
            finally:
                active.remove(ctx.node_id)
        return dict(text='交付')

    async def wait_for(predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(.005)
        raise AssertionError('scheduler did not advance')

    monkeypatch.setattr(goal_host, 'AgentExecutor', Mock(return_value=execute))
    items = [item(str(i)) for i in range(6)]
    result = await bridge.dispatch(update_params(*items, concurrency=4))
    rid = result['run_id']
    try:
        await wait_for(lambda: len(started) == 4)
        await bridge.dispatch(update_params(*items, concurrency=2, request_id='lower'))
        await bridge.dispatch(update_params(*items, concurrency=4))  # Retry the old receipt.
        assert bridge.backend().read()['graph']['concurrency'] == 2
        assert goal_host.LIVE[rid].concurrency == 2
        assert goal_host.SESSIONS[rid].goal_runtime_scheduler.kind_limits['model'] == 2
        assert len(active) == 4 and not cancelled
        releases[started[0]].set()
        await wait_for(lambda: len(active) == 3)
        releases[started[1]].set()
        await wait_for(lambda: len(active) == 2)
        assert len(started) == 4
        releases[started[2]].set()
        await wait_for(lambda: len(started) == 5)
        assert len(active) <= 2 and not cancelled
    finally:
        for release in releases.values():
            release.set()
        await asyncio.wait_for(goal_host.TASKS[rid], 60)
    assert len(started) == 6 and not cancelled


@pytest.mark.asyncio
async def test_pause_preserves_completed_work_and_does_not_autorestart(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    calls, started = [], asyncio.Event()
    async def execute(ctx):
        calls.append(ctx.node_id)
        if ctx.node_id == 'slow' and calls.count('slow') == 1:
            started.set()
            await asyncio.Event().wait()
        return dict(text='交付')
    monkeypatch.setattr(goal_host, 'AgentExecutor', Mock(return_value=execute))
    result = await bridge.dispatch(update_params(item('fast'), item('slow', ['fast'])))
    await asyncio.wait_for(started.wait(), 10)
    await bridge.control(ControlParams(operation='pause', request_id='pause'))
    await asyncio.wait_for(goal_host.TASKS[result['run_id']], 10)
    assert (await bridge.wait(ReadParams()))['status'] == 'paused'
    with pytest.raises(ValueError, match='已暂停'):
        await bridge.dispatch(update_params(item('new'), request_id='new'))
    await bridge.control(ControlParams(operation='resume', request_id='resume'))
    final = await asyncio.wait_for(bridge.wait(ReadParams()), 10)
    assert final['status'] == 'completed' and calls == ['fast', 'slow', 'slow']


@pytest.mark.asyncio
async def test_save_reuse_versions_parameters_and_configuration_reach_executor(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    calls = []
    async def execute(ctx):
        calls.append(copy.deepcopy(ctx.node))
        return dict(text='交付')
    monkeypatch.setattr(goal_host, 'AgentExecutor', Mock(return_value=execute))
    await bridge.dispatch(update_params(item('part'), inputs=dict(source='source-chapter.json', output_dir='original-output')))
    await asyncio.wait_for(bridge.wait(ReadParams()), 10)
    first_state = bridge.backend().read()
    recipe = await bridge.save(SaveParams())
    again = await bridge.save(SaveParams())
    assert recipe['version'] == again['version'] == 1
    request = RunParams(request_id='reuse', recipe_id=recipe['id'], version=1,
        inputs=dict(source='different-source.json', output_dir='fresh-output'),
        overrides={'part': dict(model=dict(MODEL, provider_key='other'),
            prompt='保留原始公式', constraints=['只写独立输出目录'], service_tier='fast')})
    reused = await bridge.run_recipe(request)
    duplicate = await bridge.run_recipe(request)
    assert duplicate['run_id'] == reused['run_id'] and duplicate['reused_receipt']
    await asyncio.wait_for(bridge.wait(ReadParams(run_id=reused['run_id'])), 10)
    assert len(calls) == 2 and reused['recipe']['version'] == 2
    assert 'different-source.json' in calls[1]['task'] and 'fresh-output' in calls[1]['task']
    assert calls[1]['model']['provider_key'] == 'other'
    assert calls[1]['model']['effort'] == 'max'
    assert calls[1]['config']['prompt'] == '保留原始公式'
    assert calls[1]['config']['constraints'] == ['只写独立输出目录']
    assert calls[1]['config']['service_tier'] == 'fast'
    old = RecipeStore(tmp_path).read(recipe['id'], 1)
    assert old['graph']['nodes'][0]['config']['prompt'] == ''
    assert bridge.backend().read()['attempts'] == first_state['attempts']
    request.inputs['output_dir'] = 'changed-output'
    with pytest.raises(ValueError, match='相同请求编号'):
        await bridge.run_recipe(request)


@pytest.mark.asyncio
async def test_stale_task_and_external_workspace_cannot_dispatch(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='当前项目内'):
        await bridge.dispatch(update_params(item('a'), workspace=str(tmp_path.parent)))
    assert not goal_host.TASKS
    runtime.session.meta.dialogue_focus['task_id'] = 'another-task'
    with pytest.raises(ValueError, match='身份已变化'):
        await bridge.dispatch(update_params(item('a')))
    assert not goal_host.TASKS


@pytest.mark.asyncio
async def test_model_route_failure_is_atomic_and_visuals_survive_reload(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    async def execute(ctx):
        return dict(text='实际交付')
    monkeypatch.setattr(goal_host, 'AgentExecutor', Mock(return_value=execute))
    await bridge.dispatch(update_params(item('part')))
    await asyncio.wait_for(bridge.wait(ReadParams()), 10)
    before = bridge.backend().read()
    srv.MODEL_REGISTRY.resolve.side_effect = ValueError('模型不可用')
    with pytest.raises(ValueError, match='模型不可用'):
        await bridge.dispatch(update_params(item('new'), request_id='bad'))
    assert bridge.backend().read() == before
    bridge.publish(BlockParams(node_id='part', block_id='io', slot='output', kind='table', title='实际输出',
        columns=['材料', '结果'], rows=[['行测章节', '已整理']]))
    restored = ChatWorkflow(srv, runtime)
    result = restored.read(ReadParams())
    block = result['tasks'][0]['presentation_blocks']['output:io']
    assert block['rows'] == [['行测章节', '已整理']] and block['author'] == 'chat'


@pytest.mark.asyncio
async def test_actual_agent_receives_custom_prompt_constraints_and_fast(tmp_path, monkeypatch):
    from mewcode import expert_web
    from tests.workflow_core.test_agent_adapter import offline_builder
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    builder = offline_builder(tmp_path, [])
    observed = []
    def build(*args):
        agent, extra, role = builder(*args)
        agent.client.service_tier = 'default'
        original = agent.run
        async def run(conversation):
            observed.append((args[3].as_dict(), agent.client.service_tier, str(conversation.history)))
            async for event in original(conversation):
                yield event
        agent.run = run
        return agent, extra, role
    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    await bridge.dispatch(update_params(dict(item('part'), prompt='保留原始公式',
        constraints=['仅检查指定材料'], service_tier='fast')))
    result = await asyncio.wait_for(bridge.wait(ReadParams()), 60)
    assert result['status'] == 'completed', result
    assert observed[0][0] == MODEL and observed[0][1] == 'fast'
    assert '保留原始公式' in observed[0][2] and '仅检查指定材料' in observed[0][2]
    assert (tmp_path / 'part.txt').read_text(encoding='utf-8') == 'OCR OUTPUT'
