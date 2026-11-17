"""Host decision cards: typed permission requests precede material questions."""
from __future__ import annotations

import copy

from mewcode.experts.authorization_ledger import covers, valid_request


def elevation_card(record, case, plan, option, authority):
    authority = [valid_request(request) for request in authority]
    free = next((candidate for candidate in plan['options']
        if not candidate['needs']['authority'] and not candidate['changes_acceptance']
        and not candidate['needs']['capabilities'] and not candidate['needs']['user_input']), None)
    statements = [entry for entry in record.get('authorizations', {}).get('entries', [])
        if entry['auth_id'] in {request.get('based_on_statement') for request in authority}]
    input_requirements = list(option['needs']['user_input'])
    external_only = all(request['operation_class'] == 'external_send' for request in authority)
    return {'kind': 'elevation', 'round': len(case['rounds']), 'option': copy.deepcopy(option),
        'plan': copy.deepcopy(plan), 'authority': authority, 'statements': statements,
        'alternative': copy.deepcopy(free), 'input_requirements': input_requirements,
        'question': '继续当前步骤需要执行下列操作，现有授权尚未覆盖所列范围。',
        'cost_policy': str((record.get('run_inputs') or {}).get('model_cost_policy') or ''),
        'options': [{'value': 'approve', 'label': '允许发送所列内容' if external_only else '批准所列操作',
            'description': '仅批准卡片明确列出的操作范围；费用和其他必要信息仍须核对。' if input_requirements else
                '仅批准卡片明确列出的操作范围，不表示业务验收通过。'},
            {'value': 'alternative', 'label': '不提权，改用备选',
                'description': free['method'] if free else '当前方案没有无需提权的备选方法。'},
            {'value': 'close', 'label': '停止本次修复', 'description': '保留成果和检查点，当前步骤仍未完成。'}]}


def misrouted_elevation(record, case):
    """Migrate an old typed authorization hidden behind an input-only card."""
    pending = case.get('pending') or {}
    if case.get('status') != 'awaiting_user' or pending.get('kind') != 'input' or case.get('closed_by_user'):
        return None
    from mewcode.experts.node_ledger import ledger_for_run
    stage = record.get('node_states', {}).get(case['node_id'], {})
    if (ledger_for_run(record).view(case['node_id']).epoch != case['epoch']
            or stage.get('input_revision', '') != case['input_revision']):
        return None
    plan = pending.get('plan') or {}
    if plan.get('case_id') != case['case_id'] or plan.get('round') != len(case['rounds']) + 1:
        return None
    option = next((row for row in plan.get('options', []) if row['option_id'] == pending.get('option_id')), None)
    if not option:
        return None
    uncovered = []
    try:
        for request in option['needs']['authority']:
            valid_request(request)
            check = covers(record, {**request, 'scope': {**request['scope'], 'epoch': case['epoch'],
                'case_id': case['case_id'], 'round': len(case['rounds']) + 1, 'node_ids': [case['node_id']]}})
            if check.status == 'denied':
                return None
            if check.status != 'covered':
                uncovered.append(request)
    except (KeyError, TypeError, ValueError):
        return None
    return elevation_card(record, case, plan, option, uncovered) if uncovered else None
