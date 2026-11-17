"""Atomic workflow transitions. Attempts and graph revisions are never overwritten."""
from __future__ import annotations

import copy
import time
import uuid

from .schema import digest, descendants, impact, normalize, scope, walk

DONE = {'completed', 'skipped'}
LOOP_LIMIT_REASONS = {'达到设定循环次数', '达到设定循环时限', '达到设定循环 Token 上限'}


def fresh(graph, *, imported=None):
    graph = normalize(graph)
    return dict(format='mewcode.workflow.run.v2', graph=graph, graph_history=[copy.deepcopy(graph)],
                status='pending', nodes={}, attempts={}, commands={}, sequence=0, events=[],
                bindings={}, draft=None, draft_history=[], draft_future=[], pending_apply=None,
                migration=imported or {}, held=[], stop_requested=False)


def event(state, kind, **fields):
    state['sequence'] += 1
    row = dict(sequence=state['sequence'], kind=kind, at=time.time(),
               graph_revision=state['graph']['revision'], **fields)
    state['events'].append(row)
    # Full node transcripts/attempts live separately; this is a bounded control feed.
    state['events'] = state['events'][-200:]
    return row


def current(state, path):
    return state['nodes'].setdefault(path, dict(status='pending', generation=0, attempt_id='', result={}))


def invalidate(state, paths, *, hold=False):
    for path in paths:
        row = current(state, path)
        row.update(status='paused' if hold else 'pending', generation=row['generation'] + 1,
                   previous_attempt_id=row.get('attempt_id', ''), attempt_id='', result={}, reason='')
        row.pop('loop', None)
        row.pop('manual_start', None)
        row.pop('resume_checkpoint', None)
        row.pop('automatic_continuation', None)
        row.pop('published_delivery_id', None)
        row.pop('skip_reason', None)
    if hold:
        state['held'] = sorted(set(state['held']) | set(paths))
    else:
        state['held'] = sorted(set(state['held']) - set(paths))


def start_attempt(state, path, inputs, iteration):
    row = current(state, path)
    identity = uuid.uuid4().hex
    number = sum(attempt['node_id'] == path for attempt in state['attempts'].values()) + 1
    attempt = dict(id=identity, node_id=path, number=number, generation=row['generation'],
                   graph_revision=state['graph']['revision'], iteration=list(iteration),
                   status='running', started_at=time.time(), inputs=copy.deepcopy(inputs),
                   progress=[], diagnostics=[], artifacts=[], result={}, messages=[], pending_tools={})
    state['attempts'][identity] = attempt
    row.update(status='running', attempt_id=identity, reason='')
    from . import dialogue
    dialogue.register(state, attempt)
    dialogue.bind_inputs(state, attempt)
    event(state, 'node_started', node_id=path, attempt_id=identity, iteration=list(iteration))
    return attempt


def finish_attempt(state, identity, result):
    attempt = state['attempts'][identity]
    attempt.update(status=result.get('status', 'completed'), result=copy.deepcopy(result), ended_at=time.time())
    attempt['artifacts'] = copy.deepcopy(result.get('artifacts', attempt.get('artifacts', [])))
    row = state['nodes'].get(attempt['node_id'])
    if row and row['generation'] == attempt['generation'] and row.get('attempt_id') == identity:
        row.update(status=attempt['status'], result=copy.deepcopy(result), reason=result.get('reason', ''))
        from . import dialogue
        dialogue.finish_primary(state, attempt, result)
        event(state, 'node_finished', node_id=attempt['node_id'], attempt_id=identity, status=row['status'])
        return True
    event(state, 'historical_result', node_id=attempt['node_id'], attempt_id=identity)
    return False


def retire_loop_limit_pauses(state):
    """Resume obsolete resource stops when an authorized scheduler is attached."""
    if state.get('dialogue_only') or state.get('stop_requested'):
        return
    for path, node in walk(state['graph']):
        row = state['nodes'].get(path, {})
        loop = row.get('loop') or {}
        if (node['kind'] != 'loop' or row.get('status') != 'paused' or path in state['held']
                or not (loop.get('limit_pause') or row.get('reason') in LOOP_LIMIT_REASONS)):
            continue
        old = copy.deepcopy(loop.pop('limit_pause', None))
        previous = state['attempts'].get(row.get('attempt_id'), {})
        row.update(previous_attempt_id=row.get('attempt_id', ''), resume_checkpoint=True)
        start_attempt(state, path, loop.get('inputs') or {}, previous.get('iteration') or [])
        event(state, 'loop_limit_retired', node_id=path, previous_limit=old)


