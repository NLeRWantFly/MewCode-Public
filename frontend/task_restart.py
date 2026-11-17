"""Explicitly restart a stopped task through the normal saved-graph handoff."""
from __future__ import annotations

import copy

from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.orchestration.task_design import TaskDesign


def preserve_requirements(before, after):
    """A scheduling rebuild cannot silently revise the business contract."""
    if before.goal != after.goal or before.goal_contract != after.goal_contract:
        raise ValueError('重构需保留原业务总目标、参数、约束和最终验收；要求变更请使用修订入口')
    old_meta, new_meta = before.meta, after.meta
    if old_meta.get('orchestration_mode') != new_meta.get('orchestration_mode'):
        raise ValueError('重构不能隐式切换编排模式')
    protected = {'goal', 'input', 'artifact', 'acceptance', 'constraint', 'decision', 'assumption'}
    old = {row['id']: row for row in ((old_meta.get('collaboration_spec') or {}).get('document') or {}).get('entries', [])
           if row.get('kind') in protected}
    new = {row['id']: row for row in ((new_meta.get('collaboration_spec') or {}).get('document') or {}).get('entries', [])}
    if any(new.get(key) != row for key, row in old.items()):
        raise ValueError('重构不能删除或改写已有业务要求、决定或约束来源')


def install(app, srv, scope):
    from frontend.task_design import LocalRequest, response_value
    from frontend.workbench_startup import receipt as startup_receipt, save as save_inputs
    from mewcode.orchestration.draft_store import DraftStore

    def inherit_inputs(project, cid, prepared, spec, source):
        # These are input values, not new gate answers or tool permissions.
        # The existing resolver still validates them against the unchanged
        # declarations and the execution path checks actual authorization.
        state = DraftStore.read_state(project, cid) or {}
        if not startup_receipt(state, prepared['task_id'], spec):
            save_inputs(srv, project, cid, prepared['task_id'],
                {'spec_hash': digest(spec), 'design_revision': prepared['revision']},
                copy.deepcopy(source.get('run_inputs') or {}))

    async def finish_restart(project, cid, design, prepared, spec, source):
        inherit_inputs(project, cid, prepared, spec, source)
        if source.get('status') != 'terminated':
            import asyncio
            await asyncio.to_thread(srv.EXPERT_RUNS.transition, source['run_id'], 'terminated',
                reason='用户明确重构并重新开始；原运行、成果与费用记录保留。')
        scope({'project': project, 'conversation_id': cid})
        if prepared['status'] == 'ready':
            prepared = design.control('pause', task_id=prepared['task_id'], revision=prepared['revision'],
                                      reason='重构后的编排已保存，等待从入口启动新运行。')
        return prepared

    @app.post('/api/orchestration/task-design/restart')
    async def restart(request: Request):
        design = prepared = None
        expected = None
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get('confirm_restart') is not True:
                raise ValueError('需要明确选择重构并重新开始')
            project, cid = scope(body)
            if srv.SESSION.chat_busy(cid):
                raise ValueError('请先暂停当前执行，再重新开始')
            design = TaskDesign(project, cid)
            current = design.read() or {}
            run_id = body.get('expected_run_id')
            source = srv.EXPERT_RUNS.read_execution_fields(run_id, 'run_id', 'conversation_id',
                'status', 'active_node_ids', 'kernel_calls', 'run_inputs', 'updated_at') if run_id else {}
            if source.get('conversation_id') != cid or source.get('run_id') != run_id:
                raise ValueError('原运行不属于当前对话')
            if source.get('status') not in {'paused', 'blocked', 'needs_attention', 'terminated'} or source.get('active_node_ids'):
                raise ValueError('原运行仍在执行，未重新开始')
            if any(row.get('state') in {'dispatched', 'unknown'} for row in (source.get('kernel_calls') or {}).values()):
                raise ValueError('原运行仍有未结算调用，需先核对实际结果，不能重复执行')
            plan = srv._conversation_plan_store(cid).get(project)
            if not plan or not plan.spec:
                raise ValueError('当前没有可重构的已保存方案')
            candidate = srv._own_orchestration_spec(OrchestrationSpec.from_value(body.get('spec')))
            same_request = (current.get('restart') or {}).get('request_id') == body.get('request_id')
            if not same_request and source.get('updated_at') != body.get('expected_run_updated_at'):
                raise ValueError('原运行检查点已变化，未重新开始')
            if not same_request:
                preserve_requirements(plan.spec, candidate)
            if candidate.spec_id == plan.spec.spec_id and not same_request:
                raise ValueError('新编排需使用独立执行图标识，不能恢复旧循环')
            srv.validate_spec(candidate, require_bindings=True)
            flow = srv.to_expert_flow(candidate)
            flow.validate()
            if candidate.meta.get('orchestration_mode') == 'collaborative':
                from mewcode.orchestration.collab_compile import compile_plan
                compile_plan(flow)
            if (current.get('task_id'), current.get('revision'), current.get('run_id')) != (
                    body.get('expected_task_id'), body.get('expected_revision'), run_id) and not same_request:
                raise ValueError('原任务或版本已变化，未重新开始')
            prepared = design.restart(task_id=body.get('expected_task_id'), revision=body.get('expected_revision'),
                run_id=run_id, request_id=body.get('request_id'), spec_hash=digest(candidate.as_dict()),
                document=copy.deepcopy((candidate.meta.get('collaboration_spec') or {}).get('document')))
            if prepared['status'] in {'ready', 'paused'} and (prepared.get('build_request') or {}).get('state') == 'saved':
                if prepared['build_request'].get('saved_spec_hash') != digest(plan.spec.as_dict()):
                    raise ValueError('已保存的新编排又发生变化，未重放旧重新开始请求')
                prepared = await finish_restart(project, cid, design, prepared, plan.spec.as_dict(), source)
                return {'task_design': prepared, 'started': False, 'already_saved': True}
            build = prepared.get('build_request') or {}
            expected = {'task_id': prepared['task_id'], 'task_revision': prepared['revision'],
                        'build_id': build.get('id'), 'build_epoch': build.get('epoch')}
            candidate.meta['task_design'] = {**candidate.meta.get('task_design', {}), 'task_id': prepared['task_id']}
            if isinstance(candidate.meta.get('collaboration_spec'), dict):
                candidate.meta['collaboration_spec']['task_id'] = prepared['task_id']
            saved = response_value(await srv.api_orchestration_open_canvas(LocalRequest({
                'conversation_id': cid, 'spec': candidate.as_dict(),
                'expected_version': body.get('expected_graph_version'), 'task_design_expected': expected})))
            prepared = design.saved_build(expected)
            # The explicit replacement retires the old scheduler, preserving
            # its ledger. Existing authorization inheritance can then transfer
            # remaining capped usage without two resumable spenders.
            prepared = await finish_restart(project, cid, design, prepared, saved['spec'], source)
            return {'task_design': prepared, 'spec': saved['spec'],
                    'source_run_id': run_id, 'source_terminated': True, 'started': False,
                    'notice': '新编排已保存，原运行、成果与费用记录保留；启动仍走正常执行入口。'}
        except (ValueError, TypeError, OSError, KeyError, RuntimeError) as exc:
            if design and prepared and expected:
                try:
                    design.finish_build(expected, 'failed', str(exc))
                except ValueError:
                    pass
            return JSONResponse({'error': str(exc)}, status_code=409)
