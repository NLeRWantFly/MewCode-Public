"""Deterministic pause explanation derived from the current host checkpoint."""
from mewcode.experts.recovery_state import blockers


def pause_summary(record):
    rows = []
    for node_id, stage in (record.get('node_states') or {}).items():
        if stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}:
            continue
        case = stage.get('recovery_case') or {}
        handling = case.get('handling') or {}
        pending = case.get('pending') or stage.get('pending_interaction') or {}
        for blocker in blockers(stage, error_kind=stage.get('error_kind', '')):
            resolution = next((row for row in stage.get('blocker_resolution', [])
                               if row.get('fact_id') == blocker.get('fact_id')), {})
            rows.append({'node_id': node_id, 'blocker': blocker['kind'],
                'owner': resolution.get('owner') or handling.get('owner') or '未知',
                'next': resolution.get('next') or (handling.get('summary') or {}).get('recommendation') or pending.get('question') or '未知',
                'user_action': pending.get('question') if handling.get('owner') == 'user' else '等待系统处理'})
    return rows


def pause_reason(record, fallback='已暂停'):
    rows = pause_summary(record)
    if not rows:
        return fallback
    return '\n'.join(f"节点 {row['node_id']}：{row['blocker']}；处理人：{row['owner']}；"
                     f"下一步：{row['next']}；你可以：{row['user_action'] or '未知'}" for row in rows)
