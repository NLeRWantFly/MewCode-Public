"""Native workflow plans, before a run exists. No model review or execution."""
from __future__ import annotations

import copy
import json
import uuid

from .schema import digest, normalize, walk


def spec_for_graph(graph, original=None):
    """Keep the native graph intact inside the conversation's existing plan ledger."""
    from mewcode.orchestration.spec import OrchestrationSpec
    graph = normalize(graph)
    raw = copy.deepcopy(original or {})
    raw.update(label=graph['label'], goal=graph['goal'], version=graph['revision'],
        require_confirm_gate=False, finalizer_node_id=graph['nodes'][-1]['id'] if graph['nodes'] else '',
        nodes=[dict(node_id=n['id'], kind='implement', label=n['label'], goal=n['task'],
                    model=n.get('model'), bind_later=not bool(n.get('model')),
                    plan_brief=dict(summary=n['task'], known_inputs=n['inputs'], outputs=n['outputs']),
                    **({'position': graph['layout'][n['id']]} if n['id'] in graph['layout'] else {}))
               for n in graph['nodes']],
        edges=[dict(from_node=e['source'], to_node=e['target'], condition=e['port']) for e in graph['edges']])
    meta = raw.setdefault('meta', {})
    for key in ('loops', 'canvas_groups', 'node_kinds', 'node_configs', 'node_constraints'):
        meta.pop(key, None)
    meta.update(workflow_core=graph, orchestration_mode='classic')
    return OrchestrationSpec.from_value(raw)


def from_material_spec(raw, previous=None):
    """Compile authored blocks; preserve native components when editing their prose."""
    from .migration import graph_from_flow
    source = copy.deepcopy(raw)
    source.setdefault('meta', {}).pop('workflow_core', None)
    graph = graph_from_flow(source, goal=source.get('goal', ''))
    if source['meta'].get('loops'):
        # Legacy loops used separately addressable body nodes. Their external
        # consumers now depend on the loop component and its published outputs.
        for node in graph['nodes']:
            if node['kind'] == 'loop' and node['id'] not in {n['node_id'] for n in raw.get('nodes', [])}:
                node['outputs'] = list(dict.fromkeys(output for _, child in walk(node['config']['graph'])
                                                    for output in child.get('outputs', [])))
        aliases = {n['id']: path.split('/')[0] for path, n in walk(graph) if '/' in path}
        raw = copy.deepcopy(raw)
        metadata = raw.setdefault('meta', {}).get('workflow_draft') or {}
        for bindings in metadata.get('node_input_bindings', {}).values():
            for name, target in list(bindings.items()):
                bindings[name] = aliases.get(target, target)
        for value in (raw.get('goal_contract') or {}).get('run_inputs', []):
            value['collect_at'] = aliases.get(value.get('collect_at'), value.get('collect_at', 'start'))
        root_ids = {node['id'] for node in graph['nodes']}
        graph['layout'] = {key: point for key, point in graph['layout'].items() if key in root_ids}
    authored = {n['node_id']: n for n in raw.get('nodes', [])}
    for node in graph['nodes']:
        changed = authored.get(node['id'], {})
        if changed.get('kind') in {'work', 'judge', 'branch', 'loop'} and 'config' in changed:
            node['config'] = copy.deepcopy(changed['config'])
    before = ((previous or {}).get('meta') or {}).get('workflow_core')
    if not before and any(n.get('kind') not in {'work', 'judge', 'branch', 'loop'}
                          for n in raw.get('nodes', [])):
        # Legacy cards were narrower, and collapsed loop components no longer
        # share their body nodes' old coordinates. Let the native canvas lay
        # out the first migration; native user positions survive later edits.
        graph['layout'] = {}
        for _, node in walk(graph):
            if node['kind'] == 'loop':
                node['config']['graph']['layout'] = {}
    if before:
        old = {n['id']: n for n in before['nodes']}
        wrappers = {n['node_id']: n for n in previous.get('nodes', [])}
        for index, node in enumerate(graph['nodes']):
            prior, wrapper, changed = old.get(node['id']), wrappers.get(node['id']), authored.get(node['id'])
            if not prior or not wrapper or not changed:
                continue
            merged = copy.deepcopy(prior)
            for field in ('label', 'task', 'model', 'inputs', 'outputs'):
                if field != 'model' or node[field] is not None or field in merged:
                    merged[field] = copy.deepcopy(node[field])
            # Compatibility wrappers are all work nodes; their unchanged kind
            # must not flatten a branch, judgement or nested loop.
            if changed.get('kind') != wrapper.get('kind'):
                merged.update(kind=node['kind'], config=node['config'])
            elif 'config' in changed:
                merged['config'] = copy.deepcopy(changed['config'])
            for field in ('tools', 'deny_tools'):
                if field in changed:
                    merged[field] = copy.deepcopy(changed[field])
            graph['nodes'][index] = merged
        edge_groups = {}
        for edge in before['edges']:
            edge_groups.setdefault((edge['source'], edge['target'], edge['port']), []).append(edge)
        for index, edge in enumerate(graph['edges']):
            matches = edge_groups.get((edge['source'], edge['target'], edge['port']))
            if matches:
                graph['edges'][index] = copy.deepcopy(matches.pop(0))
    graph = normalize(graph)
    graph['revision'] = before['revision'] if before else raw.get('version', 1)
    if before:
        if graph != before:
            graph['revision'] = before['revision'] + 1
        else:
            graph = copy.deepcopy(before)
    return spec_for_graph(graph, raw)


