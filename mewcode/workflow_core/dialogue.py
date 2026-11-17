"""Durable node conversations and deliveries, independent of graph invalidation.

All functions operate inside the run journal transaction. A request is another
turn in the target node instance, never a rewind of its downstream graph.
"""
from __future__ import annotations

import copy
import time
import uuid

from .schema import digest, walk

TERMINAL = {'completed', 'failed', 'cancelled'}


def ensure(state):
    for key in ('node_sessions', 'node_requests', 'deliveries'):
        state.setdefault(key, {})


def register(state, attempt, node=None):
    ensure(state)
    identity = attempt.get('session_id') or 'ns_' + digest([
        attempt['node_id'], attempt.get('generation', 0), attempt.get('iteration', [])])[:24]
    attempt['session_id'] = identity
    if identity not in state['node_sessions']:
        node = node or dict(walk(state['graph'])).get(attempt['node_id']) or {}
        state['node_sessions'][identity] = dict(id=identity, node_id=attempt['node_id'],
            generation=attempt.get('generation', 0), iteration=copy.deepcopy(attempt.get('iteration', [])),
            graph_revision=attempt['graph_revision'], node=copy.deepcopy(node),
            head_attempt_id=attempt['id'], primary_attempt_id=attempt['id'],
            published_delivery_id='', notifications=[], seen_notifications=[], turns=[])
    session = state['node_sessions'][identity]
    if attempt['id'] not in session['turns']:
        session['turns'].append(attempt['id'])
    if not attempt.get('request_id'):
        session['primary_attempt_id'] = attempt['id']
    return session


def checkpoint(state, attempt):
    session = register(state, attempt)
    if attempt.get('messages'):
        session['head_attempt_id'] = attempt['id']


def bootstrap(state):
    ensure(state)
    # Add metadata to old records lazily; never discard their transcripts/results.
    for attempt in list(state['attempts'].values()):
        if attempt.get('session_id'):
            continue
        session = register(state, attempt)
        if attempt.get('messages'):
            session['head_attempt_id'] = attempt['id']
        if attempt.get('status') == 'completed' and not attempt.get('request_id'):
            publish(state, attempt, attempt.get('result') or {}, notify=False)


def frozen_result(delivery):
    """Consumers receive immutable file paths, while provenance keeps originals."""
    result = copy.deepcopy(delivery.get('result') or {})
    replacements = {}
    for item in result.get('artifacts', []):
        if item.get('stored_path'):
            original = item.get('path', '')
            item.setdefault('source_path', original)
            item['path'] = item['stored_path']
            if original:
                replacements[original] = item['stored_path']
    def rewrite(value):
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: item if key in {'source_path', 'stored_path'} else rewrite(item)
                    for key, item in value.items()}
        return replacements.get(value, value) if isinstance(value, str) else value
    result = rewrite(result)
    result['delivery_id'] = delivery['id']
    result['delivery_version'] = delivery['number']
    return result


def bind_inputs(state, attempt):
    """Freeze each incoming producer revision at the start of an execution."""
    versions = attempt.setdefault('input_versions', {})
    for key, value in list(attempt['inputs'].items()):
        if not isinstance(value, dict):
            continue
        identity = value.get('delivery_id')
        if identity in state.get('deliveries', {}):
            versions[key] = identity
            attempt['inputs'][key] = frozen_result(state['deliveries'][identity])


def _artifact_key(item):
    return item.get('logical_path') or item.get('source_path') or item.get('path') or item['id']


def _notify(session, notification):
    if not any(row['id'] == notification['id'] for row in session['notifications']):
        session['notifications'].append(copy.deepcopy(notification))


