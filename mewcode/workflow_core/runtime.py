"""Dependency scheduler. The executor's normal result is the delivery boundary."""
from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import time
from dataclasses import dataclass, field

from . import state as transitions
from . import dialogue
from . import continuation
from .activity import execution_event
from .schema import evaluate, lookup, port_matches, walk

LIVE = {}
FAILURES = {}
log = logging.getLogger(__name__)


def observation(run_id):
    core = LIVE.get(run_id)
    return (core.activity_snapshot() if core else dict(scheduler_active=False)), copy.deepcopy(FAILURES.get(run_id))


class YieldExecution(Exception):
    """The complete tool turn is saved; the scheduler can use its slot elsewhere."""


class MemoryBackend:
    """Same atomic interface as the durable store, useful for offline execution."""
    def __init__(self, state):
        self.state = copy.deepcopy(state)

    def read(self):
        return copy.deepcopy(self.state)

    def query(self, project):
        return copy.deepcopy(project(self.state))

    def change(self, callback):
        candidate = copy.deepcopy(self.state)
        result = callback(candidate)
        self.state = candidate
        return copy.deepcopy(result)


@dataclass
class Execution:
    node: dict
    node_id: str
    attempt_id: str
    attempt_number: int
    graph_revision: int
    inputs: dict
    iteration: list
    background: str
    checkpoint: dict
    publish: object
    cancelled: asyncio.Event
    request: dict = field(default_factory=dict)
    channel: object = None
    recovery: dict = field(default_factory=dict)

    def send_message(self, **body):
        return self.channel('send', **body)

    def poll_messages(self, *, ack=True):
        messages = self.channel('receive', ack=ack)
        if messages:
            self.inputs.update(self.channel('inputs'))
        return messages

    def wait_for_reply(self, request_id):
        result = self.channel('wait', request_id=request_id)
        if result['status'] == 'waiting':
            raise YieldExecution()
        return result

    def cooperate(self):
        if self.channel and self.channel('should_yield'):
            raise YieldExecution()


