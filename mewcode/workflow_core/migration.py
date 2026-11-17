"""Lossless legacy archives, explicit control components and reusable deliveries."""
from __future__ import annotations

import copy
import time

from .schema import FORMAT, ALIASES, normalize, walk
from .state import current, fresh


def graph_from_flow(flow, *, goal=''):
    raw = flow.as_dict() if hasattr(flow, 'as_dict') else copy.deepcopy(flow)
    if raw.get('format') == FORMAT:
        return normalize(raw)
    meta = raw.get('meta') or {}
    if meta.get('workflow_core'):
        return normalize(meta['workflow_core'])
    kinds = meta.get('node_kinds') or {}
    briefs = meta.get('node_plan_briefs') or {}
    graph = dict(format=FORMAT, revision=1, label=raw.get('label') or raw.get('name') or '工作流',
                 goal=goal or raw.get('goal') or meta.get('goal') or '', nodes=[], edges=[], layout={})
    for item in raw.get('nodes', []):
        key = item.get('node_id') or item.get('id')
        kind = kinds.get(key) or item.get('kind')
        if kind in {'role', None}:
            kind = 'judge' if item.get('role') == 'auditor' else 'work'
        kind = ALIASES.get(kind, kind)
        config = copy.deepcopy((meta.get('node_configs') or {}).get(key) or item.get('config') or {})
        brief = briefs.get(key) or item.get('plan_brief') or {}
        from mewcode.orchestration.convert import clean_generated_responsibility
        task = item.get('goal') or item.get('task') or brief.get('summary') or clean_generated_responsibility(item.get('responsibility') or '')
        if brief.get('steps'):
            task += '\n' + '\n'.join(brief['steps'])
        notes = (meta.get('node_constraints') or {}).get(key) or []
        if notes:
            task += '\n要求：\n' + '\n'.join(str(note.get('text') or note.get('body') or note) if isinstance(note, dict) else str(note) for note in notes)
        if kind == 'judge':
            config.setdefault('criteria', item.get('acceptance') or item.get('rubric') or brief.get('acceptance') or [])
            if kinds.get(key) == 'gate' or item.get('kind') == 'gate':
                config['manual'] = True
        if kind == 'pipeline':
            kind = 'work'
            config['legacy_pipeline'] = copy.deepcopy(item)
        graph['nodes'].append(dict(id=key, kind=kind, label=item.get('label') or key, task=task,
            inputs=copy.deepcopy(item.get('inputs') or brief.get('known_inputs') or []), outputs=copy.deepcopy(item.get('outputs') or brief.get('outputs') or []),
            model=copy.deepcopy(item.get('model')), config=config,
            **{name: copy.deepcopy(item[name]) for name in ('tools', 'deny_tools') if name in item}))
        if item.get('position'):
            graph['layout'][key] = copy.deepcopy(item['position'])
    branches = {}
    for index, edge in enumerate(raw.get('edges', [])):
        source = edge.get('from_node') or edge.get('source') or edge.get('from')
        port = edge.get('condition') or edge.get('port') or 'always'
        condition = edge.get('input_when')
        if condition:
            key = (source, condition['input'])
            if key not in branches:
                branch = 'branch_' + str(index)
                branches[key] = branch
                graph['nodes'].append(dict(id=branch, kind='branch', label='按 ' + condition['input'] + ' 分支',
                    config={'source': 'inputs.' + condition['input']}))
                graph['edges'].append(dict(id='branch_input_' + str(index), source=source, target=branch, port=port))
            source, port = branches[key], condition['equals']
        graph['edges'].append(dict(id=edge.get('edge_id') or edge.get('id') or f'e_{index}', source=source,
            target=edge.get('to_node') or edge.get('target') or edge.get('to'), port=port))
    loops = copy.deepcopy(meta.get('loops') or [])
    # Migrate inner regions first; enclosing loops then contain these explicit components.
    replacements = {}
    for loop in sorted(loops, key=lambda item: len(item.get('node_ids') or [])):
        members = {replacements.get(key, key) for key in loop.get('node_ids', [])}
        members.update(replacements.get(loop.get(key), loop.get(key)) for key in ('entry_node_id', 'check_node_id'))
        members.discard(None)
        loop_id = 'loop_' + str(loop['loop_id'])
        if loop_id in {node['id'] for node in graph['nodes']}:
            raise ValueError(f'循环 {loop["loop_id"]} 与节点 ID 冲突，请重命名该组件')
        inside = [node for node in graph['nodes'] if node['id'] in members]
        if not inside:
            raise ValueError(f'循环 {loop["loop_id"]} 的循环体不存在')
        check = replacements.get(loop.get('check_node_id'), loop.get('check_node_id'))
        for child in inside:
            if child['id'] == check and child['kind'] == 'judge':
                child['config']['legacy_loop_decision'] = True
        body_edges, outside_edges = [], []
        for edge in graph['edges']:
            a, b = edge['source'] in members, edge['target'] in members
            if edge['port'] in {'loop', 'repeat', 'continue', 'loop:continue'} and a and b:
                continue
            if a and b:
                body_edges.append(edge)
            elif a or b:
                changed = copy.deepcopy(edge)
                changed['source' if a else 'target'] = loop_id
                if a:
                    changed['port'] = 'always'
                outside_edges.append(changed)
            else:
                outside_edges.append(edge)
        config = dict(mode='foreach' if loop.get('mode') == 'foreach' else 'until',
                      graph=dict(nodes=inside, edges=body_edges,
                                 layout={key: point for key, point in graph['layout'].items() if key in members}),
                      until={'path': f'results.{check}.value', 'equals': True})
        if config['mode'] == 'foreach':
            config['items'] = copy.deepcopy(loop.get('items') or [])
        # Only explicit user resource settings survive; old defaults are not new policy.
        if isinstance(loop.get('user_limits'), dict):
            config['limits'] = copy.deepcopy(loop['user_limits'])
        elif (meta.get('workflow_user_limits') or {}).get(loop['loop_id']):
            config['limits'] = copy.deepcopy(meta['workflow_user_limits'][loop['loop_id']])
        graph['nodes'] = [node for node in graph['nodes'] if node['id'] not in members]
        graph['nodes'].append(dict(id=loop_id, kind='loop', label=loop.get('label') or loop['loop_id'],
                                  task=loop.get('exit_condition') or '', config=config))
        unique = {}
        for edge in outside_edges:
            unique.setdefault((edge['source'], edge['target'], edge['port']), edge)
        graph['edges'] = list(unique.values())
        for key in loop.get('node_ids', []):
            replacements[key] = loop_id
    return normalize(graph)