def publish(state, attempt, result, *, notify=True):
    session = register(state, attempt)
    if attempt.get('delivery_id'):
        return state['deliveries'][attempt['delivery_id']]
    request = state['node_requests'].get(attempt.get('request_id'), {})
    parent_id = session['published_delivery_id']
    parent = state['deliveries'].get(parent_id, {})
    merged = copy.deepcopy(parent.get('result') or {}) if request else {}
    merged.update(copy.deepcopy(result))
    artifacts = {_artifact_key(item): copy.deepcopy(item)
                 for item in (parent.get('result') or {}).get('artifacts', [])} if request else {}
    changed = []
    for item in result.get('artifacts', []):
        key = _artifact_key(item)
        if not artifacts.get(key) or artifacts[key].get('sha256') != item.get('sha256'):
            changed.append(key)
        artifacts[key] = copy.deepcopy(item)
    merged['artifacts'] = list(artifacts.values())
    identity = 'delivery_' + attempt['id']
    number = int(parent.get('number', 0)) + 1
    merged.update(delivery_id=identity, delivery_version=number)
    delivery = dict(id=identity, number=number, session_id=session['id'], node_id=attempt['node_id'],
        attempt_id=attempt['id'], parent_id=parent_id, request_id=request.get('id', ''),
        result=merged, changed_files=changed, references=copy.deepcopy(request.get('references') or []),
        input_versions=copy.deepcopy(attempt.get('input_versions') or {}),
        unresolved_requests=[r['id'] for r in state['node_requests'].values()
                             if r['sender_session_id'] == session['id'] and r['status'] != 'completed'],
        created_at=time.time())
    state['deliveries'][identity] = delivery
    session['published_delivery_id'] = identity
    attempt['delivery_id'] = identity
    primary = state['attempts'].get(session['primary_attempt_id'], {})
    if request and primary.get('status') in {'running', 'suspended'}:
        primary['artifacts'] = copy.deepcopy(merged['artifacts'])
        primary.setdefault('inputs', {})['node_revision'] = frozen_result(delivery)
    row = state['nodes'].get(attempt['node_id'], {})
    current = state['attempts'].get(row.get('attempt_id'), {})
    if current.get('session_id') == session['id']:
        row['published_delivery_id'] = identity
        if row['status'] == 'completed':
            row['result'] = copy.deepcopy(merged)
    if not notify or not parent_id:
        return delivery
    # Only consumers that actually bound this producer instance are notified.
    for consumer in list(state['node_sessions'].values()):
        primary = state['attempts'].get(consumer['primary_attempt_id'], {})
        bound = primary.get('input_versions', {})
        sources = [key for key, old in bound.items()
                   if state['deliveries'].get(old, {}).get('session_id') == session['id'] and old != identity]
        if not sources:
            continue
        _notify(consumer, dict(id='revision:' + identity, kind='revision', delivery_id=identity,
            node_id=attempt['node_id'], input_keys=sources, changed_files=changed,
            references=delivery['references'], request_id=request.get('id', ''),
            text='上游已补交新版本。继续不受影响的工作，在检查点处理与当前成果有关的变化。'))
        # Completed consumers can make a local amendment without resetting the DAG.
        # An update that only explains "unaffected" does not publish another version.
        sender_active = (consumer['id'] == request.get('sender_session_id') and
                         state['attempts'].get(request.get('sender_attempt_id'), {}).get('status') in {'running', 'suspended'})
        if primary.get('status') == 'completed' and not sender_active:
            enqueue(state, dict(node_id=consumer['node_id'], target_attempt_id=primary['id'],
                kind='update', message='上游交付已修订。检查既有交付中受影响的部分，复用其余成果；'
                    '需要修改时补交文件，不受影响时直接说明。\n' + str(delivery['references'] or changed),
                references=delivery['references'], request_id='update:' + identity + ':' + consumer['id'],
                parent_request_id=request.get('id', '')), sender=None, internal=True)
    return delivery


def finish_primary(state, attempt, result):
    checkpoint(state, attempt)
    if result.get('status', 'completed') == 'completed':
        delivery = publish(state, attempt, result)
        attempt['result'] = copy.deepcopy(delivery['result'])


