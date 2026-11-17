"""Task-bound host controls for explicit, exact role model selections."""
from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.orchestration.task_design import TaskDesign


def install(app, srv, scope):
    @app.post('/api/orchestration/task-design/role-models')
    async def change_models(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get('confirm_model_change') is not True:
                raise ValueError('需要明确选择角色模型')
            project, cid = scope(body)
            design = TaskDesign(project, cid)
            current = design.read() or {}
            expected = (body.get('expected_task_id'), body.get('expected_revision'), body.get('expected_run_id'))
            if (current.get('task_id'), current.get('revision'), current.get('run_id')) != expected or not expected[2]:
                raise ValueError('任务、版本或运行已变化，未修改角色模型')
            if srv.SESSION.chat_busy(cid):
                raise ValueError('请先暂停当前任务，再修改角色模型')
            catalog = await srv.MODEL_REGISTRY.catalog(local_only=True)
            scope(body)
            latest = design.read() or {}
            if (latest.get('task_id'), latest.get('revision'), latest.get('run_id')) != expected or srv.SESSION.chat_busy(cid):
                raise ValueError('任务已变化，未修改角色模型')
            from mewcode.orchestration.collab_service import bound_record
            record = bound_record(srv.EXPERT_RUNS, latest, cid)
            if record is None:
                raise ValueError('当前任务没有绑定协作运行')
            changed = srv.EXPERT_RUNS.set_role_models(record['run_id'], conversation_id=cid,
                models=body.get('models'), catalog=catalog, expected_updated_at=body.get('expected_updated_at'),
                command_id=body.get('command_id') or '', reason=body.get('reason') or '')
            public = srv.EXPERT_RUNS._public_value(changed, include_details=False)
            await srv.WS_EVENTS.send_json({'type': 'expert_checkpoint_restored', 'run_id': record['run_id'],
                                         'conversation_id': cid, 'snapshot': public})
            return {'snapshot': public, 'started': False}
        except (ValueError, TypeError, KeyError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
