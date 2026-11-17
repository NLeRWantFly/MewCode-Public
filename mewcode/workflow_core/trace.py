"""A conversation's public execution map. Recording never schedules work."""
from __future__ import annotations

import copy
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .exploration import WorkflowTask
from .schema import digest, normalize, scope, walk
from .state import current, event

PROFILE = 'goal.trace.v1'


def is_trace(state):
    return state.get('graph', {}).get('execution_profile') == PROFILE


def executable(state):
    if is_trace(state):
        raise ValueError('探索图只记录对话过程；请在主对话中提出修改。完成并明确复用后才按图执行。')


class TraceTask(WorkflowTask):
    kind: Literal['work', 'judge', 'branch', 'loop'] = 'work'
    config: dict = Field(default_factory=dict, description=(
        'Program structure: branch uses source, cases[{port,equals}] and default; loop uses graph{nodes,edges}, '
        'mode=count/foreach/until and count/items/until. Independent dependencies express parallel work; '
        'multiple predecessors express a join. No scheduling occurs.'))
    input_contract: list = Field(default_factory=list, description='Reusable input definitions; inputs binds concrete values')
    outputs: list = Field(default_factory=list, description='Reusable output definitions')


class TraceEdge(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    source: str
    target: str
    port: str = 'always'


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    node_id: str = Field(description='Full node path, including enclosing loop IDs')
    status: Literal['pending', 'running', 'completed', 'failed', 'blocked', 'needs_input', 'paused', 'unknown', 'skipped']
    text: str = Field(default='', max_length=16000)
    reason: str = Field(default='', max_length=2000)
    result: dict = Field(default_factory=dict, description='Public outputs and selected branch value/port; never hidden reasoning')
    actor: str = Field(default='chat', max_length=200, description='Main chat or actual subagent/task receipt ID')
    iteration: list[int] = Field(default_factory=list, max_length=20)


def update(state, params, template, *, capture=None):
    fingerprint = digest(params.model_dump())
    prior = state.setdefault('trace_requests', {}).get(params.request_id)
    if prior:
        if prior['fingerprint'] != fingerprint:
            raise ValueError('同一请求编号不能提交不同的探索记录')
        return copy.deepcopy(prior['result'])
    graph = copy.deepcopy(state['graph'])
    part = scope(graph, params.scope)
    nodes = {node['id']: node for node in part['nodes']}
    if len({item.id for item in params.items}) != len(params.items):
        raise ValueError('探索记录中的节点 ID 重复')
    added, revised, replacements = [], [], {}
    for item in params.items:
        config = dict(template['config'], **item.config)
        config.update(task_inputs=copy.deepcopy(item.inputs), prompt=item.prompt, constraints=item.constraints)
        if item.service_tier is not None:
            config['service_tier'] = item.service_tier
        node = dict(id=item.id, label=item.label, task=item.task, kind=item.kind,
                    config=config, model=copy.deepcopy(item.model or template['model']),
                    inputs=copy.deepcopy(item.input_contract), outputs=copy.deepcopy(item.outputs))
        existed = item.id in nodes
        (revised if item.id in nodes else added).append(item.id)
        nodes[item.id] = node
        if params.edges is None and (not existed or 'depends_on' in item.model_fields_set):
            part['edges'] = [edge for edge in part['edges'] if edge['target'] != item.id]
            part['edges'].extend(dict(id='trace_' + digest([parent, item.id])[:20], source=parent,
                                     target=item.id, port='always') for parent in item.depends_on)
        for replaced in item.replaces:
            if replaced not in nodes or replaced == item.id or replaced in item.depends_on:
                raise ValueError('被替换的探索节点不存在或与新节点相同')
            if replaced in replacements:
                raise ValueError('同一探索节点不能同时被两个节点替换')
            replacements[replaced] = item.id
    part['nodes'] = list(nodes.values())
    if params.edges is not None:
        part['edges'] = [edge.model_dump() for edge in params.edges]
    for edge in part['edges']:
        if edge['source'] in replacements:
            edge['source'] = replacements[edge['source']]
    if 'concurrency' in params.model_fields_set:
        graph['concurrency'] = params.concurrency
    graph = normalize(graph)
    def defaults(part, inherited):
        for node in part['nodes']:
            node.setdefault('model', copy.deepcopy(inherited))
            config = node.setdefault('config', {})
            for key, value in template['config'].items():
                config.setdefault(key, value)
            if node.get('kind') == 'loop':
                defaults(config['graph'], node['model'])
    defaults(graph, template['model'])
    if graph != state['graph']:
        graph['revision'] += 1
        state['graph'] = normalize(graph)
        state['graph_history'].append(copy.deepcopy(state['graph']))
    names = dict(walk(state['graph']))
    for replaced, replacement in replacements.items():
        path = (params.scope + '/' if params.scope else '') + replaced
        current(state, path).update(superseded_by=replacement)
    for observation in params.observations:
        if observation.node_id not in names:
            raise ValueError('探索节点不存在：' + observation.node_id)
        row = current(state, observation.node_id)
        result = dict(observation.result)
        if capture and result.get('artifacts'):
            artifacts = []
            for artifact in result['artifacts']:
                saved = capture(observation.node_id, artifact)
                state.setdefault('trace_artifacts', {})[saved['id']] = saved
                artifacts.append(saved)
            result['artifacts'] = artifacts
        value = dict(observation.model_dump(), result=copy.deepcopy(result), at=time.time())
        state.setdefault('trace_observations', []).append(value)
        if observation.text:
            result['text'] = observation.text
        row.update(status=observation.status, reason=observation.reason, result=result,
                   actor=observation.actor, iteration=observation.iteration)
    # Completion belongs to the chat: failed attempts and unselected paths stay factual.
    if params.completed is not None:
        state['status'] = 'completed' if params.completed else 'running'
    elif added or revised or params.observations:
        state['status'] = 'running'
    if params.summary:
        state['conversation_summary'] = params.summary
    state.pop('solidified_recipe', None)
    event(state, 'trace_updated', added=added, revised=revised, completed=state['status'] == 'completed')
    result = dict(added=added, revised=revised, revision=state['graph']['revision'],
                  completed=state['status'] == 'completed', execution_started=False, purpose='exploration')
    state['trace_requests'][params.request_id] = dict(fingerprint=fingerprint, result=copy.deepcopy(result))
    return result


def publish(state, node_id, body):
    from .presentation import PresentationBlock
    if node_id not in dict(walk(state['graph'])):
        raise ValueError('探索节点不存在：' + node_id)
    block = PresentationBlock.model_validate(body).model_dump()
    if block['kind'] == 'image':
        normalized = lambda value: str(value or '').replace('\\', '/').casefold()
        found = next((value for value in state.get('trace_artifacts', {}).values()
            if block['artifact_id'] and block['artifact_id'] == value['id'] or not block['artifact_id']
            and normalized(block['artifact_path']) == normalized(value['path'])), None)
        if not found or not found['name'].lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
            raise ValueError('图片需先通过探索记录的 result.artifacts 登记真实产物路径')
        block['artifact_id'] = found['id']
    blocks = state.setdefault('trace_blocks', {}).setdefault(node_id, {})
    key = block['slot'] + ':' + block['block_id']
    blocks[key] = dict(block, node_id=node_id, author='chat',
        order=blocks.get(key, {}).get('order', time.time_ns()), updated_at=time.time())
    event(state, 'presentation_updated', node_id=node_id, block_id=block['block_id'], slot=block['slot'])
    return dict(block_id=block['block_id'], slot=block['slot'], kind=block['kind'], saved=True)
