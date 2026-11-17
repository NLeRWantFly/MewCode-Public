"""Bind conversational diagnosis/recovery to the selected workbench run."""
from __future__ import annotations

import asyncio
import copy
import re
import secrets

from mewcode.conversation import Message
from mewcode.orchestration.chat_routing import dialogue_focus
from mewcode.permissions import PermissionMode
from mewcode.workflow_core import recovery
from mewcode.workflow_core.runtime import LIVE, observation
from mewcode.workflow_core.store import ensure

from frontend.workbench_api import Workbench
from frontend.workflow_core_api import start_if_needed

LOCKS = {}


def selected_run(srv, cid, context=None):
    """An explicit selection or saved focus; never the most recent unrelated run."""
    if context is not None:
        if not isinstance(context, dict) or context.get('conversation_id') != cid:
            raise ValueError('工作台所属对话已变化，请重新选择当前任务')
        values = {key: context[key] for key in ('project', 'conversation_id', 'task_id', 'run_id') if key in context}
        if not values.get('run_id'):
            raise ValueError('当前工作台任务还没有启动，没有可恢复的检查点')
    else:
        runtime = srv.SESSION.runtime_for(cid) or srv.SESSION
        focus = dialogue_focus(runtime)
        run_id = focus.get('run_id') if focus.get('kind') == 'orchestration' else ''
        if not run_id:
            # The task-design binding survives a diagnostic chat changing focus.
            from mewcode.orchestration.draft_store import DraftStore
            design = (DraftStore.read_state(srv.SESSION.work_dir, cid) or {}).get('task_design') or {}
            run_id = design.get('run_id')
        if not run_id:
            raise ValueError('当前对话没有绑定的工作流，请在工作台选择需要检查的任务')
        values = dict(conversation_id=cid, run_id=run_id)
    _, _, _, _, record = Workbench(srv).select(values)
    if not record:
        raise ValueError('所选任务没有运行检查点')
    return record


def append_message(runtime, role, text):
    conversation = getattr(runtime, 'conversation', None)
    if conversation is not None:
        (conversation.add_user_message if role == 'user' else conversation.add_assistant_message)(text)
    durable = getattr(runtime, 'session', None)
    if durable is not None:
        durable.append(Message(role=role, content=text))


async def repair_environment(runtime, report):
    """Reuse the existing host repair only for a native execution failure.

    Node agents retain environment/package repairs and unknown side effects.
    A host diagnostic error never discards the other recoverable branches.
    """
    checker = getattr(getattr(runtime, 'agent', None), 'permission_checker', None)
    uses_native = getattr(checker, 'uses_native_commands', lambda: False)()
    reasons = ' '.join(row['reason'] for row in report['findings'])
    if not uses_native or not re.search(r'native|隔离边界|执行环境|runtime|LPAC', reasons, re.I):
        return None
    from mewcode.sandbox.native_recovery import recover_native_runtime
    try:
        return await asyncio.to_thread(recover_native_runtime)
    except (OSError, ValueError, RuntimeError) as exc:
        return dict(action='failed', changed=False, reason=str(exc))


