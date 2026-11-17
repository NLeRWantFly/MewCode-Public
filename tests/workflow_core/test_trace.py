import asyncio
import copy
from unittest.mock import AsyncMock, Mock

import pytest

from mewcode.permissions import PermissionMode
from mewcode.workflow_core import goal_host, trace
from mewcode.workflow_core.activity import observe_view
from mewcode.workflow_core.blueprint import documents, extract
from mewcode.workflow_core.chat_tools import UpdateParams, ReadParams, SaveParams, ControlParams, RunParams, BlockParams
from mewcode.workflow_core.projection import view, node_detail
from mewcode.workflow_core.recipes import crystallize, instantiate
from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.schema import walk
from mewcode.workflow_core.state import command, edit, fresh
from tests.workflow_core.test_chat_tools import host
from tests.workflow_core.test_exploration import item
from tests.test_task_design_entry import task_api_env


def update(*items, request_id='first', **fields):
    return UpdateParams(request_id=request_id, intent='execute', items=list(items), **fields)


@pytest.mark.asyncio
async def test_recording_has_no_execution_or_model_dependency_and_preserves_failed_history(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    execute = Mock(side_effect=AssertionError('recording cannot execute'))
    launch = AsyncMock(side_effect=AssertionError('recording cannot launch'))
    monkeypatch.setattr(goal_host, 'AgentExecutor', execute)
    monkeypatch.setattr(goal_host, 'launch', launch)
    srv.MODEL_REGISTRY.resolve.side_effect = ValueError('offline')
    runtime.agent.permission_mode = PermissionMode.PLAN
    params = update(item('try'), observations=[dict(node_id='try', status='failed', text='第一次路径不适用')])
    result = await bridge.update(params)
    assert result['execution_started'] is False
    assert await bridge.update(params) == result
    assert not goal_host.TASKS
    state = bridge.backend(result['run_id']).read()
    assert not state['attempts'] and state['status'] == 'running'
    assert state['nodes']['try']['status'] == 'failed'
    await bridge.update(update(item('old-running'), request_id='old-running',
        observations=[dict(node_id='old-running', status='running', text='未采用路线的历史记录')]))
    completed = await bridge.update(update(item('deliver'), request_id='deliver',
        observations=[dict(node_id='deliver', status='completed', text='替代路线已交付')],
        completed=True, summary='保留原始引用；采用替代路线完成交付。'))
    assert completed['completed']
    assert bridge.read(ReadParams())['status'] == 'completed'
    record = srv.EXPERT_RUNS.load(result['run_id'])
    projected = view(record)
    assert projected['task']['status'] == '已完成'
    assert projected['task']['controls'] == [] and not projected['editor']['editable']
    assert projected['task']['purpose'] == 'exploration'
    assert projected['structure']['nodes'][0]['status'] == '执行失败'
    old = next(node for node in projected['structure']['nodes'] if node['id'] == 'old-running')
    assert old['execution']['active'] is False and old['execution']['phase'] == 'recorded'
    observe_view(projected, live={'scheduler_active': False, 'active_node_ids': []})
    assert projected['task']['status'] == '已完成'
    assert '替代路线' in documents(extract(record))['documents']['brief']['text']
    execute.assert_not_called(); launch.assert_not_called(); srv.MODEL_REGISTRY.resolve.assert_not_called()


@pytest.mark.asyncio
async def test_trace_cannot_be_edited_dispatched_or_waited_as_a_pipeline(tmp_path, monkeypatch):
    srv, _, bridge = host(tmp_path, monkeypatch)
    result = await bridge.update(update(item('a')))
    rid = result['run_id']
    backend = bridge.backend(rid)
    before = backend.read()
    with pytest.raises(ValueError, match='主对话'):
        backend.change(lambda state: edit(state, {'action': 'delete', 'kind': 'node', 'id': 'a'}))
    with pytest.raises(ValueError, match='主对话'):
        backend.change(lambda state: command(state, {'operation': 'resume', 'request_id': 'resume'}))
    with pytest.raises(ValueError, match='主对话'):
        await bridge.control(ControlParams(run_id=rid, request_id='resume', operation='resume'))
    with pytest.raises(ValueError, match='主对话'):
        await goal_host.launch(srv, srv.EXPERT_RUNS.load(rid))
    executor = AsyncMock()
    with pytest.raises(ValueError, match='主对话'):
        await Core(MemoryBackend(before), executor).run()
    executor.assert_not_called()
    assert (await bridge.wait(ReadParams(run_id=rid)))['status'] == 'running'
    assert backend.read() == before
    with pytest.raises(ValueError, match='交付'):
        await bridge.save(SaveParams(run_id=rid))
    with pytest.raises(ValueError, match='完成后'):
        extract(srv.EXPERT_RUNS.load(rid))


@pytest.mark.asyncio
async def test_program_structure_roundtrip_and_explicit_reuse_with_fresh_execution(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    branch = dict(item('route'), kind='branch', inputs={'route': 'left'},
        config={'source': 'inputs.route', 'cases': [{'equals': 'left', 'port': 'left'}], 'default': 'right'})
    loop = dict(item('repeat'), kind='loop', config={'mode': 'count', 'count': 2,
        'graph': {'nodes': [dict(id='leaf', kind='work', task='逐轮处理输入', label='循环内工作')], 'edges': []}})
    edges = [dict(id='to-left', source='route', target='left', port='left'),
        dict(id='to-right', source='route', target='right', port='right'),
        dict(id='left-loop', source='left', target='repeat'),
        dict(id='right-loop', source='right', target='repeat'),
        dict(id='loop-join', source='repeat', target='join'),
        dict(id='parallel-join', source='parallel', target='join')]
    recorded = await bridge.update(update(branch, item('left'), item('right'), loop, item('parallel'), item('join'),
        edges=edges, inputs={'output_dir': 'old-output'}, observations=[
            dict(node_id='route', status='completed', result={'value': 'left'}),
            dict(node_id='right', status='skipped', text='这次未选择'),
            dict(node_id='repeat/leaf', status='completed', iteration=[1], actor='agent-receipt-7', text='原任务第二轮交付')],
        completed=True, summary='按条件选路，两轮处理，与并发工作汇合后交付。'))
    original = bridge.backend(recorded['run_id']).read()
    assert not original['attempts']
    recipe = await bridge.save(SaveParams())
    assert recipe['conversation_summary'].startswith('按条件')
    assert recipe['graph']['execution_profile'] == 'goal.recipe.v1'
    assert len(list(walk(recipe['graph']))) == 7
    graph, bindings = instantiate(recipe, {'output_dir': 'new-output'})
    assert not fresh(graph)['nodes'] and not fresh(graph)['attempts']
    assert graph['edges'] == original['graph']['edges']
    assert graph['nodes'][3]['config']['graph']['nodes'][0]['model']['effort'] == 'max'
    assert bindings['route']['route'] == 'left'
    assert 'actor' not in str(graph) and '原任务第二轮交付' not in str(graph)
    calls = []
    async def execute(ctx):
        if ctx.node_id == 'join':
            assert 'parallel' in [path for path, _ in calls]
            assert sum(path == 'repeat/leaf' for path, _ in calls) == 2
        calls.append((ctx.node_id, ctx.iteration))
        return {'text': '本轮交付'}
    monkeypatch.setattr(goal_host, 'AgentExecutor', Mock(return_value=execute))
    reused = await bridge.run_recipe(RunParams(request_id='explicit-reuse', recipe_id=recipe['id'],
        inputs={'output_dir': 'new-output'}))
    assert reused['execution_started'] and reused['run_id'] != recorded['run_id']
    assert bridge.display_backend().run_id == reused['run_id']
    finished = await asyncio.wait_for(bridge.wait(ReadParams(run_id=reused['run_id'])), 45)
    assert finished['status'] == 'completed'
    assert 'right' not in [path for path, _ in calls]
    assert [iteration for path, iteration in calls if path == 'repeat/leaf'] == [[0], [1]]
    assert bridge.backend(recorded['run_id']).read() == dict(original,
        chat_recipe_id=recipe['id'], solidified_recipe={key: recipe[key] for key in ('id', 'version', 'name', 'digest')})
    detail = node_detail(srv.EXPERT_RUNS.load(recorded['run_id']), 'repeat/leaf')
    assert detail['observations'][0]['actor'] == 'agent-receipt-7'


@pytest.mark.asyncio
async def test_invalid_structure_is_atomic_and_scope_updates_preserve_other_branches(tmp_path, monkeypatch):
    _, _, bridge = host(tmp_path, monkeypatch)
    result = await bridge.update(update(item('a'), item('b', ['a'])))
    backend = bridge.backend(result['run_id'])
    before = backend.read()
    with pytest.raises(ValueError, match='环'):
        await bridge.update(update(item('a', ['b']), request_id='cycle'))
    assert backend.read() == before
    with pytest.raises(ValueError, match='同一请求'):
        await bridge.update(update(item('changed')))
    assert backend.read() == before
    await bridge.update(update(item('a'), request_id='revision', observations=[
        dict(node_id='a', status='completed', text='已完成')]))
    state = backend.read()
    assert state['graph']['nodes'][1] == before['graph']['nodes'][1]
    assert state['graph']['edges'] == before['graph']['edges']


@pytest.mark.asyncio
async def test_trace_visual_blocks_use_real_immutable_artifacts_without_an_executor(tmp_path, monkeypatch):
    srv, _, bridge = host(tmp_path, monkeypatch)
    image = tmp_path / 'figure.png'
    image.write_bytes(b'image-version-one')
    result = await bridge.update(update(item('a'), observations=[dict(node_id='a', status='completed',
        result={'artifacts': [{'path': str(image), 'stored_path': 'untrusted', 'id': 'untrusted'}]})]))
    artifact = bridge.read(ReadParams())['tasks'][0]['artifacts'][0]
    assert artifact['id'] != 'untrusted'
    from pathlib import Path
    assert Path(artifact['stored_path']).read_bytes() == b'image-version-one'
    image.write_bytes(b'image-version-two')
    bridge.publish(BlockParams(node_id='a', block_id='figure', slot='output', kind='image',
        title='处理结果', artifact_path=str(image)))
    projected = view(srv.EXPERT_RUNS.load(result['run_id']))
    assert projected['structure']['nodes'][0]['presentation_blocks'][0]['artifact_id'] == artifact['id']
    assert Path(artifact['stored_path']).read_bytes() == b'image-version-one'
    assert not goal_host.TASKS
    with pytest.raises(ValueError, match='当前项目'):
        await bridge.update(update(request_id='external', observations=[dict(node_id='a', status='completed',
            result={'artifacts': [{'path': str(tmp_path.parent/'secret.png')}]})]))


@pytest.mark.asyncio
async def test_trace_api_shows_history_without_runtime_fault_and_extracts_once(task_api_env, tmp_path, monkeypatch):
    import httpx
    from frontend import server
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.workflow_core.store import ensure
    from mewcode.workflow_core.web import envelope
    from tests.workflow_core.test_exploration import MODEL

    _, _, _, scope, _ = task_api_env
    store = ExpertRunStore(tmp_path / 'trace-runs', storage='sqlite')
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    template = dict(model=MODEL, config={'workspace': str(tmp_path)})
    state = fresh(dict(execution_profile=trace.PROFILE, nodes=[], edges=[], label='探索', goal='完成实际任务'))
    trace.update(state, update(item('a'), observations=[dict(node_id='a', status='failed', text='原尝试失败')]), template)
    rid = 'e' * 32
    store.create(run_id=rid, conversation_id=scope['conversation_id'], task='探索', attachments=[],
        profile={'executor': MODEL}, strategy='stable', flow=envelope(state['graph']))
    backend = ensure(store, rid, state['graph'])
    backend.change(lambda saved: saved.update(state))
    owner = dict(scope, run_id=rid)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test',
        headers={'x-mew-workbench': '1'}) as client:
        projected = (await client.get('/api/workbench/view', params=owner)).json()
        assert projected['task']['purpose'] == 'exploration' and projected['task']['status'] == '探索中'
        assert projected['task']['controls'] == [] and projected['inbox'] == []
        listing = (await client.get('/api/workbench/tasks', params=scope)).json()
        assert next(task for task in listing['tasks'] if task['run_id'] == rid)['status'] == '探索中'
        response = await client.post('/api/workbench/run-action', json=dict(owner, operation='resume', request_id='resume'))
        assert response.status_code == 409
        response = await client.post('/api/workbench/node-message', json=dict(owner, node_id='a', kind='ask', message='重做', request_id='ask'))
        assert response.status_code == 409
        response = await client.get('/api/workbench/export', params=owner)
        assert response.status_code == 409
        backend.change(lambda saved: saved.update(status='completed', conversation_summary='通过替代路线完成。'))
        exported = (await client.get('/api/workbench/export', params=owner)).json()
        assert '通过替代路线' in exported['documents']['brief']['text']
        saved = (await client.post('/api/workbench/recipes', json=owner)).json()
        again = (await client.post('/api/workbench/recipes', json=owner)).json()
        assert saved['id'] == again['id'] and saved['version'] == again['version']
        assert saved['graph']['execution_profile'] == 'goal.recipe.v1'
        assert backend.read()['status'] == 'completed' and not backend.read()['attempts']


@pytest.mark.asyncio
async def test_new_chat_does_not_display_an_old_paused_pipeline(task_api_env, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import httpx
    from frontend import server
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.workflow_core.store import ensure
    from mewcode.workflow_core.web import envelope
    from tests.workflow_core.test_exploration import MODEL

    _, _, _, scope, _ = task_api_env
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    old = fresh(dict(nodes=[dict(id='old', task='旧任务')], edges=[]))['graph']
    rid = 'd' * 32
    store.create(run_id=rid, conversation_id=scope['conversation_id'], task='旧任务', attachments=[],
        profile={'executor': MODEL}, strategy='stable', flow=envelope(old))
    backend = ensure(store, rid, old)
    backend.change(lambda state: state.update(status='paused'))
    focus = dict(kind='chat', task='当前对话的独立请求', task_id='f' * 32)
    runtime = SimpleNamespace(session=SimpleNamespace(meta=SimpleNamespace(dialogue_focus=focus)))
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda cid: runtime)
    monkeypatch.setattr(server.SESSION, 'chat_busy', lambda cid: True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.get('/api/workbench/view', params=scope)
        assert response.status_code == 200, response.text
        current = response.json()
        assert current['task']['id'] == focus['task_id']
        assert current['task']['purpose'] == 'exploration'
        assert current['task']['status'] == '处理中' and current['task']['run_id'] == ''
        assert current['task']['controls'] == [] and not current['structure']['nodes']
        history = (await client.get('/api/workbench/view', params=dict(scope, run_id=rid))).json()
        assert history['task']['status'] == '已暂停'
    assert backend.read()['status'] == 'paused'


def test_replaced_nested_attempt_is_history_and_keeps_successor_dependency():
    from tests.workflow_core.test_exploration import MODEL
    state = fresh(dict(execution_profile=trace.PROFILE))
    template = dict(model=MODEL, config={})
    trace.update(state, update(dict(item('loop'), kind='loop', config={'mode': 'count', 'count': 2,
        'graph': {'nodes': [{'id': 'old', 'task': '旧方法'}, {'id': 'join', 'task': '汇合'}],
                  'edges': [{'source': 'old', 'target': 'join'}]}}),
        observations=[dict(node_id='loop/old', status='failed', text='旧方法失败')]), template)
    trace.update(state, update(dict(item('new'), replaces=['old']), request_id='replace', scope='loop',
        completed=True, summary='改用新方法，保留旧尝试。'), template)
    recipe = crystallize(state)
    body = recipe['graph']['nodes'][0]['config']['graph']
    assert {node['id'] for node in body['nodes']} == {'new', 'join'}
    assert [(edge['source'], edge['target']) for edge in body['edges']] == [('new', 'join')]
    assert state['nodes']['loop/old']['status'] == 'failed'
    assert state['trace_observations'][0]['text'] == '旧方法失败'
