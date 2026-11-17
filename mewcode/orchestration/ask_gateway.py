"""One journal-backed question identity for collaborative execution and recovery."""
from __future__ import annotations

import copy

from mewcode.kernel.collaboration import dependencies, event, state
from mewcode.kernel.events import digest

REQUIRED = frozenset({"authorization", "irreversible", "required_input", "business", "constraint", "unknown_effect"})


def _legacy_identity(record, node_id, questions):
    value = state(record)
    basis = dependencies(value, node_id) if node_id in value["contracts"] else digest([value["spec_version"], value["plan_version"]])
    return digest([value["task_id"], node_id, questions, basis,
                   (record.get("authorization_manifest") or {}).get("scope_digest"), record.get("authorized_revision")])


def decision_authority(record):
    """Compare actual authority, not differently derived manifest checksums."""
    manifest = copy.deepcopy(record.get('authorization_manifest') or {})
    manifest.pop('scope_digest', None)
    manifest['call_bindings'] = manifest.get('call_bindings') or {}
    manifest['task_authorization'] = manifest.get('task_authorization') or None
    if not manifest['task_authorization']:
        manifest.pop('task_id', None)
    return digest([manifest, record.get('authorized_revision'), record.get('authorizations')])


def decision_basis(record, node_id, questions):
    """Bind decisions to requirements and input content, not receipt bookkeeping."""
    from mewcode.kernel.collaboration import semantic_basis
    from mewcode.experts.model_route_recovery import current_node_model
    value = state(record)
    seen = {}
    def input_basis(key):
        if key in seen:
            return seen[key]
        seen[key] = 'cycle'
        contract = value['contracts'].get(key)
        if contract is None:
            return None
        certificate = value['certificates'].get(key) or {}
        artifacts = [{field: item[field] for field in ('kind', 'path', 'name', 'sha256', 'content') if field in item}
                     for item in certificate.get('artifacts', [])]
        seen[key] = digest({'contract': semantic_basis(value, key), 'artifacts': artifacts,
            'invalidated': bool(certificate.get('invalidated')), 'checks': certificate.get('checks'),
            'quality_passed': certificate.get('quality_passed'),
            # A gate may carry its entire decision in the report with no files.
            'report': certificate.get('report_hash') if not artifacts else None,
            'inputs': {upstream: input_basis(upstream) for upstream in
                       contract.get('bound_dependencies', contract.get('depends_on', []))}})
        return seen[key]
    contract = value['contracts'].get(node_id)
    nodes = (value.get('runtime_flow') or record.get('locked_flow') or {}).get('nodes', [])
    node = next((row for row in nodes if row.get('node_id') == node_id), {'node_id': node_id})
    return {'task': value['task_id'], 'node': node_id, 'questions': digest(questions),
        'requirements': semantic_basis(value, node_id) if contract else digest([value['spec_version'], value['plan_version']]),
        'iteration': copy.deepcopy((contract or {}).get('iteration')),
        'inputs': {key: input_basis(key) for key in (contract or {}).get('bound_dependencies', (contract or {}).get('depends_on', []))},
        'authorization': decision_authority(record),
        'model': current_node_model(record, node)}


def identity(record, node_id, questions):
    # Keep still-current pre-upgrade questions answerable without rewriting history.
    legacy = _legacy_identity(record, node_id, questions)
    changed_model = any(row.get('node_id') == node_id for row in record.get('collaboration_model_changes', []))
    if legacy in state(record)['questions'] and not changed_model:
        return legacy
    return digest(['decision-v2', decision_basis(record, node_id, questions)])


def decision_freshness(record, row):
    if row.get('id') == identity(record, row['node_id'], row['questions']):
        return {'current': True, 'scope_status': 'unchanged', 'automatic_reuse': True,
                'changed_fields': [], 'reason': ('问题、要求、输入内容、模型和授权范围仍匹配。'
            if row.get('basis') else '旧版问题与依赖、授权指纹仍匹配，未记录该节点的后续换模。')}
    previous = row.get('basis')
    if previous:
        current = decision_basis(record, row['node_id'], row['questions'])
        changed = [key for key in current if previous.get(key) != current[key]]
        reason = '决定适用范围已变化：' + ', '.join(changed)
    else:
        changed = ['legacy_dependencies_or_authority']
        reason = ('历史答复确实存在，但旧记录没有分项范围快照；当前依赖或授权指纹不匹配。'
                  '这不能证明原范围已经失效。须核对原问题、当时答复和实际页面/模型/授权变化；'
                  '不得视为从未回答，也不得扩大到其他节点、模型或全量范围。')
    return {'current': False, 'scope_status': 'changed' if previous else 'unverified',
            'automatic_reuse': False, 'changed_fields': changed, 'reason': reason}


