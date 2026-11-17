"""User-facing progress, not a projection of every tool call."""
from __future__ import annotations

import copy

from .schema import digest, impact, walk
from .activity import execution_fallback, last_progress, timestamp, show_input_wait
from .display import LABELS
from .continuation import pending as pending_continuations
from .presentation import visible as presentation_blocks


def view(record, *, design=None):
    from mewcode.orchestration.task_view import identity, finish
    state = record['workflow_core']
    from .trace import is_trace
    tracing = is_trace(state)
    graph = state.get('draft') or state['graph']
    status = state['status']
    recovering = pending_continuations(state)
    task = dict(id=identity(record, design), run_id=record['run_id'], conversation_id=record.get('conversation_id', ''),
                mode='workflow', title=graph['label'], goal=graph['goal'], status=LABELS.get(status, status),
                execution_status=status, plan_version=state['graph']['revision'], spec_version=graph['revision'],
                controls=['pause', 'cancel'] if status == 'running' else ['resume'], readonly=False,
                updated_at=record.get('updated_at'), kernel_revision=record.get('kernel_revision', 0),
                run_attempt=record.get('attempt', 1), reason=record.get('status_reason', ''),
                current_activity='正在按依赖执行' if status == 'running' else LABELS.get(status, status),
                last_activity_at=max([timestamp(e.get('at')) for e in state.get('events', [])
                                      if e['kind'] in {'node_started', 'node_finished', 'node_reply'}] +
                                     [last_progress(a) for a in state['attempts'].values()] + [0]),
                runtime_fault=copy.deepcopy(state.get('runtime_fault')))
    task['automatic_recovery_count'] = len(recovering)
    if record.get('chat_origin') and not tracing and graph.get('execution_profile') == 'goal.chat.v1':
        task['purpose'] = 'delegation'
    if tracing:
        task.update(purpose='exploration', controls=[], readonly=True, reason='',
                    status='已完成' if status == 'completed' else '探索中',
                    current_activity='任务已交付，可提取复用' if status == 'completed' else '由主对话推进；图记录公开步骤与结果',
                    runtime_fault=None)
    if state.get('solidified_recipe'):
        task['solidified_recipe'] = copy.deepcopy(state['solidified_recipe'])
    groups, nodes, edges, deliverables = {}, [], [], []
    for path, node in walk(graph):
        row = state['nodes'].get(path) or {}
        attempt = state['attempts'].get(row.get('attempt_id')) or {}
        delivery = state.get('deliveries', {}).get(row.get('published_delivery_id'), {})
        artifacts = copy.deepcopy((delivery.get('result') or {}).get('artifacts') or attempt.get('artifacts') or row.get('result', {}).get('artifacts') or [])
        requests = [r for r in state.get('node_requests', {}).values()
                    if r['node_id'] == path and r['status'] in {'queued', 'running', 'waiting'}]
        active_replies = [state['attempts'].get(r.get('attempt_id'), {}) for r in requests
                          if r['status'] in {'running', 'waiting'}]
        activity = ('正在处理 ' + str(len(requests)) + ' 条追问／补交') if requests else ''
        if attempt.get('status') == 'suspended' and attempt.get('waiting_on'):
            activity = '等待节点回复，已保存进度'
        if attempt.get('status') == 'suspended' and attempt.get('waiting_tasks'):
            activity = f'等待 {len(attempt["waiting_tasks"])} 个工作项，已保存进度并释放执行名额'
        if attempt.get('report'):
            artifacts.append(dict(attempt['report'], kind='report'))
        progress = copy.deepcopy(attempt.get('progress') or [])[-5:]
        if tracing:
            progress = [dict(text=entry['text'], at=entry['at']) for entry in state.get('trace_observations', [])
                        if entry['node_id'] == path and entry.get('text')][-5:]
        displayed_attempt = active_replies[0] if active_replies else attempt
        execution = execution_fallback('running' if active_replies else row.get('status', 'pending'),
                                       reason=row.get('reason', ''), attempt=displayed_attempt,
                                       run_status=status, activity=activity)
        if tracing:
            execution = dict(phase='recorded', active=False,
                text='记录：' + LABELS.get(row.get('status', 'pending'), '未开始'),
                detail=row.get('reason', ''))
        parent, _, local_id = path.rpartition('/')
        detail = dict(kind=node['kind'], goal=node['task'], inputs=node['inputs'], outputs=node['outputs'],
                      requirements=node['config'].get('criteria', []), constraints=[], checks=[],
                      artifacts=artifacts, config=copy.deepcopy(node['config']), model=node.get('model'),
                      progress=progress, final_text=row.get('result', {}).get('text', ''),
                      attempt_id=attempt.get('id'), iteration=row.get('iteration') or attempt.get('iteration') or [], activity=activity,
                      delivery_version=delivery.get('number'), execution=execution,
                      last_activity_at=max([last_progress(attempt)] + [last_progress(a) for a in active_replies]),
                      execution_suspended=displayed_attempt.get('status') == 'suspended',
                      execution_active=not tracing and (row.get('status') == 'running' or bool(active_replies)))
        detail['node_dialogue'] = {name: sum(request['status'] == name for request in requests)
                                   for name in ('running', 'waiting', 'queued')}
        blocks = presentation_blocks(state, displayed_attempt)
        if tracing:
            blocks = copy.deepcopy(list(state.get('trace_blocks', {}).get(path, {}).values()))
        if blocks:
            detail['presentation_blocks'] = blocks
        recovery = next((ticket for ticket in recovering if ticket['node_id'] == path), None)
        if recovery:
            detail['automatic_continuation'] = copy.deepcopy(recovery)
            detail['execution'] = execution = dict(phase='retrying',active=False,
                text='对话已中断，等待自动续接', detail='沿用原检查点、模型和工具回执；上下文不足时用新上下文接续。')
        step = dict(id=path, name=node['label'], status=LABELS.get(row.get('status', 'pending'), '未开始'),
                    status_reason=row.get('reason', ''), attempts=sum(a['node_id'] == path for a in state['attempts'].values()),
                    acceptance=None, detail=detail, detail_ref=dict(run_id=record['run_id'], node_id=path))
        if recovery and row.get('status') in {'blocked','failed'}:
            step['status'] = '恢复中'
        groups.setdefault(parent, []).append(step)
        nodes.append(dict(id=path, local_id=local_id, scope=parent, label=node['label'], kind=node['kind'],
                          goal=node['task'], status=step['status'], status_reason=step['status_reason'],
                          progress=progress, artifacts=artifacts, config=copy.deepcopy(node['config']),
                          milestone_id=parent or 'workflow', iteration=detail['iteration'], activity=activity,
                          delivery_version=delivery.get('number'), execution=copy.deepcopy(execution)))
        if blocks:
            nodes[-1]['presentation_blocks'] = blocks
        for artifact in artifacts:
            deliverables.append(dict(artifact, node_id=path, file_id=artifact.get('id'),
                                     status='已产出', reason='', kind=artifact.get('kind', 'file')))
    def collect(part, prefix=''):
        for edge in part['edges']:
            selection = ''
            source = state['nodes'].get(prefix + edge['source'], {})
            if tracing and source.get('status') == 'completed':
                selected = source.get('result', {}).get('value')
                selected = 'pass' if selected is True else 'fail' if selected is False else selected
                if edge['port'] != 'always' and selected is not None:
                    selection = 'selected' if edge['port'] == selected else 'not_selected'
            edges.append(dict(id=prefix + edge['id'], local_id=edge['id'], scope=prefix.rstrip('/'),
                              **{'from': prefix + edge['source'], 'to': prefix + edge['target']},
                              condition=edge['port'], **({'selection': selection} if selection else {})))
        for node in part['nodes']:
            if node['kind'] == 'loop':
                collect(node['config']['graph'], prefix + node['id'] + '/')
    collect(graph)
    plans = [dict(id=parent or 'workflow', title='循环体：' + parent if parent else '任务计划',
                  status=task['status'], steps=steps) for parent, steps in groups.items()]
    editor = dict(editable=True, basis=digest(graph), revision=graph['revision'], applied_revision=state['graph']['revision'],
                  has_draft=bool(state.get('draft')), switching=bool(state.get('pending_apply')),
                  can_undo=bool(state['draft_history']), can_redo=bool(state['draft_future']),
                  positions=copy.deepcopy(graph['layout']),
                  impact=impact(state['graph'], graph) if state.get('draft') else None)
    if tracing:
        editor.update(editable=False, has_draft=False, can_undo=False, can_redo=False)
    for path, node in walk(graph):
        if node['kind'] == 'loop':
            editor['positions'].update({path + '/' + key: point for key, point in node['config']['graph']['layout'].items()})
    result = dict(schema_version=2, task=task, legacy=False, legacy_message='', inbox=[], requirements=[],
                  plan=plans, editor=editor, deliverables=deliverables,
                  changes=[dict(id=str(e['sequence']), summary=e['kind'], **e)
                           for e in state['events'] if e['kind'] in {'graph_applied', 'control'}],
                  structure=dict(nodes=nodes, edges=edges, groups=[dict(id=g['id'], label=g['title']) for g in plans],
                                 versions=[], workflow=True), workflow_revision=state['graph']['revision'])
    show_input_wait(result)
    return finish(result)


