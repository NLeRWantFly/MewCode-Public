"""Deterministic dossiers and decision reports; source details remain pageable."""
from __future__ import annotations

import copy
import json
import re

from mewcode.experts.authorization_ledger import entries
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.recovery_case import cases, digest, error_head_hash
from mewcode.permissions.reviewer import redact_sensitive


def _input_empty(value):
    return not value if isinstance(value, list) else not str(value or '').strip()


def _node_rows(record):
    return {
        row.get('node_id'): row
        for row in (record.get('locked_flow') or {}).get('nodes', [])
        if isinstance(row, dict) and row.get('node_id')
    }


def _flow_node_index(record, node_id):
    for index, row in enumerate((record.get('locked_flow') or {}).get('nodes', [])):
        if isinstance(row, dict) and row.get('node_id') == node_id:
            return index
    return None


def _safe_text(value, limit=1200):
    text = redact_sensitive(str(value or ''))
    text = re.sub(r'(?i)\bsk-[a-z0-9_-]+', '[REDACTED_KEY]', text)
    text = re.sub(r'(?i)\b[A-Z]:\\Users\\[^\r\n,;" ]+', '<USER_PATH>', text)
    return text[:limit]


def _error_excerpt(value):
    text = redact_sensitive(str(value or ''))
    text = re.sub(r'(?i)\bsk-[a-z0-9_-]+', '[REDACTED_KEY]', text)
    text = re.sub(r'(?i)\b[A-Z]:\\Users\\[^\r\n,;" ]+', '<USER_PATH>', text)
    text = text[-4000:]
    return '\n'.join(text.splitlines()[-40:]).strip() or '工具失败，但宿主未记录报错原文。'


def _safe_tree(value):
    if isinstance(value, str):
        return _safe_text(value, 4000)
    if isinstance(value, dict):
        return {key: _safe_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_tree(item) for item in value]
    return copy.deepcopy(value)


def _upstream_summaries(record, node_id):
    flow = record.get('locked_flow') or {}
    node_rows = _node_rows(record)
    results = []
    for edge in flow.get('edges') or []:
        if not isinstance(edge, dict) or edge.get('to_node') != node_id:
            continue
        source_id = edge.get('from_node')
        stage = (record.get('node_states') or {}).get(source_id) or {}
        result = stage.get('result') or stage
        if not isinstance(result, dict):
            result = {'text': str(result)}
        handoff = result.get('handoff') or stage.get('handoff') or {}
        if not isinstance(handoff, dict):
            handoff = {}
        summary = handoff.get('summary') or result.get('text') or ''
        decisions = handoff.get('decisions') or []
        open_items = handoff.get('open_items') or []
        row = {'node_id': source_id, 'label': node_rows.get(source_id, {}).get('label') or source_id,
               'summary': _safe_text(summary, 1200)}
        if decisions:
            row['decisions'] = [_safe_text(item, 400) for item in decisions[:8]]
        if open_items:
            row['open_items'] = [_safe_text(item, 400) for item in open_items[:8]]
        results.append(row)
    return results


def _node_context(record, node_id):
    row = _node_rows(record).get(node_id) or {}
    brief = (((record.get('locked_flow') or {}).get('meta') or {}).get('node_plan_briefs') or {}).get(node_id) or {}
    goal = row.get('goal') or row.get('responsibility') or brief.get('summary') or ''
    criteria = (row.get('completion_criteria') or row.get('acceptance_criteria')
                or brief.get('completion_criteria') or brief.get('acceptance_criteria')
                or brief.get('outputs') or row.get('outputs') or [])
    if isinstance(criteria, dict):
        criteria = list(criteria.values())
    if not isinstance(criteria, list):
        criteria = [criteria]
    return {
        'goal': _safe_text(goal, 1600),
        'completion_criteria': [_safe_text(item, 500) for item in criteria[:12] if str(item or '').strip()],
        'upstream_handoffs': _upstream_summaries(record, node_id),
    }


def _input_definition(item, *, node_labels=None):
    """Keep a typed question definition, never the current input value."""
    node_labels = node_labels or {}
    allowed = ('name', 'label', 'description', 'type', 'required', 'options',
               'collect_at', 'required_when', 'default', 'separator')
    value = {key: copy.deepcopy(item[key]) for key in allowed if key in item}
    target = value.get('collect_at', 'start')
    if target and target != 'start':
        value['collect_at_label'] = node_labels.get(target, target)
    value.setdefault('label', value.get('name', ''))
    value.setdefault('description', '')
    value.setdefault('options', [])
    value.setdefault('collect_at', 'start')
    return value


def _input_defect_projection(record, case, *, input_definitions, deferred_inputs, reason):
    """Return the stable projection marker for an informational input card.

    The marker describes the card projection rather than granting any new
    authority.  Include the current definitions and bound values so a real
    change makes the legacy-card migration eligible again.
    """
    options = (case.get('plan') or {}).get('options') or []
    discovery = any((item.get('strategy') or {}).get('kind') == 'input_discovery'
                    and 'Glob' in ((item.get('strategy') or {}).get('tools') or [])
                    for item in options if isinstance(item, dict))
    kind = ('input_discovery_incident' if discovery else
            'deferred_input_incident' if deferred_inputs and not input_definitions else
            'input_defect_incident')
    return {
        'kind': kind,
        # Version 2 makes a previously misleading future-input card eligible
        # for one normal host migration when the saved discovery plan has the
        # real missing-material cause.
        'version': 2 if discovery else 1,
        'signature': digest([
            case['case_id'], input_definitions, deferred_inputs,
            record.get('run_inputs') or {}, reason or '',
            (case.get('plan') or {}).get('root_cause', {}).get('summary', '') if discovery else '',
        ]),
    }


