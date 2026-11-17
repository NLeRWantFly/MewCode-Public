from unittest.mock import AsyncMock
import asyncio
import time

import httpx
import pytest

from frontend import server
from mewcode.expert_runs import ExpertRunStore
from mewcode.workflow_core.runtime import Core
from mewcode.workflow_core.store import ensure
from mewcode.workflow_core.web import envelope
from tests.test_task_design_entry import task_api_env
from tests.workflow_core.test_runtime import graph


@pytest.mark.asyncio
async def test_stalled_display_agrees_across_cached_view_list_and_node(task_api_env, tmp_path, monkeypatch):
    from mewcode.workflow_core.runtime import FAILURES, LIVE
    from mewcode.workflow_core.state import start_attempt

    _, _, _, scope, _ = task_api_env
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    rid = 'a' * 32
    flow = graph(['work'])
    store.create(run_id=rid, conversation_id=scope['conversation_id'], task='OCR', attachments=[],
                 profile={}, strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    stamp = time.time() - 600

    def stalled(state):
        state['status'] = 'running'
        attempt = start_attempt(state, 'work', {}, [])
        attempt.update(started_at=stamp, last_activity_at=stamp)
        state['events'][-1]['at'] = stamp
        return attempt['id']

    identity = backend.change(stalled)
    before = backend.read()
    owner = dict(scope, run_id=rid)
    core = Core(backend, None, run_id=rid)
    LIVE[rid] = core
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            async def read_all():
                view = (await client.get('/api/workbench/view', params=owner)).json()
                listing = (await client.get('/api/workbench/tasks', params=scope)).json()
                detail = (await client.get('/api/workbench/node', params=dict(owner, node_id='work'))).json()
                task = next(row for row in listing['tasks'] if row['run_id'] == rid)
                return view, task, detail

            view, task, detail = await read_all()
            assert view['task']['status'] == task['status'] == '已阻塞'
            assert view['structure']['nodes'][0]['status'] == '已阻塞'
            assert detail['attempts'][0]['display_status'] == 'blocked'
            assert backend.read() == before

            # Stream activity changes the read projection even before the next durable checkpoint.
            active = asyncio.get_running_loop().create_future()
            core.tasks[identity] = active
            core.observe(identity, 'work')
            view, task, detail = await read_all()
            assert view['task']['status'] == task['status'] == '进行中'
            assert view['structure']['nodes'][0]['status'] == '进行中'
            assert 'display_status' not in detail['attempts'][0]
            assert backend.read() == before

            old_version = view['version']
            await core.publish(identity, 'activity', activity='tool_running', tool='ReadFile', tool_id='read')
            view, task, detail = await read_all()
            assert view['version'] != old_version
            assert view['structure']['nodes'][0]['execution']['text'] == '正在调用工具 · ReadFile'
            assert backend.read() == before  # Live overlay also survives the cached-view path.

            active.set_result(None)
            core.observe(identity, 'work')  # A late activity signal is not a live conversation.
            view, task, detail = await read_all()
            assert view['task']['status'] == task['status'] == '已阻塞'
            assert detail['attempts'][0]['display_status'] == 'blocked'
            assert view['structure']['nodes'][0]['execution']['phase'] == 'blocked'
            assert backend.read() == before

            LIVE.pop(rid)
            FAILURES[rid] = dict(reason='磁盘空间已满，对话已中断。', node_ids=['work'])
            view, task, detail = await read_all()
            assert view['task']['status'] == task['status'] == '已阻塞'
            assert '磁盘空间已满' in view['task']['reason']
            assert detail['attempts'][0]['activity_reason'] == view['task']['reason']
            assert backend.read() == before
    finally:
        for task in core.tasks.values():
            task.cancel()
        LIVE.pop(rid, None)
        FAILURES.pop(rid, None)


@pytest.mark.asyncio
async def test_running_canvas_edits_apply_and_node_controls_use_real_api(task_api_env, tmp_path, monkeypatch):
    current, _, _, scope, _ = task_api_env
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    launches = AsyncMock()
    monkeypatch.setattr(server, 'launch_expert_resume', launches)
    rid = 'c' * 32
    flow = graph(['prepare', 'ocr', 'store'], [('prepare', 'ocr', 'always'), ('ocr', 'store', 'always')])
    store.create(run_id=rid, conversation_id=scope['conversation_id'], task='OCR', attachments=[],
                 profile={}, strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    async def execute(ctx):
        await ctx.publish('progress', text='保留公开进展')
        return dict(text='正常交付')
    await Core(backend, execute).run()
    owner = dict(scope, run_id=rid)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test',
                                headers={'X-Mew-Workbench': '1'}) as client:
        view = (await client.get('/api/workbench/view', params=owner)).json()
        assert view['task']['mode'] == 'workflow'
        current['busy'] = True
        edited = await client.post('/api/workbench/graph/edit', json=dict(owner, basis=view['editor']['basis'],
            operation=dict(action='insert', kind='node', id='check', edge_id='e_1_ocr_store',
                           fields=dict(kind='judge', config={'expression': True}))))
        assert edited.status_code == 200, edited.text
        revision = edited.json()['revision']
        stale = await client.post('/api/workbench/graph/apply', json=dict(owner, request_id='stale', expected_revision=revision-1))
        assert stale.status_code == 409
        current['busy'] = False
        request = dict(owner, request_id='apply', expected_revision=revision)
        applied = await client.post('/api/workbench/graph/apply', json=request)
        assert applied.status_code == 200, applied.text
        await Core(backend, execute).run()
        again = await client.post('/api/workbench/graph/apply', json=request)
        assert again.json()['applied']
        assert launches.await_count == 1
        details = await client.get('/api/workbench/node', params=dict(owner, node_id='store'))
        assert len(details.json()['attempts']) == 2
        rerun = await client.post('/api/workbench/run-action', json=dict(owner, operation='rerun',
            node_id='ocr', request_id='one-node'))
        assert rerun.status_code == 200, rerun.text
        assert not rerun.json()['view']['inbox']
        assert backend.read()['nodes']['store']['status'] == 'paused'
        wrong = await client.post('/api/workbench/run-action', json=dict(owner, conversation_id='elsewhere',
            operation='resume', request_id='wrong'))
        assert wrong.status_code == 409


@pytest.mark.asyncio
async def test_human_node_message_api_keeps_dag_and_checks_scope(task_api_env, tmp_path, monkeypatch):
    current, _, _, scope, _ = task_api_env
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    launches = AsyncMock()
    monkeypatch.setattr(server, 'launch_expert_resume', launches)
    current['busy'] = False
    rid = 'f' * 32
    flow = graph(['a', 'b'], [('a', 'b', 'always')])
    store.create(run_id=rid, conversation_id=scope['conversation_id'], task='OCR', attachments=[],
                 profile={}, strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    async def execute(ctx):
        if ctx.request:
            assert ctx.request['sender_kind'] == 'user'
            assert ctx.request['sender_node_id'] == ''
            return dict(text='Original node replied')
        await ctx.publish('checkpoint', messages=[{'role': 'assistant', 'content': 'Saved context'}])
        return dict(text='Original delivery')
    await Core(backend, execute).run()
    original = backend.read()['nodes']
    owner = dict(scope, run_id=rid)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test',
                                headers={'X-Mew-Workbench': '1'}) as client:
        payload = dict(owner, node_id='a', kind='ask', message='Explain page 53', request_id='human-message',
                       sender_kind='node', sender_node_id='forged', references=['page:53'])
        response = await client.post('/api/workbench/node-message', json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['request']['sender_kind'] == 'user'
        assert response.json()['view']['inbox'] == []
        assert backend.read()['nodes'] == original
        await Core(backend, execute).run()
        again = await client.post('/api/workbench/node-message', json=payload)
        assert again.status_code == 200 and again.json()['request']['status'] == 'completed'
        details = (await client.get('/api/workbench/node', params=dict(owner, node_id='a'))).json()
        assert details['capabilities']['node_messages']
        assert details['requests'][0]['reply']['text'] == 'Original node replied'
        assert len(details['deliveries']) == 1
        assert backend.read()['nodes'] == original
        denied = await client.post('/api/workbench/node-message', json=dict(payload, conversation_id='foreign', request_id='no'))
        assert denied.status_code == 409
        foreign = await client.post('/api/workbench/node-message', json=dict(payload, request_id='cross-site'),
                                   headers={'Sec-Fetch-Site': 'cross-site'})
        assert foreign.status_code == 403
        queued = await client.post('/api/workbench/node-message', json=dict(payload, request_id='cancel-only'))
        assert queued.status_code == 200
        cancelled = await client.post('/api/workbench/node-message/cancel', json=dict(owner, message_id='cancel-only'))
        assert cancelled.status_code == 200
        assert backend.read()['node_requests']['cancel-only']['status'] == 'cancelled'
        assert backend.read()['nodes'] == original