class Core:
    def __init__(self, backend, executor, *, concurrency=4, run_id='', emit=None, report=None, retry_delay=2):
        self.backend, self.executor = backend, executor
        self.concurrency = max(1, int(concurrency))
        self.run_id, self.emit, self.report = run_id, emit, report
        self.wake = asyncio.Event()
        self.tasks = {}
        self.cancellations = {}
        self.report_tasks = set()
        self.report_attempted = set()
        self.activity = {}
        self.last_activity_at = 0.0
        self.runtime_fault = None
        self.retry_delay = retry_delay

    def query(self, project):
        query = getattr(self.backend, 'query', lambda read: read(self.backend.read()))
        return query(project)

    def scheduling_snapshot(self):
        def project(state):
            snapshot = {key: state[key] for key in (
                'graph', 'nodes', 'status', 'stop_requested', 'pending_apply', 'held',
                'prefer_work', 'dialogue_only', 'node_requests', 'automatic_continuations') if key in state}
            identities = set(self.tasks)
            identities.update(ticket['source_attempt_id']
                              for ticket in (state.get('automatic_continuations') or {}).values()
                              if ticket['status'] == 'scheduled')
            snapshot['attempts'] = {identity: {key: attempt[key] for key in (
                'id', 'node_id', 'request_id', 'generation', 'status', 'session_id', 'pending_tools') if key in attempt}
                for identity in identities if (attempt := state['attempts'].get(identity))}
            return snapshot
        return self.query(project)

    def observe(self, identity, node_id=''):
        stamp = time.time()
        prior = self.activity.get(identity, {})
        self.activity[identity] = dict(prior, node_id=node_id or prior.get('node_id', ''), at=stamp)
        self.last_activity_at = stamp
        return stamp

    def activity_snapshot(self):
        nodes = {}
        for value in tuple(self.activity.values()):
            nodes[value['node_id']] = max(nodes.get(value['node_id'], 0), value['at'])
        active = [identity for identity, task in tuple(self.tasks.items()) if not task.done()]
        executions = {}
        for identity in active:
            item = self.activity.get(identity, {})
            execution = item.get('execution')
            if execution and execution['at'] >= executions.get(item['node_id'], {}).get('at', 0):
                executions[item['node_id']] = dict(execution, attempt_id=identity)
        return dict(last_activity_at=self.last_activity_at, nodes=nodes, scheduler_active=True,
                    executions=executions,
                    attempt_executions={identity: copy.deepcopy(self.activity.get(identity, {}).get('execution', {}))
                                        for identity in active},
                    active_attempt_ids=active,
                    active_node_ids=sorted({self.activity.get(identity, {}).get('node_id', '') for identity in active} - {''}))

    def record_failure(self, exc):
        causes, current = [], exc
        while current is not None and len(causes) < 5:
            causes.append(str(current))
            current = current.__cause__ or current.__context__
        message = '; '.join(causes)
        reason = ('磁盘空间已满，无法保存进度，对话已中断。' if any(text in message.lower() for text in
                  ('no space left', 'disk is full', 'database or disk is full')) else
                  '磁盘读写失败，无法保存进度，对话已中断。' if 'disk i/o error' in message.lower() else
                  '执行异常，对话已中断：' + message[:500])
        fault = dict(at=time.time(), reason=reason,
            node_ids=sorted({self.activity.get(key, {}).get('node_id', '') for key in self.tasks} - {''}))
        self.runtime_fault = fault
        if self.run_id:
            FAILURES[self.run_id] = fault
            while len(FAILURES) > 128:
                FAILURES.pop(next(iter(FAILURES)))
        def save(state):
            state.update(status='blocked', runtime_fault=copy.deepcopy(fault))
        try:
            self.backend.change(save)
        except Exception:
            # The read path retains this failure even when the disk cannot
            # accept another checkpoint. The previous durable context stays.
            log.exception('Unable to persist workflow interruption')

    def control(self, body):
        result = self.backend.change(lambda state: dialogue.cancel(state, body['message_id'])
            if body.get('operation') == 'cancel_request' else transitions.command(state, body))
        self.wake.set()
        return result

    def send_message(self, body, *, sender_attempt_id=''):
        result = self.backend.change(lambda state: dialogue.enqueue(state, body,
            sender=state['attempts'].get(sender_attempt_id) if sender_attempt_id else None))
        self.wake.set()
        return result

    def channel(self, identity, action, **body):
        def change(state):
            attempt = state['attempts'][identity]
            if action == 'send':
                if not body.get('target_attempt_id'):
                    consumed = next((state['deliveries'][value] for value in attempt.get('input_versions', {}).values()
                        if value in state['deliveries'] and state['deliveries'][value]['node_id'] == body.get('node_id')), None)
                    if consumed:
                        body.setdefault('base_delivery_id', consumed['id'])
                        body['base_delivery_id'] = body.get('base_delivery_id') or consumed['id']
                        body['target_attempt_id'] = consumed['attempt_id']
                return dialogue.enqueue(state, body, sender=attempt)
            if action == 'nodes':
                return [dict(node_id=path, status=row['status'], attempt_id=row.get('attempt_id', ''),
                    delivery_id=row.get('published_delivery_id', ''),
                    iteration=state['attempts'].get(row.get('attempt_id'), {}).get('iteration', []))
                    for path, row in state['nodes'].items()]
            if action == 'inputs':
                return copy.deepcopy(attempt['inputs'])
            if action == 'receive':
                return dialogue.receive(state, identity, ack=body.get('ack', True))
            if action == 'ack':
                return dialogue.acknowledge(state, identity, body['notification_ids'])
            if action == 'wait':
                return dialogue.wait_for(state, identity, body['request_id'])
            if action == 'read':
                return [dialogue.request_view(state, request) for request in state.get('node_requests', {}).values()
                        if request['sender_session_id'] == attempt['session_id'] and
                        (not body.get('request_id') or request['id'] == body['request_id'])]
            if action == 'adopt':
                return dialogue.adopt_input(state, attempt, body['delivery_id'])
            if action == 'checkpoint':
                return continuation.checkpoint_page(state, identity, **body)
            if action == 'presentation':
                from .presentation import publish
                return publish(state, identity, **body)
            raise ValueError('未知节点消息操作')
        query = getattr(self.backend, 'query', lambda project: project(self.backend.read()))
        if action in {'nodes', 'inputs', 'read', 'checkpoint'}:
            return query(change)
        if action == 'receive':
            def has_messages(state):
                attempt = state['attempts'][identity]
                session = state.get('node_sessions', {}).get(attempt.get('session_id'))
                return session is None or any(item['id'] not in session['seen_notifications']
                                              for item in session['notifications'])
            if not query(has_messages):
                return []
        if action in {'should_yield', 'waiting'}:
            def should_yield(state):
                attempt = state['attempts'][identity]
                if action == 'waiting':
                    request = state.get('node_requests', {}).get(attempt.get('waiting_on'), {})
                    return bool(request) and request['status'] not in dialogue.TERMINAL
                if dialogue.should_yield(state, attempt):
                    return True
                # A busy inbox may request a yield only at a full tool boundary.
                return (not attempt.get('request_id') and len(self.tasks) >= self.concurrency and
                        any(r['status'] == 'queued' for r in state.get('node_requests', {}).values()))
            return query(should_yield)
        result = self.backend.change(change)
        self.wake.set()
        return result

    async def notify(self, kind, **fields):
        if self.emit:
            value = self.emit(dict(type='workflow_' + kind, run_id=self.run_id, **fields))
            if inspect.isawaitable(value):
                await value

    async def publish(self, identity, kind, **fields):
        stamp = self.observe(identity)
        if kind == 'activity':
            execution = execution_event(fields.get('activity', 'starting'), **{
                key: value for key, value in fields.items() if key != 'activity'})
            prior = self.activity[identity].get('execution', {})
            recent = (dict(tool=execution['tool'], outcome=execution['outcome']) if execution.get('outcome')
                      else prior.get('last_tool'))
            if recent:
                execution['last_tool'] = recent
                if execution['phase'] == 'waiting_model':
                    result_label = {'completed': '已返回', 'failed': '返回错误', 'unknown': '结果待核对'}.get(recent['outcome'], '')
                    execution['text'] += f" · {recent['tool']} {result_label}"
            if all(prior.get(key) == value for key, value in execution.items()):
                return  # Tokens in the same phase do not write or flood the UI.
            execution['at'] = stamp
            self.activity[identity]['execution'] = execution
            history = self.activity[identity].setdefault('phase_events', [])
            history.append(copy.deepcopy(execution))
            del history[:-200]
            await self.notify('activity', node_id=self.activity[identity]['node_id'], attempt_id=identity,
                              execution=execution)
            return
        from .store import publish_attempt
        metadata = dict(stamp=stamp, execution=self.activity[identity].get('execution'),
                        phases=self.activity[identity].get('phase_events', []))
        publish = getattr(self.backend, 'publish', None)
        if publish:
            binding = publish(identity, kind, fields, **metadata)
        else:
            binding = self.backend.change(lambda state: publish_attempt(state, identity, kind, fields, **metadata))
        if binding:
            await self.notify(kind, **binding, **fields)

    @staticmethod
    def incoming(state, graph, node, prefix, inherited):
        edges = [edge for edge in graph['edges'] if edge['target'] == node['id']]
        row = transitions.current(state, prefix + node['id'])
        values, active, unresolved = dict(inherited), not edges, False
        for edge in edges:
            source = transitions.current(state, prefix + edge['source'])
            if source['status'] not in transitions.DONE:
                unresolved = True
                continue
            if source['status'] == 'skipped' and source.get('skip_reason') != 'user':
                continue
            if port_matches(edge['port'], source.get('result') or {}):
                active = True
                source_node = next(n for n in graph['nodes'] if n['id'] == edge['source'])
                if source_node['kind'] in {'judge', 'branch'}:
                    previous = state['attempts'].get(source.get('attempt_id'), {})
                    values.update(copy.deepcopy(previous.get('inputs') or {}))
                values[edge['source']] = copy.deepcopy(source.get('result') or {})
        values.update(copy.deepcopy(state['bindings'].get(prefix + node['id'], {})))
        if row.get('manual_start'):
            return 'ready', values
        if unresolved:
            return 'waiting', values
        return ('ready' if active else 'skip'), values

    def tick(self, state):
        """Advance control components atomically; return work ready to dispatch."""
        ready = []
        if state.get('dialogue_only'):
            return ready
        pending = state.get('pending_apply')
        frozen = set((pending or {}).get('impact', {}).get('affected', []))
        frozen.update(state['attempts'][identity]['node_id'] for identity in self.tasks
                      if state['nodes'].get(state['attempts'][identity]['node_id'], {}).get('attempt_id') != identity)
        def advance(graph, prefix='', inherited=None, iteration=()):
            inherited = inherited or {}
            for node in graph['nodes']:
                path = prefix + node['id']
                row = transitions.current(state, path)
                if path in frozen or path in state['held']:
                    continue
                if row['status'] in transitions.DONE | {'failed', 'blocked', 'needs_input', 'paused', 'unknown'}:
                    continue
                if row['status'] == 'running':
                    saved = state['attempts'].get(row.get('attempt_id'), {})
                    if saved.get('status') == 'suspended' and dialogue.can_resume(state, saved):
                        if len(self.tasks) + len(ready) < self.concurrency:
                            session = state['node_sessions'][saved['session_id']]
                            session.pop('resume_primary', None)
                            previous = copy.deepcopy(state['attempts'][session['head_attempt_id']])
                            saved.update(status='running')
                            saved.pop('waiting_on', None)
                            ready.append((copy.deepcopy(node), copy.deepcopy(saved), previous))
                        continue
                    if node['kind'] == 'loop':
                        advance_loop(node, path, row, iteration)
                    continue
                status, inputs = self.incoming(state, graph, node, prefix, inherited)
                if status == 'waiting':
                    continue
                if status == 'skip':
                    row.update(status='skipped', skip_reason='branch', result={})
                    continue
                missing = [item['name'] for item in node['inputs'] if isinstance(item, dict)
                           and item.get('required', True) and item.get('name') not in inputs]
                if missing:
                    row.update(status='needs_input', reason='缺少输入：' + '、'.join(missing))
                    continue
                config = node['config']
                if node['kind'] == 'loop':
                    if not row.get('attempt_id') or state['attempts'].get(row['attempt_id'], {}).get('status') != 'running':
                        transitions.start_attempt(state, path, inputs, iteration)
                    if not row.get('loop'):
                        items = config.get('items', [])
                        if isinstance(items, str):
                            try:
                                items = lookup(inputs, items)
                            except ValueError as exc:
                                row.update(status='needs_input', reason=str(exc))
                                continue
                        if config.get('mode') == 'foreach' and not isinstance(items, list):
                            row.update(status='needs_input', reason='循环材料必须是数组')
                            continue
                        row['loop'] = dict(round=0, items=copy.deepcopy(items), results=[],
                                           inputs=copy.deepcopy(inputs), active=False, elapsed=0.0,
                                           tokens=0, started_at=time.time())
                    row['status'] = 'running'
                    advance_loop(node, path, row, iteration)
                elif node['kind'] == 'branch' or node['kind'] == 'judge' and 'expression' in config:
                    attempt = transitions.start_attempt(state, path, inputs, iteration)
                    try:
                        context = dict(inputs=inputs, **inputs)
                        value = evaluate(config['expression'], context) if 'expression' in config else lookup(
                            context, config.get('source', 'value'))
                        if node['kind'] == 'branch' and config.get('cases'):
                            match = next((case['port'] for case in config['cases'] if case.get('equals') == value),
                                         config.get('default'))
                            if match is None:
                                raise ValueError('分支没有匹配出口，请补充条件或默认出口')
                            value = match
                        result = dict(status='completed', value=value, text=str(value), artifacts=[])
                    except (ValueError, TypeError, KeyError) as exc:
                        result = dict(status='needs_input', reason=str(exc), text=str(exc))
                    transitions.finish_attempt(state, attempt['id'], result)
                else:
                    if len(self.tasks) + len(ready) < self.concurrency:
                        attempt = transitions.start_attempt(state, path, inputs, iteration)
                        prior = state['attempts'].get(row.get('previous_attempt_id', ''), {}) if row.get('resume_checkpoint') else {}
                        ready.append((copy.deepcopy(node), copy.deepcopy(attempt), copy.deepcopy(prior)))

        def advance_loop(node, path, row, iteration):
            config, loop = node['config'], row['loop']
            body = config['graph']
            mode = config.get('mode', 'until')
            if not loop['active']:
                if (mode == 'foreach' and loop['round'] >= len(loop['items']) or
                        mode == 'count' and loop['round'] >= config['count']):
                    row.update(status='completed', result=dict(status='completed', items=loop['results'],
                        text=f'已完成 {loop["round"]} 轮', artifacts=[a for result in loop['results']
                            for value in result.values() for a in value.get('artifacts', [])]))
                    return
                # Resource counters describe the work. Only graph decisions,
                # item/count completion and user controls end a loop.
                children = [key for key, _ in walk(body, path + '/')]
                transitions.invalidate(state, children)
                loop.update(active=True, round_started_at=time.time())
                transitions.event(state, 'loop_round', node_id=path, iteration=[*iteration, loop['round']])
            context = dict(loop['inputs'], iteration=loop['round'], previous=loop['results'][-1] if loop['results'] else {})
            if mode == 'foreach':
                context['item'] = loop['items'][loop['round']]
            advance(body, path + '/', context, (*iteration, loop['round']))
            children = {child['id']: transitions.current(state, path + '/' + child['id']) for child in body['nodes']}
            if not all(child['status'] in transitions.DONE for child in children.values()):
                return
            results = {key: copy.deepcopy(child.get('result') or {}) for key, child in children.items()}
            loop['results'].append(results)
            loop['round'] += 1
            loop['elapsed'] += time.time() - loop['round_started_at']
            loop['tokens'] += sum(value.get('tokens', 0) for value in results.values())
            loop['active'] = False
            if mode == 'until':
                try:
                    if 'until' not in config:
                        raise ValueError('请设置循环退出条件')
                    exiting = evaluate(config['until'], dict(results=results, iteration=loop['round'], **results))
                except (ValueError, TypeError, KeyError) as exc:
                    row.update(status='needs_input', reason=str(exc))
                    return
                if exiting:
                    row.update(status='completed', result=dict(status='completed', outputs=results,
                        text=f'已完成 {loop["round"]} 轮', iterations=loop['round'],
                        artifacts=[a for value in results.values() for a in value.get('artifacts', [])]))
        advance(state['graph'], inherited=state.get('inputs') or {})
        for path, node in walk(state['graph']):
            row = state['nodes'].get(path, {})
            attempt = state['attempts'].get(row.get('attempt_id')) or {}
            if node['kind'] == 'loop' and row.get('status') not in {None, 'running', 'pending'} and attempt.get('status') == 'running':
                transitions.finish_attempt(state, attempt['id'], dict(row.get('result') or {},
                    status=row['status'], reason=row.get('reason', '')))
        return ready

    def tick_requests(self, state):
        ready = []
        busy = {state['attempts'][identity]['node_id'] for identity in self.tasks}
        for request in state.get('node_requests', {}).values():
            if request['node_id'] in busy or request['node_id'] in state['held']:
                continue
            if len(self.tasks) + len(ready) >= self.concurrency:
                break
            if request['status'] == 'queued':
                session = state['node_sessions'][request['target_session_id']]
                primary = state['attempts'][session['primary_attempt_id']]
                if (session.get('resume_primary') and primary['status'] == 'suspended'
                        and dialogue.can_resume(state, primary)):
                    continue
                if any(other['node_id'] == request['node_id'] and other['status'] in {'running', 'waiting'}
                       for other in state['node_requests'].values()):
                    continue
                ready.append(dialogue.start_request(state, request))
            elif request['status'] == 'waiting':
                attempt = state['attempts'][request['attempt_id']]
                if not dialogue.can_resume(state, attempt):
                    continue
                previous = copy.deepcopy(attempt)
                attempt.update(status='running')
                attempt.pop('waiting_on', None)
                request['status'] = 'running'
                node = state['node_sessions'][attempt['session_id']]['node']
                ready.append((copy.deepcopy(node), copy.deepcopy(attempt), previous))
            else:
                continue
            busy.add(request['node_id'])
        return ready

    async def dispatch(self, entries):
        if entries:
            self.backend.change(lambda state: state.update(prefer_work=bool(entries[-1][1].get('request_id'))))
        for node, attempt, previous in entries:
            self.observe(attempt['id'], attempt['node_id'])
            self.cancellations[attempt['id']] = asyncio.Event()
            self.tasks[attempt['id']] = asyncio.create_task(self.execute(node, attempt, previous))
            await self.notify('node_started', node_id=attempt['node_id'], attempt_id=attempt['id'],
                graph_revision=attempt['graph_revision'], iteration=attempt['iteration'],
                request_id=attempt.get('request_id', ''))

    async def execute(self, node, attempt, previous):
        identity = attempt['id']
        self.observe(identity, attempt['node_id'])
        await self.publish(identity, 'activity', activity='starting')
        cancelled = self.cancellations[identity]
        try:
            def take_recovery(state):
                recovery = state.get('recovery_contexts', {}).pop(attempt['node_id'], None)
                recovery = recovery or previous.get('recovery') or {}
                hint = ((state.get('node_requests', {}).get(attempt.get('request_id'), {})
                         if attempt.get('request_id') else state['nodes'][attempt['node_id']])).pop('automatic_continuation', None)
                if hint:
                    recovery = dict(recovery, automatic_continuation=hint)
                if recovery:
                    state['attempts'][identity]['recovery'] = copy.deepcopy(recovery)
                return recovery
            saved = self.query(lambda state: dict(
                stop_requested=state.get('stop_requested'), held=state.get('held', []),
                request=state.get('node_requests', {}).get(attempt.get('request_id'), {}),
                node=state['nodes'].get(attempt['node_id'], {}),
                has_recovery=attempt['node_id'] in state.get('recovery_contexts', {}),
                background=state['graph'].get('goal', '')))
            if (saved.get('stop_requested') or cancelled.is_set() or
                    any(attempt['node_id']==held or attempt['node_id'].startswith(held+'/') for held in saved.get('held', [])) or
                    saved['request'].get('status')=='cancelled'):
                raise asyncio.CancelledError()
            source = saved['request'] if attempt.get('request_id') else saved['node']
            recovery = (self.backend.change(take_recovery) if saved['has_recovery']
                        or previous.get('recovery') or source.get('automatic_continuation') else {})
            context = Execution(node, attempt['node_id'], identity, attempt['number'], attempt['graph_revision'],
                                attempt['inputs'], attempt['iteration'], saved['background'],
                                previous, lambda kind, **fields: self.publish(identity, kind, **fields), cancelled,
                                saved['request'],
                                lambda action, **body: self.channel(identity, action, **body), recovery)
            result = await self.executor(context)
            if not isinstance(result, dict):
                result = dict(status='completed', text=str(result))
            result.setdefault('status', 'completed')
            if result['status'] not in {'completed', 'failed', 'blocked', 'needs_input', 'paused', 'unknown'}:
                result['status'] = 'failed'
            result.setdefault('artifacts', self.query(lambda state: state['attempts'][identity]['artifacts']))
            if node['kind'] == 'judge' and context.request.get('kind') != 'ask' and result['status'] == 'completed' and 'value' not in result:
                result.update(status='needs_input', reason='判断节点未返回条件结果')
        except YieldExecution:
            def suspend(state):
                value = state['attempts'][identity]
                value['status'] = 'suspended'
                if value.get('request_id'):
                    state['node_requests'][value['request_id']]['status'] = 'waiting'
            self.backend.change(suspend)
            self.wake.set()
            return
        except asyncio.CancelledError:
            result = dict(status='paused', text='', reason='已暂停并保留执行记录')
        except Exception as exc:
            result = dict(status='blocked', text='', reason='模型对话已中断，节点尚未交付：' + str(exc))
        # Tool uncertainty remains in the transcript for the executor to reconcile.
        # It is not a second completion gate after the node has returned a result.
        if attempt.get('request_id'):
            self.backend.change(lambda state: dialogue.finish_request(state, identity, result))
            accepted = True
        else:
            accepted = self.backend.change(lambda state: transitions.finish_attempt(state, identity, result))
        if accepted and result['status'] in {'blocked','failed'} and continuation.cause(result.get('reason')):
            self.backend.change(lambda state: continuation.schedule(state, identity, delay=getattr(self,'retry_delay',2)))
        await self.notify('node_finished', node_id=attempt['node_id'], attempt_id=identity,
                          graph_revision=attempt['graph_revision'], status=result['status'], current=accepted)
        self.queue_reports()
        self.wake.set()

    def queue_reports(self):
        if not self.report:
            return
        identities = self.query(lambda state: [identity for identity, attempt in state['attempts'].items()
            if attempt['status'] not in {'running', 'suspended'} and not attempt.get('report')
            and identity not in self.report_attempted])
        for identity in identities:
            self.report_attempted.add(identity)
            async def save_report(identity=identity):
                try:
                    value = self.report(self.query(lambda state: state['attempts'][identity]))
                    if inspect.isawaitable(value):
                        await value
                except Exception as exc:
                    message = str(exc)
                    self.backend.change(lambda state: state['attempts'][identity].update(report_error=message))
            task = asyncio.create_task(save_report())
            self.report_tasks.add(task)
            task.add_done_callback(self.report_tasks.discard)

    async def run(self):
        from .trace import executable
        self.backend.query(executable)
        if self.run_id and self.run_id in LIVE:
            raise ValueError('该运行已有调度器')
        if self.run_id:
            LIVE[self.run_id] = self
            FAILURES.pop(self.run_id, None)
        self.runtime_fault = None
        def recover(state):
            dialogue.bootstrap(state)
            state['status'] = 'running'
            state.pop('runtime_fault', None)
            state['stop_requested'] = False
            for path, row in state['nodes'].items():
                if row['status'] in {'running', 'unknown'} and not row.get('loop') and path not in state['held']:
                    attempt = state['attempts'].get(row.get('attempt_id'), {})
                    if attempt.get('status') == 'suspended':
                        continue
                    unresolved = any(tool.get('status') in {'running', 'unknown', 'outcome_unknown'}
                                     for tool in attempt.get('pending_tools', {}).values())
                    if attempt:
                        attempt.update(status='unknown' if unresolved else 'paused', ended_at=time.time())
                    row.update(status='pending', previous_attempt_id=row.get('attempt_id', ''), resume_checkpoint=True,
                               reason='')
            for request in state.get('node_requests', {}).values():
                if request['status'] == 'running':
                    attempt = state['attempts'][request['attempt_id']]
                    attempt['status'] = 'suspended'
                    request['status'] = 'waiting'
            transitions.retire_loop_limit_pauses(state)
            for identity in list(state['attempts']):
                continuation.schedule(state, identity, delay=getattr(self,'retry_delay',2))
        try:
            self.backend.change(recover)
            while True:
                self.wake.clear()
                self.queue_reports()
                snapshot = self.scheduling_snapshot()
                pending = snapshot.get('pending_apply')
                affected = set((pending or {}).get('impact', {}).get('affected', []))
                for identity, task in list(self.tasks.items()):
                    if task.done():
                        await task
                        self.tasks.pop(identity)
                        self.cancellations.pop(identity, None)
                        continue
                    attempt = snapshot['attempts'][identity]
                    row = snapshot['nodes'].get(attempt['node_id'], {})
                    request_cancelled = snapshot.get('node_requests', {}).get(attempt.get('request_id'), {}).get('status') == 'cancelled'
                    if (snapshot['stop_requested'] or attempt['node_id'] in affected or
                            any(attempt['node_id']==held or attempt['node_id'].startswith(held+'/') for held in snapshot.get('held',[])) or
                            (not attempt.get('request_id') and row.get('generation') != attempt['generation']) or
                            row.get('status') == 'paused' or request_cancelled):
                        self.cancellations[identity].set()
                        pending_tool = any(t.get('status') == 'running' for t in attempt.get('pending_tools', {}).values())
                        if not getattr(self.executor, 'cooperative_cancel', False) or not pending_tool:
                            task.cancel()
                if pending and not any(snapshot['attempts'][identity]['node_id'] in affected for identity in self.tasks):
                    self.backend.change(transitions.apply_pending)
                    await self.notify('graph_applied', graph_revision=self.query(lambda state: state['graph']['revision']))
                    continue
                if snapshot['stop_requested']:
                    if not self.tasks:
                        break
                else:
                    if continuation.pending(snapshot):
                        self.backend.change(continuation.advance)
                    # Alternate priority so a busy inbox cannot starve graph work.
                    ticks = (self.tick, self.tick_requests) if snapshot.get('prefer_work') else (self.tick_requests, self.tick)
                    for tick in ticks:
                        await self.dispatch(self.backend.change(tick))
                    if not self.tasks:
                        state = self.scheduling_snapshot()
                        retry_wait = continuation.delay(state)
                        if retry_wait is not None:
                            await self.wait_for_work(retry_wait)
                            continue
                        rows = [transitions.current(state, node['id']) for node in state['graph']['nodes']]
                        if all(row['status'] in transitions.DONE for row in rows) and not dialogue.pending(state):
                            self.backend.change(lambda value: value.update(status='completed'))
                            break
                        # A control-only pass may enable another node or start another loop round.
                        before = copy.deepcopy(state['nodes'])
                        probe = self.backend.change(self.tick)
                        await self.dispatch(probe)
                        if not probe and before == self.query(lambda state: state['nodes']):
                            def settle(value):
                                statuses = {row['status'] for row in value['nodes'].values()}
                                blocked = statuses & {'failed', 'blocked', 'needs_input', 'unknown'}
                                value['status'] = 'paused' if not blocked and (value['held'] or 'paused' in statuses) else 'blocked'
                            self.backend.change(settle)
                            break
                        await asyncio.sleep(0)
                        continue
                await self.wait_for_work(self.query(continuation.delay))
        except asyncio.CancelledError:
            try:
                self.backend.change(lambda state: state.update(status='paused', stop_requested=True))
            except Exception as exc:
                self.record_failure(exc)
            raise
        except Exception as exc:
            self.record_failure(exc)
            raise
        finally:
            try:
                for identity, task in self.tasks.items():
                    self.cancellations[identity].set()
                    task.cancel()
                await asyncio.gather(*self.tasks.values(), return_exceptions=True)
                try:
                    self.queue_reports()
                    if self.report_tasks:
                        await asyncio.gather(*self.report_tasks, return_exceptions=True)
                except Exception as exc:
                    self.record_failure(exc)
            finally:
                if self.run_id and LIVE.get(self.run_id) is self:
                    LIVE.pop(self.run_id, None)
        return self.backend.read()

    async def wait_for_work(self, timeout=None):
        waiter = asyncio.create_task(self.wake.wait())
        try:
            await asyncio.wait([waiter, *self.tasks.values()], timeout=timeout,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
