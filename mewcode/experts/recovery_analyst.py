"""A reusable read-only primary-route analyst; plans never authorize execution."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from typing import Any

from mewcode.experts.authorization_ledger import authorization_block
from mewcode.experts.problem_dossier import dossier_summary
from mewcode.experts.root_cause import validate_plan
from mewcode.tools.submit_recovery_plan import SubmitRecoveryPlanParams

ANALYST_TOOLS = ('ReadFile', 'Glob', 'Grep', 'ReadStageResult', 'ReadCaseDossier',
                 'ReadAuthorizations', 'ReadRecoveryContext', 'VerifyRecoveryCheckpoint',
                 'ProbeCapabilities', 'ToolSearch', 'SubmitRecoveryPlan')
ANALYSIS_CONTEXT_VERSION = 6
ANALYST_SYSTEM = '''你是恢复分析员，只读。你不执行任何修改，只通过 SubmitRecoveryPlan 提交方案。
1. 先判定根因类别并给出证据引用；明确区分“可以换方法重复尝试”与“根本性问题”。
2. 只核验原来失败的步骤现在能否执行，方案带宿主可执行的核验探针。同一方法允许在修复前置条件后重试，不为凑新方法换名称或泛查能力。
3. 方案需要的授权逐项以类型化形式列出；先对照授权块 A，已覆盖的写 covered_by，不要再提。
4. 用户口头表态是线索：若已表达意愿，在 based_on_statement 引用 ST 编号，让宿主一键确认，不要让用户重说。
5. 不要建议绕过权限、放宽验收、修改业务范围；会改变验收的方案必须标 changes_acceptance=true。
6. 不要重复已被拒绝的提权请求。证据不足时先用 ReadCaseDossier / ProbeCapabilities 取证，再提交。
7. 只读分析和已经批准的恢复轮次不需要新的授权项，needs.authority 应为空；有实际新增操作时才使用工具 Schema 的 operation_class 与对象 scope，不能填自由文本范围或自行发明类别。
8. 档案摘要已有具体证据时直接提交方案；读取档案只使用列出的分节，不泛查完整历史。提交成功即完成本轮分析，方案接纳不等于原业务验收通过。
9. 根因 evidence_refs 只引用 root_evidence_refs 中的编号；历史回合里的其他编号可作为方法说明，不能冒充当前根因凭证。contract_defect 仅在当前 blockers 存在 contract_defect 或 graph_invalid 宿主记录时可用；方案提交失败不自动证明业务图合同缺陷。
10. 操作授权只放 needs.authority，不重复放进 user_input。user_input 只提出用户尚未提供的事实或业务选择，问题须独立可回答，写明材料、页码、模型和所需费用信息，不使用“上述”、AUTH/ST 等内部编号代替具体问题。已有明确批准的范围不重复要求确认；批准外发不自动追加费用或补齐材料。若缺的是当前运行参数，优先在 needs.input_questions 中复制宿主给出的字段定义和候选值；不能发明新字段、未来阶段字段或新类型，旧版可继续使用 user_input。
11. diagnosis 是必需的结构化汇报：symptom.summary 用一句话说清现象，symptom.excerpt 必须逐字摘自 dossier 中带 evidence_id 的脱敏原文，填写相同的 symptom.evidence_id；impact 写具体受影响步骤；tried 只列真实已尝试方法和结果。
12. 提出 1~3 个彼此不同的 hypotheses。每个原因都引用 dossier 中真实 evidence ID 和原文摘录，写清 confidence、open_questions、option_id；对应方案的 hypothesis_id 必须等于该原因 id，并填写面向用户的 user_label、actor 和 consequence。探查方案也要填写 user_label、actor 和 consequence。不要用“环境问题”“系统错误”等泛化说法代替有证据的原因，也不要写禁用套话。若是探查后的新版判断，delta 只能使用“已排除、已确认、把握升高、把握降低、新增、无变化”。
13. 方案中提供一项只读探查：strategy.kind='investigate'，工具只能是 ReadFile、Glob、Grep、ReadCaseDossier、ProbeCapabilities；填写 diagnosis.investigation 的 scope、tools 和以秒为单位的 budget。所有原因把握都低时推荐这项探查。每个方案需有可执行策略和宿主可核验的 verification。
14. business_decision 只用于用户独有的范围、费用、目标或验收取舍；探查、回到门禁、批大小、工具与节点衔接属于系统技术处理，不得伪装成业务选择。
15. diagnosis.goal 写本次修复要达到的可核验 target、milestones（每项含证据形式 evidence 和预期投入 expected_seconds）以及 stop_when。参考 timing_reference 的同类实际投入并结合当前路线估计；样本不足时说明估计依据不足，不编造历史。可落盘的阶段产物证据使用 file:工作区相对路径，宿主核对文件实际新增或变化；日志不列为成果。同一产物只计一次进展，变化不等于业务验收通过。预期投入是软提醒，不是轮数或运行硬上限；是否继续由宿主根据可核验的实际进展决定。不要根据同一方法是否曾用过单独拒绝方案；同一方法若有新进展或前置条件已变化，仍可再次尝试。
15. 当 dossier.recovery_boundary.needs_review=true 时，boundary_review 必须填写 action（continue 或 converge）、reason、具体 next_step 和真实 evidence_refs。日志变多、只读取证或模型自述“有进展”不算进展；不得虚构回执或以无依据的重复尝试继续。'''


def input_questions_for_option(option: dict[str, Any]) -> list[dict[str, Any]]:
    """Return typed input candidates while preserving legacy ``user_input``."""

    needs = (option or {}).get('needs') or {}
    return [dict(item) for item in (needs.get('input_questions') or [])
            if isinstance(item, dict) and isinstance(item.get('name'), str)]


def input_requests_for_option(option: dict[str, Any]) -> list[Any]:
    """Return typed candidates plus old prose requests in stable order."""

    needs = (option or {}).get('needs') or {}
    result: list[Any] = list(input_questions_for_option(option))
    result.extend(item for item in (needs.get('user_input') or []) if item)
    return result


def _validate_input_questions(plan: dict[str, Any], dossier: dict[str, Any], *, node_id: str) -> None:
    """Validate model candidates against the live current-stage contract.

    Pydantic validates the shape of the model response.  This second check is
    host-owned: it rejects a well-shaped but fabricated field, a field
    collected by a later node, and candidates whose type or values do not
    match the actual graph contract.
    """

    current = {
        row.get('name'): row for row in (dossier.get('stage_input_questions')
                                         or dossier.get('input_definitions') or [])
        if isinstance(row, dict) and isinstance(row.get('name'), str)
    }
    for option in plan.get('options') or []:
        needs = option.get('needs') or {}
        for candidate in needs.get('input_questions') or []:
            if not isinstance(candidate, dict):
                raise ValueError('needs.input_questions 必须是对象列表')
            name = candidate.get('name')
            actual = current.get(name)
            if actual is None:
                raise ValueError(f'恢复输入字段不属于当前阶段合同：{name}')
            # ``stage_input_questions`` already excludes future declarations;
            # retain this explicit check for old dossiers supplied by tests or
            # restored snapshots.
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


class RecoveryAnalyst:
    def __init__(self, *, dispatch, store, run_id):
        self.dispatch, self.store, self.run_id = dispatch, store, run_id

    async def analyze(self, case, dossier, *, node, policy, feedback=''):
        self.last_tokens = 0
        self.last_wait_seconds = 0.
        holder = {}
        snapshot = self.store.load(self.run_id) or {}
        prior_started = snapshot.get('node_states', {}).get(case['analyst_session'], {}).get('started_at')
        n = len(case['rounds']) + 1
        previous = case['rounds'][-1] if case['rounds'] else None
        incremental = {'case_id': case['case_id'], 'round': n,
            'previous_round': previous, 'authorizations': dossier['authorizations'],
            'attempted_methods': [r['method_fingerprint'] for r in case['rounds']], 'feedback': feedback,
            'user_input_refs': [item['source_ref'] for item in dossier.get('user_inputs', [])],
            'root_evidence_ref': 'dossier:' + case['case_id'],
            'root_evidence_refs': ['dossier:' + case['case_id'], *[row['fact_id'] for row in dossier['blockers']]]}
        incremental['timing_reference'] = dossier.get('timing_reference') or {}
        incremental['recovery_boundary'] = dossier.get('recovery_boundary') or {}
        incremental['investigation_requested'] = case.get('investigation_requested') or {}
        incremental['continuation_direction'] = case.get('continuation_direction') or {}
        if incremental['user_input_refs']:
            incremental['input_rule'] = '用户补充已变化；用 ReadCaseDossier 读取完整 user_inputs，并重新核对方案与 verification 参数。'
        saved_analysis = snapshot.get('node_states', {}).get(case['analyst_session'], {})
        first = not saved_analysis.get('messages') or saved_analysis.get('analysis_context_version') != ANALYSIS_CONTEXT_VERSION
        if first:
            incremental['similar_cases'] = [{'case_id': c['case_id'], 'status': c['status'],
                'methods': [{'method': r.get('method', r['option_id']), 'result': r['verification'].get('result'),
                    'evidence_digest': r['evidence_digest']} for r in c['rounds']]}
                for c in dossier['similar_cases']]
        prompt = authorization_block(snapshot, node) + ANALYST_SYSTEM + '\n' + json.dumps(incremental, ensure_ascii=False)
        if (case.get('failed_step') or {}).get('tool') in {'Bash', 'RunVerification'}:
            prompt += ('\n原失败步骤是命令。用 ReadRecoveryContext(section="commands") 读取真实宿主回执；'
                '有同一命令的成功且已清理回执，直接用 VerifyRecoveryCheckpoint 核验，不重跑。'
                '若 command_started=false，分析具体拒绝理由；提出的操作仍须独立 AUTO 审查，并在原生命令边界执行。'
                '命令审查在隔离事务创建前进行，不能要求尚未创建的事务证明命令已经执行。'
                '核验探针必须是 VerifyRecoveryCheckpoint，不用 ReadAuditArtifact 查 tool_id，不设想尚未由宿主生成的回执文件。'
                '执行员不挂载嵌套 RequestAssistance，不能把它写入策略。只读取证不解除原拒绝，也不能标记业务完成。'
                '执行员实际工具清单：' + str(dossier['capabilities'].get('recovery_executor_tools', [])))
        prompt += '\n当前检查点的上一项方案：' + (previous.get('option_id', '') if previous else '尚未执行')
        if previous:
            prompt += ('\n宿主核验：' + previous['verification'].get('result', '') +
                '；证据：' + str(previous['verification'].get('evidence_refs', [])) +
                '。请给出下一轮计划；所有已试方法指纹见上方增量。')
        if first:
            prompt += '\n' + dossier_summary(dossier)
        analyst_policy = replace(policy, node_tools=ANALYST_TOOLS, parallel_readonly=True,
            assistance_session=False, assistance_holder=None, assistance_permission_log=None,
            recovery_analysis={'case_id': case['case_id'], 'round': n, 'holder': holder, 'dossier': dossier},
            audit_contract=None, loop_context=None, candidate_context=None, handoff_holder=None,
            command_allowlist=(), command_receipt_context=None, expansion_holder=None, expansion_write_paths=None,
            allow_delegation=False, max_direct_agents=0, allow_recursive_delegation=False,
            execution_recovery=False, resume_messages=True, max_iterations=policy.max_iterations,
            timeout_seconds=policy.timeout_seconds,
            token_budget=policy.token_budget,
            input_revision=case['input_revision'], node_epoch=case['epoch'],
            stage_inputs={'@run': snapshot, '@case': case, node.node_id: snapshot.get('node_states', {}).get(node.node_id, {})})
        helper = replace(node, node_id=case['analyst_session'], role='planner',
            label=node.label + ' · 恢复分析', members=(), model=None)
        result = None
        try:
            async with asyncio.timeout(analyst_policy.timeout_seconds):
                result = await self.dispatch(helper, prompt, analyst_policy)
        finally:
            if result is not None:
                self.last_tokens = int(result.billable_tokens if result.billable_tokens is not None else
                    result.input_tokens + result.output_tokens)
                self.last_wait_seconds = float(result.wait_seconds or 0.)
            else:
                # A timeout/cancellation can occur after paid responses. The role
                # dispatcher checkpoints usage on cancellation; never reuse an
                # older dispatch's tokens when this one did not start.
                saved = (self.store.load(self.run_id) or {}).get('node_states', {}).get(helper.node_id, {})
                if saved.get('started_at') is not None and saved['started_at'] != prior_started:
                    self.last_tokens = int(saved.get('billable_tokens', 0))
                    self.last_wait_seconds = float(saved.get('wait_seconds', 0.))
        if not holder:
            saved = (self.store.load(self.run_id) or {}).get('node_states', {}).get(helper.node_id, {})
            tools = saved.get('tools') or {}
            errors = [str(tool.get('output', ''))[:500] for tool in (tools.values() if isinstance(tools, dict) else tools)
                if tool.get('name') == 'SubmitRecoveryPlan' and tool.get('status') == 'failed'
                and float(tool.get('started_at') or 0) >= float(saved.get('started_at') or 0)]
            raise ValueError('分析员未提交恢复方案' + ('；最近提交错误：' + errors[-1] if errors else ''))
        # Re-validate mock/embedded dispatchers too; they must not bypass the tool schema.
        plan = SubmitRecoveryPlanParams(**holder).model_dump(by_alias=True)
        if plan['case_id'] != case['case_id'] or plan['round'] != n:
            raise ValueError('分析员返回旧案件或旧轮次')
        if result is None or result.status not in {'completed', 'needs_attention'}:
            raise ValueError('分析派发未正常结束：' + (result.status if result is not None else '无回执'))
        validate_plan(plan, case, dossier)
        _validate_input_questions(plan, dossier, node_id=node.node_id)
        return plan


def select_option(plan, record, case, *, grant_store=None):
    from mewcode.experts.authorization_ledger import covers
    ordered = sorted(plan['options'], key=lambda o: o['option_id'] != plan['recommended_option_id'])
    selected = case.get('selected_recovery_option') or {}
    if selected.get('case_id') == case['case_id'] and selected.get('round') == len(case['rounds']):
        # A concrete user selection is not permission to substitute a different
        # plan. If it cannot execute, report its actual remaining requirement.
        ordered = [option for option in ordered if option['option_id'] == selected.get('option_id')]
    for option in ordered:
        if option['changes_acceptance']:
            continue
        requests = option['needs']['authority']
        checks = [covers(record, {**r, 'scope': {**r['scope'], 'epoch': case['epoch'],
            'case_id': case['case_id'], 'round': len(case['rounds']) + 1, 'node_ids': [case['node_id']]}}, grant_store=grant_store) for r in requests]
        if not any(c.status == 'denied' for c in checks):
            return option, [r for r, c in zip(requests, checks) if c.status != 'covered']
    return None, []