def prepare(record, node_id, questions, *, category="business", optional_proof=None):
    value = state(record)
    if category not in REQUIRED | {"optional"} or not isinstance(questions, list) or not 1 <= len(questions) <= 3:
        raise ValueError("一次提问必须包含 1–3 个问题及宿主问题分类")
    key = identity(record, node_id, questions)
    previous = value["questions"].get(key)
    if previous:
        return copy.deepcopy(previous)
    milestone = value["contracts"].get(node_id, {}).get("milestone_id", "task")
    eligible = bool(category == "optional" and optional_proof and optional_proof.get("reviewed") is True
                    and optional_proof.get("reversible") is True and optional_proof.get("changes_requirements") is False
                    and optional_proof.get("changes_authority") is False and optional_proof.get("missing_required_input") is False)
    if category == "optional" and not eligible:
        category = "required_input"
    row = {"id": key, "node_id": node_id, "milestone_id": milestone, "category": category,
           "questions": copy.deepcopy(questions), "status": "waiting", "spec_version": value["spec_version"],
           "basis": decision_basis(record, node_id, questions)}
    if eligible:
        row['optional_proof'] = copy.deepcopy(optional_proof)
        count = value["optional_questions"].get(milestone, 0)
        default = optional_proof.get("default")
        if count >= value["policy"]["optional_questions_per_milestone"]:
            if not valid_answer(questions, default):
                row.update(status="blocked", reason="可选提问额度已用完，但没有合法默认；保留具体阻塞")
            else:
                row.update(status="answered", answer=copy.deepcopy(default), answer_source="assumption")
                event(value, "assumption_recorded", question_id=key, answer=default, proof=optional_proof)
        else:
            value["optional_questions"][milestone] = count + 1
    value["questions"][key] = row
    event(value, "question_recorded", question_id=key, category=category, milestone_id=milestone)
    return copy.deepcopy(row)


def valid_answer(questions, answer):
    if not isinstance(answer, dict):
        return False
    names = {question.get("name") or f"q{index}" for index, question in enumerate(questions)}
    if set(answer) != names:
        return False
    for index, question in enumerate(questions):
        name = question.get("name") or f"q{index}"
        selected = answer[name]
        options = question.get("options") or []
        if options:
            choices = {o.get("value", o.get("id", o.get("label"))) if isinstance(o, dict) else o for o in options}
            if not isinstance(selected, (str, int, bool)) or selected not in choices:
                return False
        elif not isinstance(selected, str) or not selected.strip():
            return False
    return True


def answer(record, question_id, result, *, source="user", choice_bundle=None,
           choice_answers=None, choice_inputs=None):
    value = state(record)
    row = value["questions"][question_id]
    if row['status'] not in {'waiting', 'answered'}:
        raise ValueError('问题已失效或被替代，请回答当前问题。')
    if identity(record, row["node_id"], row["questions"]) != question_id:
        raise ValueError("相关规格或授权已经变化，旧答案没有生效")
    assumption = (source == "auto_selected_recommended" and row["category"] == "optional"
                  and result == row.get("optional_proof", {}).get("default"))
    valid = valid_answer(row["questions"], result)
    if choice_bundle:
        from mewcode.orchestration.interaction_choices import project_questions, resolve_answers
        # The ledger freezes displayed IDs and their exact value mapping. The
        # caller returns decoded values, which must be derived from that same
        # frozen bundle rather than compared to the UI IDs or trusted directly.
        if project_questions(row["questions"], choice_bundle) != row["questions"]:
            raise ValueError("选项包与已记录问题不一致，旧答案没有生效")
        resolved = resolve_answers(choice_bundle, choice_answers, choice_inputs=choice_inputs)
        names = {q.get('name') or f'q{i}' for i,q in enumerate(row['questions'])}
        valid = (isinstance(result, dict) and set(result) == names
                 and not resolved['actions'] and resolved['values'] == result)
    if (source != "user" and not assumption) or not valid:
        raise ValueError("必要问题必须收到明确、完整的用户答案")
    if row["status"] == "answered":
        if row["answer"] != result:
            raise ValueError("同一问题已有不同答案，请以新修订提交")
        return
    row.update(status="answered", answer=copy.deepcopy(result), answer_source="assumption" if assumption else "user")
    if choice_bundle:
        row.update(answer_selection=copy.deepcopy(choice_answers),
                   answer_choice_bundle={key: choice_bundle.get(key) for key in ('id','version')})
    event(value, "assumption_recorded" if assumption else "question_answered", question_id=question_id,
          source="assumption" if assumption else "user")


def present(questions, *, internal_ids, labels):
    """Replace actual host IDs only; preserve unrelated product/model numbers."""
    result = copy.deepcopy(questions)
    for question in result:
        for field in ("question", "message", "context"):
            text = question.get(field)
            if not isinstance(text, str):
                continue
            for key in sorted(internal_ids, key=len, reverse=True):
                import re
                text = re.sub(r"(?<![A-Za-z0-9_-])" + re.escape(key) + r"(?![A-Za-z0-9_-])", labels.get(key, "当前步骤"), text)
            question[field] = text
    return result