def stage_input_context(record, node_id):
    """Return current and deferred run-input questions for one stage.

    ``stage_input_questions`` is the source of truth for whether a field is
    due now.  Empty values scheduled for a later node are returned as context,
    but are intentionally not promoted to an entry blocker.
    """
    flow = record.get('locked_flow') or {}
    meta = flow.get('meta') or {}
    declarations = meta.get('run_input_contract') or []
    if not isinstance(declarations, list) or not declarations:
        return {
            'contract': [], 'values': {}, 'stage_input_questions': [],
            'deferred_input_questions': [], 'deferred_inputs': [],
        }
    node_rows = _node_rows(record)
    node_ids = set(node_rows)
    values = record.get('run_inputs') or {}
    if not isinstance(values, dict):
        values = {}
    branch_inputs = {
        (edge.get('input_when') or {}).get('input')
        for edge in (flow.get('edges') or [])
        if isinstance(edge, dict) and edge.get('from_node') == node_id
        and isinstance(edge.get('input_when'), dict)
        and (edge.get('input_when') or {}).get('input')
    }
    try:
        from mewcode.orchestration.run_inputs import stage_input_questions, validate_input_contract
        defined = validate_input_contract(declarations, node_ids=node_ids or None)
        current, normalized = stage_input_questions(
            declarations, values, node_id, branch_inputs=branch_inputs
        )
    except Exception as exc:
        return {
            'contract': copy.deepcopy(declarations), 'values': {},
            'stage_input_questions': [], 'deferred_input_questions': [],
            'deferred_inputs': [], 'contract_error': str(exc),
        }
    labels = {key: row.get('label') or key for key, row in node_rows.items()}
    current_rows = [_input_definition(item, node_labels=labels) for item in current]
    deferred = []
    for name, item in defined.items():
        target = item.get('collect_at', 'start')
        if target in {'start', node_id} or not _input_empty(normalized.get(name)):
            continue
        deferred.append(_input_definition(item, node_labels=labels))
    contract = [_input_definition(item, node_labels=labels) for item in declarations]
    return {
        'contract': contract,
        'values': copy.deepcopy(normalized),
        'stage_input_questions': current_rows,
        'deferred_input_questions': deferred,
        'deferred_inputs': deferred,
    }


def input_definitions_for_requests(record, node_id, requests):
    """Map analyst material requests onto current typed input declarations."""
    context = stage_input_context(record, node_id)
    current = context.get('stage_input_questions') or []
    deferred = context.get('deferred_input_questions') or []
    requests = [str(item).strip() for item in (requests or []) if str(item).strip()]
    if not requests:
        return copy.deepcopy(current)
    # A declared field that is collected by a later node is context for the
    # dossier, never a current recovery question.  Only plans with no typed
    # contract at all retain the legacy prose-to-field compatibility path.
    legacy_prose = not context.get('contract')
    chosen = []
    for request in requests:
        folded = request.casefold()
        match = next((item for item in current if folded in str(item.get('name', '')).casefold()
                      or folded in str(item.get('label', '')).casefold()
                      or folded in str(item.get('description', '')).casefold()), None)
        if match is None:
            # Future declarations are already represented by deferred_inputs
            # in the dossier.  Do not turn them into recovery_input_N or
            # accidentally map them to the one unrelated current field.
            match = next((item for item in deferred if folded in str(item.get('name', '')).casefold()
                          or folded in str(item.get('label', '')).casefold()
                          or folded in str(item.get('description', '')).casefold()), None)
            if match is not None:
                continue
        if match is None:
            # Some older plans only carried prose and have no run-input
            # contract. Preserve that request as a concrete, current-stage
            # field rather than turning it into a blank question.
            if not legacy_prose:
                continue
            index = len(chosen) + 1
            match = {
                'name': f'recovery_input_{index}', 'label': request,
                'description': request, 'options': [], 'collect_at': node_id,
                'collect_at_label': _node_rows(record).get(node_id, {}).get('label', node_id),
                'required': True, 'legacy': True,
            }
        if match not in chosen:
            chosen.append(copy.deepcopy(match))
    return chosen


