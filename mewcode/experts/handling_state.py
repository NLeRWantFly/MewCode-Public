"""Read-only UI projection of actual work and current intervention ownership."""
from __future__ import annotations

import copy


def handling_for_snapshot(snapshot):
    states = snapshot.get('node_states') or snapshot.get('stages') or {}
    cursor = snapshot.get('stage_cursor') or ''
    stage = states.get(cursor) or {}
    case = stage.get('recovery_case') or {}
    active = [value for value in snapshot.get('active_node_ids') or []
              if (states.get(value) or {}).get('status') == 'running'
              and not (states.get(value) or {}).get('pending_interaction')
              and not (states.get(value) or {}).get('pending_permission')]
    helpers = [row for key, row in states.items() if key.startswith(cursor + '__')
               and row.get('status') == 'running'
               and not row.get('pending_interaction') and not row.get('pending_permission')] if cursor else []
    if active or helpers:
        return {'owner': 'system', 'handling_state': 'working', 'reason': '正在执行当前处理步骤。'}
    if case.get('status') == 'open' and case.get('pending'):
        return {'owner': 'system', 'handling_state': 'blocked',
                'reason': '已提交的选择尚未接续；恢复状态需要重新核对。'}
    if case.get('closed_by_user'):
        return {'owner': 'system', 'handling_state': 'blocked', 'reason_kind': 'stopped',
                'reason': '本次修复已停止，原检查点已保留。'}
    handling = (stage.get('handling') if case.get('status') == 'superseded'
                else case.get('handling') or stage.get('handling'))
    if isinstance(handling, dict):
        result = copy.deepcopy(handling)
        if result.get('handling_state') == 'working' and stage.get('status') in {'paused', 'needs_attention'}:
            result.update(handling_state='blocked', reason='当前没有活动调用，处理停在已保存的检查点。')
        return result
    if stage.get('pending_interaction') or stage.get('pending_permission'):
        return {'owner': 'user', 'handling_state': 'needs_user', 'reason': '等待你确认已展示的具体事项。'}
    if case.get('status') in {'reported', 'awaiting_user'} and case.get('pending'):
        return {'owner': 'user', 'handling_state': 'needs_user', 'reason': '等待当前问题的回答。'}
    if stage.get('status') in {'paused', 'needs_attention', 'blocked'}:
        return {'owner': 'system', 'handling_state': 'blocked', 'reason': '处理已停在保存的检查点，需要系统核对原因。'}
    return None
