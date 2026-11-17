"""Retire one obsolete host prompt, without answering or clearing operations."""
from __future__ import annotations

import copy
import time


def retire_obsolete_question_blockers(record, node_id):
    """Archive only blockers bound to a host-retired technical prompt, never an answer."""
    if node_id not in (record.get('node_ledgers') or {}):
        return 0
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    ledger = ledger_for_run(record)
    view = ledger.view(node_id)
    retired = {f.payload.get('question_fact_id'): f for f in view.history
               if f.kind == 'maintenance' and f.source == 'host'
               and f.payload.get('reason') == 'technical_assistance_prompt'}
    count = 0
    for question in view.history:
        maintenance = retired.get(question.fact_id)
        if (question.kind != 'question' or maintenance is None
                or question.superseded_by != maintenance.fact_id or question.epoch != view.epoch):
            continue
        request_id = question.payload.get('request_id')
        if not request_id:
            continue
        for blocker in view.active_blockers:
            if (blocker.payload.get('kind') in {'human_question', 'user_input'}
                    and (blocker.payload.get('ref') or blocker.payload.get('request_id')) == request_id):
                ledger.supersede(blocker.fact_id, maintenance.fact_id)
                count += 1
    if count:
        project_node_view(record, node_id)
    return count


def retire_failed_assistance_prompt(record, node_id):
    stage = (record.get('node_states') or {}).get(node_id) or {}
    pending = stage.get('pending_interaction') or {}
    state = stage.get('scheduler_recovery') or {}
    decision = (state.get('decisions') or [{}])[-1]
    controller = decision.get('controller') or {}
    receipt = decision.get('assistance_receipt') or {}
    request = stage.get('assistance_request') or {}
    questions = pending.get('questions') or []
    helper = (record.get('node_states') or {}).get(node_id + '__assist_' + str(receipt.get('request_id') or '')[:12]) or {}
    if (pending.get('tool') != 'RecoveryDecision' or decision.get('action') != 'human'
            or decision.get('source') != 'primary_model_assistance'
            or controller.get('action') not in {'assist', 'patch_node'}
            or receipt.get('next_step') not in {'ask_human', 'fail'}
            or not receipt.get('request_id') or request.get('request_id') != receipt['request_id']
            or request.get('status') != 'completed' or receipt.get('question')
            or (helper.get('handoff') or {}).get('question') or (stage.get('handoff') or {}).get('question')
            or stage.get('pending_permission') or len(questions) != 1):
        return False
    question = questions[0]
    if not isinstance(question, dict):
        return False
    reason = str(controller.get('reason') or '')
    message = str(question.get('message') or '') if isinstance(question, dict) else ''
    if (not reason or question.get('type') != 'text' or question.get('name') != 'direction'
            or not message.startswith('节点「')
            or f'」需要具体决定：{reason} 已尝试：' not in message
            or not message.endswith('。请补充业务方向、必要材料或所需授权的处理方式。')):
        return False
    request_id = pending.get('request_id')
    if any(row.get('answered') and (row.get('pending') or {}).get('request_id') == request_id
           for row in stage.get('interaction_history') or []):
        return False
    if node_id not in (record.get('node_ledgers') or {}):
        return False
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    ledger = ledger_for_run(record)
    view = ledger.view(node_id)
    fact = next((f for f in view.history if f.fact_id == pending.get('question_fact_id')
                 and f.kind == 'question' and f.epoch == view.epoch and not f.superseded_by
                 and f.payload.get('request_id') == request_id
                 and f.payload.get('questions') == questions), None)
    if fact is None or any(f.kind == 'answer' and f.payload.get('question_fact_id') == fact.fact_id
                           for f in view.history):
        return False
    retired = ledger.record(node_id, 'maintenance', {'reason': 'technical_assistance_prompt',
        'question_fact_id': fact.fact_id, 'assistance_request_id': receipt['request_id']})
    ledger.supersede(fact.fact_id, retired.fact_id)
    retire_obsolete_question_blockers(record, node_id)
    stage.setdefault('interaction_history', []).append({'pending': copy.deepcopy(pending),
        'status': 'superseded', 'answered': False, 'reason': 'technical_assistance_prompt', 'at': time.time()})
    stage.pop('pending_interaction')
    from mewcode.permissions.reviewer import redact_sensitive
    notes = redact_sensitive(str(receipt.get('notes') or '主模型协助未完成，检查点已保留。'))
    decision.update(action='hold_until_clearance', execution_status='已停止', error=notes,
        host_check=notes, chosen={'action': 'hold_until_clearance', 'next_step': notes})
    from mewcode.experts.auto_card import auto_card
    decision['presentation'] = auto_card(decision, stage)
    decision['presentation'].update(title='主模型协助未完成，已停止', reason=notes,
        next_step='核对协助的失败回执与未完成条件后，从原检查点接续。')
    state['phase'] = 'stopped'
    project_node_view(record, node_id)
    return True
