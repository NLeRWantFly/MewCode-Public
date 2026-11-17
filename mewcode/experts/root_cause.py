"""Host validation of model hypotheses and deterministic early exit."""
from dataclasses import dataclass
import json
import re
import unicodedata

from mewcode.experts.recovery_case import FUNDAMENTAL, RETRYABLE, USER_ROUTED, method_fingerprint


DISALLOWED_DIAGNOSIS_PHRASES = (
    '需要具体授权、业务决定或补充材料',
    '请按具体原因处理',
    '当前卡片没有自动增加轮数的选项',
    '通过正常维护路径重新评估',
    '具体原因尚未查明',
)
_GENERIC_HYPOTHESES = {'原因', '未知', '未知原因', '系统错误', '环境问题', '运行异常', '执行失败', '配置问题', '能力不足'}


@dataclass(frozen=True)
class Verdict:
    action: str
    root_cause: str
    reason: str


def validate_input_questions(plan, case, dossier):
    """Ensure typed analyst candidates are real current-stage fields.

    Schema validation only proves that a model returned an object of the
    right shape.  This host check binds candidate names, types, conditions,
    and values to the live dossier so a model cannot invent a field or ask
    for a future-stage parameter as the current blocker.
    """

    current = {
        row.get('name'): row for row in (dossier.get('stage_input_questions')
                                         or dossier.get('input_definitions') or [])
        if isinstance(row, dict) and isinstance(row.get('name'), str)
    }
    node_id = case.get('node_id', '')
    for option in plan.get('options') or []:
        for candidate in ((option.get('needs') or {}).get('input_questions') or []):
            if not isinstance(candidate, dict):
                raise ValueError('needs.input_questions 必须是对象列表')
            name = candidate.get('name')
            actual = current.get(name)
            if actual is None:
                raise ValueError(f'恢复输入字段不属于当前阶段合同：{name}')
            collect_at = candidate.get('collect_at')
            if ((collect_at is not None and collect_at not in {'start', node_id})
                    or actual.get('collect_at', 'start') not in {'start', node_id}):
                raise ValueError(f'恢复输入字段尚未到当前收集阶段：{name}')
            if (candidate.get('type') is not None
                    and candidate.get('type') != actual.get('type', 'text')):
                raise ValueError(f'恢复输入字段类型与当前合同不一致：{name}')
            if (candidate.get('required') is not None
                    and bool(candidate['required']) != bool(actual.get('required', True))):
                raise ValueError(f'恢复输入字段必填约束与当前合同不一致：{name}')
            expected_options = {item.get('value') for item in (actual.get('options') or [])
                                if isinstance(item, dict)}
            for item in candidate.get('options') or []:
                if not isinstance(item, dict) or item.get('value') not in expected_options:
                    raise ValueError(f'恢复输入字段候选不属于当前合同：{name}')
            if candidate.get('required_when') is not None and candidate.get('required_when') != actual.get('required_when'):
                raise ValueError(f'恢复输入字段条件与当前合同不一致：{name}')


def _text_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _text_leaves(key)
            yield from _text_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _text_leaves(item)


def _evidence_sources(case, dossier):
    """Index only text that is present in this host-built case dossier."""
    from mewcode.permissions.reviewer import redact_sensitive

    sources = {'dossier:' + str(case['case_id']): '\n'.join(
        redact_sensitive(text) for text in _text_leaves(dossier))}
    for row in dossier.get('tool_failures') or []:
        if not isinstance(row, dict):
            continue
        evidence_id = row.get('evidence_id') or row.get('evidence_id_ref')
        excerpt = row.get('excerpt') or row.get('error_excerpt')
        if evidence_id and isinstance(excerpt, str):
            sources[str(evidence_id)] = redact_sensitive(excerpt)
    for row in dossier.get('blockers') or []:
        if isinstance(row, dict) and row.get('fact_id'):
            sources[str(row['fact_id'])] = '\n'.join(
                redact_sensitive(text) for text in _text_leaves(row))
    for row in dossier.get('command_receipts') or []:
        if not isinstance(row, dict):
            continue
        text = '\n'.join(redact_sensitive(item) for item in _text_leaves(row))
        for key in ('evidence_id', 'evidence_tool_id', 'receipt_ref', 'receipt_id', 'tool_id', 'ref'):
            if row.get(key):
                sources[str(row[key])] = text
    for row in dossier.get('rounds') or []:
        if not isinstance(row, dict):
            continue
        verification = row.get('verification') or {}
        text = '\n'.join(redact_sensitive(item) for item in _text_leaves(row))
        for ref in verification.get('evidence_refs') or []:
            if ref:
                sources[str(ref)] = text
    return sources


