"""Host-scoped controls for the editable workflow runtime."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from mewcode.workflow_core.runtime import LIVE
from mewcode.workflow_core.schema import digest, walk
from mewcode.workflow_core.state import command, edit
from mewcode.workflow_core.store import ensure
from mewcode.workflow_core import dialogue

LAUNCHES = {}


def gesture(request):
    origin = request.headers.get('origin')
    if (request.headers.get('x-mew-workbench') != '1' or request.headers.get('sec-fetch-site') == 'cross-site'
            or origin and origin != str(request.base_url).rstrip('/')):
        raise PermissionError('请在当前工作台操作')


async def start_if_needed(workbench, run_id):
    if run_id in LIVE:
        LIVE[run_id].wake.set()
        return True
    srv = workbench.srv
    record = srv.EXPERT_RUNS.load(run_id)
    from mewcode.workflow_core.trace import executable
    if record:
        executable(record['workflow_core'])
    if not record or record['workflow_core']['status'] != 'running':
        return False
    if (record.get('parent_run_id') or record.get('chat_origin')) and record['workflow_core']['graph'].get('execution_profile') in {'goal.open.v1', 'goal.recipe.v1', 'goal.chat.v1'}:
        from mewcode.workflow_core.goal_host import launch
        return await launch(srv, record)
    cid = record['conversation_id']
    if not srv.SESSION.chat_busy(cid):
        await srv.launch_expert_resume(record)
        return True
    # Applying a revision at the last node may race the previous chat task's finalization.
    # Queue exactly one continuation, owned by the same conversation and run.
    if run_id not in LAUNCHES:
        async def launch_later():
            try:
                while srv.SESSION.chat_busy(cid):
                    if run_id in LIVE:
                        LIVE[run_id].wake.set()
                        return
                    latest = srv.EXPERT_RUNS.read_execution_fields(run_id, 'workflow_core')
                    if latest['workflow_core']['status'] != 'running':
                        return
                    await asyncio.sleep(.1)
                if srv._active_conversation_id() == cid:
                    latest = srv.EXPERT_RUNS.load(run_id)
                    if latest['workflow_core']['status'] == 'running':
                        await srv.launch_expert_resume(latest)
            finally:
                LAUNCHES.pop(run_id, None)
        LAUNCHES[run_id] = asyncio.create_task(launch_later())
    return False


async def edit_graph(workbench, body, record):
    backend = ensure(workbench.srv.EXPERT_RUNS, record['run_id'])
    state = backend.read()
    graph = state.get('draft') or state['graph']
    if body.get('basis') and body['basis'] != digest(graph):
        raise ValueError('草稿版本已变化，请刷新后重试')
    operation = body.get('operation') or {}
    result = []
    def change(state):
        changed = edit(state, operation, expected_revision=body.get('expected_revision', graph['revision']))
        # Explicit manual model changes retain their exact provider and effort.
        before = dict(walk(graph))
        for path, node in walk(state['draft']):
            if node.get('model') and node.get('model') != before.get(path, {}).get('model'):
                from mewcode.model_registry import ModelRef
                workbench.srv.MODEL_REGISTRY.resolve(ModelRef.from_value(node['model']))
        result.append(changed)
    backend.change(change)
    return dict(result[0], execution_started=False, view=workbench.view(body))


async def run_action(workbench, body, record):
    from mewcode.permissions import PermissionMode
    if (workbench.srv.SESSION.permission_mode == PermissionMode.PLAN
            and body.get('operation') not in {'pause', 'stop', 'cancel'}):
        raise ValueError('请切换到允许执行的权限模式')
    backend = ensure(workbench.srv.EXPERT_RUNS, record['run_id'])
    values = {key: value for key, value in body.items() if key in {
        'operation', 'node_id', 'request_id', 'expected_revision', 'attempt_id', 'inputs', 'result', 'value'}}
    values['operation'] = {'retry_node': 'rerun', 'resume_after_node': 'continue_from', 'cancel': 'stop'}.get(
        values.get('operation'), values.get('operation'))
    core = LIVE.get(record['run_id'])
    result = core.control(values) if core else backend.change(lambda state: command(state, values))
    started = await start_if_needed(workbench, record['run_id'])
    return dict(result, started=started, view=workbench.view(body))


async def node_message(workbench, body, record):
    from mewcode.workflow_core.trace import executable
    executable(record['workflow_core'])
    from mewcode.permissions import PermissionMode
    if workbench.srv.SESSION.permission_mode == PermissionMode.PLAN and body.get('kind', 'ask') != 'ask':
        raise ValueError('补交会修改产物，请切换到允许执行的模式')
    backend = ensure(workbench.srv.EXPERT_RUNS, record['run_id'])
    values = {key: value for key, value in body.items() if key in {
        'node_id', 'kind', 'message', 'references', 'request_id', 'target_attempt_id', 'base_delivery_id', 'parent_request_id'}}
    core = LIVE.get(record['run_id'])
    result = core.send_message(values) if core else backend.change(lambda state: dialogue.enqueue(state, values))
    started = await start_if_needed(workbench, record['run_id'])
    return dict(request=result, started=started, view=workbench.view(body))


def install(app, workbench):
    @app.post('/api/workbench/goal')
    async def start_goal(request: Request):
        from mewcode.workflow_core.exploration import build_goal
        from mewcode.workflow_core.goal_host import create
        try:
            gesture(request)
            body = await request.json()
            _, _, _, _, parent = workbench.select(body, read_only=True)
            if not parent:
                raise ValueError('请选择当前任务，作为试跑来源')
            parent = workbench.srv.EXPERT_RUNS.read_execution_fields(parent['run_id'],
                'run_id', 'conversation_id', 'host_execution_scope')
            graph = build_goal(label=body['label'], goal=body['goal'], model=body['model'],
                workspace=body['workspace'], concurrency=body.get('concurrency', 8),
                parameters=body.get('parameters', []), file_scope=body.get('file_scope'),
                tools=body.get('tools'), service_tier=body.get('service_tier'))
            run_id = await create(workbench.srv, parent, graph, body.get('inputs') or {})
            return dict(run_id=run_id, parent_run_id=parent['run_id'], started=True)
        except PermissionError as exc:
            return JSONResponse({'error': str(exc)}, status_code=403)
        except (ValueError, KeyError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)

    @app.get('/api/workbench/recipes')
    async def read_recipes(request: Request):
        from mewcode.workflow_core.recipes import RecipeStore
        try:
            values = dict(request.query_params)
            project, _ = workbench.scope(values)
            store = RecipeStore(project)
            if values.get('recipe_id'):
                return await asyncio.to_thread(store.read, values['recipe_id'],
                    int(values['version']) if values.get('version') else None)
            return dict(recipes=await asyncio.to_thread(store.list))
        except (ValueError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)

    @app.post('/api/workbench/recipes')
    async def save_recipe(request: Request):
        from mewcode.workflow_core.recipes import RecipeStore, crystallize
        try:
            gesture(request)
            body = await request.json()
            project, _, _, _, record = workbench.select(body, read_only=True)
            if not record:
                raise ValueError('请先选择正常交付的工作流')
            record = workbench.srv.EXPERT_RUNS.read_execution_fields(record['run_id'], 'run_id', 'workflow_core')
            if not record.get('workflow_core'):
                raise ValueError('该运行没有原生工作流记录')
            def save():
                backend = ensure(workbench.srv.EXPERT_RUNS, record['run_id'])
                store = RecipeStore(project)
                def persist(state):
                    prior = state.get('solidified_recipe')
                    if prior:
                        return store.read(prior['id'], prior['version'])
                    value = crystallize(state, source_run_id=record['run_id'])
                    value['id'] = state.get('chat_recipe_id', '')
                    recipe = store.save(value)
                    state.update(chat_recipe_id=recipe['id'], solidified_recipe={
                        key:recipe[key] for key in ('id', 'version', 'name', 'digest')})
                    return recipe
                return backend.change(persist)
            return await asyncio.to_thread(save)
        except PermissionError as exc:
            return JSONResponse({'error': str(exc)}, status_code=403)
        except (ValueError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)

    @app.post('/api/workbench/recipes/instantiate')
    async def instantiate_recipe(request: Request):
        from mewcode.workflow_core.recipes import RecipeStore, instantiate
        try:
            gesture(request)
            body = await request.json()
            project, _ = workbench.scope(body)
            recipe = await asyncio.to_thread(RecipeStore(project).read, body['recipe_id'], body.get('version'))
            graph, bindings = instantiate(recipe, body.get('inputs') or {})
            return dict(workflow=graph, bindings=bindings, execution_started=False)
        except PermissionError as exc:
            return JSONResponse({'error': str(exc)}, status_code=403)
        except (ValueError, KeyError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)

    @app.post('/api/workbench/node-message')
    async def send_node_message(request: Request):
        try:
            gesture(request)
            body = await request.json()
            _, _, _, _, record = workbench.select(body)
            if not record or not record.get('workflow_core'):
                raise ValueError('该运行没有可追问的节点记录')
            return await node_message(workbench, body, record)
        except PermissionError as exc:
            return JSONResponse({'error': str(exc)}, status_code=403)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)

    @app.post('/api/workbench/node-message/cancel')
    async def cancel_node_message(request: Request):
        try:
            gesture(request)
            body = await request.json()
            _, _, _, _, record = workbench.select(body)
            if not record or not record.get('workflow_core'):
                raise ValueError('消息所属运行不存在')
            core = LIVE.get(record['run_id'])
            values = dict(operation='cancel_request', message_id=body.get('message_id', ''))
            result = core.control(values) if core else ensure(workbench.srv.EXPERT_RUNS, record['run_id']).change(
                lambda state: dialogue.cancel(state, values['message_id']))
            return dict(request=result, view=workbench.view(body))
        except PermissionError as exc:
            return JSONResponse({'error': str(exc)}, status_code=403)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)

    @app.post('/api/workbench/graph/apply')
    async def apply_graph(request: Request):
        try:
            gesture(request)
            body = await request.json()
            _, _, _, _, record = workbench.select(body)
            if not record:
                raise ValueError('请先启动工作流')
            return await run_action(workbench, dict(body, operation='apply'), record)
        except PermissionError as exc:
            return JSONResponse({'error': str(exc)}, status_code=403)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)

    @app.get('/api/workbench/node')
    async def node(request: Request):
        try:
            body = dict(request.query_params)
            _, _, _, _, record = workbench.select(body)
            if not record or not record.get('workflow_core'):
                raise ValueError('该运行没有新版节点记录')
            from mewcode.workflow_core.projection import node_detail
            from mewcode.workflow_core.activity import observe_node_detail
            from mewcode.workflow_core.runtime import observation
            from mewcode.expert_runs import redact_for_display
            live, fault = observation(record['run_id'])
            detail = observe_node_detail(node_detail(record, body.get('node_id', '')), live=live,
                fault=fault or record['workflow_core'].get('runtime_fault'))
            return redact_for_display(detail)
        except (ValueError, LookupError, KeyError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=404)

    @app.get('/api/workbench/artifact')
    async def artifact(request: Request):
        try:
            body = dict(request.query_params)
            _, _, _, _, record = workbench.select(body)
            if not record or not record.get('workflow_core'):
                raise ValueError('找不到产物')
            found = next((item for attempt in record['workflow_core']['attempts'].values()
                          for item in [*attempt.get('artifacts', []), *([attempt['report']] if attempt.get('report') else [])]
                          if item.get('id') == body.get('file_id')), None)
            found = found or record['workflow_core'].get('trace_artifacts', {}).get(body.get('file_id'))
            if not found:
                raise ValueError('找不到该版本的产物')
            path = Path(found.get('stored_path') or '')
            root = (Path(workbench.srv.EXPERT_RUNS.root) / record['run_id']).resolve()
            if not path.is_file() or not path.resolve().is_relative_to(root):
                raise ValueError('产物快照不存在')
            if body.get('download') == '1':
                return FileResponse(path, filename=found['name'], media_type='application/octet-stream')
            if body.get('inline') == '1':
                media = {'.png':'image/png', '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.webp':'image/webp'}.get(
                    Path(found['name']).suffix.lower())
                if not media:
                    raise ValueError('该产物不支持图片显示，请下载查看')
                return FileResponse(path, media_type=media, headers={'X-Content-Type-Options':'nosniff'})
            with path.open('rb') as stream:
                data = stream.read(128 * 1024)
            try:
                preview = data.decode('utf-8-sig') if b'\0' not in data else '二进制文件，请下载查看。'
            except UnicodeError:
                preview = '二进制文件，请下载查看。'
            return dict(id=found['id'], name=found['name'], preview=preview,
                        size=found.get('size'), truncated=path.stat().st_size > len(data))
        except (ValueError, LookupError, KeyError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=404)