def build_dossier(record, case, capabilities=None):
    stage = record.get('node_states', {}).get(case['node_id'], {})
    view = ledger_for_run(record).view(case['node_id'])
    input_context = stage_input_context(record, case['node_id'])
    tool_rows = stage.get('tools', {})
    tool_rows = tool_rows.values() if isinstance(tool_rows, dict) else tool_rows or []
    failures = {}
    for tool_row in tool_rows:
        if not isinstance(tool_row, dict) or not (tool_row.get('is_error') or tool_row.get('status') == 'failed'):
            continue
        tool = str(tool_row.get('name') or '')
        raw_output = tool_row.get('output', '')
        error_hash = error_head_hash(raw_output)
        key = (tool, error_hash)
        if key not in failures:
            excerpt = _error_excerpt(raw_output)
            evidence_id = 'E-' + digest([case['case_id'], tool, error_hash])[:10].upper()
            failures[key] = {'tool': tool, 'error_head_hash': error_hash,
                'count': 0, 'evidence_id': evidence_id,
                'excerpt': excerpt, 'error_excerpt': excerpt}
        failures[key]['count'] += 1
    dossier = {'signature': case['signature'], 'case_id': case['case_id'], 'epoch': case['epoch'],
        'failed_step': case.get('failed_step') or {},
        'first_seen': min((f.created_at for f in view.history), default=0),
        'last_seen': max((f.created_at for f in view.history), default=0),
        'blockers': [{'fact_id': f.fact_id, **dict(f.payload)} for f in view.active_blockers],
        'tool_failures': list(failures.values()),
        'node_context': _node_context(record, case['node_id']),
        'command_receipts': [dict(f.payload) for f in view.current_receipts],
        'rounds': case['rounds'], 'progress': [dict(f.payload) for f in view.history if f.kind == 'progress'],
        'capabilities': capabilities or {}, 'authorizations': [{'auth_id': r['auth_id'], 'kind': r['kind']} for r in entries(record)],
        'user_inputs': [],
        'similar_cases': [r for r in cases(record) if r['case_id'] != case['case_id']
            and ((case['signature']['capability'] and r['signature']['capability'] == case['signature']['capability'])
                 or r['signature']['error_head_hash'] == case['signature']['error_head_hash'])],
        'usage': {'tokens': stage.get('billable_tokens', 0), 'seconds': stage.get('elapsed', 0)},
        'alternatives': (capabilities or {}).get('alternatives', []),
        # Keep both descriptive names for callers introduced in different
        # recovery-card versions.  They contain definitions, never submitted
        # values, and future fields are informational only.
        'run_input_contract': input_context['contract'],
        'stage_input_questions': input_context['stage_input_questions'],
        'input_definitions': input_context['stage_input_questions'],
        'deferred_input_questions': input_context['deferred_input_questions'],
        'deferred_inputs': input_context['deferred_inputs'],
        **({'input_contract_error': input_context['contract_error']}
           if input_context.get('contract_error') else {}),
        **({'recovery_boundary': copy.deepcopy(case.get('boundary') or case.get('recovery_boundary') or {})}
           if 'boundary' in case or 'recovery_boundary' in case else {}),
    }
    from statistics import median
    samples = [float(row['seconds']) for prior in [case, *dossier['similar_cases']]
               if prior.get('analysis_model') == case.get('analysis_model')
               for row in prior.get('rounds', [])
               if isinstance(row.get('seconds'), (int, float)) and row['seconds'] > 0
               and (row.get('verification') or {}).get('progress_evidence')]
    dossier['timing_reference'] = {'sample_count': len(samples), 'insufficient_samples': len(samples) < 3,
        'median_seconds': median(samples) if samples else None,
        'scope': '本运行中同模型档位的同类案件；仅计有宿主进展证据的分段'}
    for ref, row in (record.get('case_inputs') or {}).items():
        if not isinstance(row, dict) or row.get('case_id') != case['case_id']:
            continue
        item = {'source_ref': ref, 'text': row['text'], 'question': row.get('question', '')}
        if row.get('note') is not None:
            item['note'] = row['note']
        if row.get('context_note') is not None:
            item['context_note'] = row['context_note']
            item['context_note_role'] = 'context_only'
        dossier['user_inputs'].append(item)
    return dossier


def dossier_summary(dossier, budget=3000):
    # Keep references and counts; never truncate source evidence and claim completeness.
    value = {key: dossier[key] for key in ('case_id', 'signature', 'epoch', 'authorizations', 'usage', 'failed_step')}
    value['tool_failures'] = [{**{key: row.get(key) for key in ('tool', 'count', 'evidence_id')},
                               'excerpt': str(row.get('excerpt') or '')[:600]}
                              for row in (dossier.get('tool_failures') or [])[:3]]
    if dossier.get('node_context'):
        context = dossier['node_context']
        value['node_context'] = {
            'goal': str(context.get('goal') or '')[:700],
            'completion_criteria': [str(item)[:250] for item in (context.get('completion_criteria') or [])[:4]],
            'upstream_handoffs': [{key: str(row.get(key) or '')[:500]
                for key in ('label', 'summary')} for row in (context.get('upstream_handoffs') or [])[:3]],
        }
    if 'recovery_boundary' in dossier:
        value['recovery_boundary'] = _safe_tree(dossier.get('recovery_boundary') or {})
    user_inputs = []
    for row in dossier.get('user_inputs') or []:
        if not isinstance(row, dict):
            continue
        item = {key: row[key] for key in ('source_ref', 'question', 'context_note_role') if key in row}
        for key, limit in (('text', 600), ('note', 1200), ('context_note', 2000)):
            if key in row:
                item[key] = str(row[key])[:limit]
        user_inputs.append(item)
    if user_inputs:
        value['user_inputs'] = user_inputs
    value.update(blocker_count=len(dossier['blockers']), failure_groups=len(dossier['tool_failures']),
        rounds=len(dossier['rounds']), available_sections=list(dossier),
        current_blockers=[{key: row.get(key) for key in ('fact_id', 'kind', 'ref', 'clearance')}
            for row in dossier['blockers'][:4]],
        recent_rounds=[{'round': row['n'], 'method': row.get('method', row['option_id']),
            'executor_receipt_ref': row.get('executor_receipt_ref'),
            'result': row['verification'].get('result'),
            'reason': row['verification'].get('reason', '')[:160]}
            for row in dossier['rounds'][-2:]],
        command_receipt_count=len(dossier['command_receipts']),
        details='摘要仅含当前要点；完整明细用 ReadCaseDossier 分页读取')
    text = redact_sensitive(json.dumps(value, ensure_ascii=False))
    if len(text) > budget:
        # Keep the bounded original error and node goal, while compressing
        # secondary context such as long user notes and earlier attempts.
        compact = copy.deepcopy(value)
        for row in compact.get('tool_failures') or []:
            row['excerpt'] = row.get('excerpt', '')[:350]
        for row in compact.get('user_inputs') or []:
            for key in ('text', 'note', 'context_note'):
                if key in row:
                    row[key] = str(row[key])[-250:]
        if compact.get('node_context'):
            compact['node_context']['goal'] = compact['node_context']['goal'][:350]
            compact['node_context']['completion_criteria'] = compact['node_context']['completion_criteria'][:2]
            compact['node_context']['upstream_handoffs'] = compact['node_context']['upstream_handoffs'][:1]
        compact['recent_rounds'] = compact.get('recent_rounds', [])[-1:]
        text = redact_sensitive(json.dumps(compact, ensure_ascii=False))
    if len(text) > budget:
        text = json.dumps({'case_id': dossier['case_id'], 'error_evidence': value.get('tool_failures', [])[:1],
            'node_context': value.get('node_context'), 'details': '其他明细用 ReadCaseDossier 分节读取'}, ensure_ascii=False)
    return text if len(text) <= budget else json.dumps({'case_id': dossier['case_id'], 'details': '用 ReadCaseDossier 读取完整档案'}, ensure_ascii=False)


