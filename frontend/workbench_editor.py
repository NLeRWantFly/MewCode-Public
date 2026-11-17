"""Authenticated host gesture endpoint for the planning canvas."""
from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.orchestration import manual_graph
from mewcode.permissions import PermissionMode


def base_spec(workbench, project, cid, state):
    from frontend.workbench_startup import saved_spec
    design = state.get('task_design') or {}
    return saved_spec(workbench.srv, project, cid, design.get('task_id')) if design else None


def install(app, workbench):
    @app.post('/api/workbench/graph/edit')
    async def edit_graph(request: Request):
        origin = request.headers.get('origin')
        if (request.headers.get('x-mew-workbench') != '1' or
                origin and origin != str(request.base_url).rstrip('/') or
                request.headers.get('sec-fetch-site') == 'cross-site'):
            return JSONResponse({'error': '请在当前工作台编辑。'}, status_code=403)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('请求必须为对象。')
            project, cid, state, _ = workbench.context(body)
            if body.get('run_id'):
                _, _, _, _, record = workbench.select(body)
                if record and record.get('workflow_core'):
                    if workbench.srv.SESSION.permission_mode == PermissionMode.PLAN:
                        raise ValueError('请切换到允许编辑的权限模式')
                    from frontend.workflow_core_api import edit_graph
                    return await edit_graph(workbench, body, record)
            def guard():
                workbench.scope(body)
                srv = workbench.srv
                if (srv.SESSION.permission_mode == PermissionMode.PLAN or srv.SESSION.chat_busy(cid)
                        or getattr(srv, 'graph_generation_running', lambda *_: False)(project, cid)):
                    raise ValueError('请等当前生成结束或暂停，并切换到允许编辑的权限模式。')
            guard()
            if not body.get('run_id'):
                from frontend.workflow_design import edit_plan
                result = edit_plan(workbench, body, project, cid, guard)
                from mewcode.orchestration.workbench_events import changed
                changed('design', conversation_id=cid)
                return result
            operation = body.get('operation')
            fields = operation.get('fields') if isinstance(operation, dict) else None
            data = fields.get('data') if isinstance(fields, dict) else None
            model = data.get('model') if isinstance(data, dict) else None
            if model is not None:
                from mewcode.model_registry import ModelRef
                from mewcode.experts.model_route_recovery import validate_difficulty_model
                validate_difficulty_model(workbench.srv.MODEL_REGISTRY, ModelRef.from_value(model))
            result = manual_graph.edit(project, cid, body, base=base_spec(workbench, project, cid, state), guard=guard)
            result['view'] = workbench.view({'project':project, 'conversation_id':cid})
            from mewcode.orchestration.workbench_events import changed
            changed('design', conversation_id=cid)
            return result
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
