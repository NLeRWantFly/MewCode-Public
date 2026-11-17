"""Explicit previews of legacy run controls, confirmed in the shared inbox.

The short-lived preview cache grants no authority. Original host endpoints
perform the operation and its checkpoint/version checks after confirmation.
"""
from __future__ import annotations

import copy
import json
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse
from mewcode.orchestration import inbox
from mewcode.orchestration.workbench_events import changed


def cache(srv):
    if not hasattr(srv, '_workbench_run_actions'):
        srv._workbench_run_actions = {}
    return srv._workbench_run_actions


def key(task):
    return task.get('conversation_id', ''), task['id'], task.get('run_id', '')


def collect(srv, task, record):
    preview = getattr(srv, '_workbench_run_actions', {}).get(key(task))
    if not preview or task.get('readonly') or record.get('updated_at') != preview['expected_updated_at']:
        return []
    return [inbox.make('run_action', task, preview['id'], title=preview['title'], background=preview['summary'],
        questions=[inbox.choice('action','是否确认这项操作',[('accept','确认本次操作'),('decline','保留当前状态')])],
        basis=preview, context=preview)]


def decoded(result):
    data = json.loads(result.body) if isinstance(result, JSONResponse) else result
    if isinstance(result, JSONResponse) and result.status_code >= 400:
        raise ValueError(data.get('error') or '操作预览未成功。')
    return data


async def answer(workbench, item, body):
    srv, preview = workbench.srv, item['context']
    storage = cache(srv)
    identity = (body['conversation_id'], item['task_id'], item['run_id'])
    if storage.get(identity, {}).get('id') != preview['id']:
        raise ValueError(inbox.STALE)
    action = (body.get('answers') or {}).get('action')
    if action == 'decline':
        storage.pop(identity, None)
        return dict(ok=True, execution_started=False)
    if action != 'accept':
        raise ValueError('请选择是否确认这项操作。')
    data, run_id = preview['body'], item['run_id']
    operation = preview['operation']
    if operation == 'checkpoint':
        result = await srv.api_restore_expert_checkpoint(run_id, preview['checkpoint_id'], srv.CheckpointRestoreBody(**data))
    elif operation == 'saved_plan':
        result = await srv.api_accept_saved_plan_revision(run_id, srv.SavedPlanRevisionBody(**data))
    elif operation == 'loop_mode':
        result = await srv.api_expert_loop_control(run_id, srv.LoopControlBody(**data))
    elif operation == 'loop_tokens':
        result = await srv.api_expert_loop_token_policy(run_id, srv.LoopTokenPolicyBody(**data))
    elif operation in {'retry_node', 'resume_after_node'}:
        messages = []
        channel = srv.ConversationWebSocket(srv.WS_EVENTS, body['conversation_id'])
        class Relay:
            async def send_json(self, message):
                messages.append(message)
                await channel.send_json(message)
        started = await srv.dispatch_expert_resume(srv.SESSION, Relay(), data)
        if not started:
            raise ValueError(next((row.get('text') for row in messages if row.get('type') == 'error'), '无法恢复当前任务。'))
        result = dict(ok=True, execution_started=True)
    else:
        result = await srv.api_recovery_case_answer(srv.RecoveryCaseAnswerBody(**data))
    decoded(result)
    if storage.get(identity, {}).get('id') == preview['id']:
        storage.pop(identity, None)
    return result


