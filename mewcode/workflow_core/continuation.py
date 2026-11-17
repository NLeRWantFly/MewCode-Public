"""Resume technical interruptions without restarting delivered work or user pauses."""
from __future__ import annotations

import copy
import json
import re
import time

from . import dialogue
from .state import event


def cause(reason):
    text = str(reason or '')
    if re.search(r'上下文压缩|压缩仅减少|未达到最低收益|摘要生成失败|context.{0,20}(?:length|overflow|limit)|maximum context', text, re.I):
        return dict(kind='context', fresh_context=True)
    if re.search(r'timed?\s*out|timeout|超时|\bEOF\b|\bdisconnected\b|connection (?:reset|closed|aborted)|连接(?:中断|断开|重置)|'
                 r'network error|\b(?:429|502|503|504)\b|rate.?limit|temporarily unavailable|'
                 r'模型对话已结束，(?:节点|尚未收到).*交付', text, re.I):
        return dict(kind='transport', fresh_context=False)
    return None


def eligible(state, attempt):
    if not attempt or state['status'] != 'running' or state.get('stop_requested'):
        return False
    path = attempt['node_id']
    if any(path == held or path.startswith(held + '/') for held in state.get('held', [])):
        return False
    if attempt.get('status') not in {'blocked', 'failed'}:
        return False
    if attempt.get('request_id'):
        request = state.get('node_requests', {}).get(attempt['request_id'], {})
        latest = max((r for r in state.get('node_requests', {}).values()
                      if r.get('target_session_id') == request.get('target_session_id')),
                     key=lambda r: (r.get('created_at', 0), r['id']), default={})
        return (request.get('status') == 'failed' and request.get('attempt_id') == attempt['id']
                and latest.get('id') == request.get('id'))
    if state.get('dialogue_only'):
        return False
    row = state['nodes'].get(path, {})
    return (row.get('attempt_id') == attempt['id'] and row.get('generation') == attempt.get('generation')
            and row.get('status') in {'blocked', 'failed'})


def schedule(state, identity, *, now=None, delay=2):
    attempt = state['attempts'][identity]
    failure = cause((attempt.get('result') or {}).get('reason'))
    if not failure or not eligible(state, attempt):
        return False
    tickets = state.setdefault('automatic_continuations', {})
    if identity in tickets:
        return False
    number = 1 + sum(t['node_id'] == attempt['node_id'] and t['session_id'] == attempt.get('session_id')
                     for t in tickets.values())
    stamp = time.time() if now is None else now
    tickets[identity] = dict(id=identity, source_attempt_id=identity, node_id=attempt['node_id'],
        session_id=attempt.get('session_id'), status='scheduled', number=number,
        due_at=stamp + min(float(delay) * 2**min(number-1, 5), 60), **failure)
    event(state, 'automatic_continuation_scheduled', node_id=attempt['node_id'], attempt_id=identity,
          interruption=failure['kind'], number=number)
    return True


def pending(state):
    return [t for t in state.get('automatic_continuations', {}).values()
            if t['status'] == 'scheduled' and eligible(state, state['attempts'].get(t['source_attempt_id'], {}))]


def advance(state, *, now=None):
    stamp = time.time() if now is None else now
    for ticket in state.get('automatic_continuations', {}).values():
        if ticket['status'] != 'scheduled':
            continue
        attempt = state['attempts'].get(ticket['source_attempt_id'], {})
        if not eligible(state, attempt):
            continue
        if ticket['due_at'] > stamp:
            continue
        ticket['status'] = 'resumed'
        hint = copy.deepcopy(ticket)
        if attempt.get('request_id'):
            old = state['node_requests'][attempt['request_id']]
            session = state['node_sessions'][old['target_session_id']]
            request = dialogue.enqueue(state, dict(node_id=old['node_id'], kind=old['kind'],
                message=old['message'], references=old['references'], target_attempt_id=session['primary_attempt_id'],
                parent_request_id=old['id'], request_id='auto:' + attempt['id']),
                sender=state['attempts'].get(old.get('sender_attempt_id')), internal=True)
            state['node_requests'][request['id']].update(recovery_of=old['id'],
                working_copy_id=old.get('working_copy_id') or old['id'], automatic_continuation=hint)
        else:
            state['nodes'][attempt['node_id']].update(status='pending', previous_attempt_id=attempt['id'],
                resume_checkpoint=True, automatic_continuation=hint, reason='')
        event(state, 'automatic_continuation_resumed', node_id=attempt['node_id'], attempt_id=attempt['id'])


def delay(state):
    tickets = pending(state)
    return max(0.0, min(t['due_at'] for t in tickets) - time.time()) if tickets else None


def restart_context(checkpoint, recovery):
    hint = (recovery or {}).get('automatic_continuation') or {}
    return bool(hint.get('fresh_context') or
                (cause((checkpoint.get('result') or {}).get('reason')) or {}).get('fresh_context'))


def context_note(checkpoint):
    # The full old transcript remains in its immutable attempt. Only a bounded
    # index is placed in the fresh context; the model can page the original.
    summary = dict(attempt_id=checkpoint.get('id'), node_id=checkpoint.get('node_id'),
        progress=[str(p.get('text', ''))[-1800:] for p in checkpoint.get('progress', [])[-5:]],
        artifacts=[{k:a.get(k) for k in ('id','name','path','stored_path')} for a in checkpoint.get('artifacts', [])[-20:]])
    return ('上一轮上下文已达到容量，现用新上下文接续同一节点；旧对话、错误、工具回执与产物仍完整保留。'
            '使用 ReadNodeCheckpoint 按需读取原尝试记录，勿重新执行已完成的业务。'
            '未知工具结果先检查实际文件、进程或服务。用户暂停或停止仍有效。\n'
            + json.dumps(summary, ensure_ascii=False))


def checkpoint_page(state, identity, *, attempt_id='', section='summary', index=0, offset=0, limit=10000):
    current = state['attempts'][identity]
    target = state['attempts'].get(attempt_id)
    if not target or (target['node_id'],target.get('session_id')) != (current['node_id'],current.get('session_id')):
        raise ValueError('只能读取当前节点会话的已保存记录')
    if section == 'summary':
        value = dict(id=target['id'],status=target['status'],result=target.get('result'),
            messages=len(target.get('messages', [])),progress=target.get('progress', [])[-5:],
            artifacts=target.get('artifacts', []))
    elif section == 'messages':
        values = target.get('messages', [])
        if not 0 <= index < len(values):
            raise ValueError('对话记录序号超出范围')
        value = values[index]
    elif section == 'tools':
        values = list((target.get('pending_tools') or {}).items())
        if not 0 <= index < len(values):
            raise ValueError('工具记录序号超出范围')
        value = dict(id=values[index][0], **values[index][1])
    else:
        raise ValueError('未知的检查点记录类型')
    text = json.dumps(value,ensure_ascii=False,default=str)
    end = offset + limit
    return dict(attempt_id=target['id'],section=section,index=index,offset=offset,
        total_chars=len(text),text=text[offset:end],next_offset=end if end<len(text) else None)
