"""Checkpointed primary-model assistance under the inherited host policy."""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Literal, Mapping
from mewcode.kernel.async_commit import durable_call
from mewcode.runtime_workers import io_call
from mewcode.experts.node_ledger import ledger_for_run, project_node_view
from mewcode.experts.blocking_policy import refresh_clearances, CLEARANCE_PRODUCERS, ClearanceContext


@dataclass(frozen=True)
class AssistanceRequest:
    request_id: str
    node_id: str
    epoch: int
    kind: str
    need: str
    evidence_refs: tuple[str, ...]
    requested_tools: tuple[str, ...]
    origin: Literal['node', 'controller']
    scope_requests: tuple[dict, ...] = ()


@dataclass(frozen=True)
class AssistanceAction:
    tool: str
    summary: str
    receipt_id: str | None
    permission_decision: str


@dataclass(frozen=True)
class AssistanceReceipt:
    request_id: str
    node_id: str
    epoch: int
    actions: tuple[AssistanceAction, ...]
    artifacts: tuple[str, ...]
    clearances: tuple[dict, ...]
    next_step: Literal['resume', 'rebase', 'ask_human', 'fail']
    patch: Mapping | None
    notes: str
    question: str = ''
    question_options: tuple[dict | str, ...] = ()
    question_context: str = ''
    authority_needed: tuple[dict, ...] = ()


@dataclass
class RunContext:
    node: object
    policy: object
    result: object = None


def validate_clearances(record, request, claims):
    """A model's proposed reference is not itself a clearance observation."""
    ledger = ledger_for_run(record)
    view = ledger.view(request.node_id)
    facts = {f.fact_id: f for f in view.history}
    blockers = {f.fact_id: f for f in view.active_blockers}
    accepted, errors = [], []
    for claim in claims:
        evidence = facts.get(claim.get('evidence_ref'))
        blocker = blockers.get(claim.get('blocker_fact_id'))
        producer = CLEARANCE_PRODUCERS.get(claim.get('clearance_kind'))
        valid = (evidence and evidence.source == 'host' and evidence.epoch == request.epoch
                 and evidence.payload.get('assistance_request_id') == request.request_id and blocker and producer)
        produced = producer(ClearanceContext(blocker, view)) if valid else None
        if produced and produced.evidence_ref == evidence.fact_id:
            row = asdict(produced)
            ledger.record(request.node_id, 'clearance', row)
            accepted.append(row)
        else:
            errors.append('丢弃未通过宿主核验的 clearance: ' + str(claim.get('evidence_ref', '')))
    return tuple(accepted), errors