def edit(state, operation, *, expected_revision=None):
    from .trace import executable
    executable(state)
    graph = copy.deepcopy(state.get('draft') or state['graph'])
    if expected_revision is not None and expected_revision != graph['revision']:
        raise ValueError('草稿版本已变化，请刷新后重试')
    action = operation.get('action')
    if action in {'undo', 'redo'}:
        source, target = ('draft_history', 'draft_future') if action == 'undo' else ('draft_future', 'draft_history')
        if not state[source]:
            raise ValueError('没有可以撤销或重做的编辑')
        state[target].append(graph)
        restored = state[source].pop()
        restored['revision'] = graph['revision'] + 1
        state['draft'] = restored
    else:
        operations = operation.get('operations', []) if action == 'batch' else [operation]
        if not operations:
            raise ValueError('编辑操作不能为空')
        for op in operations:
            part = scope(graph, op.get('scope', ''))
            kind, verb = op.get('kind'), op.get('action')
            if verb == 'positions':
                positions = op.get('positions', {})
                import math
                for key, point in positions.items():
                    if key not in {n['id'] for n in part['nodes']} or set(point) != {'x', 'y'} or any(
                            type(v) not in {int, float} or not math.isfinite(v) for v in point.values()):
                        raise ValueError('节点位置无效')
                if positions:
                    part['layout'].update(copy.deepcopy(positions))
                else:
                    part['layout'] = {}
                continue
            if kind not in {'node', 'edge'}:
                raise ValueError('只能编辑节点或连线')
            bucket = part['nodes' if kind == 'node' else 'edges']
            identity = op.get('id') or ('n_' if kind == 'node' else 'e_') + uuid.uuid4().hex[:12]
            existing = next((row for row in bucket if row['id'] == identity), None)
            if verb == 'delete':
                if not existing:
                    raise ValueError('节点或连线不存在')
                bucket.remove(existing)
                if kind == 'node':
                    part['edges'] = [edge for edge in part['edges'] if identity not in (edge['source'], edge['target'])]
                    part['layout'].pop(identity, None)
            elif verb in {'upsert', 'insert'}:
                fields = copy.deepcopy(op.get('fields') or {})
                data = fields.pop('data', {})
                if kind == 'node':
                    fields = {**fields, **data}
                    for source, destination in {'title': 'label', 'body': 'task', 'node_kind': 'kind', 'acceptance': 'criteria'}.items():
                        if source in fields:
                            fields[destination] = fields.pop(source)
                    if 'criteria' in fields:
                        fields.setdefault('config', {})['criteria'] = fields.pop('criteria')
                else:
                    fields.update(data)
                    for source, destination in {'from_node': 'source', 'to_node': 'target', 'condition': 'port'}.items():
                        if source in fields:
                            fields[destination] = fields.pop(source)
                allowed = ({'label', 'task', 'kind', 'config', 'inputs', 'outputs', 'model', 'tools', 'deny_tools'}
                           if kind == 'node' else {'source', 'target', 'port', 'label'})
                if set(fields) - allowed:
                    raise ValueError('不支持的组件属性：' + ', '.join(sorted(set(fields) - allowed)))
                if existing:
                    existing.update(fields)
                else:
                    bucket.append(dict(id=identity, **fields))
                if verb == 'insert' and kind == 'node':
                    edge = next((e for e in part['edges'] if e['id'] == op.get('edge_id')), None)
                    if not edge:
                        raise ValueError('待插入的连线不存在')
                    target = edge['target']
                    edge['target'] = identity
                    part['edges'].append(dict(id='e_' + uuid.uuid4().hex[:12], source=identity, target=target,
                                              port=op.get('out_port', 'always')))
            else:
                raise ValueError('不支持的编辑操作')
        graph['revision'] += 1
        # A disconnected/incomplete loop is a valid draft. Runtime validates its required inputs.
        graph = normalize(graph)
        state['draft_history'].append(copy.deepcopy(state.get('draft') or state['graph']))
        state['draft_history'] = state['draft_history'][-100:]
        state['draft_future'] = []
        state['draft'] = graph
    return {'revision': state['draft']['revision'], 'impact': impact(state['graph'], state['draft'])}


