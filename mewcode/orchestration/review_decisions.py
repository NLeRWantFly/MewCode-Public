"""Turn a blocking review choice into the existing durable question flow."""
from __future__ import annotations

import copy
import time

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.events import digest
from mewcode.orchestration import ask_gateway


def question_for(issue):
    decision = issue.get('decision')
    if not decision and issue.get('resolution') == 'capability_gap':
        decision = {'question': '当前审核缺少必要的能力或证据，下一步如何处理？',
            'scope': issue.get('description') or '当前能力或证据尚未满足冻结验收要求。',
            'options': [
                {'value': 'request_revision', 'label': '提出要求或方案修订', 'effect': 'request_revision',
                 'description': '进入现有修订入口明确变更，修订生效前保持未通过；不将缺失证据视为已验证。'},
                {'value': 'defer', 'label': '等待补齐能力或证据', 'effect': 'defer',
                 'description': '保留现有要求、成果和未决事项；补齐后再继续核验。'}]}
    if not decision:
        # Old saved verdicts have no alternatives. Offer only explicit next
        # steps, never invent an evidence waiver or a broader authorization.
        decision = {'question': '这项核验尚未解决，请选择后续处理方式。',
            'scope': issue.get('description') or issue.get('acceptance_test') or '当前审核存在未解决的必需条件。',
            'options': [
                {'value': 'review_existing_evidence', 'label': '核对已有证据', 'effect': 'review',
                 'description': '只重新核验当前审核，保留原要求及已完成产物。'},
                {'value': 'request_revision', 'label': '提出要求修订', 'effect': 'request_revision',
                 'description': '转到现有要求修订入口；采用修订前保持未通过。'},
                {'value': 'defer', 'label': '暂时保留', 'effect': 'defer',
                 'description': '保留当前成果和未决状态。'}]}
    name = 'review_' + digest(issue.get('issue_id') or issue)[:16]
    question = {'name': name, 'question': decision['question'], 'context': decision['scope'],
        'type': 'choice', 'allow_other': False, 'requires_user_answer': True,
        'options': [{key: option[key] for key in ('value', 'label', 'description')} for option in decision['options']]}
    return question, {option['value']: option['effect'] for option in decision['options']}


def source_basis(record, node_id):
    basis = ask_gateway.decision_basis(record, node_id, [])
    basis.pop('questions')
    return digest(basis)


def prepare_interaction(record, *, node_id, issue_id, source, question):
    """Freeze this choice, retaining an obsolete generated card as unanswered."""
    from mewcode.experts.recovery_state import save_interaction
    if source_basis(record, node_id) != source:
        raise ValueError('审核输入已经变化，未替换问题。')
    legacy_id = 'review_' + digest([record['run_id'], node_id, issue_id, source])[:24]
    request_id = 'review_' + digest([record['run_id'], node_id, issue_id, source, question])[:24]
    stage = record.setdefault('node_states', {}).setdefault(node_id, {})
    pending = stage.get('pending_interaction')
    value = ledger.state(record)
    if pending and pending.get('request_id') != request_id:
        # Only upgrade the same review and its original typed alternatives.
        # Unrelated questions and changed review inputs remain protected by
        # the ordinary unresolved-interaction contract.
        core = lambda row: {k: v for k, v in row.items()
                            if k not in {'type', 'allow_other', 'requires_user_answer'}}
        original = pending.get('source_questions') or pending.get('questions') or []
        if (pending.get('request_id') != legacy_id or pending.get('tool') != 'ReviewDecision'
                or pending.get('kind') != 'ask_user' or [core(row) for row in original] != [core(question)]):
            raise ValueError('现有待答问题与本次审核不匹配，未替换。')
        old_rows = [row for row in value['questions'].values()
                    if row['node_id'] == node_id and row['questions'] == pending['questions']]
        if any(row['status'] == 'answered' for row in old_rows):
            raise ValueError('旧审核问题已有答复，未替换。')
        for row in old_rows:
            row.update(status='superseded', superseded_by_request=request_id)
        stage.setdefault('interaction_history', []).append({'pending': copy.deepcopy(pending),
            'status': 'superseded', 'answered': False, 'at': time.time(),
            'reason': 'review_choice_contract_updated', 'replacement_request_id': request_id})
        stage.pop('pending_interaction')
        ledger.event(value, 'review_question_superseded', node_id=node_id,
                     old_request_id=legacy_id, request_id=request_id)
    ask_gateway.prepare(record, node_id, [question], category='business')
    save_interaction(record, 'node_states', node_id, {'kind': 'ask_user',
        'request_id': request_id, 'tool': 'ReviewDecision', 'questions': [question]})
    return request_id


def prior_answer(record, node_id, issue_id):
    basis = source_basis(record, node_id)
    return next((row for row in (record.get('collaboration') or {}).get('review_decision_answers', {}).values()
                 if row['node_id'] == node_id and row['issue_id'] == issue_id and row['source_basis'] == basis), None)


def record_answer(record, *, node_id, issue_id, source, question_id, answer, effects):
    """The live question, answer, and review input must all still match."""
    value = ledger.state(record)
    question = value['questions'].get(question_id) or {}
    if (source_basis(record, node_id) != source or question.get('node_id') != node_id
            or question.get('status') != 'answered' or question.get('answer_source') != 'user'
            or question.get('answer') != answer or not ask_gateway.decision_freshness(record, question)['current']
            or len(answer) != 1 or next(iter(answer.values())) not in effects):
        raise ValueError('审核问题、答案或相关输入已经变化，未继续执行。')
    identity = digest([node_id, issue_id, source])
    saved = value.setdefault('review_decision_answers', {}).get(identity)
    if saved:
        return copy.deepcopy(saved)
    effect = effects[next(iter(answer.values()))]
    row = {'node_id': node_id, 'issue_id': issue_id, 'source_basis': source,
           'question_id': question_id, 'answer': copy.deepcopy(answer), 'effect': effect}
    value['review_decision_answers'][identity] = row
    ledger.event(value, 'review_decision_answered', **row)
    return copy.deepcopy(row)
