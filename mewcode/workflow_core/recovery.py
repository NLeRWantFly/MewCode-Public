"""Diagnose a saved workflow and resume its unfinished conversations in place.

Recovery is a user operation, not another acceptance gate. Original tool
receipts, deliveries and graph requirements remain evidence for the node.
"""
from __future__ import annotations

import copy
import re
import time

from .schema import digest, walk
from .state import DONE, LOOP_LIMIT_REASONS, command, event


def chat_intent(text):
    """Recognize direct operational requests, including '检查为什么卡住并继续'.

    Questions about the feature and quoted examples remain ordinary chat.
    This is an ingress shortcut; it never guesses which historical run to use.
    """
    value = str(text or '').strip()
    if (not value or len(value) > 4000 or re.search(r'[“”「」`"]|(?:实现|开发|新增|设计|能否|能不能|是否可以|如何使用|怎么使用|错别字)', value)
            or re.search(r'\b(?:implement|feature|example|can you explain|how to)\b', value, re.I)):
        return ''
    diagnosis = bool(re.search(r'(?:检查|排查|诊断|修复|修好|解决|看看|看下|查看|查一下).{0,40}'
                              r'(?:卡住|卡死|卡在|阻塞|受阻|停住|停了|不动|没推进|无法推进|失败|中断|运行|执行|流程|编排|任务)', value)
                     or re.search(r'(?:为什么|为何).{0,16}(?:卡住|阻塞|受阻|不动|停住|无法推进)', value)
                     or re.search(r'\b(?:diagnose|fix|repair|investigate|check)\b.{0,60}'
                                  r'\b(?:stuck|blocked|stalled|workflow|run|failure)\b', value, re.I))
    if not diagnosis:
        return ''
    if re.search(r'(?:不要|暂不|别|先不|不必)\s*(?:自动)?\s*(?:继续|恢复|续跑|重试|执行)|'
                 r'\b(?:do not|don.t)\s+(?:resume|continue|restart)\b', value, re.I):
        return 'inspect'
    resume = re.search(r'(?:并|然后|之后|后|再|并且|接着|直接|自动|顺便|，|,|；|;)\s*'
                       r'(?:从.{0,30}?)?(?:继续|恢复|续跑)(?=$|[\s，,。.!！;；]|执行|运行|任务|流程|编排|未完成|原任务|剩余)|'
                       r'(?:继续|恢复|续跑).{0,20}(?:并|然后|顺便).{0,20}(?:检查|排查|诊断|修复)|'
                       r'\b(?:and|then)\s+(?:resume|continue)\b', value, re.I)
    # A direct repair request includes continuing the affected unfinished work;
    # a request to only investigate is read-only.
    repair = re.match(r'^(?:请|帮我|麻烦)?\s*(?:自动)?(?:修复|修好|解决)', value)
    return 'recover' if resume or repair else 'inspect'