class AssistanceCoordinator:
    def __init__(self, *, store, run_id, dispatch, emit, before_resume, task, work_dir, model,
                 verification_dispatch=None, verification_model=None, merge_invoker=None):
        self.store, self.run_id, self.dispatch = store, run_id, dispatch
        self.emit, self.before_resume = emit, before_resume
        self.task, self.work_dir, self.model = task, work_dir, model
        self.verification_dispatch, self.verification_model = verification_dispatch, verification_model
        self.merge_invoker = merge_invoker
        self.arbitration = None

    async def _investigate(self, request, ctx):
        from mewcode.kernel.investigations import Investigations, InvestigationGate
        from mewcode.kernel.events import digest
        node, policy = ctx.node, ctx.policy
        supervisor = Investigations(self.store, self.run_id, self.work_dir, arbitration=self.arbitration)
        helper_id = node.node_id + '__investigate_' + request.request_id[:12]
        judgment_digest = []
        model = self.model or {}
        verifier = self.verification_model or {}
        scope_tools = {tool for scope in request.scope_requests for tool in scope['scope'].get('tools', [])}
        if any(scope['operation_class'] == 'run_command_prefix' for scope in request.scope_requests):
            scope_tools.add('Bash')
        if request.requested_tools and not set(request.requested_tools) <= scope_tools:
            raise ValueError('调查工具请求必须包含在具体授权范围中')
        async def execute(directory):
            snapshot = next(row for row in supervisor.snapshots.list() if row['work_dir'] == directory)
            snapshot['scope_requests'] = list(request.scope_requests)
            holder = {}
            isolated = replace(policy, work_dir=directory, investigation_snapshot=snapshot,
                assistance_session=True, assistance_holder=holder, assistance_protected_paths=(),
                assistance_permission_log=[], node_tools=tuple(sorted(scope_tools | {'ReadFile', 'Glob', 'Grep'})),
                parallel_readonly=False, loop_context=None, audit_contract=None, candidate_context=None,
                expansion_holder=None, expansion_write_paths=None, command_allowlist=(),
                command_receipt_context=None, allow_delegation=False, max_direct_agents=0,
                allow_recursive_delegation=False, resume_messages=False, authorization_node_id=request.node_id,
                stage_inputs={**(policy.stage_inputs or {}), '@assistance': asdict(request)},
                input_revision=digest([request.request_id, snapshot['snapshot_id']]))
            helper = replace(node, node_id=helper_id, role='executor', label=node.label + ' · 快照调查', members=(), model=None)
            await self.dispatch(helper, '在当前隔离快照中调查；拟改动只能写入当前目录的对应相对路径。'
                '保留原权限、安全审查和验收标准；超出声明范围时停止并报告。\n' + request.need, isolated)
        async def verify(directory, changes, change_digest):
            if self.verification_dispatch is None:
                return {'passed': False, 'reason': '缺少独立核查入口', 'change_digest': change_digest}
            check_node = replace(node, node_id=helper_id + '_verify', role='auditor', members=(), model=None)
            check_policy = replace(policy, work_dir=directory, parallel_readonly=True, assistance_session=False,
                assistance_holder=None, loop_context=None, audit_contract=None, candidate_context=None,
                expansion_holder=None, expansion_write_paths=None, command_allowlist=(), command_receipt_context=None,
                node_tools=('ReadFile', 'Glob', 'Grep'), deny_tools=('Bash', 'WriteFile', 'EditFile'),
                allow_delegation=False, max_direct_agents=0, allow_recursive_delegation=False, resume_messages=False)
            result = await self.verification_dispatch(check_node, '独立核查以下快照改动是否满足原任务与原验收要求。'
                '实际读取改动文件，使用 SubmitVerdict 给出结论和依据。\n原任务：' + self.task
                + '\n改动路径：' + json.dumps(changes, ensure_ascii=False), check_policy)
            verdict = result.verdict or {}
            judgment_digest.append(change_digest)
            return {'passed': verdict.get('verdict') == 'PASS',
                    'reason': verdict.get('summary') or result.text, 'change_digest': change_digest}
        async def merge(changes, backup):
            if self.merge_invoker is None:
                raise InvestigationGate('business', '缺少受审查的并回执行入口')
            return await self.merge_invoker(changes, backup, node, policy, judgment_digest[-1], request.scope_requests)
        try:
            await self.before_resume()
            result = await supervisor.run(node_id=node.node_id, requests=list(request.scope_requests), execute=execute,
                verify=verify, merge=merge, executor_provider=model.get('provider_key'), verifier_provider=verifier.get('provider_key'))
            receipt = AssistanceReceipt(request.request_id, node.node_id, request.epoch, (), (), (),
                'resume' if result['status'] == 'merged' else 'ask_human', None,
                '快照调查已完成并并回。' if result['status'] == 'merged' else '调查未改变任何东西；当前阻塞仍待下一处理人。')
        except InvestigationGate as gate:
            receipt = AssistanceReceipt(request.request_id, node.node_id, request.epoch, (), (), (),
                'ask_human', None, gate.reason, gate.reason,
                ({'value': 'hold', 'label': '保存快照并暂停', 'recommended': True},
                 {'value': 'replan', 'label': '重新规划处理方案'}),
                '当前改动保留在隔离快照中。', tuple(gate.requests or request.scope_requests) if gate.kind == 'authorization_extension' else ())
        def save(record):
            stage = record['node_states'][node.node_id]
            stage.setdefault('assistance_receipts', []).append(asdict(receipt))
            ledger_for_run(record).record(node.node_id, 'assistance', asdict(receipt), identity=['receipt', request.request_id])
        (await durable_call(self.store.mutate, self.run_id, save, durable=True))
        return receipt

    async def run(self, request: AssistanceRequest, ctx: RunContext, *, preserve_contract=False) -> AssistanceReceipt:
        node, policy = ctx.node, ctx.policy
        existing = ((await io_call(self.store.load, self.run_id)) or {}).get("node_states", {}).get(node.node_id) or {}
        prior = next((r for r in existing.get("assistance_receipts") or [] if r.get("request_id") == request.request_id), None)
        if prior:
            return AssistanceReceipt(**{**prior, "actions": tuple(AssistanceAction(**a) for a in prior.get("actions") or [])})
        if request.kind == 'investigation':
            if preserve_contract:
                raise ValueError('协作合同内的只读协助不能启动调查写入，请在任务关口处理。')
            return await self._investigate(request, ctx)
        expected_revision = existing.get("input_revision")
        from mewcode.experts.recovery_state import digest
        expected_flow = digest(((await io_call(self.store.load, self.run_id)) or {}).get("locked_flow"))
        helper_id = node.node_id + '__assist_' + request.request_id[:12]
        holder, permission_log = {}, []
        def claim(record):
            ledger = ledger_for_run(record)
            if ledger.view(node.node_id).epoch != request.epoch:
                raise ValueError('Assistance belongs to a stale node epoch')
            stage = record['node_states'][node.node_id]
            stage['assistance_request'] = {**asdict(request), 'status': 'running'}
            stage['assistance_checkpoint'] = {'message_count': len(stage.get('messages') or []),
                'fact_count': len(ledger.view(node.node_id).history), 'request_id': request.request_id}
            stage['status'] = 'escalated'
            ledger.set_phase(node.node_id, 'escalated', reason='primary_assistance')
        snapshot = (await durable_call(self.store.mutate, self.run_id, claim, durable=True))
        protected = []
        if node.role == 'auditor' or policy.audit_contract or (policy.loop_context or {}).get('reviewer'):
            for stage in (snapshot.get('node_states') or {}).values():
                protected.extend(str(a.get('path')) for a in (stage.get('handoff') or {}).get('artifacts') or []
                                 if isinstance(a, dict) and a.get('path'))
            protected.extend(str(p) for p in [(policy.audit_contract or {}).get('artifact_dir'),
                (policy.audit_contract or {}).get('baseline_dir')] if p)
        from mewcode.experts.recovery_case import active_case
        selected_case = active_case(snapshot, node.node_id)
        case_tools = tuple(dict.fromkeys((*request.requested_tools, 'ReadStageResult', 'ReadAuthorizations',
            'ReadRecoveryContext', 'VerifyRecoveryCheckpoint', 'ToolSearch',
            'SubmitAssistanceReceipt', 'SubmitHandoff'))) if selected_case else ('*',)
        assistance_policy = replace(policy, node_tools=case_tools,
            parallel_readonly=True if preserve_contract else policy.parallel_readonly if selected_case else False,
            audit_contract=None, loop_context=None, candidate_context=None, expansion_write_paths=None,
            expansion_holder=None, command_allowlist=(), command_receipt_context=None,
            allow_delegation=False, max_direct_agents=0, allow_recursive_delegation=False,
            execution_recovery=True, assistance_session=True, assistance_holder=holder,
            assistance_permission_log=permission_log, assistance_protected_paths=tuple(protected),
            work_dir=self.work_dir, resume_messages=False, handoff_holder=None, input_revision=expected_revision or '',
            stage_inputs={'@run': snapshot, '@assistance': asdict(request), node.node_id: snapshot['node_states'][node.node_id]})
        # An external-send grant is not this helper's role budget. The visual
        # invocation journal charges its provider receipts once across rounds;
        # independently configured node/loop budgets remain in policy.
        helper = replace(node, node_id=helper_id, role='executor', label=node.label + ' · 主模型协助', members=(), model=None)
        from mewcode.experts.authorization_ledger import authorization_block
        prompt = (authorization_block(snapshot, node) + '你是主会话模型。只执行 @assistance 中这个已选定的方案，不扩大范围或改做别的。工具遵循主会话全局权限与 Auto 审查。'
                  '遇到未被授权块 A 覆盖的操作：停止该操作，在回执 authority_needed 中列出具体范围，不自行变通。'
                  '保留原节点目标和已完成成果，写操作效果未知时先核验文件、git 和产物证据。'
                  '用 SubmitAssistanceReceipt 提交回执，再用 SubmitHandoff 保存本段结论。'
                  '核验未知写操作时，effect_verifications 填原 request_id、effect(applied/not_applied/partial)，'
                  'evidence_refs 引用本次实际读取文件或 git 状态的 tool_id；结合原命令及目标产物逐项判断。'
                  'resume 回调原节点验证；rebase 提交仅影响本节点和未开始下游的 patch；具体授权或业务决定用 ask_human。\n'
                  + request.need + '\n原任务：\n' + self.task)
        if preserve_contract:
            prompt += ('\n本次协作诊断仅可读取原范围的已有证据，不能改文件、调用命令或改合同。'
                       '原执行节点与模型保持。可接续时提交 resume；需要写入、调查或修改计划时提交 ask_human，不能 rebase。')
        await self.emit({'type': 'expert_step', 'node_id': node.node_id, 'status': 'running',
            'label': '主模型协助', 'assistance': asdict(request)})
        await self.before_resume()
        try:
            result = await self.dispatch(helper, prompt, assistance_policy)
        except asyncio.CancelledError:
            def interrupted(record):
                stage = record['node_states'][node.node_id]
                stage['assistance_request']['status'] = 'interrupted'
                ledger_for_run(record).record(node.node_id, 'assistance',
                    {**asdict(request), 'status': 'interrupted', 'helper_node_id': helper_id})
            (await durable_call(self.store.mutate, self.run_id, interrupted, durable=True))
            raise
        except Exception as exc:
            holder.update(next_step='fail', notes='协助执行未完成：' + type(exc).__name__)
            result = None
        await self.before_resume()
        latest = (await io_call(self.store.load, self.run_id)) or {}
        helper_stage = (latest.get('node_states') or {}).get(helper_id) or {}
        actions = tuple(AssistanceAction(str(row.get('tool', '')), str(row.get('summary', '')),
            row.get('receipt_id'), str(row.get('permission_decision', 'unverified'))) for row in permission_log)
        if not holder:
            actual = [{'tool': t.get('name', ''), 'status': t.get('status', ''), 'is_error': t.get('is_error', False)}
                for t in helper_stage.get('tools', {}).values()]
            holder.update(next_step='ask_human', notes='executor_incomplete：执行员未提交回执；'
                + '；'.join(f"{t['tool']}：{t['status']}" for t in actual[-8:]))
            if not actions:
                actions = tuple(AssistanceAction(t.get('name', ''), '宿主保存的工具状态：' + t.get('status', ''),
                    key, 'checked_per_call') for key, t in helper_stage.get('tools', {}).items())
        receipt = None
        def finish(record):
            nonlocal receipt
            ledger = ledger_for_run(record)
            stage = record['node_states'][node.node_id]
            view = ledger.view(node.node_id)
            if (view.epoch != request.epoch or stage.get("input_revision") != expected_revision
                    or digest(record.get("locked_flow")) != expected_flow):
                raise ValueError('Node changed during assistance')
            observed = [{**t, 'tool_id': key} for key, t in (helper_stage.get('tools') or {}).items()
                        if t.get('status') == 'completed' or (t.get('status') == 'failed'
                            and (t.get('host_observation') or {}).get('file_state', {}).get('exists') is False)]
            verification_errors = []
            for claim in holder.get('effect_verifications') or []:
                from mewcode.experts.command_receipts import resolve_unknown_receipt
                from mewcode.experts.effect_verification import verify_effect_observations
                original = next((f for f in reversed(view.history)
                    if f.kind == 'receipt' and f.payload.get('status') == 'outcome_unknown'
                    and f.payload.get('request_id') == claim.get('request_id')), None)
                refs = claim.get('evidence_refs') or []
                evidence = [t for t in observed if t['tool_id'] in refs and t.get('name') == 'ReadFile']
                if not original or not refs or set(refs) != {t['tool_id'] for t in evidence}:
                    verification_errors.append('效果核验缺少本次协助的完整只读工具证据。')
                    continue
                checked = verify_effect_observations(dict(original.payload), evidence, self.work_dir)
                if checked is None or checked['effect'] != claim.get('effect'):
                    verification_errors.append('宿主证据无法机械确认原命令目标与所报效果；未知写入继续保留防重放约束。')
                    continue
                facts = [ledger.record(node.node_id, 'receipt', {
                    'effect_verification_observation': True, 'current_state_observed': True,
                    'request_id': claim['request_id'], 'effect_fingerprint': original.payload.get('effect_fingerprint'),
                    'assistance_request_id': request.request_id, 'tool_id': t['tool_id'],
                    'tool_name': t.get('name'), 'args': t.get('args') or {},
                    'is_error': t.get('is_error', False), 'host_observation': copy.deepcopy(t.get('host_observation') or {}),
                    'result_hash': digest(t.get('output') or ''),
                    'result_summary': str(t.get('output') or '')[:4000]},
                    identity=['effect_observation', request.request_id, t['tool_id']]) for t in evidence]
                try:
                    resolve_unknown_receipt(record, node.node_id, claim['request_id'], verification={
                        'effect': checked['effect'], 'proof': checked['proof'],
                        'evidence_refs': [f.fact_id for f in facts]}, work_dir=self.work_dir)
                except ValueError as exc:
                    verification_errors.append(str(exc))
            for blocker in view.active_blockers:
                if blocker.payload.get('kind') == 'capability_missing':
                    names = set(helper_stage.get('available_tools') or [])
                    requested = set(request.requested_tools)
                    probe = next((t for t in observed if t.get('name') in requested and not t.get('is_error')), None)
                    if requested and requested <= names and probe:
                        ledger.record(node.node_id, 'receipt', {'blocker_fact_id': blocker.fact_id,
                            'assistance_request_id': request.request_id, 'probe_success': True,
                            'required_tools_available': True, 'tool_receipt_id': probe.get('tool_id')})
            accepted, errors = validate_clearances(record, request, holder.get('clearances') or [])
            errors.extend(verification_errors)
            automatic = refresh_clearances(record, node.node_id)
            accepted += tuple(asdict(c) for c in automatic)
            step = holder['next_step']
            from mewcode.experts.recovery_state import blockers
            from mewcode.experts.recovery_context import tool_effect_pending
            unsettled = [{**t, 'tool_id': key} for key, t in (helper_stage.get('tools') or {}).items()
                         if tool_effect_pending(t)]
            helper_blockers = blockers(helper_stage)
            if (result is None or result.status != 'completed' or unsettled or helper_blockers
                    or helper_stage.get('pending_permission') or helper_stage.get('pending_interaction')):
                step = 'ask_human'
                errors.append('协助执行尚未成功结束或存在未决工具效果；已提交的回执不能批准回调。')
                from mewcode.experts.command_receipts import request_receipts
                for tool in unsettled:
                    if tool.get('status') not in {'running', 'outcome_unknown'}:
                        continue
                    args = tool.get('args') or {}
                    rows = request_receipts(node.node_id, expected_revision or '', [{
                        'command': args.get('command') or tool.get('name', ''),
                        'input_scope': [self.work_dir], 'forbidden_changes': [],
                        'verification_command': ''}], status='outcome_unknown', epoch=request.epoch)
                    for row in rows:
                        row.update(helper_node_id=helper_id, tool_id=tool.get('tool_id'),
                                   effect_class='unknown', assistance_request_id=request.request_id)
                        ledger.record(node.node_id, 'receipt', row)
                        stage.setdefault('command_receipts', []).append(row)
                        ledger.record(node.node_id, 'blocker', {'kind': 'effect_unknown',
                            'ref': row['request_id'], 'request_id': row['request_id'], 'helper_node_id': helper_id})
            if result and ((result.handoff or {}).get('blockers') or (result.handoff or {}).get('question')):
                step = 'ask_human'
                errors.append('协助会话仍报告未解决事项，已保留检查点。')
            active = ledger.view(node.node_id).active_blockers
            from mewcode.experts.blocking_policy import BLOCKER_CLASS
            unresolved_hard = any(BLOCKER_CLASS.get(f.payload.get("kind"), "business") in {"authority", "business"} for f in active)
            if step in {"resume", "rebase"} and unresolved_hard:
                step = "ask_human"
                errors.append("仍有未核验的授权、写入效果或业务问题，保留原边界。")
            if step == 'rebase' and preserve_contract:
                step = 'ask_human'
                errors.append('本次只读协助不能修改协作合同，请通过任务关口修订。')
            if step == 'rebase':
                from mewcode.experts.maintenance_adoption import adopt_assistance_patch
                try:
                    adopt_assistance_patch(copy.deepcopy(record), request, holder.get('patch'))
                except ValueError as exc:
                    step = 'ask_human'
                    errors.append('协助修订未接纳：' + str(exc))
            notes = str(holder.get('notes') or '') + ('\n' + '\n'.join(errors) if errors else '')
            receipt = AssistanceReceipt(request.request_id, node.node_id, request.epoch, actions,
                tuple(holder.get('artifacts') or []), accepted, step, holder.get('patch'), notes,
                str((result.handoff or {}).get('question') or '') if result else '',
                tuple((result.handoff or {}).get('question_options') or []) if result else (),
                str((result.handoff or {}).get('question_context') or '') if result else '',
                tuple(holder.get('authority_needed') or []))
            ledger.record(node.node_id, 'assistance', asdict(receipt), identity=['receipt', request.request_id])
            stage['assistance_request']['status'] = 'completed'
            stage.setdefault('assistance_usage', {})[request.request_id] = (
                result.billable_tokens if result and result.billable_tokens is not None else
                result.input_tokens + result.output_tokens if result else 0)
            stage.setdefault('assistance_receipts', []).append(asdict(receipt))
            stage['assistance_resume_message'] = '主模型协助回执：' + json.dumps(asdict(receipt), ensure_ascii=False)
            if step == 'resume':
                stage['status'] = 'running'
                if not ledger.view(node.node_id).active_blockers:
                    stage.pop('error_kind', None)
                ledger.set_phase(node.node_id, 'running', reason='assistance_callback')
            project_node_view(record, node.node_id)
            if receipt.next_step == 'rebase':
                from mewcode.experts.maintenance_adoption import adopt_assistance_patch
                adopt_assistance_patch(record, request, receipt.patch)
        (await durable_call(self.store.mutate, self.run_id, finish, durable=True))
        await self.emit({'type': 'expert_step', 'node_id': node.node_id, 'status': 'completed',
            'label': '主模型协助回执', 'assistance_receipt': asdict(receipt)})
        return receipt

    async def __call__(self, *, node, result, policy, allowed, host_facts):
        snapshot = (await io_call(self.store.load, self.run_id)) or {}
        ledger = ledger_for_run(snapshot)
        stage = (snapshot.get('node_states') or {}).get(node.node_id) or {}
        pending = stage.get('assistance_request') or {}
        if ((policy.token_budget is not None and policy.token_budget < 1024)
                or (policy.timeout_seconds is not None and policy.timeout_seconds <= 0)
                or host_facts.get('hard_boundary') or stage.get('pending_permission')
                or (result.handoff or {}).get('question')):
            return None
        if pending.get('status') in {'running', 'interrupted'}:
            return {'action': 'human', 'source': 'assistance_interrupted',
                'chosen': {'next_step': '上次协助尚未核对结束状态，保留检查点和宿主回执。'}}
        kind = host_facts.get('assistance_kind') or ('effect_verification' if result.error_kind == 'effect_unknown' else
            'environment' if result.error_kind == 'environment_fault' else 'capability' if result.error_kind == 'capability_missing' else 'diagnosis')
        if pending.get('status') != 'pending':
            pending = {}
        need = pending.get('need') or result.text[-4000:] or '核对当前阻塞并提供可验证的接续步骤'
        if host_facts.get('patch_required'):
            need += '\n恢复控制器要求提交节点 patch，用 rebase 返回；保留目标、模型、资源额度与验收合同。'
        requested_tools = pending.get('requested_tools') or (
            ['Bash'] if kind == 'capability' and stage.get('command_receipts') else [])
        request = AssistanceRequest(pending.get('request_id') or uuid.uuid4().hex, node.node_id, ledger.view(node.node_id).epoch,
            pending.get('kind') or kind, need,
            tuple(pending.get('evidence_refs') or ()), tuple(requested_tools),
            'node' if pending.get('status') == 'pending' else 'controller', tuple(pending.get('scope_requests') or ()))
        started = time.monotonic()
        if host_facts.get('preserve_collaboration_contract') and request.kind == 'investigation':
            return {'action': 'human', 'chosen': {'next_step': '当前请求需要调查写入，请在任务关口处理。'}}
        receipt = await self.run(request, RunContext(node, policy, result),
                                 preserve_contract=bool(host_facts.get('preserve_collaboration_contract')))
        action = 'diagnose_path' if receipt.next_step in {'resume', 'rebase'} else 'human'
        return {'action': action,
            'source': 'primary_model_assistance', 'assistance_receipt': asdict(receipt),
            'decision_tokens': (((await io_call(self.store.load, self.run_id)) or {}).get('node_states', {}).get(node.node_id) or {}).get('assistance_usage', {}).get(request.request_id, 0),
            'decision_seconds': time.monotonic() - started, 'human_question': receipt.question,
            'human_question_options': list(receipt.question_options), 'human_question_context': receipt.question_context,
            'chosen': {'action': action, 'next_step': receipt.notes}}
