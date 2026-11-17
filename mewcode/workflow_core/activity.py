"""Display whether an unfinished node still has a live model conversation."""
from __future__ import annotations

from datetime import datetime

STOPPED_REASON = '模型对话已结束，节点尚未交付。'

PHASES = {'starting': '正在准备对话', 'waiting_model': '等待模型响应', 'reasoning': '模型正在思考',
          'output': '正在回复', 'tool_queued': '准备调用工具', 'tool_running': '正在调用工具',
          'tool_returned': '工具已返回', 'tool_error': '工具返回错误', 'tool_unknown': '工具结果待核对',
          'retrying': '正在重试模型请求', 'waiting_permission': '等待工具授权', 'waiting_input': '等待你的回复',
          'finishing': '正在保存交付'}


def execution_event(phase, *, tool='', tools=None, tool_id='', outcome='', retry_attempt=0,
                    retry_max=0, retry_wait=0):
    """Only public phase metadata; never expose reasoning, arguments or tool output."""
    names = [str(name)[:100] for name in (tools or ([tool] if tool else []))]
    text = PHASES.get(phase, '节点正在执行')
    if names:
        text += ' · ' + '、'.join(names[:2]) + (f' 等 {len(names)} 个' if len(names) > 2 else '')
    if phase == 'retrying':
        text += f' · {retry_attempt}/{retry_max}' if retry_max else ''
    return dict(phase=phase, text=text, tool=str(tool)[:100], tools=names, tool_id=tool_id, outcome=outcome,
                detail=f'本次请求退避等待 {retry_wait:g} 秒后重试' if phase == 'retrying' and retry_wait else '',
                active=phase not in {'waiting_input', 'waiting_permission'})


def execution_fallback(status, *, reason='', attempt=None, run_status='', activity=''):
    attempt = attempt or {}
    if status == 'running' and run_status in {'paused', 'terminated'}:
        status = run_status
    if attempt.get('status') == 'suspended' and status == 'running':
        return dict(phase='waiting_node' if attempt.get('waiting_on') else 'yielded',
                    text='等待节点回复，进度已保存' if attempt.get('waiting_on') else '等待接续，进度已保存',
                    active=False)
    if status == 'running':
        return dict(attempt.get('execution') or dict(phase='starting', text=activity or '节点正在执行', active=True))
    labels = {'pending': '等待执行', 'completed': '已交付', 'skipped': '已跳过', 'paused': '已暂停，进度已保存',
              'terminated': '已停止', 'blocked': '对话已中断，尚未交付', 'failed': '执行失败',
              'needs_input': '等待你的处理', 'unknown': '等待核对执行结果'}
    return dict(phase=status, text=labels.get(status, '尚未开始'), detail=reason, active=False)


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def last_progress(attempt):
    return max([timestamp(attempt.get(key)) for key in ('last_activity_at', 'started_at', 'ended_at')]
        + [timestamp(row.get('at')) for row in attempt.get('progress', [])]
        + [timestamp(row.get('at')) for row in attempt.get('diagnostics', [])])


def show_input_wait(view):
    """Normalize both fresh and persisted display documents without journal writes."""
    task = view['task']
    if task.get('mode') != 'workflow' or task.get('execution_status') != 'blocked' or task.get('runtime_fault'):
        return
    steps = [step for group in view.get('plan', []) for step in group.get('steps', [])]
    waiting = [step for step in steps if step['status'] == '等你处理']
    if not waiting or any(step['status'] in {'已阻塞', '执行失败', '待核对'} for step in steps):
        return
    task.update(status='等你处理', execution_status='needs_input', controls=['cancel'],
                waiting_node_ids=[step['id'] for step in waiting], waiting_for='user',
                current_activity='等待处理：' + '、'.join(step['name'] for step in waiting),
                reason='；'.join(step.get('status_reason') or step.get('detail', {}).get('goal', '') for step in waiting))
    for group in view.get('plan', []):
        if any(step['status'] == '等你处理' for step in group.get('steps', [])):
            group['status'] = '等你处理'