def diagnose(record, *, live=None, fault=None):
    state = record['workflow_core']
    live = live or {}
    active = set(live.get('active_node_ids', []))
    scheduler = bool(live.get('scheduler_active'))
    findings, unfinished, waiting = [], [], []
    nodes = dict(walk(state['graph']))

    def add(kind, path, reason, **extra):
        findings.append(dict(kind=kind, node_id=path, label=nodes.get(path, {}).get('label', path), reason=reason, **extra))

    failure = fault or state.get('runtime_fault')
    if failure:
        add('scheduler_fault', '', failure.get('reason') or '调度已中断')
    for path, node in nodes.items():
        row = state['nodes'].get(path, {})
        status = row.get('status', 'pending')
        if status in DONE:
            continue
        unfinished.append(path)
        attempt = state['attempts'].get(row.get('attempt_id'), {})
        reason = row.get('reason') or (attempt.get('result') or {}).get('reason') or ''
        stage = record.get('node_states', {}).get(path, {})
        if node['kind'] == 'judge' and node['config'].get('manual') and status == 'needs_input':
            waiting.append(path)
            add('user_input', path, reason or node['task'] or '等待人工判断')
            continue
        if stage.get('pending_permission') or stage.get('pending_interaction'):
            waiting.append(path)
            add('user_input', path, reason or '这个节点正在等待所需信息或工具权限')
        if path in active:
            execution = live.get('executions', {}).get(path, {})
            add('active', path, execution.get('text') or '节点对话仍在执行，不重复启动', phase=execution.get('phase', ''))
            continue
        if row.get('loop') and (row['loop'].get('limit_pause') or reason in LOOP_LIMIT_REASONS):
            add('loop_limit', path, reason or '达到配置的循环限额', limit=copy.deepcopy(row['loop'].get('limit_pause')))
        elif attempt.get('status') == 'suspended' and attempt.get('waiting_on'):
            request = state.get('node_requests', {}).get(attempt['waiting_on'], {})
            add('node_reply', path, '等待上游节点回复，已保存当前工具回合', request_id=attempt['waiting_on'],
                target_node_id=request.get('node_id', ''), request_status=request.get('status', 'missing'))
        elif status in {'failed', 'blocked', 'unknown', 'paused', 'needs_input'}:
            add('interrupted', path, reason or '节点尚未交付，将从保存的对话和工具回执接续')
        elif status == 'running' and node['kind'] != 'loop' and attempt.get('status') != 'suspended':
            add('orphaned', path, '已保存运行状态，但对应的模型对话已结束')
        tools = attempt.get('pending_tools') or {}
        uncertain = [dict(call_id=key, name=value.get('name', value.get('tool', '')), status=value.get('status'))
                     for key, value in tools.items() if value.get('status') in {'running', 'unknown', 'outcome_unknown'}]
        if uncertain:
            add('unknown_tools', path, '历史工具结果未确认；先检查实际文件、进程或服务，再接续未完成工作', tools=uncertain)
    requests = state.get('node_requests', {})
    latest = {request.get('target_session_id'): request['id'] for request in requests.values()}
    pending_requests, retry_requests = [], []
    for request in requests.values():
        path = request['node_id']
        current = state['attempts'].get(state['nodes'].get(path, {}).get('attempt_id'), {})
        status = request['status']
        retry = (status == 'failed' and latest.get(request.get('target_session_id')) == request['id']
                 and current.get('session_id') == request.get('target_session_id'))
        if status not in {'queued', 'running', 'waiting', 'paused'} and not retry:
            continue
        pending_requests.append(request['id'])
        if retry:
            retry_requests.append(request['id'])
        stage = record.get('node_states', {}).get(path, {})
        if stage.get('pending_permission') or stage.get('pending_interaction'):
            if path not in waiting:
                waiting.append(path)
                add('user_input', path, '补交对话正在等待所需信息或工具权限')
        if request.get('attempt_id') in live.get('active_attempt_ids', []):
            add('active', path, '节点正在处理追问或补交，不重复派发', request_id=request['id'])
        elif retry:
            add('reply_failed', path, (request.get('reply') or {}).get('reason') or '补交对话已中断，将保留失败记录并接续原上下文',
                request_id=request['id'])
        elif status == 'paused':
            add('reply_paused', path, '追问或补交已暂停，将接续原对话', request_id=request['id'])
        elif not scheduler:
            add('reply_interrupted', path, '追问或补交尚未返回，原会话已保存', request_id=request['id'])
    if not scheduler and (unfinished or pending_requests):
        add('scheduler_stopped', '', '当前没有运行中的调度器，将接回原运行')
    return dict(run_id=record['run_id'], status=state['status'], scheduler_active=scheduler,
                active_node_ids=sorted(active), unfinished=unfinished, waiting_for_user=waiting,
                graph_revision=state['graph']['revision'], findings=findings,
                pending_requests=pending_requests, retry_requests=retry_requests,
                retained=dict(attempts=len(state['attempts']), deliveries=len(state.get('deliveries', {})),
                              completed_nodes=sum(row.get('status') == 'completed' for row in state['nodes'].values())))