def enqueue(state, body, *, sender=None, internal=False):
    bootstrap(state)
    node_id = str(body.get('node_id') or '')
    kind = body.get('kind', 'ask')
    if kind not in ({'ask', 'revise', 'update'} if internal else {'ask', 'revise'}):
        raise ValueError('请选择追问或补交')
    message = str(body.get('message') or '').strip()
    if not message:
        raise ValueError('请填写消息内容')
    row = state['nodes'].get(node_id, {})
    prior = state['node_requests'].get(str(body.get('request_id') or ''), {})
    previous_session = state['node_sessions'].get(prior.get('target_session_id'), {})
    anchor = state['attempts'].get(body.get('target_attempt_id') or previous_session.get('primary_attempt_id') or row.get('attempt_id'), {})
    if not anchor or anchor.get('node_id') != node_id:
        raise ValueError('请选择已有执行上下文的节点或历史执行记录')
    target = register(state, anchor)
    references = body.get('references') or []
    if not isinstance(references, list) or not all(isinstance(item, str) for item in references):
        raise ValueError('引用应是文件、页码或记录标识组成的数组')
    base = body.get('base_delivery_id') or (prior['base_delivery_id'] if prior else target['published_delivery_id'])
    if base and state['deliveries'].get(base, {}).get('session_id') != target['id']:
        raise ValueError('所选交付不属于目标节点的这次执行')
    sender_session = register(state, sender)['id'] if sender else ''
    if sender_session == target['id']:
        raise ValueError('本节点的问题请直接在当前上下文处理，无需向自己发请求')
    parent = str(body.get('parent_request_id') or '')
    if parent and parent not in state['node_requests']:
        raise ValueError('原消息不存在')
    data = dict(node_id=node_id, target_session_id=target['id'], kind=kind, message=message,
        references=copy.deepcopy(references), base_delivery_id=base, parent_request_id=parent,
        sender_session_id=sender_session, sender_node_id=sender.get('node_id', '') if sender else '',
        sender_attempt_id=sender.get('id', '') if sender else '', sender_kind='node' if sender else 'system' if internal else 'user')
    fingerprint = digest(data)
    identity = str(body.get('request_id') or ('node:' + fingerprint if sender else uuid.uuid4().hex))
    if len(identity) > 240:
        raise ValueError('消息标识过长')
    if identity in state['node_requests']:
        old = state['node_requests'][identity]
        if old['fingerprint'] != fingerprint:
            raise ValueError('同一消息标识不能用于不同内容')
        return copy.deepcopy(old)
    request = dict(id=identity, fingerprint=fingerprint, **data, status='queued',
        created_at=time.time(), attempt_id='', reply={}, delivery_id='')
    state['node_requests'][identity] = request
    if state.get('stop_requested'):
        state['dialogue_only'] = True
    state['status'] = 'running'
    from .state import event
    event(state, 'node_message', node_id=node_id, request_id=identity, sender_node_id=data['sender_node_id'])
    return copy.deepcopy(request)


def start_request(state, request):
    session = state['node_sessions'][request['target_session_id']]
    previous = state['attempts'][session['head_attempt_id']]
    primary = state['attempts'][session['primary_attempt_id']]
    delivery_id = session['published_delivery_id']
    identity = uuid.uuid4().hex
    attempt = dict(id=identity, node_id=session['node_id'], session_id=session['id'],
        number=1 + sum(a['node_id'] == session['node_id'] for a in state['attempts'].values()),
        generation=session['generation'], graph_revision=session['graph_revision'],
        iteration=copy.deepcopy(session['iteration']), kind='followup', request_id=request['id'],
        status='running', started_at=time.time(), inputs=copy.deepcopy(primary.get('inputs') or {}),
        input_versions=copy.deepcopy(primary.get('input_versions') or {}),
        progress=[], diagnostics=[], artifacts=[], result={}, messages=[], pending_tools={})
    if delivery_id:
        attempt['inputs']['previous_delivery'] = frozen_result(state['deliveries'][delivery_id])
    state['attempts'][identity] = attempt
    session['turns'].append(identity)
    request.update(status='running', attempt_id=identity, working_base_id=delivery_id)
    return copy.deepcopy(session['node']), copy.deepcopy(attempt), copy.deepcopy(previous)


def finish_request(state, identity, result):
    attempt = state['attempts'][identity]
    request = state['node_requests'][attempt['request_id']]
    cancelled = request['status'] == 'cancelled'
    if cancelled:
        result = dict(result, status='paused', reason='这次请求已取消，已产生的执行记录保留。')
    attempt.update(status=result.get('status', 'completed'), result=copy.deepcopy(result), ended_at=time.time())
    checkpoint(state, attempt)
    session = state['node_sessions'][attempt['session_id']]
    if state['attempts'][session['primary_attempt_id']]['status'] == 'suspended':
        session['resume_primary'] = True
    request.update(status='completed' if attempt['status'] == 'completed' else
                   'cancelled' if cancelled else 'paused' if attempt['status'] == 'paused' else 'failed',
                   reply=copy.deepcopy(result), ended_at=time.time())
    if request['status'] == 'completed' and request['kind'] != 'ask':
        # A notification response need not invent an output just to acknowledge it.
        if request['kind'] != 'update' or result.get('artifacts'):
            delivery = publish(state, attempt, result)
            request['delivery_id'] = delivery['id']
    sender = state['node_sessions'].get(request['sender_session_id'])
    if sender:
        _notify(sender, dict(id='reply:' + request['id'], kind='reply', request_id=request['id'],
            node_id=attempt['node_id'], status=request['status'], reply=copy.deepcopy(result),
            delivery_id=request['delivery_id']))
        primary = state['attempts'][sender['primary_attempt_id']]
        sender_active = state['attempts'].get(request['sender_attempt_id'], {}).get('status') in {'running', 'suspended'}
        if primary['status'] == 'completed' and not sender_active and not any(
                r['target_session_id'] == sender['id'] and r['parent_request_id'] == request['id']
                for r in state['node_requests'].values()):
            enqueue(state, dict(node_id=sender['node_id'], target_attempt_id=primary['id'], kind='update',
                message='此前发出的节点请求已有回复。保留已有交付，接续处理相关部分；无需重做其他工作。',
                request_id='reply-update:' + request['id'], parent_request_id=request['id']), internal=True)
    from .state import event
    event(state, 'node_reply', node_id=attempt['node_id'], request_id=request['id'], status=request['status'])


