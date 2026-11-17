"""Versioned executable recipes distilled from actual goal runs."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .schema import digest, normalize, walk


def transform(value, replace):
    if isinstance(value, str):
        return replace(value)
    if isinstance(value, list):
        return [transform(item, replace) for item in value]
    if isinstance(value, dict):
        return {key:transform(item, replace) for key, item in value.items()}
    return copy.deepcopy(value)


def crystallize(state, *, source_run_id=''):
    if state['status'] != 'completed':
        raise ValueError('请在当前目标正常交付后保存可复用流程')
    graph = copy.deepcopy(state['graph'])
    graph.pop('chat_defaults', None)
    graph.pop('chat_recipe_request', None)
    owners = {path for path,node in walk(graph) if node.get('config', {}).get('exploration_owner')}
    owners.update(path for path,row in state['nodes'].items() if row.get('superseded_by'))
    def prune(part, prefix=''):
        removed = {node['id'] for node in part['nodes'] if prefix + node['id'] in owners}
        part['nodes'] = [node for node in part['nodes'] if node['id'] not in removed]
        part['edges'] = [edge for edge in part['edges'] if edge['source'] not in removed and edge['target'] not in removed]
        part['layout'] = {key:point for key,point in part.get('layout', {}).items() if key not in removed}
        for node in part['nodes']:
            if node['kind'] == 'loop':
                prune(node['config']['graph'], prefix + node['id'] + '/')
    prune(graph)
    if not graph['nodes']:
        raise ValueError('当前目标没有形成可复用工作项，公开交付和运行历史仍然保留')
    for _, node in walk(graph):
        node['config'].pop('goal_parent', None)
        node['config'].pop('task_signature', None)
    parameters = {key:state.get('inputs', {}).get(key) for key in graph.get('run_parameters', [])}
    if any(not isinstance(value, str) or not value for value in parameters.values()):
        raise ValueError('首版可复用参数需要明确的非空字符串样例')
    def parameterize(text):
        for key, sample in sorted(parameters.items(), key=lambda item:len(item[1]), reverse=True):
            text = text.replace(sample, '{{run_inputs.' + key + '}}')
        return text
    graph['layout'] = {key:point for key,point in graph.get('layout', {}).items() if key not in owners}
    graph.update(execution_profile='goal.recipe.v1', revision=1)
    graph = transform(graph, parameterize)
    # Explicit per-task bindings are part of the recipe, not historical results.
    bindings = {path:copy.deepcopy(node['config'].get('task_inputs') or {}) for path, node in walk(graph)}
    return dict(format='mewcode.workflow.recipe.v1', id='', version=1, name=state['graph']['label'],
        source_run_id=source_run_id, created_at=time.time(), parameters={key:dict(type='string',example=value)
            for key,value in parameters.items()}, graph=normalize(graph), bindings=bindings,
        applicability=copy.deepcopy(state['graph'].get('applicability') or {}),
        conversation_summary=state.get('conversation_summary', ''), digest=digest(graph))


def instantiate(recipe, inputs):
    if recipe.get('format') != 'mewcode.workflow.recipe.v1':
        raise ValueError('未知流程格式')
    expected = set(recipe.get('parameters') or {})
    if set(inputs) != expected or any(not isinstance(value, str) or not value for value in inputs.values()):
        raise ValueError('请为保存流程的全部参数提供非空字符串，不要增加未声明参数')
    def bind(text):
        return re.sub(r'\{\{run_inputs\.([\w.-]+)\}\}', lambda match: inputs[match[1]], text)
    graph = normalize(transform(recipe['graph'], bind))
    graph['recipe'] = {key:recipe[key] for key in ('id', 'version', 'digest')}
    return graph, transform(recipe.get('bindings') or {}, bind)


class RecipeStore:
    def __init__(self, project):
        self.path = Path(project) / '.mewcode' / 'workflow-recipes.sqlite3'

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute('CREATE TABLE IF NOT EXISTS recipes (id TEXT NOT NULL, version INTEGER NOT NULL, '
                           'payload TEXT NOT NULL, PRIMARY KEY (id, version))')
        return connection

    def save(self, recipe):
        value = copy.deepcopy(recipe)
        value['id'] = value.get('id') or uuid.uuid4().hex
        value['digest'] = digest(value['graph'])
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT MAX(version) FROM recipes WHERE id=?', (value['id'],)).fetchone()
            value['version'] = (row[0] or 0) + 1
            connection.execute('INSERT INTO recipes VALUES (?, ?, ?)',
                (value['id'], value['version'], json.dumps(value, ensure_ascii=False, allow_nan=False)))
        connection.close()
        return value

    def read(self, identity, version=None):
        with self.connect() as connection:
            query = 'SELECT payload FROM recipes WHERE id=?' + (' AND version=?' if version is not None else '')
            row = connection.execute(query + ' ORDER BY version DESC LIMIT 1',
                (identity, version) if version is not None else (identity,)).fetchone()
        connection.close()
        if not row:
            raise ValueError('保存的流程版本不存在')
        return json.loads(row[0])

    def list(self):
        with self.connect() as connection:
            rows = connection.execute('SELECT payload FROM recipes r WHERE version='
                '(SELECT MAX(version) FROM recipes WHERE id=r.id) ORDER BY id').fetchall()
        connection.close()
        return [json.loads(row[0]) for row in rows]