def describe(report):
    significant = [row for row in report['findings'] if row['kind'] not in {'active', 'scheduler_stopped'}]
    if significant:
        details = '；'.join(dict.fromkeys((row['label'] + '：' if row['label'] else '') + row['reason'] for row in significant[:5]))
    elif report['scheduler_active']:
        details = '当前节点仍在执行或正常等待回复，没有发现已停止的执行'
    elif not report['unfinished'] and not report['pending_requests']:
        details = '当前工作流已经交付完成'
    else:
        details = '执行已停止，保存的进度仍在'
    return '检查结果：' + details + '。'


def resume(state, report, *, request_id, text):
    """One durable, repeatable recovery operation; never rerun completed nodes."""
    fingerprint = digest([text, report['run_id']])
    receipts = state.setdefault('recovery_requests', {})
    if request_id in receipts:
        previous = receipts[request_id]
        if previous['fingerprint'] != fingerprint:
            raise ValueError('同一恢复请求不能用于不同消息')
        return copy.deepcopy(previous)
    waiting = set(report['waiting_for_user'])
    # A manual question is a local dependency, not a reason to block other work.
    preserved = {key: copy.deepcopy(value) for key, value in state['nodes'].items() if key in waiting}
    preserved_requests = {key: copy.deepcopy(value) for key, value in state.get('node_requests', {}).items()
                          if value['node_id'] in waiting and value['status'] == 'paused'}
    preserved_attempts = {value['attempt_id']: copy.deepcopy(state['attempts'][value['attempt_id']])
                          for value in preserved_requests.values()}
    healthy = report['scheduler_active'] and not any(row['kind'] in {
        'loop_limit', 'interrupted', 'orphaned', 'reply_paused', 'reply_failed', 'reply_interrupted', 'scheduler_fault'}
        for row in report['findings'])
    finished = not report['unfinished'] and not report['pending_requests']
    actions = []
    if not healthy and not finished:
        command(state, dict(operation='resume', request_id='recovery:' + request_id))
        state['nodes'].update(preserved)
        state.get('node_requests', {}).update(preserved_requests)
        state['attempts'].update(preserved_attempts)
        from . import dialogue
        for identity in report['retry_requests']:
            old = state['node_requests'][identity]
            if old['node_id'] in waiting:
                continue
            session = state['node_sessions'][old['target_session_id']]
            continued = dialogue.enqueue(state, dict(node_id=old['node_id'], kind=old['kind'], message=old['message'],
                references=old['references'], target_attempt_id=session['primary_attempt_id'], parent_request_id=identity,
                request_id='recovery-reply:' + digest([request_id, identity])[:40]),
                sender=state['attempts'].get(old.get('sender_attempt_id')), internal=True)
            # Keep the partially edited working copy, not only the last published
            # delivery. Only this host transition may set this binding.
            state['node_requests'][continued['id']].update(recovery_of=identity,
                working_copy_id=old.get('working_copy_id') or identity)
            actions.append('resume_failed_reply')
        # A stopped executor with a stale running row is recovered by Core.run;
        # an existing scheduler needs the same transition for an orphaned row.
        if report['scheduler_active']:
            for finding in report['findings']:
                if finding['kind'] == 'orphaned':
                    row = state['nodes'][finding['node_id']]
                    row.update(status='pending', previous_attempt_id=row.get('attempt_id', ''), resume_checkpoint=True)
        paths = set(report['unfinished']) | {state['node_requests'][key]['node_id'] for key in report['pending_requests']}
        for path in paths:
            if path in waiting or path in report['active_node_ids']:
                continue
            state.setdefault('recovery_contexts', {})[path] = dict(request_id=request_id, request=text,
                findings=[copy.deepcopy(row) for row in report['findings'] if row['node_id'] in {'', path}])
        actions.append('resume_checkpoint')
        if any(row['kind'] == 'loop_limit' for row in report['findings']):
            actions.append('retire_legacy_loop_limit')
    receipt = dict(id=request_id, fingerprint=fingerprint, at=time.time(), request=text,
                   diagnosis=copy.deepcopy(report), actions=actions,
                   status='completed' if finished else 'already_running' if healthy else 'prepared')
    receipts[request_id] = receipt
    event(state, 'recovery_requested', request_id=request_id, actions=actions)
    return copy.deepcopy(receipt)
