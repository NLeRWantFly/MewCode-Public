"""The canvas and scheduler share this graph, including nested loop bodies."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

FORMAT = 'mewcode.workflow.v2'
KINDS = {'work', 'judge', 'branch', 'loop'}
ALIASES = {'implement': 'work', 'plan': 'work', 'reduce': 'work', 'execution': 'work',
           'audit': 'judge', 'decision': 'judge', 'gate': 'judge'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def normalize(value):
    if not isinstance(value, dict):
        raise ValueError('流程必须是对象')
    graph = copy.deepcopy(value)
    graph.update(format=FORMAT, revision=int(graph.get('revision') or 1))
    graph.setdefault('label', '工作流')
    graph.setdefault('goal', '')
    graph.setdefault('nodes', [])
    graph.setdefault('edges', [])
    graph.setdefault('layout', {})
    if not isinstance(graph['nodes'], list) or not isinstance(graph['edges'], list):
        raise ValueError('节点和连线必须是数组')
    for node in graph['nodes']:
        if not isinstance(node, dict):
            raise ValueError('节点必须是对象')
        node['id'] = str(node.get('id') or node.get('node_id') or '')
        node['kind'] = ALIASES.get(node.get('kind'), node.get('kind', 'work'))
        node.setdefault('label', node.get('title') or node['id'])
        node.setdefault('task', node.get('goal') or '')
        node.setdefault('config', {})
        node.setdefault('inputs', [])
        node.setdefault('outputs', [])
        if node['kind'] == 'loop' and isinstance(node['config'], dict):
            node['config']['graph'] = normalize(node['config'].get('graph') or {})
    for index, edge in enumerate(graph['edges']):
        if not isinstance(edge, dict):
            raise ValueError('连线必须是对象')
        edge['source'] = str(edge.get('source') or edge.get('from') or edge.get('from_node') or '')
        edge['target'] = str(edge.get('target') or edge.get('to') or edge.get('to_node') or '')
        edge.setdefault('id', f'e_{index}_{edge["source"]}_{edge["target"]}')
        edge.setdefault('port', edge.get('condition') or 'always')
    validate(graph)
    return graph


def validate(graph, prefix=''):
    nodes = graph['nodes']
    ids = [node['id'] for node in nodes]
    if len(ids) != len(set(ids)):
        raise ValueError(f'{prefix}节点 ID 重复')
    for node in nodes:
        key = prefix + node['id']
        if not re.fullmatch(r'[\w.:-]{1,160}', node['id']):
            raise ValueError(f'{key}：节点 ID 不能包含路径分隔符或空格')
        if node['kind'] not in KINDS or not isinstance(node['config'], dict):
            raise ValueError(f'{key}：未知组件或配置格式错误')
        if not isinstance(node['inputs'], list) or not isinstance(node['outputs'], list):
            raise ValueError(f'{key}：输入和产出必须是数组')
        if node['kind'] == 'loop':
            config = node['config']
            if config.get('mode', 'until') not in {'until', 'foreach', 'count'}:
                raise ValueError(f'{key}：循环类型无效')
            if config.get('mode') == 'count' and (type(config.get('count')) is not int or config['count'] < 0):
                raise ValueError(f'{key}：循环次数必须是非负整数')
            for name, limit in config.get('limits', {}).items():
                if name not in {'iterations', 'seconds', 'tokens'} or (limit is not None and
                        (type(limit) not in {int, float} or not math.isfinite(limit) or limit <= 0)):
                    raise ValueError(f'{key}：自选循环上限无效')
            validate(config['graph'], key + '/')
    adjacency = {key: [] for key in ids}
    by_id = {node['id']: node for node in nodes}
    indegree = dict.fromkeys(ids, 0)
    edge_ids = set()
    for edge in graph['edges']:
        a, b = edge['source'], edge['target']
        if a not in adjacency or b not in adjacency or edge['id'] in edge_ids:
            raise ValueError(f'{prefix}连线 {edge["id"]} 的端点不存在或 ID 重复')
        if by_id[a]['kind'] == 'work' and edge['port'] != 'always':
            raise ValueError(f'{prefix}{a} 是执行节点，不能使用条件出口 {edge["port"]}；'
                             '完成后继续请连接 always，需要条件分流请使用判断或分支节点')
        edge_ids.add(edge['id'])
        adjacency[a].append(b)
        indegree[b] += 1
    queue = [key for key in ids if not indegree[key]]
    count = 0
    while queue:
        key = queue.pop()
        count += 1
        for child in adjacency[key]:
            indegree[child] -= 1
            if not indegree[child]:
                queue.append(child)
    if count != len(ids):
        raise ValueError(f'{prefix}普通连线形成环；请使用循环组件承载循环体')


def walk(graph, prefix=''):
    for node in graph['nodes']:
        path = prefix + node['id']
        yield path, node
        if node['kind'] == 'loop':
            yield from walk(node['config']['graph'], path + '/')


def scope(graph, path=''):
    for key in path.split('/') if path else []:
        node = next((node for node in graph['nodes'] if node['id'] == key), None)
        if not node or node['kind'] != 'loop':
            raise ValueError('循环组件不存在')
        graph = node['config']['graph']
    return graph


def descendants(graph, roots):
    result = set(roots)
    adjacency = {}
    def collect(part, prefix=''):
        for edge in part['edges']:
            adjacency.setdefault(prefix + edge['source'], set()).add(prefix + edge['target'])
        for node in part['nodes']:
            if node['kind'] == 'loop':
                parent = prefix + node['id']
                children = {parent + '/' + child['id'] for child in node['config']['graph']['nodes']}
                adjacency.setdefault(parent, set()).update(children)
                # Changes within a loop also invalidate its output and external consumers.
                for child in children:
                    adjacency.setdefault(child, set()).add(parent)
                collect(node['config']['graph'], parent + '/')
    collect(graph)
    queue = list(result)
    while queue:
        for child in adjacency.get(queue.pop(), ()):
            if child not in result:
                result.add(child)
                queue.append(child)
    return result


def execution_shape(node):
    value = {key: copy.deepcopy(node.get(key)) for key in
             ('kind', 'task', 'inputs', 'outputs', 'model', 'tools', 'deny_tools', 'config')}
    if node['kind'] == 'loop':
        value['config'].pop('graph', None)
    return value


def impact(before, after):
    old, new = dict(walk(before)), dict(walk(after))
    changed = set(old) ^ set(new)
    changed.update(key for key in old.keys() & new.keys() if execution_shape(old[key]) != execution_shape(new[key]))
    def incoming(graph, prefix=''):
        result = {prefix + node['id']: [] for node in graph['nodes']}
        for edge in graph['edges']:
            result[prefix + edge['target']].append({k: v for k, v in edge.items() if k not in {'id', 'label'}})
        for node in graph['nodes']:
            if node['kind'] == 'loop':
                result.update(incoming(node['config']['graph'], prefix + node['id'] + '/'))
        return {key: sorted(values, key=digest) for key, values in result.items()}
    a, b = incoming(before), incoming(after)
    changed.update(key for key in a.keys() & b.keys() if a[key] != b[key])
    affected = descendants(before, changed) | descendants(after, changed)
    return {'changed': sorted(changed), 'affected': sorted(affected),
            'retained': sorted(new.keys() - affected), 'removed': sorted(old.keys() - new.keys())}


def lookup(context, path):
    current = context
    for part in str(path).split('.'):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ValueError(f'缺少条件输入：{path}')
    return current


def evaluate(expression, context):
    """JSON predicates, never eval or arbitrary Python/JavaScript execution."""
    if not isinstance(expression, dict):
        return expression
    if 'all' in expression:
        return all(evaluate(item, context) for item in expression['all'])
    if 'any' in expression:
        return any(evaluate(item, context) for item in expression['any'])
    if 'not' in expression:
        return not evaluate(expression['not'], context)
    left = lookup(context, expression['path']) if 'path' in expression else expression.get('value')
    op = expression.get('op', 'truthy')
    right = expression.get('equals', expression.get('right'))
    if 'equals' in expression:
        op = 'eq'
    if op == 'truthy':
        return bool(left)
    if op == 'value':
        return left
    if op == 'eq':
        return left == right
    if op == 'ne':
        return left != right
    if op == 'gt':
        return left > right
    if op == 'gte':
        return left >= right
    if op == 'lt':
        return left < right
    if op == 'lte':
        return left <= right
    if op == 'in':
        return left in right
    raise ValueError(f'不支持的条件运算：{op}')


def port_matches(port, result):
    if port in {'always', '', None}:
        return True
    selected = result.get('value')
    if port in {'pass', 'true', 'yes'}:
        return selected is True or selected == 'pass'
    if port in {'fail', 'false', 'no'}:
        return selected is False or selected == 'fail'
    return selected == port or str(selected) == str(port)