def _business_options(case):
    options = []
    for item in (case.get('plan') or {}).get('options') or []:
        if not isinstance(item, dict) or not item.get('option_id'):
            continue
        label = item.get('label') or item.get('method') or item['option_id']
        description = item.get('description') or ''
        if not description:
            description = '风险：{risk}；可逆：{reversible}；不改变原验收：{acceptance}'.format(
                risk=item.get('risk', '未说明'),
                reversible='是' if item.get('reversible') else '否',
                acceptance='是' if not item.get('changes_acceptance') else '否',
            )
        options.append({'value': item['option_id'], 'label': label,
                        'description': description,
                        'recommended': item['option_id'] == (case.get('plan') or {}).get('recommended_option_id')})
    return options


def _report_plan(case):
    """Return the user-facing plan, suppressing exhausted investigation choices."""

    plan = copy.deepcopy(case.get('plan') or {})
    boundary = case.get('boundary') or case.get('recovery_boundary') or {}
    if not boundary.get('investigation_closed'):
        return plan
    removed = {item.get('option_id') for item in plan.get('options') or []
               if isinstance(item, dict) and item.get('option_id')
               and (item.get('strategy') or {}).get('kind') == 'investigate'}
    plan['options'] = [item for item in plan.get('options') or []
                       if isinstance(item, dict)
                       and (item.get('strategy') or {}).get('kind') != 'investigate']
    if plan.get('recommended_option_id') in removed:
        plan['recommended_option_id'] = next(
            (item.get('option_id') for item in plan['options'] if item.get('option_id')), None)
    return plan


def _recovery_option_choices(case):
    """Project saved RecoveryOption rows into safe card values.

    The executable strategy stays in ``case.plan``.  Cards expose only the
    stable option id and review metadata so answering one can run that exact
    strategy without asking the model to choose again.
    """

    plan = _report_plan(case)
    options = []
    for item in plan.get('options') or []:
        if not isinstance(item, dict) or not item.get('option_id'):
            continue
        needs = item.get('needs') or {}
        input_names = [row.get('label') or row.get('name')
                       for row in needs.get('input_questions') or []
                       if isinstance(row, dict) and (row.get('label') or row.get('name'))]
        input_names.extend(str(row) for row in needs.get('user_input') or [] if str(row).strip())
        details = item.get('consequence') or item.get('description') or (
            f"风险：{item.get('risk', '未说明')}；可逆：{'是' if item.get('reversible') else '否'}；"
            f"不改变原验收：{'否' if item.get('changes_acceptance') else '是'}"
        )
        if input_names:
            details += '；需要补充：' + '、'.join(input_names)
        options.append({
            'value': item['option_id'],
            'label': item.get('user_label') or item.get('label') or item.get('method') or item['option_id'],
            'description': details,
            'recommended': item['option_id'] == plan.get('recommended_option_id'),
            **({'hypothesis_id': item['hypothesis_id']} if item.get('hypothesis_id') else {}),
            **({'actor': item['actor']} if item.get('actor') else {}),
            **({'consequence': item['consequence']} if item.get('consequence') else {}),
        })
    return options


def _fallback_decision_options():
    """Keep a model-free incident actionable without pretending it is fixed."""

    return [
        {'value': 'research', 'label': '研究或补充材料',
         'description': '提供目录或材料线索，重新准备具体候选。',
         'action': 'research', 'requires_input': True},
        {'value': 'manual', 'label': '其他',
         'description': '说明需要走的人工处理路径。',
         'action': 'manual', 'requires_input': True},
        {'value': 'defer', 'label': '稍后处理',
         'description': '保留当前卡片和检查点，稍后继续。',
         'action': 'defer', 'requires_input': False},
        {'value': 'close', 'label': '停止本案件',
         'description': '保留检查点；当前节点仍未完成。'},
    ]


def _downstream_labels(record, node_id):
    rows = _node_rows(record)
    return [rows[edge['to_node']].get('label') or edge['to_node']
            for edge in (record.get('locked_flow') or {}).get('edges', [])
            if isinstance(edge, dict) and edge.get('from_node') == node_id
            and edge.get('to_node') in rows]