def _normalized_hypothesis(text):
    text = unicodedata.normalize('NFKC', str(text)).casefold()
    text = re.sub(r'[\W_]+', '', text, flags=re.UNICODE)
    return text


def validate_diagnosis(plan, case, dossier):
    """Bind each user-facing claim to real dossier evidence and an option."""
    diagnosis = plan.get('diagnosis')
    if not isinstance(diagnosis, dict):
        return
    hypotheses = diagnosis.get('hypotheses') or []
    if not 1 <= len(hypotheses) <= 3:
        raise ValueError('诊断必须包含 1 到 3 个可能原因')
    if not diagnosis.get('investigation') or not diagnosis.get('goal'):
        raise ValueError('诊断必须写明只读探查范围和目标里程碑')
    sources = _evidence_sources(case, dossier)

    def check_evidence(evidence_id, excerpt):
        if not evidence_id or not isinstance(excerpt, str) or not excerpt.strip():
            raise ValueError('诊断证据必须包含编号和原文摘录')
        source = sources.get(str(evidence_id))
        safe_excerpt = excerpt.strip()
        if source is None or safe_excerpt not in source:
            raise ValueError(f'证据摘录不在档案中：{evidence_id}')

    symptom = diagnosis.get('symptom') or {}
    check_evidence(symptom.get('evidence_id'), symptom.get('excerpt'))
    option_by_id = {str(row.get('option_id')): row for row in plan.get('options') or []
                    if isinstance(row, dict) and row.get('option_id')}
    investigation_options = [row for row in option_by_id.values()
        if (row.get('strategy') or {}).get('kind') == 'investigate']
    if len(investigation_options) != 1:
        raise ValueError('诊断方案必须包含且只包含一个只读探查选项')
    seen_ids = set()
    seen_summaries = set()
    seen_option_ids = set()
    seen_fingerprints = set()
    for hypothesis in hypotheses:
        hypothesis_id = str(hypothesis.get('id') or '').strip()
        summary = str(hypothesis.get('summary') or '').strip()
        normalized = _normalized_hypothesis(summary)
        if (not hypothesis_id or hypothesis_id in seen_ids
                or not normalized or normalized in _GENERIC_HYPOTHESES):
            raise ValueError('可能原因过于笼统或编号重复')
        if any(phrase in summary for phrase in DISALLOWED_DIAGNOSIS_PHRASES):
            raise ValueError('可能原因不能使用禁用套话')
        if normalized in seen_summaries:
            raise ValueError('原因重复：可能原因描述重复')
        seen_ids.add(hypothesis_id)
        seen_summaries.add(normalized)
        for evidence in hypothesis.get('evidence') or []:
            check_evidence(evidence.get('id'), evidence.get('excerpt'))
        option_id = str(hypothesis.get('option_id') or '')
        option = option_by_id.get(option_id)
        if option is None:
            raise ValueError(f'可能原因对应的方案不存在：{option_id}')
        if option_id in seen_option_ids or option.get('hypothesis_id') != hypothesis_id:
            raise ValueError('原因重复：每个可能原因必须关联独立方案')
        if not all(str(option.get(key) or '').strip() for key in ('user_label', 'actor', 'consequence')):
            raise ValueError('可能原因方案必须写明用户选项、执行者和结果')
        fingerprint = method_fingerprint(option['strategy'])
        if fingerprint in seen_fingerprints:
            raise ValueError('原因重复：不同可能原因不能对应相同方法')
        seen_option_ids.add(option_id)
        seen_fingerprints.add(fingerprint)
    if all(hypothesis.get('confidence') == 'low' for hypothesis in hypotheses):
        investigation_ids = {str(row['option_id']) for row in plan.get('options') or []
                             if isinstance(row, dict)
                             and (row.get('strategy') or {}).get('kind') == 'investigate'}
        if plan.get('recommended_option_id') not in investigation_ids:
            raise ValueError('所有可能原因把握都低时必须推荐只读探查')

    for value in _text_leaves(diagnosis):
        if any(phrase in value for phrase in DISALLOWED_DIAGNOSIS_PHRASES):
            raise ValueError('诊断不能使用禁用套话')
    for option in plan.get('options') or []:
        for key in ('user_label', 'consequence'):
            value = option.get(key) if isinstance(option, dict) else None
            if isinstance(value, str) and any(phrase in value for phrase in DISALLOWED_DIAGNOSIS_PHRASES):
                raise ValueError('方案说明不能使用禁用套话')
        if (isinstance(option, dict) and option.get('option_id') not in seen_option_ids
                and (option.get('strategy') or {}).get('kind') != 'investigate'):
            raise ValueError('诊断方案必须对应一个可能原因或只读探查')


