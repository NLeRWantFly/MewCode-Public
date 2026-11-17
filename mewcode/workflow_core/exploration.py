"""Append real work during a running conversation, using the existing Core scheduler."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import time

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .runtime import Core
from .schema import digest, normalize, walk
from .state import DONE, current, event

PROFILE = 'goal.open.v1'
TASK_GRANULARITY = '''按可独立交付的业务目标、知识主题或完整功能组织工作项，归并同一问题的材料、概念与关系。
页码、文件片段、OCR块和工具调用通常是节点内部的读取与断点，不逐项变成画布节点；仅在职责、输入输出或真实依赖可独立时拆分，不为凑并发拆节点。用户明确要求逐页核验时保留必要粒度。
材料交付按主题与用途去重整合，来源页码和原始引用留在可追溯记录；保存流程保留可复用职责与依赖，避免把样本分页或临时调用固化为通用步骤。'''


def build_goal(*, label, goal, model, workspace, concurrency=2, parameters=(), file_scope=None, tools=None, service_tier=None):
    if not 1 <= concurrency <= 32 or not model or not model.get('model_id') or not model.get('effort'):
        raise ValueError('目标需要明确模型、推理档位和有效并发数')
    config = dict(exploration_owner=True, goal_execution=True, presentation=True, workspace=str(workspace))
    if service_tier is not None:
        if service_tier not in {'fast', 'default', 'priority'}:
            raise ValueError('未知执行服务档位')
        config['service_tier'] = service_tier
    if file_scope:
        config['file_scope'] = copy.deepcopy(file_scope)
    node = dict(id='goal', label=label, kind='work', task=goal, model=copy.deepcopy(model),
                config=config, inputs=[], outputs=[])
    if tools is not None:
        node['tools'] = list(tools)
    return normalize(dict(label=label, goal=goal, execution_profile=PROFILE, concurrency=concurrency,
                          run_parameters=list(parameters), nodes=[node], edges=[], layout={}))


class WorkflowTask(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[\w.:-]{1,100}$')
    label: str = Field(min_length=1, max_length=200)
    task: str = Field(min_length=1, max_length=16000)
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    replaces: list[str] = Field(default_factory=list, max_length=100,
        description='Explicitly replace pending or ended failed work; preserve original attempts')
    inputs: dict = Field(default_factory=dict)
    model: dict | None = Field(default=None, description='Explicit provider_key, model_id and effort; otherwise use the declared workflow model')
    prompt: str = Field(default='', max_length=16000)
    constraints: list[str] = Field(default_factory=list, max_length=100)
    service_tier: Literal['fast', 'default', 'priority'] | None = None


def tasks(state, ids=None):
    names = dict(walk(state['graph']))
    selected = ids if ids is not None else names
    result = []
    for path in selected:
        if path not in names:
            raise ValueError('工作项不存在：' + path)
        node, row = names[path], state['nodes'].get(path, {})
        attempt = state['attempts'].get(row.get('attempt_id'), {})
        result.append(dict(id=path, label=node['label'], status=row.get('status', 'pending'),
            reason=row.get('reason', ''), result=copy.deepcopy(row.get('result') or {}),
            artifacts=copy.deepcopy(attempt.get('artifacts') or row.get('result', {}).get('artifacts') or []),
            attempt_id=row.get('attempt_id', ''), presentation_blocks=copy.deepcopy(
                state.get('node_sessions', {}).get(attempt.get('session_id'), {}).get('presentation_blocks', {}))))
        if state['graph'].get('execution_profile') == 'goal.trace.v1':
            result[-1].update(actor=row.get('actor', ''), iteration=copy.deepcopy(row.get('iteration', [])),
                presentation_blocks=copy.deepcopy(state.get('trace_blocks', {}).get(path, {})))
    return result


def tasks_ready(state, attempt):
    paths = attempt.get('waiting_tasks')
    if not paths:
        return True
    from .continuation import pending
    recovering = {ticket['node_id'] for ticket in pending(state)}
    return all(state['nodes'].get(path, {}).get('status') in DONE | {'failed', 'blocked', 'needs_input', 'paused', 'unknown'}
               and path not in recovering for path in paths)


def owner(state, identity):
    attempt = state['attempts'][identity]
    row = state['nodes'].get(attempt['node_id'], {})
    node = dict(walk(state['graph'])).get(attempt['node_id'], {})
    if (state['graph'].get('execution_profile') != PROFILE or not node.get('config', {}).get('exploration_owner')
            or attempt.get('request_id') or attempt['status'] != 'running'
            or row.get('attempt_id') != identity or row.get('generation') != attempt.get('generation')):
        raise ValueError('只有当前目标主对话可以更新或等待滚动工作项')
    if state.get('stop_requested') or state['status'] != 'running':
        raise ValueError('任务已暂停或停止，工作项增量未派发')
    return attempt, node


def update(state, identity, *, request_id, items):
    attempt, parent = owner(state, identity)
    return update_items(state, attempt, parent, request_id=request_id, items=items)


def update_items(state, attempt, parent, *, request_id, items):
    """Shared graph mutation; callers validate the real node or chat authority."""
    validated = [WorkflowTask.model_validate(item).model_dump() for item in items]
    if not validated or len(validated) > 100:
        raise ValueError('请提交 1 至 100 个当前已知的工作项')
    if len({item['id'] for item in validated}) != len(validated):
        raise ValueError('同一增量中的工作项 ID 重复')
    fingerprint = digest(validated)
    key = attempt['session_id'] + ':' + request_id
    receipts = state.setdefault('task_updates', {})
    if key in receipts:
        if receipts[key]['fingerprint'] != fingerprint:
            raise ValueError('同一请求编号不能提交不同的任务增量')
        return copy.deepcopy(receipts[key]['result'])
    graph = copy.deepcopy(state['graph'])
    if '/' in attempt['node_id']:
        raise ValueError('首版滚动目标使用顶层主对话，不能修改嵌套循环的执行结构')
    existing = {node['id']: node for node in graph['nodes']}
    declared = set(existing) | {item['id'] for item in validated}
    added, revised, replacements = [], [], {}
    for item in validated:
        path = item['id']
        if path == attempt['node_id']:
            raise ValueError('工作项不能覆盖主对话身份')
        if attempt['node_id'] in item['depends_on'] or path in item['depends_on']:
            raise ValueError('工作项不能等待当前目标主对话或自身；直接通过 inputs 绑定已知材料')
        if any(dependency not in declared for dependency in item['depends_on']):
            raise ValueError('依赖的工作项尚未声明')
        for old in item['replaces']:
            if old == path or old == attempt['node_id'] or old in item['depends_on'] or old not in existing:
                raise ValueError('被替换工作项无效，不能替换自身、主对话或新工作的前驱')
            old_row = state['nodes'].get(old, {})
            if old_row.get('status', 'pending') not in {'pending', 'blocked', 'failed', 'unknown'}:
                raise ValueError('不能替换仍在执行、已交付或手动暂停的工作项')
            if any(state['nodes'].get(edge['target'], {}).get('status', 'pending') not in {'pending', 'skipped'}
                   for edge in graph['edges'] if edge['source'] == old):
                raise ValueError('已有下游开始执行，不能改写其已绑定来源；请创建独立的后续工作项')
            if old in replacements:
                raise ValueError('同一个工作项不能同时被两个新工作替换')
            replacements[old] = path
        signature = digest(item)
        if path in existing:
            if existing[path].get('config', {}).get('task_signature') != signature:
                if state['nodes'].get(path, {}).get('status', 'pending') != 'pending':
                    raise ValueError('已有工作项已开始或交付，不能重写：' + path)
                revised.append(path)
                graph['edges'] = [edge for edge in graph['edges'] if edge['target'] != path]
            else:
                continue
        config = dict(presentation=True, goal_parent=attempt['node_id'], task_signature=signature,
                      task_inputs=copy.deepcopy(item['inputs']))
        if parent.get('config', {}).get('workspace'):
            config['workspace'] = parent['config']['workspace']
        if parent.get('config', {}).get('file_scope'):
            config['file_scope'] = copy.deepcopy(parent['config']['file_scope'])
        for field in ('goal_execution', 'service_tier'):
            if field in parent.get('config', {}):
                config[field] = parent['config'][field]
        config.update(prompt=item['prompt'], constraints=item['constraints'])
        if item['service_tier'] is not None:
            config['service_tier'] = item['service_tier']
        node = dict(id=path, label=item['label'], kind='work', task=item['task'], config=config,
                    inputs=[], outputs=[], model=copy.deepcopy(item['model'] or parent.get('model')))
        for field in ('tools', 'deny_tools'):
            if field in parent:
                node[field] = copy.deepcopy(parent[field])
        if path in existing:
            graph['nodes'][graph['nodes'].index(existing[path])] = node
        else:
            graph['nodes'].append(node)
            added.append(path)
        for dependency in item['depends_on']:
            edge_id = 'goal_' + hashlib.sha256((dependency + '\0' + path).encode()).hexdigest()[:20]
            graph['edges'].append(dict(id=edge_id, source=dependency, target=path, port='always'))
    if added or revised:
        for edge in graph['edges']:
            if edge['source'] in replacements:
                edge['source'] = replacements[edge['source']]
        graph['revision'] += 1
        state['graph'] = normalize(graph)
        state['graph_history'].append(copy.deepcopy(state['graph']))
        for old, replacement in replacements.items():
            current(state, old).update(status='skipped', skip_reason='replanned',
                superseded_by=replacement, reason='由 ' + replacement + ' 接续，原执行和失败记录保留')
        for item in validated:
            if item['id'] in added + revised:
                state.setdefault('bindings', {})[item['id']] = copy.deepcopy(item['inputs'])
        event(state, 'tasks_added', node_id=attempt['node_id'], node_ids=added, request_id=request_id)
    result = dict(added=added, revised=revised, replaced=replacements,
                  revision=state['graph']['revision'], tasks=tasks(state, [item['id'] for item in validated]))
    receipts[key] = dict(fingerprint=fingerprint, result=copy.deepcopy(result))
    return result


def wait(state, identity, *, node_ids):
    attempt, _ = owner(state, identity)
    if not node_ids or attempt['node_id'] in node_ids:
        raise ValueError('请选择实际的子工作项，不能等待主对话自身')
    tasks(state, node_ids)
    attempt['waiting_tasks'] = list(dict.fromkeys(node_ids))
    ready = tasks_ready(state, attempt)
    return dict(status='ready' if ready else 'waiting', tasks=tasks(state, node_ids),
                message='结果已可读取。' if ready else '本轮工具结束后保存对话并释放名额，工作项结束后自动续接。')


class GoalCore(Core):
    def __init__(self, *args, recipe_store=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.recipe_store = recipe_store
        self.metrics = dict(peak_active=0, dispatched=0)

    def channel(self, identity, action, **body):
        query = getattr(self.backend, 'query', lambda project: project(self.backend.read()))
        if action in {'update_tasks', 'read_tasks', 'wait_tasks'}:
            def change(state):
                if action == 'update_tasks':
                    return update(state, identity, **body)
                if action == 'wait_tasks':
                    return wait(state, identity, **body)
                return tasks(state, body.get('node_ids') or None)
            if action == 'read_tasks':
                return query(lambda state: tasks(state, body.get('node_ids') or None))
            result = self.backend.change(change)
            self.wake.set()
            return result
        if action in {'should_yield', 'waiting'}:
            if not query(lambda state: tasks_ready(state, state['attempts'][identity])):
                return True
        return super().channel(identity, action, **body)

    def tick(self, state):
        ready = super().tick(state)
        for _, attempt, _ in ready:
            saved = state['attempts'][attempt['id']]
            if saved.get('waiting_tasks'):
                results = tasks(state, saved.pop('waiting_tasks'))
                saved['inputs']['workflow_tasks'] = copy.deepcopy(results)
                attempt['inputs']['workflow_tasks'] = results
        return ready

    async def dispatch(self, entries):
        await super().dispatch(entries)
        self.metrics['dispatched'] += len(entries)
        self.metrics['peak_active'] = max(self.metrics['peak_active'], sum(not task.done() for task in self.tasks.values()))

    async def run(self):
        state = await super().run()
        if self.recipe_store is not None and state['status'] == 'completed' and not state.get('solidified_recipe'):
            from .recipes import crystallize
            try:
                recipe = await asyncio.to_thread(self.recipe_store.save, crystallize(state, source_run_id=self.run_id))
                self.backend.change(lambda value: value.update(solidified_recipe={key:recipe[key] for key in
                    ('id', 'version', 'name', 'digest')}))
            except Exception as exc:
                reason = str(exc)
                self.backend.change(lambda value: value.update(recipe_error=reason))
        self.backend.change(lambda value: value.update(execution_metrics=dict(self.metrics, finished_at=time.time())))
        return self.backend.read()