def migrate(record, graph=None):
    if record.get('workflow_core'):
        return record['workflow_core']
    graph = graph or graph_from_flow(record.get('locked_flow') or {}, goal=record.get('task', ''))
    archive = {key: copy.deepcopy(record.get(key)) for key in (
        'locked_flow', 'collaboration', 'node_states', 'stages', 'loop_states', 'kernel_calls',
        'orchestration_mode', 'status', 'attempts', 'flow_state', 'role_models') if key in record}
    graph = copy.deepcopy(graph)
    for path, node in walk(graph):
        locked = (record.get('locked_models') or {}).get(path) or (record.get('locked_models') or {}).get(node['id'])
        if locked:
            node['model'] = copy.deepcopy(locked)
    state = fresh(graph, imported={'at': time.time(), 'from': record.get('orchestration_mode', 'classic')})
    state['inputs'] = copy.deepcopy(record.get('run_inputs') or {})
    stages = record.get('node_states') or record.get('stages') or {}
    for path, node in walk(graph):
        original = stages.get(path) or stages.get(node['id']) or {}
        row = current(state, path)
        status = original.get('status', 'pending')
        delivery = original.get('handoff') or {}
        # A delivered attempt rejected only by quality review remains usable.
        collab = record.get('collaboration') or {}
        attempts = [item for item in (collab.get('attempts') or {}).values() if item.get('contract_id') == node['id']]
        for item in reversed(attempts):
            if (item.get('delivery') or {}).get('status') == 'completed':
                delivery = item['delivery']
                break
        if not original and not delivery:
            continue
        unknown = any(tool.get('status') in {'running', 'outcome_unknown', 'unknown'}
                      for tool in (original.get('tools') or {}).values())
        delivered = status == 'completed' or delivery.get('status') == 'completed'
        result = dict(status='completed' if delivered else 'unknown' if unknown else 'failed' if status == 'failed' else 'paused',
                      text=delivery.get('text') or original.get('text') or original.get('partial_text') or '',
                      artifacts=copy.deepcopy(delivery.get('artifacts') or original.get('artifacts') or []))
        if unknown and not delivered:
            result.update(status='unknown', reason='上次工具结果未知，请核对实际结果')
        verdict = original.get('verdict') or original.get('loop_decision') or {}
        if node['kind'] == 'judge' and delivered:
            if verdict.get('verdict') in {'PASS', 'NEEDS_ATTENTION'}:
                result['value'] = verdict['verdict'] == 'PASS'
            elif verdict.get('action') in {'EXIT', 'CONTINUE'}:
                result['value'] = verdict['action'] == 'EXIT'
            else:
                result['status'] = 'pending'
        identity = 'import_' + str(len(state['attempts']) + 1)
        state['attempts'][identity] = dict(id=identity, node_id=path, number=1, generation=0,
            graph_revision=graph['revision'], iteration=[], status=result['status'], result=result,
            inputs={}, progress=[], diagnostics=[], artifacts=result['artifacts'],
            messages=copy.deepcopy(original.get('messages') or []), pending_tools=copy.deepcopy(original.get('tools') or {}),
            started_at=original.get('started_at', time.time()), imported=True)
        row.update(status=result['status'], result=result, attempt_id=identity, previous_attempt_id=identity)
    # Recover the active item/round; completed outside stages must not be replayed.
    for path, node in walk(graph):
        if node['kind'] != 'loop':
            continue
        old_id = node['id'].removeprefix('loop_')
        prior = (record.get('loop_states') or {}).get(old_id)
        if not prior:
            continue
        row = current(state, path)
        round_index = (int(prior.get('item_index') or 0) if node['config'].get('mode') == 'foreach'
                       else max(0, int(prior.get('round') or 1) - 1))
        row.update(status='completed' if prior.get('status') == 'completed' else 'running',
                   loop=dict(round=round_index, items=node['config'].get('items', []), results=[], inputs={},
                             active=prior.get('status') != 'completed', elapsed=float(prior.get('elapsed') or 0),
                             tokens=int(prior.get('tokens') or 0), started_at=time.time(), round_started_at=time.time()))
        if row['status'] == 'completed':
            row['result'] = dict(status='completed', text=prior.get('summary') or '已完成循环', artifacts=[])
    state['status'] = 'paused'
    record['workflow_legacy'] = archive
    record['workflow_core'] = state
    # Keep the legacy mode fields frozen for readers that still display old records.
    # All dispatch checks workflow_core first; no legacy acceptance can run again.
    return state