def validate_plan(plan, case, dossier):
    from mewcode.tools.submit_recovery_plan import (
        INVESTIGATION_TOOLS,
        READONLY_VERIFICATION_TOOLS,
    )

    root = plan['root_cause']['class']
    if not plan['root_cause']['evidence_refs']:
        raise ValueError('根因判断必须引用宿主档案证据')
    known = {'dossier:' + case['case_id'], *[b['fact_id'] for b in dossier['blockers']]}
    if not set(plan['root_cause']['evidence_refs']).issubset(known):
        raise ValueError('根因引用不在本案件档案中')
    fingerprints = [method_fingerprint(option['strategy']) for option in plan['options']]
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError('同一方案内不能用不同编号重复同一方法')
    command_case = (case.get('failed_step') or {}).get('tool') in {'Bash', 'RunVerification'}
    if root == 'capability_absent' and not command_case and any(r.get('available') and r['modalities'].get('vision') is True
            and not r.get('review_only') and not r.get('is_jev') for r in dossier['capabilities'].get('models', [])):
        raise ValueError('能力清单存在可绑定路线，不能报告能力不存在')
    if root == 'capability_absent' and not command_case and not dossier['capabilities'].get('models_complete', False):
        raise ValueError('能力清单不完整，不能报告能力不存在')
    if root == 'capability_absent' and command_case and dossier['capabilities'].get('command_environment', {}).get('status') != 'capability_absent':
        raise ValueError('命令能力不存在必须引用宿主命令入口预检，不由视觉模型清单推断')
    if root == 'contract_defect' and not any(b.get('kind') in {'contract_defect', 'graph_invalid'} for b in dossier['blockers']):
        raise ValueError('合同缺陷缺少宿主校验证据')
    available = dossier['capabilities'].get('recovery_executor_tools')
    for option in plan['options']:
        strategy = option['strategy']
        if strategy.get('kind') == 'investigate':
            if (not strategy.get('tools')
                    or not set(strategy['tools']) <= INVESTIGATION_TOOLS
                    or option.get('changes_acceptance')
                    or option['verification']['tool'] not in READONLY_VERIFICATION_TOOLS):
                raise ValueError('探查方案只能使用只读工具且不能改变验收')
        if available is not None:
            missing = set(strategy['tools']) - set(available)
            if missing:
                raise ValueError('恢复执行员没有挂载所需工具：' + ', '.join(sorted(missing)))
        if (case.get('failed_step') or {}).get('tool') in {'Bash', 'RunVerification'}:
            if option['verification']['tool'] != 'VerifyRecoveryCheckpoint':
                raise ValueError('命令恢复必须核验真实命令回执 VerifyRecoveryCheckpoint；读文件或能力预检不能证明原命令成功')
    validate_input_questions(plan, case, dossier)
    validate_diagnosis(plan, case, dossier)
    boundary = dossier.get('recovery_boundary') or {}
    if boundary.get('needs_review') and not plan.get('boundary_review'):
        raise ValueError('当前软边界自检必须提交 boundary_review')
    return plan


def judge(case, round):
    root = round.get('root_cause', 'unclassified')
    if root in FUNDAMENTAL:
        return Verdict('report', root, '存在根本性问题，需要用户决定替代方式')
    if root in USER_ROUTED:
        return Verdict('user', root, '当前方案需要用户给出明确决定或补充已列出的信息。')
    if root in RETRYABLE:
        return Verdict('retry', root, '存在可执行的恢复方案；由宿主根据核验进展决定是否继续。')
    return Verdict('report', root, '无法核验下一轮条件')