def _minimal_diagnosis(record, case, dossier, analysis_errors=None):
    """Use only host observations when the analyst cannot provide a plan."""
    failures = [row for row in dossier.get('tool_failures') or []
                if row.get('excerpt') and row.get('evidence_id')]
    failed_submit = next((row for row in failures if row.get('tool') == 'SubmitRecoveryPlan'), None)
    failed = failed_submit or (None if analysis_errors else next(iter(failures), None))
    node_label = (_node_rows(record).get(case['node_id']) or {}).get('label') or case['node_id']
    if failed:
        excerpt = failed['excerpt']
        evidence_id = failed['evidence_id']
        error_line = next((line.strip() for line in reversed(excerpt.splitlines()) if line.strip()), excerpt)
        summary = (f"SubmitRecoveryPlan 提交失败：{_safe_text(error_line, 350)}"
                   if failed.get('tool') == 'SubmitRecoveryPlan'
                   else f"{failed.get('tool') or '当前步骤'}返回失败，原因还没查明。")
        symptom = {'summary': summary,
                   'excerpt': excerpt, 'evidence_id': evidence_id}
    elif analysis_errors:
        error = _safe_text(str(analysis_errors[-1]), 500)
        symptom = {'summary': f'恢复方案未交付：{error}', 'excerpt': error, 'evidence_id': None}
    else:
        excerpt = '宿主未记录报错原文。'
        symptom = {'summary': f'当前步骤「{node_label}」未完成，原因还没查明。',
                   'excerpt': excerpt, 'evidence_id': None}
    downstream = _downstream_labels(record, case['node_id'])
    impact = f'当前步骤「{node_label}」尚未完成。'
    if downstream:
        impact += ' 直接下游步骤暂时无法从此处继续：' + '、'.join(str(item) for item in downstream[:8]) + '。'
    else:
        impact += ' 后续流程暂时停在这里。'
    tried = []
    for row in case.get('rounds') or []:
        verification = row.get('verification') or {}
        method = _safe_text(row.get('method') or row.get('option_id') or '已尝试方案', 300)
        result = _safe_text(verification.get('result') or '未核验', 100)
        reason = _safe_text(verification.get('reason') or '', 500)
        tried.append(method + '：' + result + (('，' + reason) if reason else ''))
    target = (dossier.get('node_context') or {}).get('goal') or f'核实「{node_label}」失败的原因，并确定可核验的处理方向。'
    return {
        'symptom': symptom,
        'impact': impact,
        'tried': tried,
        'hypotheses': [],
        'investigation': {
            'scope': '只读核对当前步骤已记录的失败详情和相关材料。',
            'tools': ['ReadCaseDossier', 'ReadFile', 'Glob', 'Grep', 'ProbeCapabilities'],
            'budget': 120,
        },
        'goal': {
            'target': _safe_text(target, 500),
            'milestones': [{'id': 'review_existing_evidence', 'label': '核对已记录的失败证据',
                'evidence': '可复核的原文摘录或只读调查记录', 'expected_seconds': 120}],
            'stop_when': '没有取得新证据，或需要授权、改变目标、承受未知副作用时收束。',
        },
        'recommended_option_id': 'investigate',
        'fallback': True,
    }


def _diagnosis_option(choice, *, recommended=False):
    item = choice
    option_id = item.get('option_id')
    needs = item.get('needs') or {}
    detail = item.get('consequence') or item.get('description') or item.get('method') or ''
    inputs = [row.get('label') or row.get('name') for row in needs.get('input_questions') or []
              if isinstance(row, dict) and (row.get('label') or row.get('name'))]
    inputs.extend(str(row) for row in needs.get('user_input') or [] if str(row).strip())
    if inputs:
        detail = (str(detail) + '；需要补充：' if detail else '需要补充：') + '、'.join(inputs)
    return {
        'value': option_id,
        'label': item.get('user_label') or item.get('label') or item.get('method') or option_id,
        'description': detail,
        'recommended': bool(recommended),
        **({'action': 'investigate'} if (item.get('strategy') or {}).get('kind') == 'investigate' else {}),
        **({'hypothesis_id': item['hypothesis_id']} if item.get('hypothesis_id') else {}),
        **({'actor': item['actor']} if item.get('actor') else {}),
        **({'consequence': item['consequence']} if item.get('consequence') else {}),
    }


def _capability_alternatives(case, alternatives=None):
    """Keep host or saved recovery alternatives without inventing capabilities."""
    if alternatives is not None:
        return copy.deepcopy(alternatives)
    plan = case.get('plan') or {}
    saved = plan.get('alternatives') or []
    if saved:
        return copy.deepcopy(saved)
    return [
        {'option_id': row.get('option_id'), 'method': row.get('method'),
         **({'user_label': row['user_label']} if row.get('user_label') else {})}
        for row in plan.get('options') or []
        if isinstance(row, dict) and row.get('option_id')
        and (row.get('strategy') or {}).get('kind') != 'investigate'
    ]


