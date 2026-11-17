"""Read-only projection of host-recorded decisions; never a permission grant."""
import json
import copy
import re


def public_decision(row):
    """Credential-entry answers are not decision evidence for another model."""
    from mewcode.permissions.decision_envelope import redact_value
    safe = copy.deepcopy(row)
    credential = re.compile(r'api.?key|access.?token|password|secret|credential|密钥|密码|凭据|令牌|\bkey(?=\b|是|为|值)', re.I)
    answers = safe.get('answer') or {}
    redacted = False
    for index, question in enumerate(row.get('questions', [])):
        name = question.get('name') or f'q{index}'
        answer = answers.get(name)
        choices = [option.get('value', option.get('id', option.get('label'))) if isinstance(option, dict) else option
                   for option in question.get('options', [])]
        sensitive = credential.search(json.dumps(question, ensure_ascii=False)) or credential.search(str(answer or ''))
        if sensitive:
            # Preserve enumerated decisions such as "provide_key"; hide free-form credentials.
            if answer not in choices:
                answers[name] = '[REDACTED: credential response]'
                redacted = True
            safe['questions'][index] = json.loads(re.sub(r'[A-Za-z0-9_.-]{24,}', '[REDACTED]',
                json.dumps(redact_value(question), ensure_ascii=False)))
    safe['answer'] = answers
    if redacted:
        for field in ('answer_selection', 'answer_notes', 'notes'):
            if field in safe:
                safe[field] = '[REDACTED: credential response]'
    safe = {field: value if field == 'basis' else redact_value(value, field_name=field) for field, value in safe.items()}
    safe['sensitive_details_redacted'] = redacted or safe != row
    return safe


def decisions(record):
    from mewcode.orchestration.ask_gateway import decision_freshness
    result = []
    for row in (record.get('collaboration') or {}).get('questions', {}).values():
        if row.get('status') != 'answered' or row.get('answer_source') != 'user':
            continue
        freshness = decision_freshness(record, row)
        safe = public_decision(row)
        text = json.dumps(safe['questions'], ensure_ascii=False)
        answer = json.dumps(safe.get('answer'), ensure_ascii=False)
        result.append({'auth_id': 'DEC-' + row['id'], 'decision_id': row['id'],
            'kind': 'host_user_decision', 'enforceable': False, 'source_ref': 'collaboration/questions/' + row['id'],
            'scope': {'node_ids': [row['node_id']], 'spec_version': row['spec_version']},
            'question_excerpt': text[:800], 'answer_excerpt': answer[:500],
            'excerpt_truncated': len(text) > 800 or len(answer) > 500,
            'sensitive_details_redacted': safe['sensitive_details_redacted'],
            'answer_source': 'user', 'freshness': freshness,
            'details': 'ReadAuthorizations(decision_id=' + row['id'] + ')',
            'notice': '宿主保存的用户决定，仅限原问题范围；不是工具权限，也不授权其他模型、节点或全量外发。'})
    return result


def decision_context(record, node_id, *, budget=2400):
    """Keep relevant host answers visible when an audit resumes old reasoning."""
    contracts = (record.get('collaboration') or {}).get('contracts', {})
    distance, pending = {node_id: 0}, [node_id]
    while pending:
        key = pending.pop(0)
        contract = contracts.get(key) or {}
        for upstream in contract.get('bound_dependencies', contract.get('depends_on', [])):
            if upstream not in distance:
                distance[upstream] = distance[key] + 1
                pending.append(upstream)
    rows = [row for row in decisions(record) if row['scope']['node_ids'][0] in distance]
    rows.sort(key=lambda row: (distance[row['scope']['node_ids'][0]], row['decision_id']))
    if not rows:
        return ''
    heading = ('【本次审查涉及的宿主用户答复】以下答复确实已保存，不能再描述为“只有上游自述”或“用户从未回答”。'
        '历史答复是否仍适用须按原问题和 freshness 核对；它们不等同于工具权限，也不涵盖其他模型或全量处理。\n')
    body = ''
    for row in rows:
        line = json.dumps({key: row[key] for key in ('decision_id', 'scope', 'answer_excerpt', 'freshness')}, ensure_ascii=False) + '\n'
        if len(heading + body + line) > budget - 120:
            break
        body += line
    return heading + body + '原问题、完整答复和其余记录可用 ReadAuthorizations 分页读取；需要补充用户决定时只列尚未覆盖的范围。\n'


def review_decisions(record, node_id):
    """A review must account for answers belonging to itself or its direct inputs."""
    contract = (record.get('collaboration') or {}).get('contracts', {}).get(node_id) or {}
    keys = {node_id, *contract.get('bound_dependencies', contract.get('depends_on', []))}
    return {row['decision_id']: {key: row[key] for key in
            ('decision_id', 'scope', 'answer_excerpt', 'freshness', 'source_ref', 'notice')}
        for row in decisions(record)
        if row['scope']['node_ids'][0] in keys and not row['sensitive_details_redacted']}