def observe_view(view, *, live=None, fault=None):
    """Use conversation lifecycle facts; elapsed silence is not a stop event."""
    task = view['task']
    if task.get('purpose') == 'exploration':
        return view
    if not fault:
        show_input_wait(view)
    if task.get('mode') != 'workflow' or task.get('execution_status') not in {'running', 'blocked', 'needs_input'}:
        return view
    fault = fault or task.get('runtime_fault') or {}
    if task.get('execution_status') == 'needs_input' and not fault:
        return view
    known = live is not None and 'scheduler_active' in live
    active = set((live or {}).get('active_node_ids', []))
    scheduler_active = (live or {}).get('scheduler_active', False)
    blocked = {}
    for group in view.get('plan', []):
        for step in group.get('steps', []):
            detail = step.get('detail') or {}
            affected = step['id'] in fault.get('node_ids', [])
            unfinished = step['status'] == '进行中' or detail.get('execution_active')
            # A suspended conversation deliberately releases its execution slot.
            # Its checkpoint remains resumable while the scheduler is alive.
            stopped = known and (not scheduler_active or detail.get('kind') != 'loop'
                                 and step['id'] not in active and not detail.get('execution_suspended'))
            if affected or unfinished and stopped:
                reason = fault.get('reason') if affected else STOPPED_REASON
                step.update(status='已阻塞', status_reason=reason, dialogue_blocked=True)
                detail['activity'] = reason
            if step['status'] == '已阻塞':
                blocked[step['id']] = step
        if group.get('steps') and all(step['status'] != '进行中' for step in group['steps']) and any(
                step['status'] == '已阻塞' for step in group['steps']):
            group['status'] = '已阻塞'
    for node in view.get('structure', {}).get('nodes', []):
        step = blocked.get(node['id'])
        if step:
            node.update(status=step['status'], status_reason=step['status_reason'], activity=step['status_reason'])
    live_executions = (live or {}).get('executions', {}) if scheduler_active else {}
    displayed = {}
    for group in view.get('plan', []):
        for step in group.get('steps', []):
            detail = step.get('detail') or {}
            if step['id'] in blocked:
                execution = execution_fallback('blocked', reason=step.get('status_reason', ''))
            elif step['id'] in active and step['id'] in live_executions:
                execution = dict(live_executions[step['id']])
            else:
                execution = detail.get('execution') or {}
            detail['execution'] = displayed[step['id']] = execution
    for node in view.get('structure', {}).get('nodes', []):
        node['execution'] = displayed.get(node['id'], node.get('execution', {}))
    task['blocked_branches'] = len(blocked)
    if fault or known and (not scheduler_active or blocked and not active and not task.get('automatic_recovery_count')):
        # Losing a live conversation is one failure cause. A persisted blocked
        # run may already have a more specific cause, including required input.
        reason = fault.get('reason') or task.get('reason') or next(
            (step.get('status_reason') for step in blocked.values() if step.get('status_reason')), STOPPED_REASON)
        task.update(status='已阻塞', execution_status='blocked', dialogue_blocked=True,
                    reason=reason, current_activity=reason, controls=['resume', 'cancel'])
    return view


def observe_node_detail(detail, *, live=None, fault=None):
    fault = fault or {}
    known = live is not None and 'scheduler_active' in live
    active = set((live or {}).get('active_attempt_ids', []))
    for attempt in detail['attempts']:
        execution = (live or {}).get('attempt_executions', {}).get(attempt['id'])
        if execution:
            attempt['execution'] = dict(execution)
        if attempt['status'] == 'blocked':
            attempt.update(display_status='blocked', activity_reason=(attempt.get('result') or {}).get('reason') or STOPPED_REASON)
        elif attempt['status'] in {'running', 'suspended'} and detail.get('execution_status') not in {'paused', 'terminated'}:
            stopped = known and (not live.get('scheduler_active') or
                                 attempt['id'] not in active and attempt['status'] != 'suspended')
            if detail['node_id'] in fault.get('node_ids', []) or stopped:
                attempt.update(display_status='blocked', activity_reason=fault.get('reason') or STOPPED_REASON)
    by_id = {attempt['id']: attempt for attempt in detail['attempts']}
    for request in detail.get('requests', []):
        attempt = by_id.get(request.get('attempt_id'), {})
        if attempt.get('display_status'):
            request.update(display_status='blocked', activity_reason=attempt['activity_reason'])
    return detail