def _diagnosis_incident_report(record, case, *, conclusion, root_cause, diagnosis,
                               analysis_errors=None, alternatives=None):
    saved_plan = case.get('plan') or {}
    plan = _report_plan(case)
    investigation_closed = bool((case.get('boundary') or case.get('recovery_boundary') or {}).get(
        'investigation_closed'))
    investigation_ids = {row.get('option_id') for row in saved_plan.get('options') or []
                         if isinstance(row, dict) and row.get('option_id')
                         and (row.get('strategy') or {}).get('kind') == 'investigate'}
    options_by_id = {row.get('option_id'): row for row in plan.get('options') or []
                     if isinstance(row, dict) and row.get('option_id')}
    recommended_id = diagnosis.get('recommended_option_id') or plan.get('recommended_option_id')
    if investigation_closed and (recommended_id in investigation_ids or recommended_id == 'investigate'):
        recommended_id = plan.get('recommended_option_id')
    investigation = (None if investigation_closed else next((row for row in options_by_id.values()
                          if (row.get('strategy') or {}).get('kind') == 'investigate'), None))
    choices = []
    if investigation is not None:
        choices.append(_diagnosis_option(investigation,
            recommended=recommended_id == investigation.get('option_id')))
    elif not investigation_closed and (diagnosis.get('fallback') or not options_by_id):
        choices.append({'value': 'investigate', 'label': '先核对报错与已有证据',
            'description': '只读取当前步骤的记录，更新诊断后再选择处理方式',
            'recommended': recommended_id == 'investigate', 'action': 'investigate', 'actor': 'auto'})
    hypotheses = diagnosis.get('hypotheses') or []
    for hypothesis in hypotheses:
        option = options_by_id.get(hypothesis.get('option_id'))
        if option is not None and option.get('option_id') not in {row.get('value') for row in choices}:
            choices.append(_diagnosis_option(option, recommended=recommended_id == option.get('option_id')))
    if diagnosis.get('fallback') or not hypotheses:
        present = {row.get('value') for row in choices}
        for option_id, option in options_by_id.items():
            if option_id not in present:
                choices.append(_diagnosis_option(option, recommended=recommended_id == option_id))
                present.add(option_id)
    if investigation_closed and recommended_id not in {row.get('value') for row in choices}:
        recommended_id = next((row.get('value') for row in choices
                               if row.get('value') not in {'investigate', 'manual'}), 'manual')
    choices.append({'value': 'manual', 'label': '其他',
        'description': '说明你的判断或处理方式；我会先复述理解，等你确认后再执行。',
        'recommended': recommended_id == 'manual', 'action': 'manual', 'actor': 'decide', 'requires_input': True})
    if investigation_closed:
        for choice in choices:
            choice['recommended'] = choice.get('value') == recommended_id
        plan['recommended_option_id'] = (recommended_id if recommended_id in options_by_id else
                                         plan.get('recommended_option_id'))
    exits = [
        {'value': 'close', 'label': '停止本次修复',
         'description': '保留当前检查点；节点仍未完成。'},
        {'value': 'defer', 'label': '稍后处理',
         'description': '保留当前卡片和检查点，稍后继续。'},
    ]
    symptom = copy.deepcopy(diagnosis.get('symptom') or {})
    for key in ('summary', 'excerpt'):
        if symptom.get(key) is not None:
            symptom[key] = _safe_text(symptom[key], 4000 if key == 'excerpt' else 500)
    projected = {
        'symptom': symptom,
        'impact': _safe_text(diagnosis.get('impact') or '', 1000),
        'tried': [_safe_text(item, 500) for item in (diagnosis.get('tried') or [])[:16]],
        'hypotheses': _safe_tree(diagnosis.get('hypotheses') or []),
        'investigation': _safe_tree(diagnosis.get('investigation')),
        'goal': _safe_tree(diagnosis.get('goal')),
        'recommended_option_id': recommended_id,
        'fallback': bool(diagnosis.get('fallback')),
    }
    for value in projected['hypotheses']:
        for key in ('summary', 'open_questions'):
            if key not in value:
                continue
            if isinstance(value[key], list):
                value[key] = [_safe_text(item, 500) for item in value[key]]
            else:
                value[key] = _safe_text(value[key], 500)
        for evidence in value.get('evidence') or []:
            evidence['excerpt'] = _safe_text(evidence.get('excerpt'), 4000)
    node_label = (_node_rows(record).get(case['node_id']) or {}).get('label') or case['node_id']
    symptom_summary = symptom.get('summary')
    if analysis_errors and conclusion and symptom_summary and symptom_summary not in conclusion:
        reason = str(conclusion).rstrip('。；; ') + '；' + str(symptom_summary)
    else:
        reason = conclusion or symptom_summary or f'当前步骤「{node_label}」尚未完成。'
    node_index = _flow_node_index(record, case['node_id'])
    title = (f'第 {node_index + 1} 步卡住了：{node_label}' if node_index is not None
             else f'当前步骤卡住了：{node_label}')
    pending = {'kind': 'incident', 'round': len(case.get('rounds') or []),
        'question': reason, 'options': copy.deepcopy(choices), 'exits': copy.deepcopy(exits)}
    if plan.get('options'):
        pending['plan'] = copy.deepcopy(plan)
    report = {
        'kind': 'incident', 'case_id': case['case_id'], 'node_id': case['node_id'],
        'node_label': node_label, 'title': title,
        **({'node_index': node_index, 'step_number': node_index + 1} if node_index is not None else {}),
        'reason': _safe_text(reason, 1000), 'root_cause': root_cause,
        'diagnosis': projected, 'options': choices, 'exits': exits,
        'round': len(case.get('rounds') or []),
        'attempts': [{'round': row.get('n'), 'method': _safe_text(row.get('method') or row.get('option_id'), 300),
            'result': (row.get('verification') or {}).get('result'),
            'evidence': (row.get('verification') or {}).get('evidence_refs', [])}
            for row in case.get('rounds') or []],
        'analysis_errors': [_safe_text(error, 500) for error in (analysis_errors or [])],
        'evidence_digest': digest([case.get('signature'), case.get('rounds'), projected]),
        'pending': pending,
    }
    if root_cause == 'capability_absent':
        report['alternatives'] = _safe_tree(_capability_alternatives(case, alternatives))
    if plan.get('options'):
        recovery_choices = _recovery_option_choices(case)
        report['recovery_options'] = recovery_choices
        report['recovery_plan'] = copy.deepcopy(plan)
    return report