def request_view(state, request):
    result = copy.deepcopy(request)
    if result.get('delivery_id'):
        result['delivery'] = frozen_result(state['deliveries'][result['delivery_id']])
    return result


def receive(state, attempt_id, *, ack=True):
    attempt = state['attempts'][attempt_id]
    session = register(state, attempt)
    fresh = [copy.deepcopy(item) for item in session['notifications'] if item['id'] not in session['seen_notifications']]
    for item in fresh:
        delivery = state['deliveries'].get(item.get('delivery_id'))
        if delivery:
            item['delivery'] = frozen_result(delivery)
            # A requested revision is adopted at this safe point. Other revisions
            # are visible to the model without silently replacing its input set.
            if item['kind'] == 'reply':
                adopt_input(state, attempt, delivery['id'])
        if ack:
            session['seen_notifications'].append(item['id'])
    return fresh


def acknowledge(state, attempt_id, notification_ids):
    session = register(state, state['attempts'][attempt_id])
    known = {item['id'] for item in session['notifications']}
    for identity in notification_ids:
        if identity in known and identity not in session['seen_notifications']:
            session['seen_notifications'].append(identity)


def adopt_input(state, attempt, delivery_id):
    delivery = state['deliveries'].get(delivery_id)
    if not delivery:
        raise ValueError('交付版本不存在')
    versions = attempt.setdefault('input_versions', {})
    keys = [key for key, value in versions.items()
            if state['deliveries'].get(value, {}).get('session_id') == delivery['session_id']]
    if not keys:
        keys = [delivery['node_id']]
    for key in keys:
        versions[key] = delivery_id
        attempt['inputs'][key] = frozen_result(delivery)
    return frozen_result(delivery)


def wait_for(state, attempt_id, request_id):
    attempt = state['attempts'][attempt_id]
    session = register(state, attempt)
    request = state['node_requests'].get(request_id)
    if not request or request['sender_session_id'] != session['id']:
        raise ValueError('只能等待本节点发出的请求')
    if request['status'] in TERMINAL:
        return request_view(state, request)
    # Reject only the circular wait, leaving the node free to answer/use what it has.
    waiting = {a['session_id']: state['node_requests'].get(a.get('waiting_on'), {}).get('target_session_id')
               for a in state['attempts'].values() if a.get('waiting_on') and a.get('status') not in TERMINAL}
    cursor, seen = request['target_session_id'], set()
    while cursor and cursor not in seen:
        if cursor == session['id']:
            raise ValueError('这次等待会形成循环。请先答复已有请求或利用现有材料继续，不要互相等待。')
        seen.add(cursor)
        cursor = waiting.get(cursor)
    attempt['waiting_on'] = request_id
    return dict(id=request_id, status='waiting', message='将在本轮工具完成后保存上下文并让出执行名额，回复后自动继续。')


def should_yield(state, attempt):
    waiting = state['node_requests'].get(attempt.get('waiting_on'), {})
    if waiting and waiting.get('status') not in TERMINAL:
        return True
    # An initial execution makes room at a tool boundary for its own inbox.
    return not attempt.get('request_id') and any(r['status'] == 'queued' and
        r['target_session_id'] == attempt['session_id'] for r in state.get('node_requests', {}).values())


def can_resume(state, attempt):
    if attempt.get('waiting_tasks'):
        from .exploration import tasks_ready
        if not tasks_ready(state, attempt):
            return False
    waiting = state['node_requests'].get(attempt.get('waiting_on'), {})
    if waiting and waiting.get('status') not in TERMINAL:
        return False
    return (attempt.get('request_id') or state['node_sessions'][attempt['session_id']].get('resume_primary') or
            not any(r['target_session_id'] == attempt['session_id'] and r['status'] in {'queued', 'running', 'waiting'}
                    for r in state['node_requests'].values()))


def pending(state):
    return any(r['status'] in {'queued', 'running', 'waiting', 'paused'} for r in state.get('node_requests', {}).values())


def cancel(state, request_id):
    request = state.get('node_requests', {}).get(request_id)
    if not request:
        raise ValueError('消息不存在')
    if request['status'] not in TERMINAL:
        request.update(status='cancelled', ended_at=time.time(),
            reply={'status': 'paused', 'reason': '这次请求已取消'})
    return copy.deepcopy(request)