def node_detail(record, path):
    state = record['workflow_core']
    graph_nodes = dict(walk(state['graph']))
    if path not in graph_nodes and not any(a['node_id'] == path for a in state['attempts'].values()):
        raise ValueError('节点不存在')
    from .trace import is_trace
    if is_trace(state):
        return dict(node_id=path, label=graph_nodes[path]['label'], goal=graph_nodes[path]['task'],
            purpose='exploration', attempts=[], requests=[], deliveries=[], current_attempt_id='',
            observations=[copy.deepcopy(row) for row in state.get('trace_observations', []) if row['node_id'] == path],
            presentation_blocks=copy.deepcopy(list(state.get('trace_blocks', {}).get(path, {}).values())),
            capabilities=dict(node_messages=False, delivery_revisions=False))
    attempts = []
    for attempt in state['attempts'].values():
        if attempt['node_id'] != path:
            continue
        attempts.append({key: copy.deepcopy(attempt.get(key)) for key in (
            'id', 'number', 'graph_revision', 'iteration', 'status', 'started_at', 'ended_at', 'last_activity_at',
                            'progress', 'diagnostics', 'artifacts', 'result', 'report', 'report_error',
                            'kind', 'request_id', 'session_id', 'delivery_id', 'input_versions', 'execution', 'waiting_on',
                            'phase_events', 'presentation_blocks', 'waiting_tasks')})
        public = attempts[-1]
        # Checkpoints inherit previous tool context. Only expose calls actually
        # made in this attempt, plus its currently executing call.
        tool_ids = {row.get('tool_id') for row in attempt.get('diagnostics', []) if row.get('tool_id')}
        tool_ids.add((attempt.get('execution') or {}).get('tool_id'))
        public['tools'] = {identity: copy.deepcopy(tool) for identity, tool in attempt.get('pending_tools', {}).items()
                           if identity in tool_ids or tool.get('queued_at', 0) >= attempt['started_at']}
        waiting = state.get('node_requests', {}).get(attempt.get('waiting_on'))
        if waiting:
            public['waiting_request'] = {key: copy.deepcopy(waiting.get(key)) for key in
                                        ('id', 'node_id', 'kind', 'status', 'message')}
            public['waiting_request']['label'] = graph_nodes.get(waiting['node_id'], {}).get('label', waiting['node_id'])
    from .dialogue import request_view
    requests = [request_view(state, request) for request in state.get('node_requests', {}).values()
                if request['node_id'] == path or request['sender_node_id'] == path]
    deliveries = [copy.deepcopy(value) for value in state.get('deliveries', {}).values() if value['node_id'] == path]
    return dict(node_id=path, label=graph_nodes.get(path, {}).get('label', path),
        goal=graph_nodes.get(path, {}).get('task', ''), execution_status=state['status'],
        current_attempt_id=state['nodes'].get(path, {}).get('attempt_id'), attempts=attempts,
        presentation_blocks=presentation_blocks(state, state['attempts'].get(
            state['nodes'].get(path, {}).get('attempt_id'), {})),
        requests=requests, deliveries=deliveries,
        published_delivery_id=state['nodes'].get(path, {}).get('published_delivery_id', ''),
        capabilities=dict(node_messages=True, delivery_revisions=True))