def command(state, body):
    from .trace import executable
    executable(state)
    request = body.get('request_id')
    if not isinstance(request, str) or not request or len(request) > 160:
        raise ValueError('控制操作需要 request_id')
    fingerprint = digest({key: value for key, value in body.items() if key != 'expected_sequence'})
    prior = state['commands'].get(request)
    if prior:
        if prior['fingerprint'] != fingerprint:
            raise ValueError('同一控制请求不能使用不同内容')
        return copy.deepcopy(prior['result'])
    operation = body.get('operation')
    if operation in {'resume', 'rerun', 'continue_from', 'apply'}:
        state.pop('dialogue_only', None)
        state.pop('runtime_fault', None)
    if operation != 'apply' and body.get('expected_revision') is not None and body['expected_revision'] != state['graph']['revision']:
        raise ValueError('执行图版本已变化，请刷新后重试')
    path = body.get('node_id', '')
    nodes = dict(walk(state['graph']))
    if path and path not in nodes:
        raise ValueError('节点不存在')
    result = {'operation': operation, 'node_id': path}
    if operation == 'apply':
        if not state.get('draft'):
            result.update(applied=True, impact={'affected': [], 'retained': sorted(nodes), 'removed': []})
        else:
            if body.get('expected_revision') != state['draft']['revision']:
                raise ValueError('草稿已更新，请刷新后应用')
            if state.get('pending_apply'):
                raise ValueError('上一次图更新仍在应用')
            changes = impact(state['graph'], state['draft'])
            state['pending_apply'] = dict(graph=copy.deepcopy(state['draft']), impact=changes, request_id=request)
            result.update(applied=False, switching=True, impact=changes)
            state['status'] = 'running'
    elif operation in {'pause', 'stop'}:
        if path:
            targets = {key for key in nodes if key == path or key.startswith(path + '/')}
            for key in targets:
                row = current(state, key)
                if row['status'] not in DONE:
                    row['status'] = 'paused'
            state['held'] = sorted(set(state['held']) | targets)
        else:
            state['status'] = 'paused' if operation == 'pause' else 'terminated'
            state['stop_requested'] = True
    elif operation == 'resume':
        state['stop_requested'] = False
        state['status'] = 'running'
        targets = [key for key in state['nodes'] if not path or key == path or key.startswith(path + '/')]
        for key in targets:
            row = current(state, key)
            if (row['status'] == 'needs_input' and nodes[key]['kind'] == 'judge'
                    and nodes[key]['config'].get('manual')):
                continue
            if row['status'] not in DONE | {'running'}:
                loop = row.get('loop')
                if loop and (loop.get('limit_pause') or row.get('reason') in LOOP_LIMIT_REASONS):
                    # Keep old usage baselines as history; resource counters
                    # no longer stop subsequent rounds.
                    loop['limit_baseline'] = {name: loop.get(counter, 0) for name, counter in
                                              [('iterations', 'round'), ('seconds', 'elapsed'), ('tokens', 'tokens')]}
                    loop.pop('limit_pause', None)
                    event(state, 'loop_limit_resumed', node_id=key, baseline=copy.deepcopy(loop['limit_baseline']))
                row['previous_attempt_id'] = row.get('attempt_id', '')
                row['resume_checkpoint'] = True
                row['status'] = 'running' if loop else 'pending'
                row.pop('reason', None)
                previous = state['attempts'].get(row.get('attempt_id'), {})
                if loop and previous.get('status') != 'running':
                    start_attempt(state, key, loop['inputs'], previous.get('iteration', []))
        state['held'] = [key for key in state['held'] if key not in targets]
        for node_request in state.get('node_requests', {}).values():
            if node_request['status'] == 'paused' and (not path or node_request['node_id'] == path or node_request['node_id'].startswith(path + '/')):
                node_request['status'] = 'waiting'
                state['attempts'][node_request['attempt_id']]['status'] = 'suspended'
    elif operation == 'judge_answer':
        if not path or nodes[path]['kind'] != 'judge' or not nodes[path]['config'].get('manual'):
            raise ValueError('请选择人工判断组件')
        if not isinstance(body.get('value'), (bool, str)):
            raise ValueError('判断需要布尔值或命名结果')
        attempt = start_attempt(state, path, state['bindings'].get(path, {}),
                                state['attempts'].get(current(state, path).get('attempt_id'), {}).get('iteration', []))
        finish_attempt(state, attempt['id'], dict(status='completed', value=body['value'],
                       text='人工判断：' + str(body['value']), artifacts=[]))
        state['held'] = [key for key in state['held'] if key != path]
        state.update(status='running', stop_requested=False)
    elif operation in {'rerun', 'continue_from', 'skip', 'adopt', 'bind_inputs'}:
        if not path:
            raise ValueError('请选择节点')
        affected = manual_scope(state['graph'], path)
        result['affected'] = sorted(affected)
        if operation == 'bind_inputs':
            bindings = body.get('inputs')
            if not isinstance(bindings, dict):
                raise ValueError('输入必须是名称到值或文件引用的映射')
            state['bindings'].setdefault(path, {}).update(copy.deepcopy(bindings))
            if current(state, path)['status'] == 'needs_input':
                current(state, path)['status'] = 'pending'
        elif operation == 'adopt':
            chosen = state['attempts'].get(body.get('attempt_id'))
            if not chosen or chosen['node_id'] != path or chosen['status'] != 'completed' or chosen.get('request_id'):
                raise ValueError('只能采用该节点已经交付的历史结果')
            invalidate(state, affected)
            current(state, path).update(status='completed', attempt_id=chosen['id'],
                                        result=copy.deepcopy(chosen['result']), adopted=True,
                                        published_delivery_id=chosen.get('delivery_id', ''))
        elif operation == 'skip':
            invalidate(state, affected)
            current(state, path).update(status='skipped', skip_reason='user',
                result=copy.deepcopy(body.get('result') or body.get('inputs') or state['bindings'].get(path) or {}))
            state['bindings'].setdefault(path, {}).update(copy.deepcopy(body.get('inputs') or {}))
        else:
            invalidate(state, affected - {path}, hold=operation == 'rerun')
            invalidate(state, {path})
            state['bindings'].setdefault(path, {}).update(copy.deepcopy(body.get('inputs') or {}))
            current(state, path)['manual_start'] = True
            # Unfinished ancestors must not execute again when starting from a chosen point.
            part_path, _, local_id = path.rpartition('/')
            part = scope(state['graph'], part_path)
            ancestors, queue = set(), [local_id]
            while queue:
                key = queue.pop()
                for edge in part['edges']:
                    if edge['target'] == key and edge['source'] not in ancestors:
                        ancestors.add(edge['source'])
                        queue.append(edge['source'])
            prefix = part_path + '/' if part_path else ''
            for ancestor in ancestors:
                row = current(state, prefix + ancestor)
                if row['status'] not in DONE:
                    invalidate(state, {prefix + ancestor})
                    current(state, prefix + ancestor).update(status='skipped', skip_reason='user', result={})
        if '/' in path:
            pieces = path.split('/')
            for index in range(1, len(pieces)):
                parent = current(state, '/'.join(pieces[:index]))
                loop = parent.get('loop')
                if loop and not loop['active']:
                    loop['round'] = max(0, loop['round'] - 1)
                    if loop['results']:
                        loop['results'].pop()
                    loop.update(active=True, round_started_at=time.time())
                if loop and parent['status'] != 'running':
                    previous = state['attempts'].get(parent.get('attempt_id'), {})
                    start_attempt(state, '/'.join(pieces[:index]), loop['inputs'], previous.get('iteration', []))
                parent['status'] = 'running' if loop else 'pending'
        state['status'] = 'running'
        state['stop_requested'] = False
    else:
        raise ValueError('未知运行操作')
    state['commands'][request] = dict(fingerprint=fingerprint, result=copy.deepcopy(result))
    event(state, 'control', operation=operation, node_id=path, request_id=request)
    return result