def plan_spec(state, base, cid):
    from mewcode.orchestration.workflow_blocks import _baseline
    from mewcode.orchestration.workflow_draft import project_graph
    return project_graph(_baseline(state, base), base, conversation_id=cid,
                         revision=state.get('revision', 0), native=True)


def sync_materials(state, spec, *, previous=None, operation=None):
    """Adopt the saved native structure without resurrecting old loop members."""
    from mewcode.orchestration.draft_store import now
    from mewcode.orchestration.draft_sources import digest as source_digest
    from mewcode.orchestration.workflow_draft import seed_document
    previous = previous or (state.get('materials') or {}).get('document') or state.get('document') or {'entries': []}
    prior = {e['id']: copy.deepcopy(e) for e in previous['entries']}
    seeded = seed_document(spec, previous=previous)
    rows = {e['id']: e for e in seeded['entries']}
    nested = {node['id']: path.split('/')[0] for path, node in walk(spec['meta']['workflow_core']) if '/' in path}
    for key, entry in prior.items():
        if key in rows:
            continue
        entry = copy.deepcopy(entry)
        if entry['kind'] in {'node', 'edge', 'loop'} or entry.get('data', {}).get('node_id') in nested:
            entry['state'] = 'retired'
            if key in nested:
                entry['superseded_by'] = nested[key]
        rows[key] = entry
    changed = [key for key, value in rows.items() if value != prior.get(key)]
    materials = state.setdefault('materials', {})
    stamp = now()
    if operation and changed:
        source_text = json.dumps(operation, ensure_ascii=False, sort_keys=True)
        source = dict(id='s_manual_' + uuid.uuid4().hex, role='user', kind='workflow_material', text=source_text,
                      line='manual:canvas', part=1, parts=1, at=stamp)
        materials.setdefault('sources', []).append(source)
        materials.setdefault('source_order', []).append(source['id'])
        for key in changed:
            rows[key].update(evidence='manual', reviewed=True)
            rows[key].setdefault('sources', []).append(dict(source_id=source['id'], quote=source_text[:1000]))
    materials.update(document={**seeded, 'entries': list(rows.values())}, base_spec=copy.deepcopy(spec),
                     base_graph_hash=source_digest(spec), sequence=materials.get('sequence', 0) + 1)
    materials.setdefault('versions', []).append(dict(sequence=materials['sequence'], at=stamp,
        turn_id='manual:canvas' if operation else 'workflow:compile',
        changes=[dict(entry_id=key, before=prior.get(key), after=copy.deepcopy(rows[key])) for key in changed]))
    materials.pop('last_submission', None)
    materials.pop('last_result_basis', None)
    return changed


def design_projection(view, state, base, cid):
    from .state import fresh
    from .projection import view as project
    try:
        spec = plan_spec(state, base, cid)
        graph = spec.meta['workflow_core']
        error = ''
    except ValueError as exc:
        graph = ((base or {}).get('meta') or {}).get('workflow_core') or normalize({})
        error = str(exc)
    record = dict(run_id='', conversation_id=cid, workflow_core=fresh(graph))
    native = project(record)
    canvas = state.get('workflow_canvas') or {}
    native['editor'].update(has_draft=False, planning=True, error=error,
        can_undo=bool(canvas.get('history')), can_redo=bool(canvas.get('future')))
    native['editor']['editable'] = not (view['task'].get('run_id') or
        (state.get('task_design') or {}).get('status') in {'queued', 'building', 'running', 'completed'} or
        ((state.get('task_design') or {}).get('build_request') or {}).get('execution_state'))
    view.update(structure=native['structure'], plan=native['plan'], editor=native['editor'],
                workflow_revision=graph['revision'])
    return view


def model_nodes(graph):
    return [(path, n) for path, n in walk(graph) if n['kind'] == 'work' or
            n['kind'] == 'judge' and not n['config'].get('manual') and 'expression' not in n['config']]
