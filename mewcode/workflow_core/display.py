"""Present graph progress separately from follow-up conversation activity."""
from __future__ import annotations


LABELS = {'pending': '未开始', 'running': '进行中', 'completed': '已完成', 'skipped': '已跳过',
          'paused': '已暂停', 'blocked': '已阻塞', 'needs_input': '等你处理', 'unknown': '待核对',
          'failed': '执行失败', 'terminated': '已停止'}
STATES = {label: status for status, label in LABELS.items()}
STATES.update({'部分受阻':'running','恢复中':'running'})


def aggregate(statuses):
    statuses = set(statuses)
    for status in ('running', 'unknown', 'failed', 'blocked', 'needs_input', 'paused', 'terminated'):
        if status in statuses:
            return status
    return 'completed' if statuses and statuses <= {'completed', 'skipped'} else 'pending'


def align(view):
    """Normalize fresh and older cached views without changing execution facts."""
    task = view['task']
    if task.get('purpose') == 'exploration':
        return
    execution = task.get('execution_status')
    steps = {step['id']: step for group in view.get('plan', []) for step in group.get('steps', [])}
    if not steps:
        return
    stopped = execution in {'paused', 'terminated', 'failed', 'unknown', 'blocked'}
    if not stopped:
        for path, step in steps.items():
            if step.get('detail',{}).get('kind') != 'loop' or step['status'] != '进行中':
                continue
            children = [child for key,child in steps.items() if key.startswith(path+'/')]
            if any(child['status'] in {'已阻塞','执行失败','待核对'} for child in children):
                step['status'] = '部分受阻'
            elif any(child.get('detail',{}).get('automatic_continuation') for child in children):
                step['status'] = '恢复中'
    if stopped:
        label = LABELS[execution]
        for step in steps.values():
            if step['status'] == '进行中' or step.get('execution_suspended'):
                step.update(status=label, execution_suspended=True,
                            status_reason=task.get('reason') or '流程已停止推进，已有进度保留。')
                step.setdefault('detail', {})['execution'] = dict(
                    phase=execution, text=label, detail=step['status_reason'], active=False)
    counts = dict(running=0, waiting=0, queued=0)
    for step in steps.values():
        detail = step.get('detail') or {}
        activity = detail.get('node_dialogue')
        if activity is not None:
            for name in counts:
                counts[name] += activity.get(name, 0)
        elif detail.get('kind') != 'loop' and detail.get('execution_active') and step['status'] != '进行中':
            # Older views expose an active reply without its request counters.
            counts['waiting' if detail.get('execution_suspended') else 'running'] += 1
    if stopped:
        counts['running'] = counts['waiting'] = 0
    root = [step for path, step in steps.items() if '/' not in path]
    progress = aggregate(STATES.get(step['status'], 'pending') for step in root)
    if stopped:
        progress = execution
    elif execution == 'needs_input':
        progress = 'needs_input'
    elif execution == 'running' and progress == 'pending':
        progress = 'running'
    task.update(workflow_status=progress, status=LABELS.get(progress, task['status']), node_dialogue=counts)
    task['blocked_branches'] = sum(step['status'] in {'已阻塞','执行失败','待核对'}
        for step in steps.values() if step.get('detail',{}).get('kind') != 'loop')
    task['waiting_branches'] = sum(step['status'] == '等你处理' for step in steps.values())
    task['paused_branches'] = sum(step['status'] == '已暂停' for step in root)
    if progress == 'needs_input':
        task.update(waiting_node_ids=[step['id'] for step in steps.values() if step['status'] == '等你处理'],
                    waiting_for='user')
    replies = counts['running'] + counts['waiting'] + counts['queued'] if execution == 'running' else 0
    if progress == 'running':
        if task.get('automatic_recovery_count'):
            task['status'] = '恢复中'
        elif task['blocked_branches']:
            task['status'] = '部分受阻'
        task['controls'] = ['pause', 'cancel']
        active = [step['name'] for step in root if step['status'] == '进行中']
        activity = '正在进行：' + '、'.join(active[:3]) if active else '正在按依赖执行'
        for name, count in (('分支已暂停', task['paused_branches']),
                            ('分支待处理', task['waiting_branches']),
                            ('节点已阻塞', task.get('blocked_branches', 0))):
            if count:
                activity += f'；{count} 个{name}'
        if task.get('automatic_recovery_count'):
            activity += f'；{task["automatic_recovery_count"]} 条对话等待自动续接'
    else:
        controls = [] if progress in {'completed', 'needs_input', 'terminated'} else ['resume']
        if replies:
            controls.append('pause')
        if progress != 'completed' or replies:
            controls.append('cancel')
        task['controls'] = controls
        reason = next((step.get('status_reason') for step in root
                       if STATES.get(step['status']) == progress and step.get('status_reason')), task.get('reason', ''))
        if reason:
            task['reason'] = reason
        activity = {'completed': '主流程已完成', 'needs_input': '主流程等待你的处理',
                    'paused': '主流程已暂停', 'blocked': '主流程已阻塞', 'failed': '主流程执行失败',
                    'unknown': '主流程等待核对结果', 'terminated': '主流程已停止'}.get(progress, LABELS.get(progress, ''))
        if reason and progress != 'completed':
            activity += '：' + reason
    if replies:
        parts = [f'{counts[name]} 条{label}' for name, label in
                 (('running', '进行中'), ('waiting', '等待回复'), ('queued', '排队')) if counts[name]]
        activity += '；节点对话：' + '，'.join(parts)
    task['current_activity'] = activity
    for group in view.get('plan', []):
        owner = steps.get(group['id'])
        group['status'] = owner['status'] if owner else task['status'] if group['id'] == 'workflow' else LABELS[
            aggregate(STATES.get(step['status'], 'pending') for step in group.get('steps', []))]
    for node in view.get('structure', {}).get('nodes', []):
        step = steps.get(node['id'])
        if step:
            node.update(status=step['status'], status_reason=step.get('status_reason', ''))
            if stopped and step.get('execution_suspended'):
                node['execution'] = dict(step['detail']['execution'])
