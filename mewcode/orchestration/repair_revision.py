"""Explicit migration of a read-only self-loop into repair + independent review.

Only this topology extension is supported. It cannot change frozen criteria,
budgets, completed predecessors or unrelated parts of a saved graph.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.review_evidence import evidence_content, content_digest, queue_progress
from mewcode.orchestration.loops import LoopContract, compile_loops, repair_workers


def validate_repair_extension(old, new, loop_id):
    before, after = ({v['node_id']: v for v in f['nodes']} for f in (old, new))
    a_meta, b_meta = old.get('meta') or {}, new.get('meta') or {}
    a_loops, b_loops = ({v['loop_id']: LoopContract.from_value(v) for v in m.get('loops', [])}
                        for m in (a_meta, b_meta))
    if a_loops.keys() != b_loops.keys() or loop_id not in a_loops:
        raise ValueError('只允许扩展原有返修循环')
    a, b = a_loops[loop_id], b_loops[loop_id]
    reviewer = a.check_node_id
    if a.node_ids != (reviewer,) or a.entry_node_id != reviewer or before[reviewer]['role'] != 'auditor':
        raise ValueError('定向迁移仅支持只读审查自循环')
    repair = b.entry_node_id
    additions = after.keys() - before.keys()
    if not before.keys() <= after.keys() or repair not in additions or len(additions) != 2:
        raise ValueError('必须且只能新增返修执行者及后置人工签认门')
    gate = next(iter(additions - {repair}))
    if (b.node_ids != (repair, reviewer) or b.check_node_id != reviewer
            or b.reviewer_node_ids != a.reviewer_node_ids or b.repair_node_ids != (repair,) or not b.requires_repair):
        raise ValueError('返修执行者必须位于原审查者之前，且审查职责不变')
    allowed = {'entry_node_id', 'node_ids', 'requires_repair', 'repair_node_ids', 'exit_condition'}
    if {k: v for k, v in a.as_dict().items() if k not in allowed} != {
            k: v for k, v in b.as_dict().items() if k not in allowed}:
        raise ValueError('迁移不能更改验收条款、固定队列或累计限额')
    for key in a_loops.keys() - {loop_id}:
        if a_loops[key].contract_hash != b_loops[key].contract_hash:
            raise ValueError('迁移不能修改其他循环')
    clean = lambda n: {k: v for k, v in n.items() if k != 'position'}
    if any(clean(before[k]) != clean(after[k]) for k in before):
        raise ValueError('迁移保留全部原节点合同；新职责写入新增执行节点')
    for key in a_meta.keys() | b_meta.keys():
        if key in {'loops', 'pipeline_graph_version', 'pipeline_graph_updated_at'}:
            continue
        left, right = a_meta.get(key), b_meta.get(key)
        if key in {'node_kinds', 'node_plan_briefs'}:
            left, right = left or {}, right or {}
            if any(left.get(k) != right.get(k) for k in before) or set(right) - set(left) - additions:
                raise ValueError('迁移不能改变原节点的类别或计划')
        elif left != right:
            raise ValueError('迁移不能改变共享元数据：' + key)
    kinds = b_meta.get('node_kinds') or {}
    if kinds.get(gate) != 'gate' or after[repair]['role'] != 'executor':
        raise ValueError('新增节点必须是执行者与真正的人工 gate')
    # Preserve every original edge's condition/router; only insert the two nodes.
    expected = []
    for edge in old['edges']:
        item = dict(edge)
        if item['to_node'] == reviewer:
            item['to_node'] = repair
        if item['from_node'] == reviewer:
            if item.get('condition', 'always') != 'pass':
                raise ValueError('原审查后继必须以通过条件连接')
            item['from_node'] = gate
        expected.append(item)
    expected.extend([dict(from_node=repair, to_node=reviewer, condition='always', router='default'),
                     dict(from_node=reviewer, to_node=gate, condition='pass', router='default')])
    canonical = lambda edges: sorted(tuple(sorted(e.items())) for e in edges)
    if canonical(expected) != canonical(new['edges']):
        raise ValueError('连线不属于受支持的局部返修/签认扩展')
    parsed = ExpertFlowSpec.from_value(new)
    compile_loops(parsed.nodes, parsed.edges, parsed.meta.get('loops', []))
    if repair_workers(b, parsed.node_map) != [repair]:
        raise ValueError('返修节点没有声明可写能力')
    return dict(loop_id=loop_id, reviewer_node_id=reviewer, repair_node_id=repair, gate_node_id=gate,
                old_contract_hash=a.contract_hash, new_contract_hash=b.contract_hash)


def migrate_review_queue(snapshot, node_id, extension, root):
    """Merge same-contract archives without accepting stale or missing evidence."""
    queues = snapshot.setdefault('review_queues', {})
    current = queues.get(node_id)
    if not current:
        return {'preserved': 0, 'restored': 0, 'invalidated': 0}
    if root is None:
        raise ValueError('迁移台账需要原任务工作区以核实证据')
    root = Path(root).resolve()
    def path(value):
        result = (root / value).resolve(strict=True)
        if not result.is_relative_to(root) or not result.is_file() or result.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('证据不在工作区内或超过读取上限')
        return result
    def digest(evidence):
        source = path(evidence['path'])
        if evidence.get('typed_text'):
            return content_digest(evidence_content(source, items_path=evidence.get('items_path', ''),
                                                   selector=evidence.get('selector')))
        with source.open('rb') as handle:
            return hashlib.file_digest(handle, 'sha256').hexdigest()
    if (current['contract_hash'] != extension['old_contract_hash'] or
            digest({'path': current['manifest_path']}) != current['manifest_sha256']):
        raise ValueError('清单或原验收合同已变化，不能复用台账')
    definition = ('manifest_path', 'manifest_sha256', 'items_path', 'key_fields', 'filters',
                  'image_path_template', 'contract_hash', 'items')
    candidates = [current] + [a['queue'] for a in reversed(snapshot.get('review_queue_archives', []))
                             if a.get('node_id') == node_id and isinstance(a.get('queue'), dict)]
    snapshot.setdefault('review_queue_archives', []).append(
        dict(node_id=node_id, queue=copy.deepcopy(current), reason='before_repair_loop_migration'))
    migrated = copy.deepcopy(current)
    restored = invalidated = 0
    for candidate in candidates[1:]:
        if any(candidate.get(k) != current.get(k) for k in definition):
            continue
        for item_id, record in candidate.get('records', {}).items():
            if item_id not in migrated['records']:
                migrated['records'][item_id] = copy.deepcopy(record)
                restored += 1
                for key in ('image_receipts', 'text_receipts'):
                    if item_id in candidate.get(key, {}):
                        migrated.setdefault(key, {})[item_id] = copy.deepcopy(candidate[key][item_id])
    items = {v['item_id']: v for v in migrated['items']}
    for item_id, record in migrated['records'].items():
        valid = item_id in items
        try:
            evidence, images = record.get('evidence', []), record.get('images', [])
            valid = valid and len({v['path'] for v in evidence}) >= 2 and any(
                path(v['path']) == path(items[item_id]['image_path']) for v in images)
            valid = valid and all(digest(v) == v['sha256'] for v in evidence + images)
        except (ValueError, OSError, KeyError, TypeError):
            valid = False
        if not valid:
            migrated.setdefault('history', []).append(dict(item_id=item_id, previous=copy.deepcopy(record)))
            record.update(state='unknown', evidence_stale=True, summary='迁移核验：证据变化/缺失，须重新复核')
            invalidated += 1
    migrated.update(contract_hash=extension['new_contract_hash'], revision=migrated.get('revision', 0) + 1)
    migrated.setdefault('contract_migrations', []).append(copy.deepcopy(extension))
    queues[node_id] = migrated
    return dict(preserved=len(current['records']), restored=restored, invalidated=invalidated,
                progress=queue_progress(migrated))