async def handle_chat(srv, *, cid, text, request_id='', context=None, client_echoed=True, attachments=()):
    intent = recovery.chat_intent(text)
    if not intent or attachments:
        return None
    if not cid or cid != srv._active_conversation_id():
        raise ValueError('请回到当前任务所属对话再检查运行')
    request_id = request_id or secrets.token_hex(16)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request_id):
        raise ValueError('恢复消息的请求 ID 无效')
    record = selected_run(srv, cid, context)
    rid = record['run_id']
    async with LOCKS.setdefault((id(srv.EXPERT_RUNS), rid), asyncio.Lock()):
        # A switch during an awaited repair may not redirect this operation.
        record = selected_run(srv, cid, context)
        if record['run_id'] != rid:
            raise ValueError('当前工作台任务已变化，原检查结果未用于其他任务')
        runtime = srv.SESSION.runtime_for(cid) or srv.SESSION.capture_conversation_runtime()
        if srv.SESSION.chat_busy(cid) and getattr(runtime, 'execution_kind', '') != 'orchestration':
            raise ValueError('当前普通对话还在执行，工作流进度未改变；本轮结束后可继续检查')
        active_run = getattr(runtime, 'expert_run_id', '') or getattr(runtime, 'last_expert_run_id', '')
        if srv.SESSION.chat_busy(cid) and active_run and active_run != rid:
            raise ValueError('当前对话正在执行另一份工作流，未修改它的任务身份或启动所选旧任务')
        if intent == 'recover' and getattr(srv.SESSION, 'permission_mode', None) == PermissionMode.PLAN:
            raise ValueError('当前处于只规划模式，可以检查原因；切换到允许执行的模式后才能续跑')
        if not record.get('workflow_core'):
            if intent == 'inspect':
                from mewcode.workflow_core.migration import migrate
                record = copy.deepcopy(record)
                migrate(record)
            else:
                ensure(srv.EXPERT_RUNS, rid)
                record = srv.EXPERT_RUNS.load(rid)
        live, fault = observation(rid)
        report = recovery.diagnose(record, live=live, fault=fault)
        sequence = record['workflow_core']['sequence']
        message = recovery.describe(report)
        prior = record['workflow_core'].get('recovery_requests', {}).get(request_id)
        if prior and prior['fingerprint'] != recovery.digest([text, rid]):
            raise ValueError('同一恢复请求不能用于不同消息')
        new = prior is None
        if new:
            append_message(runtime, 'user', text)
        started = False
        if intent == 'recover':
            backend = ensure(srv.EXPERT_RUNS, rid)
            if not prior:
                from mewcode.orchestration.execution_scope import prepare_orchestration_scope
                # Task identity is restored before building any resumed node agent.
                scope = prepare_orchestration_scope(runtime, task=record.get('task', ''), run_id=rid, checkpoint=record)
                srv.EXPERT_RUNS.mutate(rid, lambda saved: saved.update(host_execution_scope=scope),
                                      durable=True, return_snapshot=False)
                environment = await repair_environment(runtime, report)
                if srv._active_conversation_id() != cid:
                    raise ValueError('检查已保留；对话已切换，未启动其他对话中的任务')
                # Re-read after host repair: a running node may have delivered meanwhile.
                record = srv.EXPERT_RUNS.load(rid)
                if any(event.get('sequence', 0) > sequence and event.get('kind') == 'control'
                       and event.get('operation') in {'pause', 'stop'} for event in record['workflow_core']['events']):
                    raise ValueError('你已暂停或停止执行，本次检查保留原进度，不再自动续跑')
                live, fault = observation(rid)
                report = recovery.diagnose(record, live=live, fault=fault)
                message = recovery.describe(report)
                receipt = backend.change(lambda state: recovery.resume(state, report, request_id=request_id, text=text))
                if environment is not None:
                    backend.change(lambda state: state['recovery_requests'][request_id].update(environment=environment))
                backend.change(lambda state: state['recovery_requests'][request_id].update(identity_restored=True))
            else:
                receipt = prior
            if receipt['status'] == 'completed':
                message += '已有交付全部保留，无需重跑。'
            elif receipt['status'] == 'already_running':
                message += '继续现有执行，不重复派发。'
            else:
                message += '已保留已有成果和执行记录，接续未完成部分；原节点会核查历史工具结果并自行修复。'
                if report['waiting_for_user']:
                    message += '需要真实输入的节点仍显示原问题，其他可执行分支继续。'
            if new:
                append_message(runtime, 'assistant', message)
                backend.change(lambda state: state['recovery_requests'][request_id].update(message=message))
            # Publish the diagnosis before the resumed nodes start emitting progress.
            if new:
                await srv.ConversationWebSocket(srv.WS_EVENTS, cid).send_json(dict(type='workflow_recovery',
                    request_id=request_id, request=text, message=message, run_id=rid,
                    client_echoed=client_echoed, new=True, status=receipt['status']))
            # start_if_needed wakes a live scheduler or queues one continuation
            # behind its finalization. A repeated request cannot reset checkpoints.
            started = await start_if_needed(Workbench(srv), rid)
        else:
            append_message(runtime, 'assistant', message)
            await srv.ConversationWebSocket(srv.WS_EVENTS, cid).send_json(dict(type='workflow_recovery',
                request_id=request_id, request=text, message=message, run_id=rid,
                client_echoed=client_echoed, new=True, status='inspected'))
        if not srv.SESSION.chat_busy(cid) and rid not in LIVE:
            await srv.ConversationWebSocket(srv.WS_EVENTS, cid).send_json(dict(type='done', elapsed=0,
                interrupted=False, status=runtime.status_payload(), tasks=runtime.tasks_payload()))
        return dict(handled=True, new=new, message=message, run_id=rid, started=started, diagnosis=report,
                    client_echoed=True)