def incident_report(record, case, *, conclusion, root_cause='unclassified', alternatives=None,
                    input_card=True, diagnosis=None, fallback_diagnosis=False,
                    analysis_errors=None):
    stage = record.get('node_states', {}).get(case['node_id'], {})
    node_label = next((node.get('label') for node in (record.get('locked_flow') or {}).get('nodes', [])
                       if node.get('node_id') == case['node_id']), stage.get('label') or case['node_id'])
    dossier = build_dossier(record, case)
    has_boundary_contract = 'boundary' in case or 'recovery_boundary' in case or 'recovery_boundary' in dossier
    typed_input_path = root_cause == 'input_defect' and bool(
        dossier.get('stage_input_questions') or dossier.get('deferred_input_questions')
        or dossier.get('run_input_contract'))
    if fallback_diagnosis:
        diagnosis = _minimal_diagnosis(record, case, dossier, analysis_errors=analysis_errors)
    elif diagnosis is None:
        diagnosis = (case.get('plan') or {}).get('diagnosis')
        if diagnosis is None and has_boundary_contract:
            diagnosis = _minimal_diagnosis(record, case, dossier, analysis_errors=analysis_errors)
    if isinstance(diagnosis, dict) and not typed_input_path:
        report = _diagnosis_incident_report(record, case, conclusion=conclusion,
            root_cause=root_cause, diagnosis=diagnosis, analysis_errors=analysis_errors,
            alternatives=alternatives)
        if root_cause == 'input_defect' and report.get('pending', {}).get('kind') == 'incident':
            report['card_projection'] = _input_defect_projection(
                record, case, input_definitions=[],
                deferred_inputs=dossier.get('deferred_input_questions') or [],
                reason=report.get('reason', ''),
            )
        return report
    attached_diagnosis = isinstance(diagnosis, dict) and typed_input_path
    input_definitions = copy.deepcopy(dossier.get('stage_input_questions') or [])
    deferred_inputs = copy.deepcopy(dossier.get('deferred_input_questions') or [])
    discovery = None
    discovery_plan = any(
        (item.get('strategy') or {}).get('kind') == 'input_discovery'
        and 'Glob' in ((item.get('strategy') or {}).get('tools') or [])
        for item in ((case.get('plan') or {}).get('options') or [])
        if isinstance(item, dict)
    )
    if root_cause == 'input_defect':
        from mewcode.experts.recovery_context import verify_input_discovery
        discovery = verify_input_discovery(record, case['node_id'], case=case,
            case_id=case.get('case_id'))
        saved_summary = ((case.get('plan') or {}).get('root_cause') or {}).get('summary') or ''
        # Older reports replaced the actual missing-material explanation with
        # an unrelated future field.  Keep the plan's factual cause even when
        # the saved receipt now needs revalidation; future declarations remain
        # context below the card.
        if discovery_plan and (not conclusion or '没有需要立即提交' in str(conclusion)
                or '缺失参数将在' in str(conclusion)) and saved_summary:
            suffix = (' 已保存只读输入发现回执，待宿主交回原规划节点核对；'
                      '已有路径清单可复用；点击上方继续任务，宿主会重核后返回原规划节点完成交接；'
                      '无需追加轮数或填写未来批大小。不把发现结果当作输入绑定或业务验收。'
                      if discovery.get('callback_eligible') else
                      ' 当前只读输入发现回执尚未通过宿主重核验，不能据此绑定输入或完成原节点。')
            conclusion = saved_summary + suffix
    if root_cause == 'input_defect' and not input_definitions and not (
            dossier.get('run_input_contract') and deferred_inputs):
        requested = ((case.get('plan') or {}).get('options') or [])
        requested = next(([
                            *[row.get('name') for row in (item.get('needs', {}).get('input_questions') or [])
                              if isinstance(row, dict) and row.get('name')],
                            *item.get('needs', {}).get('user_input', []),
                         ] for item in requested
                         if isinstance(item, dict) and (item.get('needs', {}).get('user_input')
                                                       or item.get('needs', {}).get('input_questions'))), [])
        input_definitions = input_definitions_for_requests(record, case['node_id'], requested)

    pending = None
    if root_cause == 'input_defect' and input_definitions and input_card:
        names = '、'.join(item.get('label') or item.get('name', '') for item in input_definitions)
        targets = '；'.join(
            f"{item.get('label') or item.get('name')}→{item.get('collect_at_label') or item.get('collect_at', 'start')}"
            for item in input_definitions
        )
        if not conclusion:
            conclusion = f'当前阶段「{node_label}」缺少具体输入：{names}。本卡只补充当前阶段字段；收集阶段：{targets}。'
        pending = {
            'kind': 'input', 'round': len(case['rounds']),
            'question': conclusion, 'questions': copy.deepcopy(input_definitions),
            'input_definitions': copy.deepcopy(input_definitions),
            'deferred_inputs': deferred_inputs,
            'options': [
                {'value': 'input', 'label': '提交补充', 'description': '只提交本卡列出的当前阶段字段。'},
                {'value': 'close', 'label': '停止本案件', 'description': '保留检查点；当前节点仍未完成。'},
            ],
        }
        if _recovery_option_choices(case):
            pending['recovery_options'] = _recovery_option_choices(case)
            pending['plan'] = copy.deepcopy(case.get('plan') or {})
    elif root_cause == 'input_defect' and input_definitions and not input_card:
        names = '、'.join(item.get('label') or item.get('name', '') for item in input_definitions)
        conclusion = ((conclusion or f'当前阶段仍缺少：{names}。')
                      + ' 已保存的补充不能满足同一字段要求；本卡不重复索取相同材料，也不自动增加轮数。')
        pending = {
            'kind': 'incident', 'round': len(case['rounds']),
            'question': conclusion,
            'options': _fallback_decision_options(),
        }
        if _recovery_option_choices(case):
            pending['recovery_options'] = _recovery_option_choices(case)
            pending['plan'] = copy.deepcopy(case.get('plan') or {})
    elif (root_cause == 'input_defect' and deferred_inputs and not discovery_plan
          and not conclusion):
        future = '；'.join(
            f"{item.get('label') or item.get('name')}→{item.get('collect_at_label') or item.get('collect_at', '后续节点')}"
            for item in deferred_inputs
        )
        conclusion = (f'当前阶段「{node_label}」没有需要立即提交的输入；缺失参数将在对应节点收集：{future}。'
                      '这张入口卡不会把未来参数当作当前阻塞。')
        pending = {
            'kind': 'incident', 'round': len(case['rounds']),
            'question': conclusion,
            'options': _fallback_decision_options(),
        }
        if _recovery_option_choices(case):
            pending['recovery_options'] = _recovery_option_choices(case)
            pending['plan'] = copy.deepcopy(case.get('plan') or {})
    elif root_cause == 'input_defect':
        conclusion = conclusion or f'当前阶段「{node_label}」的输入缺陷尚未形成可提交的具体字段。'
        pending = {
            'kind': 'incident', 'round': len(case['rounds']),
            'question': conclusion,
            'options': _fallback_decision_options(),
        }
        if _recovery_option_choices(case):
            pending['recovery_options'] = _recovery_option_choices(case)
            pending['plan'] = copy.deepcopy(case.get('plan') or {})
    elif root_cause == 'business_decision':
        choices = _recovery_option_choices(case) or _business_options(case)
        if choices:
            pending = {
                'kind': 'business_decision', 'round': len(case['rounds']),
                'question': conclusion or '请从以下明确方案中选择业务取舍。',
                'options': choices + [{'value': 'close', 'label': '停止本案件',
                                       'description': '保留检查点；当前节点仍未完成。'}],
            }
        else:
            conclusion = (conclusion or '当前方案没有提供可核验的业务取舍选项。') + \
                ' 修复方向：先补齐明确的方案选项；本卡不猜测业务决定，也不索取材料。'
            pending = {
                'kind': 'incident', 'round': len(case['rounds']),
                'question': conclusion,
                'options': _fallback_decision_options(),
            }
    elif root_cause == 'authority_gap' and case.get('pending', {}).get('kind') == 'elevation':
        pending = copy.deepcopy(case['pending'])

    if root_cause == 'contract_defect':
        conclusion = (conclusion or '流程合同无法执行。') + ' 修复方向：先修正流程合同、入口条件或节点边界，再重新校验；本卡不索取业务材料。'
    report_options = (pending or {}).get('options') if pending else None
    recovery_choices = _recovery_option_choices(case)
    if root_cause in {'input_defect', 'contract_defect', 'business_decision', 'capability_absent'} and recovery_choices:
        # Preserve typed input/stop entries for legacy consumers while also
        # exposing every model option.  The pending card keeps the complete
        # executable plan for a later selection.
        report_options = recovery_choices + [
            {'value': 'close', 'label': '停止本案件',
             'description': '保留检查点；当前节点仍未完成。'}
        ]
        if pending is not None:
            pending.setdefault('recovery_options', copy.deepcopy(recovery_choices))
            pending.setdefault('plan', copy.deepcopy(case.get('plan') or {}))
    if root_cause in {'contract_defect'} and not report_options:
        report_options = _fallback_decision_options()
    if root_cause == 'business_decision' and pending:
        conclusion = conclusion or '请从下列明确方案中选择业务取舍；选择不会改变原节点的验收合同。'
    report = {'kind': 'incident', 'case_id': case['case_id'], 'node_id': case['node_id'],
        'node_label': node_label,
        'title': '恢复需要你的决定', 'reason': redact_sensitive(conclusion), 'root_cause': root_cause,
        'analysis_errors': [redact_sensitive(error) for error in
            (case['rounds'][-1]['verification'].get('analysis_errors', []) if case['rounds'] else [])],
        'alternatives': _safe_tree(_capability_alternatives(case, alternatives))
            if root_cause == 'capability_absent' else [],
        'round': len(case['rounds']),
        'attempts': [{'round': r['n'], 'method': r.get('method', r['option_id']),
            'result': r['verification'].get('result'), 'evidence': r['verification'].get('evidence_refs', [])} for r in case['rounds']],
        'options': report_options or alternatives or (
            _fallback_decision_options()
            if root_cause in {'input_defect', 'contract_defect', 'business_decision', 'capability_absent'}
            else [
                {'value': 'retry', 'label': '继续修复',
                 'description': '保留当前检查点和已授权范围，按核验结果继续处理。'},
                {'value': 'close', 'label': '停止本案件',
                 'description': '保留检查点，当前节点仍未完成。'},
            ]
        ),
        'authorizations': [{'auth_id': r['auth_id'], 'quote': r['quote'], 'enforceable': r['enforceable']} for r in entries(record)],
        'preserved': {'input_revision': case['input_revision'], 'artifacts': [dict(f.payload) for f in ledger_for_run(record).view(case['node_id']).artifacts],
            'message_count': len(stage.get('messages', []))},
        'evidence_digest': digest([case['signature'], case['rounds']]),
        'input_definitions': input_definitions,
        'questions': copy.deepcopy(input_definitions),
        'deferred_inputs': deferred_inputs,
    }
    if recovery_choices:
        report['recovery_options'] = copy.deepcopy(recovery_choices)
        report['recovery_plan'] = copy.deepcopy(case.get('plan') or {})
    if discovery_plan:
        discovery = discovery or {}
        report['input_discovery'] = {
            'status': 'saved_host_verified' if discovery and discovery.get('callback_eligible') else 'needs_revalidation',
            'relative_paths': copy.deepcopy(discovery.get('relative_paths') or []),
            'absolute_paths': copy.deepcopy(discovery.get('absolute_paths') or []),
            'evidence_digest': discovery.get('evidence_digest'),
            'reasons': copy.deepcopy(discovery.get('reasons') or []),
            'business_complete': False,
            'input_bound': False,
        }
    if (root_cause == 'input_defect' and pending
            and pending.get('kind') == 'incident'):
        # This is a valid informational incident, not an incomplete input
        # card.  Persist a stable projection marker so legacy restoration is
        # idempotent and public reads do not write the same card repeatedly.
        report['card_projection'] = _input_defect_projection(
            record, case, input_definitions=input_definitions,
            deferred_inputs=deferred_inputs, reason=report.get('reason', ''),
        )
    if pending:
        report['pending'] = pending
    if root_cause == 'business_decision' and pending:
        report['business_options'] = copy.deepcopy(pending['options'])
    if attached_diagnosis:
        fallback_report = _diagnosis_incident_report(record, case, conclusion=conclusion,
            root_cause=root_cause, diagnosis=diagnosis, analysis_errors=analysis_errors,
            alternatives=alternatives)
        report['diagnosis'] = fallback_report['diagnosis']
        report['exits'] = fallback_report['exits']
    return report