def manual_scope(graph, path):
    """Forward slice without resetting the containing loop's item or its siblings."""
    parent, _, local_id = path.rpartition('/')
    part = scope(graph, parent)
    result = descendants(part, {local_id})
    prefix = parent + '/' if parent else ''
    result = {prefix + key for key in result}
    while parent:
        outer, _, loop_id = parent.rpartition('/')
        part = scope(graph, outer)
        following = {edge['target'] for edge in part['edges'] if edge['source'] == loop_id}
        result.update((outer + '/' if outer else '') + key for key in descendants(part, following))
        parent = outer
    return result


def apply_pending(state):
    pending = state.get('pending_apply')
    if not pending:
        return
    changes = pending['impact']
    unresolved = {path for path in changes['affected'] if any(
        t.get('status') in {'running', 'unknown', 'outcome_unknown'}
        for t in state['attempts'].get(state['nodes'].get(path, {}).get('attempt_id'), {}).get('pending_tools', {}).values())}
    invalidate(state, changes['affected'])
    for path in unresolved:
        current(state, path).update(resume_checkpoint=True)
    state['graph'] = pending['graph']
    state['graph_history'].append(copy.deepcopy(state['graph']))
    if (state.get('draft') or {}).get('revision') == state['graph']['revision']:
        state['draft'] = None
        state['draft_history'] = []
        state['draft_future'] = []
    state['pending_apply'] = None
    state['stop_requested'] = False
    result = state['commands'][pending['request_id']]['result']
    result.update(applied=True, switching=False)
    event(state, 'graph_applied', affected=changes['affected'])