def install(app, workbench):
    @app.post('/api/workbench/run-action')
    async def prepare(request: Request):
        if request.headers.get('x-mew-workbench') != '1' or request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'error':'请在任务工作台操作'},status_code=403)
        origin=request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'error':'不接受跨站操作'},status_code=403)
        try:
            body=await request.json()
            project,cid,_,_,record=workbench.select(body)
            operation = body.get('operation')
            if record and (record.get('workflow_core') or operation in {
                    'resume', 'rerun', 'continue_from', 'skip', 'adopt', 'bind_inputs'}):
                from frontend.workflow_core_api import run_action
                return await run_action(workbench, body, record)
            if not record or (operation != 'stop_repair' and (record.get('status')=='running' or workbench.srv.SESSION.chat_busy(cid))):
                raise ValueError('请先暂停当前任务，再核对这项操作。')
            view=workbench.view(body)
            if view['task'].get('readonly') or body.get('expected_updated_at')!=record.get('updated_at'):
                raise ValueError(inbox.STALE)
            srv,operation=workbench.srv,body.get('operation')
            data=dict(conversation_id=cid,expected_updated_at=record['updated_at'])
            preview=dict(id=uuid.uuid4().hex,expected_updated_at=record['updated_at'],operation=operation,body=data)
            if operation=='checkpoint':
                points=decoded(await srv.api_expert_checkpoints(record['run_id'],cid))
                point=next((row for row in points['checkpoints'] if row['checkpoint_id']==body.get('checkpoint_id')),None)
                if not point or not points['can_restore']:
                    raise ValueError('检查点已不可恢复，请重新读取。')
                preview.update(checkpoint_id=point['checkpoint_id'],title='确认恢复检查点',summary='当前进度会先另存为备份。只恢复编排进度，不自动执行或覆盖稿件、聊天记录和计划。',details=point)
            elif operation=='saved_plan':
                proposal=decoded(await srv.api_saved_plan_revision(record['run_id'],cid,str(body.get('repair_loop_id') or '')))
                data.update({name:proposal[name] for name in ('expected_updated_at','fingerprint','dialogue_offset')})
                data['repair_loop_id']=str(body.get('repair_loop_id') or '')
                preview.update(title='确认接纳已保存修订',summary=proposal['summary'],details=proposal)
            elif operation=='loop_mode':
                if body.get('mode') not in {'team','strict'}:
                    raise ValueError('运行方式无效。')
                data.update(mode=body['mode'],command_id=uuid.uuid4().hex)
                preview.update(title='确认调整循环运行方式',summary=('取消循环累计 Token、累计时长和轮数限额，可能增加用量。单段约束、恢复限制、无进展检查、验收和权限继续生效。' if body['mode']=='team' else '恢复合同中的严格累计限额，已消耗用量不清零。')+'保存设置不会自动续跑。')
            elif operation=='loop_tokens':
                data.update({name:body[name] for name in ('loop_id','ceiling','floor','rate_per_minute','burst_tokens') if name in body})
                data['command_id']=uuid.uuid4().hex
                srv.LoopTokenPolicyBody(**data)
                preview.update(title='确认授权有限 Token 总额',summary='授权累计硬上限为 '+str(data['ceiling'])+' Token。宿主在这个上限内调整软窗口；不会自动续跑。',details={name:value for name,value in data.items() if name not in {'command_id','conversation_id','expected_updated_at'}})
            elif operation=='stop_repair':
                from mewcode.experts.recovery_case import cases
                case=next((row for row in cases(copy.deepcopy(record)) if row['case_id']==body.get('case_id')),None)
                if not case or case.get('status') in {'closed','resolved'}:
                    raise ValueError('当前修复已经结束。')
                data.clear();data.update(project=project,conversation_id=cid,run_id=record['run_id'],case_id=case['case_id'],round=len(case.get('rounds') or []),choice='close',remember_scope='once')
                preview.update(title='确认停止本次修复',summary='保留已有检查点和调查记录，当前步骤仍未完成。')
            elif operation in {'retry_node','resume_after_node'}:
                node_id=str(body.get('node_id') or '')
                node=next((row for row in record.get('locked_flow',{}).get('nodes',[]) if row.get('node_id')==node_id),None)
                state=(record.get('node_states') or record.get('stages') or {}).get(node_id,{})
                if not node:
                    raise ValueError('该步骤不属于当前运行。')
                if operation=='retry_node':
                    if record.get('status') not in {'paused','blocked','needs_attention'} or state.get('status') not in {'failed','stalled','budget_exhausted','needs_attention','blocked','paused'}:
                        raise ValueError('当前步骤不可重试，请重新读取任务状态。')
                    data.update(type='expert_retry',run_id=record['run_id'],retry_node_id=node_id)
                    title='确认重试：'+str(node.get('label') or node_id)
                    summary='保留前置进度、已写文件与改动记忆，明确重试这个步骤，并重新核验后续依赖；仍受原宿主权限、预算和恢复检查约束。确认后开始执行。'
                else:
                    if record.get('status') not in {'paused','blocked','needs_attention','completed','terminated'} or state.get('status')!='completed':
                        raise ValueError('接续锚点必须是已完成步骤，请重新读取任务状态。')
                    data.update(type='expert_resume',run_id=record['run_id'],resume_after_node=node_id)
                    title='确认从此步骤后接续：'+str(node.get('label') or node_id)
                    summary='保留这个已完成步骤及前置进度，从后续阶段进入宿主恢复检查。确认后开始执行。'
                preview.update(title=title,summary=summary,details=dict(node_id=node_id,status=state.get('status')))
            else:
                raise ValueError('当前操作不受支持。')
            _,_,_,_,latest=workbench.select(body)
            if latest.get('updated_at')!=record['updated_at']:
                raise ValueError(inbox.STALE)
            cache(srv)[key(view['task'])]=preview
            changed('runtime',project=project,conversation_id=cid,run_id=record['run_id'])
            return dict(ok=True,execution_started=False)
        except (ValueError,TypeError,KeyError,OSError) as exc:
            return JSONResponse({'error':str(exc)},status_code=409)
