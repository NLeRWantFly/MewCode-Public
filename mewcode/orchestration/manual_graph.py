"""Versioned user edits to the existing planning document, never a run."""
from __future__ import annotations

import copy
import json
import uuid

from .draft_sources import digest
from .draft_store import DraftStore, now
from .task_design import TaskDesign
from .workflow_blocks import _baseline
from .workflow_draft import validate_entry, check_entry_refs


def snapshot(state, base=None):
    design = state.get('task_design') or {}
    document = _baseline(state, base)
    request = design.get('build_request') or {}
    editable = not (design.get('run_id') or request.get('execution_claim_id') or request.get('execution_state')
                    or design.get('status') in {'running', 'completed', 'queued', 'building'})
    return dict(editable=editable, basis=digest([design, document, (state.get('materials') or {}).get('sequence'),
        (state.get('candidate') or {}).get('sequence'), base, state.get('workbench_positions')]),
        positions=copy.deepcopy(state.get('workbench_positions') or {}))


def edit(project, cid, body, *, base=None, guard=lambda: None):
    store = DraftStore(project, cid)
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        state = store._get(db)
        design = state.get('task_design') or {}
        current = snapshot(state, base)
        empty = not design and not _baseline(state, base)['entries']
        if (body.get('run_id') or not current['editable'] or
                body.get('task_id', '') != design.get('task_id', '') or
                body.get('basis') != current['basis'] and not (empty and body.get('basis') == '')):
            raise ValueError('草稿或运行状态已变化，请刷新后重试；未覆盖新内容。')
        guard()
        operation = body.get('operation') or {}
        if not isinstance(operation, dict):
            raise ValueError('编辑操作必须为对象。')
        action = operation.get('action')
        document = _baseline(state, base)
        before = {row['id']: copy.deepcopy(row) for row in document['entries']}
        rows = {key: copy.deepcopy(row) for key, row in before.items()}
        changed = set()
        def save_positions(positions):
            import math
            if not isinstance(positions, dict) or len(positions) > 1000:
                raise ValueError('节点位置格式错误。')
            for key, point in positions.items():
                if (key not in rows or rows[key]['kind'] != 'node' or not isinstance(point, dict)
                        or set(point) != {'x', 'y'} or any(type(v) not in {int, float}
                        or not math.isfinite(v) or abs(v) > 100000 for v in point.values())):
                    raise ValueError('节点位置无效。')
            state['workbench_positions'] = copy.deepcopy(positions)
        if action == 'positions':
            save_positions(operation.get('positions'))
        elif action == 'delete':
            key = operation.get('id')
            row = rows.get(key)
            if not row or row['kind'] not in {'node', 'edge'} or row.get('state') == 'retired':
                raise ValueError('节点或连线不存在，请刷新。')
            changed.add(key)
            if row['kind'] == 'node':
                changed.update(e['id'] for e in rows.values() if e['kind'] == 'edge'
                    and key in {e['data'].get('from_node'), e['data'].get('to_node')})
            for key in changed:
                rows[key]['state'] = 'retired'
        elif action == 'upsert':
            kind, key = operation.get('kind'), operation.get('id')
            if kind not in {'node', 'edge'}:
                raise ValueError('只能编辑节点和连线。')
            if key and (key not in rows or rows[key]['kind'] != kind or rows[key].get('state') == 'retired'):
                raise ValueError('节点或连线不存在，请刷新。')
            key = key or ('n_' if kind == 'node' else 'e_') + uuid.uuid4().hex[:16]
            row = copy.deepcopy(rows.get(key) or dict(id=key, kind=kind, title='', body='', data={}, sources=[], state='active'))
            fields = operation.get('fields')
            if not isinstance(fields, dict) or set(fields) - {'title', 'body', 'data'}:
                raise ValueError('只允许修改名称、目标和节点属性。')
            data = fields.get('data', {})
            allowed = {'node_kind', 'inputs', 'outputs', 'acceptance', 'model'} if kind == 'node' else {
                'from_node', 'to_node', 'condition', 'input_when', 'branch_group'}
            if not isinstance(data, dict) or set(data) - allowed:
                raise ValueError('节点或连线属性不受支持。')
            row.update({k: v for k, v in fields.items() if k != 'data'})
            row['data'].update(data)
            if kind == 'node' and not str(row.get('title') or '').strip():
                raise ValueError('请填写节点名称。')
            if kind == 'edge':
                a, b = row['data'].get('from_node'), row['data'].get('to_node')
                if a == b or any(target not in rows or rows[target]['kind'] != 'node'
                    or rows[target].get('state') == 'retired' for target in [a, b]):
                    raise ValueError('请选择两个不同的有效节点。')
                def signature(e):
                    value = e.get('data', {})
                    return [value.get('from_node'), value.get('to_node'), value.get('condition', 'always'),
                            value.get('input_when'), value.get('branch_group') or '']
                if any(e['kind'] == 'edge' and e['id'] != key and e.get('state') != 'retired'
                       and signature(e) == signature(row) for e in rows.values()):
                    raise ValueError('这条连线已经存在。')
            validate_entry(row, orchestration_mode=design.get('orchestration_mode', 'classic'))
            rows[key] = row
            changed.add(key)
        else:
            raise ValueError('未知编辑操作。')
        if changed:
            # Allow repair of an incomplete draft without making an unrelated
            # old reference error a prerequisite for every canvas operation.
            for entry_id in rows:
                try:
                    check_entry_refs(rows.values(), focus={entry_id})
                except ValueError as error:
                    try:
                        check_entry_refs(before.values(), focus={entry_id})
                    except ValueError as previous:
                        if str(previous) == str(error):
                            continue
                    raise error
            if 'positions' in body:
                save_positions(body['positions'])
            stamp = now()
            source_text = json.dumps(operation, ensure_ascii=False, sort_keys=True)
            source = dict(id='s_manual_' + uuid.uuid4().hex, role='user', kind='workflow_material',
                text=source_text, line='manual:canvas', part=1, parts=1, at=stamp)
            for key in changed:
                rows[key].update(evidence='manual', reviewed=True)
                rows[key]['sources'] = rows[key].get('sources', []) + [dict(source_id=source['id'], quote=source_text[:1000])]
            document['entries'] = list(rows.values())
            materials = state.setdefault('materials', {})
            materials.update(document=document, sequence=materials.get('sequence', 0) + 1, base_spec=copy.deepcopy(base))
            materials.setdefault('sources', []).append(source)
            materials.setdefault('source_order', []).append(source['id'])
            materials.setdefault('versions', []).append(dict(sequence=materials['sequence'], at=stamp,
                turn_id='manual:canvas', changes=[dict(entry_id=k, before=before.get(k), after=rows[k]) for k in sorted(changed)]))
            materials.pop('last_submission', None)
            materials.pop('last_result_basis', None)
            if not design:
                design = dict(task_id=uuid.uuid4().hex, intent='plan', orchestration_mode='classic', goal='',
                    created_at=stamp, build_attempts=0,
                    intent_authority=dict(source='host_entry', intent='plan', selected_at=stamp))
            TaskDesign._fence_request(design, 'superseded')
            design.update(status='paused', reason='手动修改已保存到草稿，可继续完善或重新构图校验。',
                revision=design.get('revision', 0) + 1, updated_at=stamp)
            state['task_design'] = design
        guard()
        store._put(db, state)
        return dict(ok=True, changed_ids=sorted(changed), execution_started=False)
