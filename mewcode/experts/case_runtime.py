"""Recovery analysis, explicit elevation, execution and host verification."""
from __future__ import annotations

import copy
import asyncio
from dataclasses import asdict, replace
import json
import time

from mewcode.experts.authorization_ledger import record_authorization, authorization_block, valid_request
from mewcode.experts.blocking_policy import refresh_clearances
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.problem_dossier import (
    build_dossier,
    incident_report,
    input_definitions_for_requests,
)
from mewcode.experts.recovery_analyst import RecoveryAnalyst, input_requests_for_option, select_option
from mewcode.experts.recovery_case import (CaseRound, active_case, apply_event, cases, digest,
    decision_version, error_head_hash, get_case, handoff_gate_recovery, method_fingerprint, migrate_legacy,
    open_case, reconcile_wait_state, save_case)
from mewcode.experts.root_cause import FUNDAMENTAL, judge
from mewcode.experts.recovery_decisions import elevation_card
from mewcode.experts.waits import BranchWaitPaused
from mewcode.experts.recovery_boundary import RecoveryBoundary, host_progress, milestone_files, milestone_progress


class CaseAnswered(Exception):
    pass


class CaseCoordinator:
    def __init__(self, *, store, run_id, emit, executor=None, capability_inventory=None,
                 capability_bind=None, probe=None, grant_store=None, autonomous=True, session=None,
                 decision_selector=None):
        self.store, self.run_id, self.emit, self.executor = store, run_id, emit, executor
        self.capability_inventory, self.capability_bind, self.probe = capability_inventory, capability_bind, probe
        self.grant_store, self.autonomous = grant_store, autonomous
        self.session = session
        self.decision_selector = decision_selector
        self.analyst = RecoveryAnalyst(dispatch=executor.dispatch, store=store, run_id=run_id) if executor else None

    def record(self):
        return self.store.load(self.run_id) or {}

    def update(self, case_id, event):
        updated = None
        def change(record):
            nonlocal updated
            case = get_case(record, case_id)
            stage = record.get('node_states', {}).get(case['node_id'], {})
            if ledger_for_run(record).view(case['node_id']).epoch != case['epoch'] or stage.get('input_revision', '') != case['input_revision']:
                raise ValueError('案件输入或代际已变化')
            updated = apply_event(record, case_id, event)
        self.store.mutate(self.run_id, change, durable=True)
        return updated

    async def publish(self, case, phase):
        archived = False
        def projected(record):
            nonlocal case, phase, archived
            # A user can stop or answer while readonly preparation is awaiting
            # the model. Publish a phase on the latest fact, never the old copy.
            latest = get_case(record, case['case_id'])
            if latest is None:
                raise BranchWaitPaused(case['node_id'], reason='恢复案件已不在当前检查点中。')
            case = latest
            stage = record.get('node_states', {}).get(case['node_id']) or {}
            projected_case = stage.get('recovery_case') or {}
            archived = (case.get('status') == 'superseded'
                or stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}
                or ledger_for_run(record).view(case['node_id']).epoch != case['epoch']
                or stage.get('input_revision', '') != case['input_revision']
                or projected_case.get('case_id') not in {None, case['case_id']})
            if case.get('closed_by_user'):
                phase = '已停止'
            elif case.get('status') == 'superseded':
                phase = '旧代际已保留'
            elif case.get('status') == 'resolved':
                case['handling'] = {'owner': 'system', 'handling_state': 'resolved', 'reason': '原失败步骤已恢复，返回原节点继续验收。'}
            case.setdefault('phases', ['发现问题'])
            if case['phases'][-1] != phase:
                case['phases'].append(phase)
            if archived:
                return
            if phase in {'统计问题', '主模型分析', '执行', '核验'} and not case.get('pending'):
                case['handling'] = {'owner': 'system', 'handling_state': 'working',
                    'reason_kind': 'technical', 'reason': '我正在处理当前步骤的问题。'}
            elif (case.get('handling') or {}).get('owner') == 'user' and case.get('pending'):
                if phase == '正在查找候选／准备处理方案':
                    case['handling']['handling_state'] = 'deciding'
                elif phase in {'待你提权', '待你补充', '已汇报'}:
                    case['handling']['handling_state'] = 'needs_user'
            usage = (stage.get('recovery_case_usage') or {}).get(str(case['epoch']), {})
            if case.get('resource_limits'):
                case['resource_usage'] = copy.deepcopy(usage)
            save_case(record, case)
            if stage:
                stage['recovery_phase'] = phase
        snapshot = self.store.mutate(self.run_id, projected, durable=True)
        node_label = next((node.get('label') for node in (snapshot.get('locked_flow') or {}).get('nodes', [])
                           if node.get('node_id') == case['node_id']), case['node_id'])
        await self.emit({'type': 'recovery_case', 'run_id': self.run_id, 'node_id': case['node_id'],
            'node_label': node_label, 'case': case, 'phase': phase,
            **({'archived': True, 'actionable': False} if archived else {})})

    def _current_case(self, case, *, allow_closed=False):
        record = self.record()
        latest = get_case(record, case['case_id'])
        stage = record.get('node_states', {}).get(case['node_id'])
        if (latest is None or stage is None or latest.get('status') in {'resolved', 'superseded'}
                or stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}
                or ledger_for_run(record).view(case['node_id']).epoch != latest['epoch']
                or stage.get('input_revision', '') != latest.get('input_revision', '')
                or (stage.get('recovery_case') or {}).get('case_id') not in {None, latest['case_id']}
                or (latest.get('closed_by_user') and not allow_closed)):
            raise BranchWaitPaused(case['node_id'], reason='当前问题已停止、过期或被新问题替代，旧结果未写入。')
        return latest

    async def pause(self, case, *, report=None, pending=None):
        latest = self._current_case(case, allow_closed=report is None and pending is None)
        if (latest and latest.get('events') != case.get('events')
                and latest.get('status') == 'open' and not latest.get('pending')):
            raise CaseAnswered()
        if report is not None:
            case = self.update(case['case_id'], {'kind': 'report',
                'identity': 'report:' + digest([report, decision_version(case)]), 'report': report})
            # ``advance(report)`` preserves typed input/elevation/business
            # cards carried by the report. Older reports still receive the
            # normal incident projection without overwriting a typed card.
            if case.get('pending', {}).get('kind') not in {'input', 'elevation', 'business_decision', 'incident'}:
                case['pending'] = {'kind': 'incident', 'round': len(case['rounds']),
                                   'options': copy.deepcopy(report.get('options', []))}
                self.store.mutate(self.run_id, lambda r: save_case(r, case), durable=True)
        elif pending is not None:
            if pending.get('plan'):
                case['plan'] = copy.deepcopy(pending['plan'])
                self.store.mutate(self.run_id, lambda r: save_case(r, case), durable=True)
            case = self.update(case['case_id'], {'kind': 'wait',
                'identity': 'wait:' + digest([pending, decision_version(case)]), 'pending': pending})
        from mewcode.experts.interaction_routing import derive_handling
        if pending is not None:
            kind = pending.get('kind', '')
            handling = derive_handling(purpose='authorization' if kind == 'elevation' else 'user_input',
                question_kind=kind, missing_user_fields=pending.get('input_definitions') or pending.get('questions') or [],
                explicit_user_gate=kind in {'business_decision', 'elevation'}, context={})
            case['handling'] = self._handling_summary(case, handling)
            self.store.mutate(self.run_id, lambda r: save_case(r, case), durable=True)
        handling = case.get('handling') or {}
        if handling.get('owner') == 'system' and handling.get('handling_state') == 'blocked':
            def blocked(record):
                current = get_case(record, case['case_id'])
                if current.get('closed_by_user') or current['status'] == 'superseded':
                    return
                current['pending'] = {}
                save_case(record, current)
                stage = record['node_states'][current['node_id']]
                stage.update(status='needs_attention', handling=copy.deepcopy(handling))
                stage.pop('pending_recovery_case', None)
                ledger_for_run(record).set_phase(current['node_id'], 'escalated', reason='system_blocked')
            self.store.mutate(self.run_id, blocked, durable=True)
            await self.publish(case, '系统受阻')
            raise BranchWaitPaused(case['node_id'], reason=handling.get('reason', ''), error_kind='system_recovery_blocked')
        def waiting(record):
            nonlocal case
            latest = get_case(record, case['case_id'])
            if latest is None or case['node_id'] not in record.get('node_states', {}):
                raise BranchWaitPaused(case['node_id'], reason='恢复案件已不在当前检查点中。')
            case = latest
            stage = record['node_states'][case['node_id']]
            if (case.get('status') == 'superseded' or stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}
                    or ledger_for_run(record).view(case['node_id']).epoch != case['epoch']
                    or stage.get('input_revision', '') != case['input_revision']):
                raise BranchWaitPaused(case['node_id'], reason='恢复案件已过期，请按当前检查点继续。')
            if case.get('closed_by_user'):
                return
            if case['status'] == 'open':
                raise CaseAnswered()
            stage.update(status='paused', pending_recovery_case={'case_id': case['case_id'], 'round': len(case['rounds'])})
            ledger_for_run(record).set_phase(case['node_id'], 'waiting_human', reason='case_decision')
        self.store.mutate(self.run_id, waiting, durable=True)
        case = get_case(self.record(), case['case_id']) or case
        future = None
        if self.session is not None and not case.get('closed_by_user'):
            waiters = getattr(self.session, '_case_waiters', None)
            if waiters is None:
                waiters = self.session._case_waiters = {}
            future = asyncio.get_running_loop().create_future()
            waiters[case['case_id']] = future
        preparing = None
        try:
            prepare = getattr(self.session, 'recovery_choice_preparer', None)
            if callable(prepare) and not case.get('closed_by_user'):
                await self.publish(case, '正在查找候选／准备处理方案')
                preparing = asyncio.create_task(prepare(case=case, snapshot=self.record()))
                if future is not None:
                    await asyncio.wait({preparing, future}, return_when=asyncio.FIRST_COMPLETED)
                if future is not None and future.done() and not preparing.done():
                    preparing.cancel()
                    await asyncio.gather(preparing, return_exceptions=True)
                else:
                    try:
                        case = await preparing
                    except (ValueError, OSError):
                        current = get_case(self.record(), case['case_id'])
                        if not current or current.get('status') not in {'reported', 'awaiting_user', 'open', 'superseded'}:
                            raise
                case = get_case(self.record(), case['case_id']) or case
            if case.get('closed_by_user') and future is not None and not future.done():
                future.set_result('close')
            if case.get('status') == 'open' and not case.get('pending'):
                raise CaseAnswered()
            if case.get('status') == 'superseded':
                raise BranchWaitPaused(case['node_id'], reason='案件已归入旧代际，请按当前检查点继续。')
            await self.publish(case, '已停止' if case.get('closed_by_user') else '待你提权' if case['pending'].get('kind') == 'elevation' else
                '待你补充' if case['pending'].get('kind') == 'input' else '已汇报')
            if future is not None:
                from mewcode.experts.waits import wait_for_human
                await wait_for_human(future, kind='case_answer', emit=self.emit, store=self.store,
                    run_id=self.run_id, stage_key=case['node_id'], wait_id=case['case_id'])
                if (get_case(self.record(), case['case_id']) or {}).get('status') == 'open':
                    raise CaseAnswered()
        finally:
            if preparing is not None and not preparing.done():
                preparing.cancel()
                await asyncio.gather(preparing, return_exceptions=True)
            if future is not None and self.session._case_waiters.get(case['case_id']) is future:
                self.session._case_waiters.pop(case['case_id'], None)
                if not future.done():
                    future.cancel()
        raise BranchWaitPaused(case['node_id'], reason=(case['report'].get('reason') or '请选择案件处理方式'), error_kind='recovery_case_awaiting_user')

    def _handling_summary(self, case, handling):
        record = self.record()
        nodes = (record.get('locked_flow') or {}).get('nodes', [])
        node = next((item for item in nodes if item.get('node_id') == case['node_id']), {})
        completed = [item.get('label', item['node_id']) for item in nodes
                     if (record.get('node_states', {}).get(item['node_id']) or {}).get('status') == 'completed']
        diagnosis = (case.get('plan') or {}).get('diagnosis') or (case.get('report') or {}).get('diagnosis') or {}
        pending = case.get('pending') or {}
        fields = pending.get('input_definitions') or []
        labels = [str(item.get('label') or item.get('name')) for item in fields if isinstance(item, dict)]
        why = handling.get('reason', '')
        if labels:
            why = '还需要你确定：' + '、'.join(labels) + '。其余技术步骤由我处理。'
        return {**handling, 'summary': {
            'goal': node.get('label') or case['node_id'],
            'progress': '已完成：' + '、'.join(completed) if completed else '已有规划和已填信息会继续复用，当前步骤尚未完成。',
            'why_user': why if handling['owner'] == 'user' else '',
            'recommendation': ('请确认下面列出的具体范围或补充所缺信息。' if handling['owner'] == 'user'
                               else '我会依据已有证据处理技术问题；当前没有可核验的继续路径。'),
            'impact': diagnosis.get('impact') or '这一步确认或恢复后，后续步骤才能继续。'}}

    async def report(self, case, conclusion, root='unclassified', plan=None, input_card=True,
                     fallback_diagnosis=False, analysis_errors=None, alternatives=None):
        latest = self._current_case(case)
        if latest and latest.get('events') != case.get('events'):
            if latest.get('status') == 'open' and not latest.get('pending'):
                raise CaseAnswered()
            case = latest
        if plan is not None:
            case['plan'] = copy.deepcopy(plan)
            self.store.mutate(self.run_id, lambda r: save_case(r, case), durable=True)
        from mewcode.experts.interaction_routing import derive_handling
        fields = input_definitions_for_requests(self.record(), case['node_id'], [
            item.get('name') if isinstance(item, dict) else str(item)
            for option in (plan or case.get('plan') or {}).get('options', [])
            for item in input_requests_for_option(option)])
        changes_scope = any(option.get('changes_acceptance') for option in
                            (plan or case.get('plan') or {}).get('options', []))
        handling = derive_handling(purpose='auto', root_cause=root,
            explicit_user_gate=changes_scope or root in {'authority_gap', 'business_decision'}, missing_user_fields=fields, context={})
        if handling['owner'] == 'system':
            handling.update(handling_state='blocked', reason=conclusion,
                reason_kind='technical')
        case['handling'] = self._handling_summary(case, handling)
        self.store.mutate(self.run_id, lambda r: save_case(r, case), durable=True)
        await self.pause(case, report=incident_report(
            self.record(), case, conclusion=conclusion, root_cause=root,
            input_card=input_card, fallback_diagnosis=fallback_diagnosis, analysis_errors=analysis_errors,
            alternatives=alternatives,
        ))

    async def report_limit(self, case, reason):
        boundary = RecoveryBoundary(case.get('boundary'))
        boundary.check(identity='hard_gate:' + digest([reason, len(case['rounds'])]),
                       method='resource_limit', seconds=0, hard_reason=reason)
        case['boundary'] = boundary.state
        self.store.mutate(self.run_id, lambda record: save_case(record, case), durable=True)
        await self.report(case, reason, fallback_diagnosis=True)

    def _reconcile_saved_input_discovery(self, node, case):
        """Reuse one accepted read-only Glob receipt before opening a new round."""
        from mewcode.experts.recovery_context import (
            reconcile_input_discovery_record,
            verify_input_discovery,
        )

        snapshot = self.record()
        checked = verify_input_discovery(snapshot, node.node_id, case=case,
            case_id=case.get('case_id', ''))
        if not checked.get('callback_eligible'):
            return None
        case_id = case['case_id']
        expected_case_digest = digest(case)
        def reconcile(record):
            result = reconcile_input_discovery_record(
                record, case_id, expected_case_digest=expected_case_digest,
                no_new_round=True,
            )
            if result is None:
                raise ValueError('恢复案件在重核验期间已变化')

        try:
            self.store.mutate(self.run_id, reconcile, durable=True)
        except ValueError:
            # A stale user continuation must follow the ordinary case path;
            # it must never clear a newer card or reuse a changed receipt.
            return None
        return {'action': 'continue_checkpoint', 'source': 'recovery_case', 'case_id': case_id,
            'chosen': {'next_step': '宿主已复用输入发现回执；返回原规划节点核对并提交交接。'},
            'revalidation': copy.deepcopy(checked)}

    @staticmethod
    def _input_discovery_resume_message(checked, *, no_new_round=False):
        from mewcode.experts.recovery_context import input_discovery_resume_message
        return input_discovery_resume_message(checked, no_new_round=no_new_round)

    async def run(self, *, node, result, policy, account=None, force_report_reason=''):
        snapshot = self.record()
        flow = snapshot.get('locked_flow') or {}
        if (flow.get('meta') or {}).get('node_kinds', {}).get(node.node_id) == 'gate':
            current = active_case(snapshot, node.node_id)
            if current and not current.get('closed_by_user'):
                def hand_back(record):
                    handoff_gate_recovery(record, node.node_id)
                self.store.mutate(self.run_id, hand_back, durable=True)
                await self.publish(get_case(self.record(), current['case_id']), '已转入具体门禁确认')
                return {'action': 'continue_checkpoint', 'source': 'normal_gate_handoff',
                    'chosen': {'next_step': '进入原节点的具体方案确认；尚未批准，也未完成业务验收。'}}
        usage = {'tokens': 0, 'seconds': 0.}
        async def charged(tokens, seconds):
            usage['tokens'] += tokens
            usage['seconds'] += seconds
            warning = None
            def persist(record):
                nonlocal warning
                stage = record.setdefault('node_states', {}).setdefault(node.node_id, {})
                epoch = ledger_for_run(record).view(node.node_id).epoch
                totals = stage.setdefault('recovery_case_usage', {}).setdefault(str(epoch), {'tokens': 0, 'seconds': 0.})
                totals['tokens'] += tokens
                totals['seconds'] += seconds
                current = active_case(record, node.node_id)
                if current and current.get('resource_limits'):
                    limits = current['resource_limits']
                    warned = stage.setdefault('recovery_resource_warnings', {}).setdefault(current['case_id'], [])
                    for name, limit in limits.items():
                        if limit > 0 and totals.get(name, 0) >= limit * .8 and name not in warned:
                            warned.append(name)
                            warning = {'type': 'recovery_resource_warning', 'run_id': self.run_id,
                                       'node_id': node.node_id, 'case_id': current['case_id'],
                                       'resource': name, 'used': totals.get(name, 0), 'limit': limit}
            self.store.mutate(self.run_id, persist, durable=True)
            if warning:
                await self.emit(warning)
            if account:
                await account(tokens, seconds)
        while True:
            try:
                remaining = replace(policy,
                    token_budget=None if policy.token_budget is None else max(0, policy.token_budget - usage['tokens']),
                    timeout_seconds=None if policy.timeout_seconds is None else max(0., policy.timeout_seconds - usage['seconds']))
                decision = await self._run(node=node, result=result, policy=remaining, account=charged,
                    force_report_reason=force_report_reason)
                if account is None:
                    decision.update(decision_tokens=usage['tokens'], decision_seconds=usage['seconds'])
                return decision
            except CaseAnswered:
                continue

    async def _run(self, *, node, result, policy, account=None, force_report_reason=''):
        case = None
        def opening(record):
            nonlocal case
            case = migrate_legacy(record, node.node_id) or active_case(record, node.node_id)
            if case is None:
                stage = record.get('node_states', {}).get(node.node_id, {})
                requested = (stage.get('assistance_request') or {}).get('requested_tools', [])
                case = open_case(record, node.node_id, error=result.error_kind or result.text,
                    capability='vision' if 'InspectVisualEvidence' in requested else '',
                    identity=str(stage.get('node_attempt', 0)))
            case = reconcile_wait_state(record, case['case_id']) or case
        self.store.mutate(self.run_id, opening, durable=True)
        def limits(record):
            nonlocal case
            case = get_case(record, case['case_id'])
            used = (record['node_states'][node.node_id].get('recovery_case_usage') or {}).get(str(case['epoch']), {})
            caps = {}
            if policy.token_budget is not None:
                caps['tokens'] = used.get('tokens', 0) + policy.token_budget
            if policy.timeout_seconds is not None:
                caps['seconds'] = used.get('seconds', 0) + policy.timeout_seconds
            if caps:
                case['resource_limits'] = caps
                case['resource_usage'] = copy.deepcopy(used)
                save_case(record, case)
        self.store.mutate(self.run_id, limits, durable=True)
        if self.executor is not None:
            case['analysis_model'] = copy.deepcopy(self.executor.model)
            self.store.mutate(self.run_id, lambda r: save_case(r, case), durable=True)
        if case.get('closed_by_user'):
            await self.pause(case)
        if case['status'] in {'reported', 'awaiting_user'}:
            reconciled = self._reconcile_saved_input_discovery(node, case)
            if reconciled is not None:
                current = get_case(self.record(), case['case_id']) or case
                await self.publish(current, '已核验')
                return reconciled
            await self.pause(case, report=case['report'] or None)
        elif case['status'] == 'executing':
            reconciled = self._reconcile_saved_input_discovery(node, case)
            if reconciled is not None:
                current = get_case(self.record(), case['case_id']) or case
                await self.publish(current, '已核验')
                return reconciled
        await self.publish(case, '统计问题')
        if force_report_reason:
            await self.report(case, force_report_reason)
        if not self.autonomous or self.analyst is None:
            await self.report(case, '恢复方案需要你的决定；当前模式不自主执行。')
        while True:
            await self.executor.before_resume()
            case = get_case(self.record(), case['case_id']) or case
            if case.get('closed_by_user') or case['status'] in {'reported', 'awaiting_user', 'superseded'}:
                await self.pause(case)
            if (policy.token_budget is not None and policy.token_budget < 1024) or (policy.timeout_seconds is not None and policy.timeout_seconds <= 0):
                reason = ('本轮模型额度剩余不足，未开始新的尝试。当前节点有已设置的模型额度上限，请调整该额度或停止修复。'
                          if policy.token_budget is not None and policy.token_budget < 1024 else
                          '当前节点已到运行时限，未开始新的尝试。请调整该时限或停止修复。')
                await self.report_limit(case, reason)
            snapshot = self.record()
            resuming_receipt = case['status'] == 'executing'
            if resuming_receipt:
                request_id = digest([case['case_id'], len(case['rounds']) + 1])[:32]
                receipts = snapshot['node_states'][node.node_id].get('assistance_receipts', [])
                if not any(r.get('request_id') == request_id for r in receipts):
                    await self.report(case, '上轮执行被中断且没有完整回执，请先核验已执行操作的效果。', 'unknown_effect')
            capabilities = self.capability_inventory(node) if self.capability_inventory else {}
            dossier = build_dossier(snapshot, case, capabilities)
            boundary = RecoveryBoundary(case.get('boundary'))
            dossier['recovery_boundary'] = copy.deepcopy(boundary.state)
            dossier['continuation_direction'] = case.get('continuation_direction', '')
            await self.publish(case, '主模型分析')
            errors = []
            analyst_tokens, analyst_seconds = 0, 0.
            analyst_failed = False
            for attempt in range(2):
                if attempt and ((policy.token_budget is not None and policy.token_budget < 1024) or
                        (policy.timeout_seconds is not None and policy.timeout_seconds <= 0)):
                    await self.report_limit(case, '当前已设定的分析额度已用完，未开始新的调用。')
                started_analysis = time.monotonic()
                try:
                    if case.get('plan', {}).get('round') == len(case['rounds']) + 1:
                        plan = copy.deepcopy(case['plan'])
                    else:
                        analysis_task = asyncio.create_task(self.analyst.analyze(
                            case, dossier, node=node, policy=policy, feedback='；'.join(errors)))
                        tasks = None
                        if self.session is not None and case.get('investigation_requested'):
                            tasks = getattr(self.session, '_case_investigations', None)
                            if tasks is None:
                                tasks = self.session._case_investigations = {}
                            tasks[case['case_id']] = analysis_task
                        try:
                            plan = await analysis_task
                        except asyncio.CancelledError:
                            latest = get_case(self.record(), case['case_id'])
                            if latest and latest.get('closed_by_user'):
                                raise BranchWaitPaused(node.node_id, reason='探查已取消，检查点和已有记录已保留。') from None
                            raise
                        finally:
                            if tasks is not None and tasks.get(case['case_id']) is analysis_task:
                                tasks.pop(case['case_id'], None)
                        latest = get_case(self.record(), case['case_id'])
                        if latest and latest.get('closed_by_user'):
                            raise BranchWaitPaused(node.node_id, reason='探查已取消，检查点已保留。')
                    break
                except BranchWaitPaused:
                    raise
                except Exception as exc:
                    errors.append(type(exc).__name__ + ': ' + str(exc))
                finally:
                    used = int(getattr(self.analyst, 'last_tokens', 0))
                    elapsed = max(0., time.monotonic() - started_analysis -
                        float(getattr(self.analyst, 'last_wait_seconds', 0.)))
                    analyst_tokens += used
                    analyst_seconds += elapsed
                    if account:
                        await account(used, elapsed)
                    self.analyst.last_tokens = 0
                    self.analyst.last_wait_seconds = 0.
                    policy = replace(policy,
                        token_budget=None if policy.token_budget is None else max(0, policy.token_budget - used),
                        timeout_seconds=None if policy.timeout_seconds is None else max(0., policy.timeout_seconds - elapsed))
            else:
                analyst_failed = True
                alternatives = [a for a in dossier['alternatives'] if a.get('strategy') and a.get('verification')
                    and set(a['strategy']['tools']) <= {'ReadFile', 'Glob', 'Grep', 'ProbeCapabilities'}]
                if case.get('investigation_requested') or not alternatives or (case['rounds'] and case['rounds'][-1]['verification'].get('analyst_failed')):
                    row = asdict(CaseRound(len(case['rounds']) + 1, 'analyst_failed', digest(['analyst_failed', len(case['rounds'])]),
                        'unclassified', tokens=analyst_tokens, seconds=analyst_seconds,
                        verification={'result': 'unchanged', 'analyst_failed': True, 'error_head_hash': error_head_hash('analyst_failed'),
                            'reason': '分析员未提交可核验方案', 'analysis_errors': errors,
                            'evidence_refs': ['dossier:' + case['case_id']]}))
                    case = self.update(case['case_id'], {'kind': 'finish', 'identity': 'analyst_failed:' + str(row['n']), 'round': row})
                    if case.pop('investigation_requested', None):
                        self.store.mutate(self.run_id, lambda r: save_case(r, case), durable=True)
                    reason = ('分析调用超时，结构化方案尚未交付。' if any(error.startswith('TimeoutError:') for error in errors)
                        else '未收到通过校验的结构化方案，请查看方案提交错误。')
                    await self.report(case, reason + '本次自动修复已停止，原业务节点仍未完成。',
                                      fallback_diagnosis=True, analysis_errors=errors)
                alt = alternatives[0]
                option = {'option_id': 'host_fallback', 'method': alt['method'], 'strategy': alt['strategy'],
                    'needs': {'capabilities': [], 'authority': [], 'user_input': []}, 'risk': 'low',
                    'reversible': True, 'changes_acceptance': False, 'verification': alt['verification']}
                plan = {'case_id': case['case_id'], 'round': len(case['rounds']) + 1,
                    'root_cause': {'class': 'unclassified', 'summary': '分析失败，采用宿主只读兜底',
                        'evidence_refs': ['dossier:' + case['case_id']], 'confidence': 'low'},
                    'options': [option], 'recommended_option_id': option['option_id'], 'not_recommended': []}
            case = get_case(self.record(), case['case_id']) or case
            root = plan['root_cause']['class']
            goal = (plan.get('diagnosis') or {}).get('goal')
            boundary.state['timing_reference'] = copy.deepcopy(dossier.get('timing_reference') or {})
            boundary.set_goal(goal, target='恢复「' + node.label + '」原失败步骤，然后返回原节点核验。')
            case['boundary'] = boundary.state
            self.store.mutate(self.run_id, lambda r: save_case(r, case), durable=True)
            if case.get('investigation_requested'):
                # A user chose readonly diagnosis, not execution of its result.
                previous_diagnosis = (case.get('report') or {}).get('diagnosis') or {}
                diagnosis = plan.get('diagnosis') or {}
                old = {h.get('id'): h for h in previous_diagnosis.get('hypotheses', [])}
                for hypothesis in diagnosis.get('hypotheses', []):
                    prior = old.get(hypothesis.get('id'))
                    ranks = {'low': 0, 'medium': 1, 'high': 2}
                    if hypothesis.get('delta') not in {'已排除', '已确认'}:
                        change = ranks.get(hypothesis.get('confidence'), 0) - ranks.get((prior or {}).get('confidence'), 0)
                        hypothesis['delta'] = ('新增' if prior is None else
                            '把握升高' if change > 0 else '把握降低' if change < 0 else '无变化')
                investigation_n = int(boundary.state.get('investigation_count', 0)) + 1
                boundary.state['investigation_count'] = investigation_n
                latest_blockers = [fact.fact_id for fact in ledger_for_run(self.record()).view(node.node_id).active_blockers]
                refs = host_progress([row['fact_id'] for row in dossier['blockers']], latest_blockers,
                                     operation_restored=False, probe={})
                check = boundary.check(identity='investigation:' + str(investigation_n),
                    method='readonly-investigation', seconds=analyst_seconds, evidence_refs=refs)
                boundary.state['investigation_closed'] = check['decision'] == 'converge'
                case['boundary'] = boundary.state
                case.pop('investigation_requested', None)
                case['plan'] = plan
                self.store.mutate(self.run_id, lambda r: save_case(r, case), durable=True)
                await self.emit({'type': 'recovery_boundary', 'run_id': self.run_id,
                    'node_id': node.node_id, 'case_id': case['case_id'], 'check': check})
                await self.report(case, plan['root_cause']['summary'], root, plan=plan)
            selected = case.get('selected_recovery_option') or {}
            user_selected = (selected.get('case_id') == case['case_id']
                             and selected.get('round') == len(case['rounds'])
                             and selected.get('option_id') == plan.get('recommended_option_id'))
            if root in FUNDAMENTAL and not user_selected:
                row = asdict(CaseRound(len(case['rounds']) + 1, plan['recommended_option_id'],
                    method_fingerprint(plan['options'][0]['strategy']), root, tokens=analyst_tokens, seconds=analyst_seconds,
                    verification={'result': 'not_run',
                        'evidence_refs': plan['root_cause']['evidence_refs']}))
                case = self.update(case['case_id'], {'kind': 'finish', 'identity': 'fundamental:' + str(row['n']), 'round': row})
                await self.report(case, plan['root_cause']['summary'], root, plan=plan)
            option, uncovered = select_option(plan, self.record(), case, grant_store=self.grant_store)
            if option is None:
                await self.report(case, '可用方案被拒绝或会改变验收；请一次选择所需修订或替代方式。', root, plan=plan)
            if not uncovered and not input_requests_for_option(option) and not user_selected:
                from mewcode.experts.interaction_routing import derive_handling, resolve_handling
                eligible = []
                for candidate in plan['options']:
                    trial = copy.deepcopy(case)
                    trial['selected_recovery_option'] = {'case_id': case['case_id'],
                        'round': len(case['rounds']), 'option_id': candidate['option_id']}
                    picked, gaps = select_option(plan, self.record(), trial, grant_store=self.grant_store)
                    if picked is not None and not gaps and not input_requests_for_option(picked):
                        eligible.append({'id': candidate['option_id'], 'description': '',
                            'facts': {'kind': candidate['strategy']['kind'],
                                      'reversible': candidate['reversible'],
                                      'recommended': candidate['option_id'] == plan['recommended_option_id']},
                            'eligible': True})
                decision = await resolve_handling(derive_handling(purpose='decision'), candidates=eligible,
                    selector=self.decision_selector, context={'issue': case['case_id']})
                choice_id = decision.get('selected_option_id')
                if choice_id:
                    option = next(candidate for candidate in plan['options'] if candidate['option_id'] == choice_id)
                    plan['recommended_option_id'] = choice_id
                case['handling'] = {key: value for key, value in decision.items()
                                    if key not in {'candidates', 'suggested_option'}}
            boundary_reason = boundary.assess_plan(plan, option)
            if boundary_reason:
                await self.report(case, boundary_reason, root, plan=plan)
            case['boundary'] = copy.deepcopy(boundary.state)
            self.store.mutate(self.run_id, lambda r: save_case(r, case), durable=True)
            if option['needs']['capabilities'] and self.capability_bind:
                binding = self.capability_bind(node, option)
                if binding.status == 'authority_required':
                    uncovered.extend(r for r in binding.authority_required if r not in uncovered)
                elif binding.status == 'capability_absent':
                    row = asdict(CaseRound(len(case['rounds']) + 1, option['option_id'],
                        method_fingerprint(option['strategy']), 'capability_absent',
                        tokens=analyst_tokens, seconds=analyst_seconds,
                        verification={'result': 'not_run', 'evidence_refs': ['dossier:' + case['case_id']]}))
                    case = self.update(case['case_id'], {'kind': 'finish',
                        'identity': 'binding_absent:' + str(row['n']), 'round': row})
                    await self.report(case, '没有可绑定的所需能力，请选择替代材料或配置可用路线。',
                                      'capability_absent', plan=plan, alternatives=binding.alternatives)
            if uncovered:
                elevation_option = copy.deepcopy(option)
                if not elevation_option.get('needs', {}).get('user_input'):
                    elevation_option.setdefault('needs', {})['user_input'] = [
                        item.get('name') or item.get('label')
                        for item in elevation_option['needs'].get('input_questions', [])
                        if isinstance(item, dict) and (item.get('name') or item.get('label'))
                    ]
                elevation_plan = copy.deepcopy(plan)
                for candidate in elevation_plan.get('options', []):
                    if isinstance(candidate, dict) and candidate.get('option_id') == option.get('option_id'):
                        candidate.update(copy.deepcopy(elevation_option))
                        break
                await self.pause(case, pending=elevation_card(
                    self.record(), case, elevation_plan, elevation_option, uncovered))
            input_requests = input_requests_for_option(option)
            if input_requests:
                request_names = [
                    item.get('label') or item.get('name', '') if isinstance(item, dict) else str(item)
                    for item in input_requests
                ]
                question = '；'.join(name for name in request_names if name)
                if any(row.get('case_id') == case['case_id'] and row.get('question') == question
                       for row in (self.record().get('case_inputs') or {}).values()):
                    await self.report(case, '已保存你的补充，但当前阶段仍缺少具体输入；请查看本卡列出的字段，不能重复提交相同材料。', 'input_defect', plan=plan, input_card=False)
                request_fields = [item.get('name') if isinstance(item, dict) else item
                                  for item in input_requests]
                input_definitions = input_definitions_for_requests(
                    self.record(), node.node_id, request_fields
                )
                deferred_inputs = build_dossier(
                    self.record(), case
                ).get('deferred_input_questions', [])
                if not input_definitions:
                    future = '；'.join(
                        f"{item.get('label') or item.get('name')}→{item.get('collect_at_label') or item.get('collect_at', '后续节点')}"
                        for item in deferred_inputs
                    )
                    await self.report(
                        case,
                        ('当前节点没有需要立即提交的输入；缺失参数将在对应节点收集：' + future
                         if future else
                         '当前恢复方案没有可提交的当前阶段字段；请重新准备具体候选。'),
                        'input_defect', plan=plan, input_card=False,
                    )
                await self.pause(case, pending={'kind': 'input', 'round': len(case['rounds']), 'plan': plan,
                    'option_id': option['option_id'], 'question': question,
                    'questions': copy.deepcopy(input_definitions),
                    'input_definitions': copy.deepcopy(input_definitions),
                    'input_questions': copy.deepcopy(option['needs'].get('input_questions') or []),
                    'deferred_inputs': copy.deepcopy(deferred_inputs),
                    'options': [
                        {'value': 'input', 'label': '提交补充',
                         'description': '只提交本卡列出的当前阶段字段。'},
                        {'value': 'close', 'label': '停止本案件',
                         'description': '保留检查点；当前节点仍未完成。'},
                    ]})
            if (policy.token_budget is not None and policy.token_budget < 1024) or (policy.timeout_seconds is not None and policy.timeout_seconds <= 0):
                await self.report_limit(case, '分析后已设定的剩余额度不足，未启动执行。')
            plan['recommended_option_id'] = option['option_id']
            if not resuming_receipt:
                case = self.update(case['case_id'], {'kind': 'start', 'identity': 'start:' + str(len(case['rounds']) + 1), 'plan': plan})
            await self.publish(case, '执行')
            from mewcode.experts.assistance import AssistanceRequest, RunContext
            context_notes = [v.get('context_note') or v.get('note') for v in (self.record().get('case_inputs') or {}).values()
                             if v.get('case_id') == case['case_id'] and (v.get('context_note') or v.get('note'))]
            hypothesis = next((h for h in (plan.get('diagnosis') or {}).get('hypotheses', [])
                               if h.get('option_id') == option['option_id']), {})
            request = AssistanceRequest(digest([case['case_id'], len(case['rounds']) + 1])[:32], node.node_id,
                case['epoch'], 'diagnosis', '只执行已选方案：' + str(option) + '\n目标与安全点：' + str(boundary.state.get('goal'))
                    + '\n选择原因：' + str(hypothesis) + '\n用户补充（不新增授权）：' + '\n'.join(context_notes), tuple(plan['root_cause']['evidence_refs']),
                tuple(option['strategy']['tools']), 'controller')
            started = time.monotonic()
            observed_before = milestone_files(boundary.state.get('goal'), getattr(self.executor, 'work_dir', None))
            milestone_before = observed_before
            def preserve_milestone_baseline(record):
                nonlocal milestone_before
                current_case = get_case(record, case['case_id'])
                baselines = current_case.setdefault('milestone_baselines', {})
                milestone_before = copy.deepcopy(baselines.setdefault(request.request_id, observed_before))
                save_case(record, current_case)
            self.store.mutate(self.run_id, preserve_milestone_baseline, durable=True)
            execution_policy = policy
            if analyst_failed or option.get('strategy', {}).get('kind') == 'investigate':
                execution_policy = replace(policy, parallel_readonly=True)
            receipt = await self.executor.run(request, RunContext(node, execution_policy, result))
            if ledger_for_run(self.record()).view(node.node_id).epoch != case['epoch']:
                # A confirmed repair changes generation; the old case is superseded,
                # never resolved by an observation of a different graph.
                return {'action': 'diagnose_path', 'source': 'recovery_case', 'case_id': case['case_id'],
                    'chosen': {'next_step': '节点代际已修订，请从新检查点重新核验。'}}
            tokens = (0 if resuming_receipt else self.record().get('node_states', {}).get(node.node_id, {}).get('assistance_usage', {}).get(request.request_id, 0))
            helper_stage = self.record().get('node_states', {}).get(node.node_id + '__assist_' + request.request_id[:12], {})
            execution_seconds = max(0., time.monotonic() - started - float(helper_stage.get('wait_seconds', 0)))
            if account:
                await account(tokens, execution_seconds)
            policy = replace(policy,
                token_budget=None if policy.token_budget is None else max(0, policy.token_budget - tokens),
                timeout_seconds=None if policy.timeout_seconds is None else max(0., policy.timeout_seconds - execution_seconds))
            await self.publish(case, '核验')
            started_probe = time.monotonic()
            try:
                probe = await self.probe(node, option) if self.probe else {'passed': False, 'reason': '没有宿主可执行的核验探针'}
            except BranchWaitPaused:
                raise
            except Exception as exc:
                probe = {'passed': False, 'error': type(exc).__name__ + ': ' + str(exc)}
            probe_seconds = max(0., time.monotonic() - started_probe - float(probe.get('wait_seconds', 0)))
            probe_tokens = int(probe.get('billable_tokens', 0))
            if account:
                await account(probe_tokens, probe_seconds)
            policy = replace(policy,
                token_budget=None if policy.token_budget is None else max(0, policy.token_budget - probe_tokens),
                timeout_seconds=None if policy.timeout_seconds is None else max(0., policy.timeout_seconds - probe_seconds))
            remaining = []
            def verified(record):
                nonlocal remaining
                refresh_clearances(record, node.node_id)
                remaining = list(ledger_for_run(record).view(node.node_id).active_blockers)
            self.store.mutate(self.run_id, verified, durable=True)
            checkpoint_proof = probe.get('checkpoint') or {}
            host_callback = False
            input_discovery_checked = None
            selected_strategy = option.get('strategy') or {}
            if selected_strategy.get('kind') == 'input_discovery':
                from mewcode.experts.recovery_context import reuse_input_discovery
                input_discovery_checked = reuse_input_discovery(
                    self.record(), node.node_id, case=case, case_id=case['case_id'], option=option)
            helper_handoff = helper_stage.get('handoff') or {}
            from mewcode.experts.recovery_context import tool_effect_pending
            helper_safe = (helper_stage.get('status') in {'completed', 'needs_attention'}
                and not helper_handoff.get('question') and (not helper_handoff.get('blockers') or
                    helper_handoff.get('blocker_kind') == 'capability_missing')
                and not helper_stage.get('pending_permission') and not helper_stage.get('pending_interaction')
                and not any(tool_effect_pending(tool)
                    for tool in (helper_stage.get('tools') or {}).values()))
            if checkpoint_proof.get('callback_eligible') and helper_safe and not receipt.question and not receipt.authority_needed:
                from mewcode.experts.recovery_context import verify_checkpoint
                checked = verify_checkpoint(self.record(), node.node_id, checkpoint_proof.get('evidence_tool_id', ''),
                    case_id=case['case_id'], epoch=case['epoch'], input_revision=case['input_revision'])
                host_callback = (checked['callback_eligible'] and checked['evidence_digest'] == checkpoint_proof.get('evidence_digest'))
            if selected_strategy.get('kind') == 'input_discovery':
                # A generic probe result cannot restore this plan.  Require a
                # second host proof bound to the current case/receipt; this
                # also keeps the saved path list available to the planner.
                operation_restored = (probe.get('reused_input_discovery') is True
                                      and input_discovery_checked is not None)
            else:
                operation_restored = probe.get('operation_restored') is True
            success = (host_callback or operation_restored) and not remaining and probe.get('passed') is True
            success = success and not receipt.question and not receipt.authority_needed
            success = success and not helper_stage.get('pending_permission') and not helper_stage.get('pending_interaction')
            success = success and not helper_handoff.get('question') and (not helper_handoff.get('blockers') or
                helper_handoff.get('blocker_kind') == 'capability_missing')
            success = success and not any(tool_effect_pending(tool)
                for tool in (helper_stage.get('tools') or {}).values())
            head = error_head_hash(probe.get('error') or result.error_kind or result.text)
            remaining_ids = sorted(b.fact_id for b in remaining)
            previous = case['rounds'][-1] if case['rounds'] else None
            proof_digest = digest({'passed': probe.get('passed'), 'error': probe.get('error'),
                'proof': probe.get('evidence_digest'), 'remaining_blockers': remaining_ids})
            prior_blockers = previous['verification'].get('remaining_blockers', []) if previous else [b['fact_id'] for b in dossier['blockers']]
            progress_refs = host_progress(prior_blockers, remaining_ids,
                operation_restored=operation_restored or host_callback, probe=probe)
            if helper_safe:
                milestone_after = milestone_files(boundary.state.get('goal'), getattr(self.executor, 'work_dir', None))
                progress_refs.extend(milestone_progress(milestone_before, milestone_after))
            outcome = 'resolved' if success else 'worse' if set(remaining_ids) - set(prior_blockers) else 'unchanged'
            verification = {'result': outcome, 'progress_evidence': progress_refs,
                'error_head_hash': head, 'evidence_refs': probe.get('evidence_refs', []), 'probe': probe,
                'analyst_failed': analyst_failed, 'remaining_blockers': remaining_ids,
                'callback_source': ('host_input_discovery' if operation_restored else
                    ('host_command_checkpoint' if checkpoint_proof.get('evidence_kind') == 'command'
                     else 'host_pixel_checkpoint') if host_callback
                    else 'host_original_step' if success else 'unrestored'),
                'business_complete': False}
            row = asdict(CaseRound(len(case['rounds']) + 1, option['option_id'], method_fingerprint(option['strategy']), root,
                authority='covered' if option['needs']['authority'] else 'none', executor_receipt_ref=request.request_id,
                verification=verification, evidence_digest=proof_digest, tokens=tokens + analyst_tokens + probe_tokens,
                seconds=execution_seconds + probe_seconds + analyst_seconds))
            row['method'] = option['method']
            row['authority_needed'] = list(receipt.authority_needed)
            milestones = (boundary.state.get('goal') or {}).get('milestones') or []
            expected = next((m.get('expected_seconds') for m in milestones if m.get('expected_seconds')), None)
            check = boundary.check(identity='finish:' + str(row['n']), method=row['method_fingerprint'],
                seconds=row['seconds'], evidence_refs=progress_refs, restored=success, expected_seconds=expected)
            case = self.update(case['case_id'], {'kind': 'finish', 'identity': 'finish:' + str(row['n']),
                'round': row, 'boundary': boundary.state})
            await self.emit({'type': 'recovery_boundary', 'run_id': self.run_id, 'node_id': node.node_id,
                             'case_id': case['case_id'], 'check': check})
            quota = probe.get('authorization_accounting')
            if not quota:
                quota = next((((tool.get('host_observation') or {}).get('review_evidence') or {}).get('authorization_accounting')
                    for tool in reversed(list((helper_stage.get('tools') or {}).values()))
                    if tool.get('is_error') and ((tool.get('host_observation') or {}).get('review_evidence') or {}).get('authorization_accounting')), None)
            if not success and quota and quota.get('request'):
                next_plan = copy.deepcopy(plan)
                next_plan['round'] = len(case['rounds']) + 1
                selected = next(o for o in next_plan['options'] if o['option_id'] == option['option_id'])
                selected['needs'].update(authority=[quota['request']], user_input=[])
                pending = elevation_card(self.record(), case, next_plan, selected, [quota['request']])
                import ntpath
                materials = '；'.join(f"{ntpath.basename(source['file_path'])} 第{source.get('page', 1)}页"
                    for source in quota.get('sources', [])) or '下方列出的图片范围'
                route = '、'.join(quota['request']['scope'].get('model_routes', []))
                pending['question'] = (f"本次看图余额 {quota['remaining_tokens']} token 不够。"
                    f"已知用量 {quota['known_used_tokens']}，保守估算用量 {quota['estimated_used_tokens']}。"
                    f"材料：{materials}；接收模型：{route}。"
                    f"是否仅为所列范围另批 {quota['required_tokens']} token？已有成果保留。")
                pending['alternative'] = None
                pending['options'] = [o for o in pending['options'] if o['value'] != 'alternative']
                await self.pause(case, pending=pending)
            if success:
                def resume(record):
                    stage = record['node_states'][node.node_id]
                    message = '恢复通路已由宿主核验，从原检查点继续；业务验收仍由原节点完成。\n'
                    if input_discovery_checked is not None:
                        message = self._input_discovery_resume_message(input_discovery_checked)
                    elif host_callback:
                        if checkpoint_proof.get('evidence_kind') == 'command':
                            message += ('原失败命令已有宿主确认的成功执行、完整输出和已清理事务。'
                                '先读 ReadRecoveryContext(section="commands") 复用该回执和输出，禁止重跑已经成功的同一命令。'
                                '继续原节点业务核验，不能仅凭恢复回执标记任务完成。\n' + str(checkpoint_proof) + '\n')
                        else:
                            message += ('已有像素观察无需重复发送，可用 ReadRecoveryContext 读取原回执。'
                            '历史档位、用量等缺项保持未知，不以整节点消耗代算；新操作继续核对授权和费用。\n'
                            + str(checkpoint_proof) + '\n')
                    stage.update(status='running', assistance_resume_message=message + authorization_block(record, node))
                    stage.pop('pending_recovery_case', None)
                    ledger_for_run(record).set_phase(node.node_id, 'running', reason='case_verified')
                self.store.mutate(self.run_id, resume, durable=True)
                await self.publish(case, '已解决')
                return {'action': 'continue_checkpoint', 'source': 'recovery_case', 'case_id': case['case_id'],
                    'chosen': {'next_step': '宿主核验通过，从原检查点继续。'}}
            verdict = judge(case, row)
            if check['decision'] == 'converge':
                await self.report(case, check['reason'], 'repeat_without_progress', plan=plan)
            if verdict.action in {'report', 'user'}:
                await self.report(case, verdict.reason, verdict.root_cause)


def _typed_input_answer(record, case, pending, answer):
    """Validate and apply the flat typed input protocol for one case card.

    The caller has already checked the case identity, round and choice.  This
    helper performs every contract check before mutating ``record`` so a bad
    or partial answer cannot clear a card or bind an unvalidated value.
    """
    definitions = [item for item in (pending.get('input_definitions') or pending.get('questions') or [])
                   if isinstance(item, dict) and isinstance(item.get('name'), str) and item.get('name')]
    if not definitions or all(item.get('legacy') is True for item in definitions):
        return None
    try:
        payload = json.loads(answer)
    except (TypeError, ValueError) as exc:
        raise ValueError('请按字段提交 JSON 对象') from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError('请填写至少一个当前阶段字段')
    note = payload.pop('_note', None)
    if note is not None and (not isinstance(note, str) or len(note) > 8000):
        raise ValueError('_note 必须是 8000 字以内的文字')
    if not payload:
        raise ValueError('请填写至少一个当前阶段字段')

    flow = record.get('locked_flow') or {}
    declarations = (flow.get('meta') or {}).get('run_input_contract') or []
    node_ids = {
        row.get('node_id') for row in (flow.get('nodes') or [])
        if isinstance(row, dict) and row.get('node_id')
    }
    branch_inputs = {
        (edge.get('input_when') or {}).get('input')
        for edge in (flow.get('edges') or [])
        if isinstance(edge, dict) and edge.get('from_node') == case['node_id']
        and isinstance(edge.get('input_when'), dict)
        and (edge.get('input_when') or {}).get('input')
    }
    from mewcode.orchestration.run_inputs import (
        _is_empty, _normalize_value, stage_input_questions, validate_input_contract,
    )
    try:
        defined = validate_input_contract(declarations, node_ids=node_ids or None)
        _, normalized = stage_input_questions(
            declarations, record.get('run_inputs') or {}, case['node_id'],
            branch_inputs=branch_inputs,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError('当前运行参数合同无效，不能接收这张卡片的回答') from exc
    displayed = {item['name'] for item in definitions}
    unknown = set(payload) - displayed
    if unknown:
        raise ValueError('回答包含本卡未显示的字段：' + '、'.join(sorted(unknown)))
    values = copy.deepcopy(record.get('run_inputs') or {})
    accepted = {}
    for name, raw in payload.items():
        item = defined.get(name)
        if item is None:
            raise ValueError(f'运行参数「{name}」未在当前合同中声明')
        if item.get('collect_at', 'start') not in {'start', case['node_id']}:
            raise ValueError(f'运行参数「{name}」尚未到当前收集阶段')
        try:
            value = _normalize_value(raw, item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f'运行参数「{item.get("label") or name}」的值无效') from exc
        options = item.get('options') or []
        if value and options and value not in {option.get('value') for option in options}:
            raise ValueError(f'运行参数「{item.get("label") or name}」请选择已提供的方案')
        if _is_empty(value):
            continue
        values[name] = value
        normalized[name] = value
        accepted[name] = copy.deepcopy(value)
    if not accepted:
        raise ValueError('请填写至少一个当前阶段字段')
    try:
        missing, normalized_after = stage_input_questions(
            declarations, normalized, case['node_id'], branch_inputs=branch_inputs,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError('回答未通过当前运行参数合同校验') from exc
    # Rebuild the card from every field that is currently required.  A value
    # submitted on this card can activate a conditional requirement that was
    # not displayed on the original card.  ``stage_input_questions`` already
    # excludes declarations collected at a future node, so this does not pull
    # future inputs into the current card.
    remaining = [copy.deepcopy(item) for item in missing if item.get('name')]
    return {
        'values': values,
        'normalized': normalized_after,
        'accepted': accepted,
        'note': note,
        'remaining': remaining,
    }


def _trim_input_choice_scope(pending, remaining):
    names = {item.get('name') for item in remaining}
    pending['source_questions'] = copy.deepcopy(remaining)
    bundle = _choice_bundle_for(pending)
    if bundle is not None:
        bundle['questions'] = [item for item in bundle.get('questions', []) if item.get('name') in names]
        bundle['id'] = digest([bundle.get('id'), remaining])
        bundle['version'] = digest([bundle.get('version'), remaining])
        from pathlib import Path
        from mewcode.orchestration.interaction_choices import _source_fingerprint
        if 'source_fingerprint' in bundle:
            bundle['source_fingerprint'] = _source_fingerprint(bundle['questions'], [Path(root) for root in bundle.get('allowed_roots', [])])


def _choice_bundle_for(pending):
    for key in ('choice_bundle', 'prepared_choice_bundle', 'bundle'):
        value = pending.get(key) if isinstance(pending, dict) else None
        if isinstance(value, dict):
            return value
    return None


def _resolve_pending_choices(pending, choice, *, bundle_id='', version='', choice_inputs=None,
                             fallback_input=''):
    """Resolve a prepared choice card, if this pending card has one.

    Legacy cards have no bundle and return ``None`` so their scalar protocol
    remains unchanged.  The returned object is the shared resolver's typed
    ``values``/``actions`` pair; actions are deliberately kept separate from
    values and never become run inputs or authorizations here.
    """

    bundle = _choice_bundle_for(pending)
    if bundle is None:
        if bundle_id or version or (choice_inputs not in (None, {})):
            raise ValueError('案件没有可用的选项包')
        return None
    from mewcode.orchestration.interaction_choices import resolve_answers

    # These controls belong to the legacy card protocol.  A prepared bundle
    # may also expose a manual/Other route; do not reinterpret ``close`` or
    # ``input`` as a custom value just because that route is present.  Still
    # validate a supplied bundle id/version at the resolver boundary so a
    # caller cannot mix an old control with a stale prepared card.
    if choice in {'close', 'input', 'approve', 'alternative', 'retry'}:
        resolve_answers(bundle, {}, bundle_id=bundle_id, version=version,
                        choice_inputs=choice_inputs)
        return None

    questions = [item for item in bundle.get('questions', [])
                 if isinstance(item, dict) and isinstance(item.get('name'), str)]
    names = [item['name'] for item in questions]
    choice_ids = {
        choice.get('id')
        for question in questions
        for choice in question.get('choices', [])
        if isinstance(choice, dict) and choice.get('id')
    }
    raw = choice
    if isinstance(choice, str):
        stripped = choice.strip()
        if stripped[:1] in {'{', '['}:
            try:
                raw = json.loads(stripped)
            except (TypeError, ValueError):
                raw = choice
    if isinstance(raw, dict) and any(name in raw for name in names):
        answers = raw
    elif isinstance(raw, list) and len(names) == 1:
        answers = {names[0]: raw}
    elif len(names) == 1 and (
            (isinstance(raw, (str, int, float, bool)) and raw in choice_ids)
            or bundle_id or version or choice_inputs not in (None, {})
            or any(isinstance(item, dict) and item.get('kind') == 'action'
                   and item.get('action') == 'manual'
                   for item in questions[0].get('choices', []))):
        answers = {names[0]: raw}
    else:
        return None
    resolver_inputs = choice_inputs
    if resolver_inputs is None and isinstance(raw, str) and isinstance(fallback_input, str) and fallback_input:
        # The legacy answer field is also accepted as the action's input when
        # a caller has not adopted the explicit choice_inputs object yet.
        resolver_inputs = {raw: fallback_input}
    return resolve_answers(bundle, answers, bundle_id=bundle_id, version=version,
                           choice_inputs=resolver_inputs)


def _plan_option_from_values(case, pending, values):
    """Find a RecoveryOption id in resolved values without trusting labels."""

    if pending.get('kind') == 'input' and pending.get('input_definitions'):
        return None
    plan = pending.get('plan') if isinstance(pending, dict) else None
    plan = plan if isinstance(plan, dict) and plan.get('options') else case.get('plan') or {}
    option_ids = {item.get('option_id') for item in plan.get('options', [])
                  if isinstance(item, dict) and item.get('option_id')}
    flattened = []
    for value in (values or {}).values():
        flattened.extend(value if isinstance(value, list) else [value])
    return next((value for value in flattened if isinstance(value, str) and value in option_ids), None)


def _record_context_note(record, case, *, identity, pending, text, note, extra=None):
    if not note and not extra:
        return
    row = {'case_id': case['case_id'], 'text': text,
           'question': pending.get('question', '')}
    if note:
        row['context_note'] = note
    if extra:
        row.update(copy.deepcopy(extra))
    record.setdefault('case_inputs', {})[identity] = row


def _save_choice_action(record, case, pending, *, identity, action, action_input,
                        values, bundle, note):
    """Persist a non-executing user action for the primary dispatcher."""

    if action == 'manual':
        if not isinstance(action_input, str) or not action_input.strip() or len(action_input) > 8000:
            raise ValueError('请说明你的判断或处理方向')
        case['investigation_requested'] = {'instruction': action_input.strip(), 'source_ref': identity}
        _record_context_note(record, case, identity=identity, pending=pending, text=action_input,
                             note=note, extra={'kind': 'user_direction'})
        case.setdefault('events', []).append(identity)
        case.update(status='open', pending={}, plan={})
        case.pop('pending_choice_action', None)
        save_case(record, case)
        stage = record.setdefault('node_states', {}).setdefault(case['node_id'], {})
        stage.pop('pending_recovery_case', None)
        stage.pop('pending_choice_action', None)
        stage['status'] = 'pending'
        return case

    case['pending_choice_action'] = {
        'action': action,
        'input': copy.deepcopy(action_input),
        'values': copy.deepcopy(values),
        'case_id': case['case_id'],
        'round': len(case['rounds']),
        'bundle_id': bundle.get('id') if isinstance(bundle, dict) else None,
        'version': bundle.get('version') if isinstance(bundle, dict) else None,
    }
    case.setdefault('events', []).append(identity)
    _record_context_note(record, case, identity=identity, pending=pending,
                         text=action, note=note,
                         extra={'kind': 'choice_action', 'action': action,
                                'action_input': copy.deepcopy(action_input)})
    # Keep the card and checkpoint visible until the primary dispatcher has
    # performed the requested maintenance/research/replanning action.  In
    # particular, defer is a wait, not a user stop.
    case['status'] = 'awaiting_user'
    case['pending'] = copy.deepcopy(pending)
    case.pop('closed_by_user', None)
    save_case(record, case)
    stage = record.setdefault('node_states', {}).setdefault(case['node_id'], {})
    stage.update(status='paused', pending_recovery_case={
        'case_id': case['case_id'], 'round': len(case['rounds'])})
    stage['pending_choice_action'] = copy.deepcopy(case['pending_choice_action'])
    return case


def _save_investigation_request(record, case, pending, *, identity, instruction, note):
    boundary = case.get('boundary') or case.get('recovery_boundary') or {}
    if boundary.get('investigation_closed'):
        raise ValueError('只读探查已收束；请选择可执行方案或补充新的处理方向')
    instruction = instruction.strip() if isinstance(instruction, str) else ''
    case['investigation_requested'] = {
        'instruction': instruction or '核对原报错、已有结果与缺失条件',
        'source_ref': identity,
    }
    case.setdefault('events', []).append(identity)
    case.update(status='open', pending={}, plan={})
    case.pop('selected_recovery_option', None)
    case.pop('pending_choice_action', None)
    _record_context_note(record, case, identity=identity, pending=pending,
                         text='只读探查', note=note)
    save_case(record, case)
    stage = record.setdefault('node_states', {}).setdefault(case['node_id'], {})
    stage.pop('pending_recovery_case', None)
    stage.pop('pending_choice_action', None)
    stage['status'] = 'pending'
    return case

def answer_case(record, case_id, *, round, choice, remember_scope='run', answer='', note='',
                bundle_id='', version='', choice_inputs=None,
                choice_bundle_id='', choice_bundle_version='', bundleid='', bundle_version=''):
    """Called only from authenticated user endpoints / confirmed maintenance actions."""
    if not isinstance(note, str) or len(note) > 2000 or "\x00" in note:
        raise ValueError('备注必须是 2000 字以内的文字')
    bundle_id = bundle_id or choice_bundle_id or bundleid
    version = version or choice_bundle_version or bundle_version
    case = get_case(record, case_id)
    if case is None:
        raise ValueError('案件没有待回答卡片')
    active_investigation = (case.get('status') == 'open'
        and isinstance(case.get('investigation_requested'), dict))
    closed_investigation_replay = (case.get('status') == 'reported'
        and case.get('closed_by_user') is True
        and isinstance(case.get('investigation_requested'), dict)
        and not case.get('pending'))
    active_investigation_close = active_investigation or closed_investigation_replay
    system_blocked_close = (case.get('status') == 'reported' and not case.get('pending')
        and (case.get('handling') or {}).get('owner') == 'system'
        and (case.get('handling') or {}).get('handling_state') == 'blocked')
    if active_investigation_close or system_blocked_close:
        if choice != 'close':
            raise ValueError('系统事项没有待答选项；这里只允许停止本次修复' if system_blocked_close
                             else '只读探查期间只允许取消并停止本案件')
        pending = case.get('pending') or {}
    elif case['status'] not in {'awaiting_user', 'reported'} or not case.get('pending'):
        raise ValueError('案件没有待回答卡片')
    else:
        pending = case['pending']
    resolved = _resolve_pending_choices(
        pending, choice, bundle_id=bundle_id, version=version,
        choice_inputs=choice_inputs, fallback_input=answer,
    )
    if choice == 'input' and _choice_bundle_for(pending):
        try:
            payload = json.loads(answer)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            from mewcode.orchestration.interaction_choices import resolve_answers
            input_note = payload.pop('_note', None)
            resolved_input = resolve_answers(_choice_bundle_for(pending), payload,
                bundle_id=bundle_id, version=version, choice_inputs=choice_inputs)
            if resolved_input['actions']:
                resolved = resolved_input
            else:
                answer = json.dumps({**resolved_input['values'],
                    **({'_note': input_note} if input_note is not None else {})}, ensure_ascii=False)
    legacy_values = [o['value'] for o in pending.get('options', [])
                     if isinstance(o, dict) and isinstance(o.get('value'), (str, int, float, bool))]
    legacy_values.extend(o['value'] for o in pending.get('recovery_options', [])
                         if isinstance(o, dict) and isinstance(o.get('value'), (str, int, float, bool)))
    legacy_values.extend(o['value'] for o in pending.get('exits', [])
                         if isinstance(o, dict) and o.get('value') in {'close', 'defer'})
    # Deferring is always safe for an actual waiting card, including legacy
    # close-only cards whose model preparation has not returned a bundle yet.
    legacy_values.append('defer')
    if active_investigation_close or system_blocked_close:
        legacy_values.append('close')
    if isinstance((case.get('report') or {}).get('diagnosis'), dict) and not _choice_bundle_for(pending):
        legacy_values.extend(['investigate', 'manual'])
    legacy_match = isinstance(choice, (str, int, float, bool)) and choice in legacy_values
    if round != len(case['rounds']) or (not legacy_match and resolved is None):
        raise ValueError('案件轮次或选项不匹配')
    if remember_scope not in {'run', 'once'}:
        raise ValueError('授权记忆范围无效')
    stage = record.get('node_states', {}).get(case['node_id'], {})
    if ledger_for_run(record).view(case['node_id']).epoch != case['epoch'] or stage.get('input_revision', '') != case['input_revision']:
        raise ValueError('案件输入或代际已变化')
    if choice == 'alternative' and pending.get('alternative') is None:
        raise ValueError('本卡片没有无需提权的备选方案')
    if choice == 'input' and (not answer.strip() or len(answer) > 8000):
        raise ValueError('请填写具体补充，最多 8000 字')
    if choice == 'input':
        try:
            parsed = json.loads(answer)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)) and not parsed:
            raise ValueError('请填写具体补充，不能提交空对象')
    for request in pending.get('authority', []):
        valid_request(request)
    identity = 'answer:' + digest({'case_id': case_id, 'round': round, 'choice': choice,
                                   'bundle_id': bundle_id, 'version': version,
                                   'choice_inputs': choice_inputs, 'pending': pending})[:48]
    if identity in case.get('events', []):
        return case

    plan_for_choice = pending.get('plan') if isinstance(pending.get('plan'), dict) else case.get('plan')
    plan_for_choice = plan_for_choice if isinstance(plan_for_choice, dict) else {}
    selected_plan_option = next((item for item in plan_for_choice.get('options', [])
        if isinstance(item, dict) and item.get('option_id') == choice), None)
    projected_investigator = any(
        isinstance(item, dict) and str(item.get('value', item.get('option_id', ''))) == str(choice)
        and item.get('action') == 'investigate'
        for source in (pending.get('options') or [], pending.get('recovery_options') or [])
        for item in source
    )
    saved_investigator = (selected_plan_option is not None
        and (selected_plan_option.get('strategy') or {}).get('kind') == 'investigate')
    investigation_action = next((item for item in (resolved or {}).get('actions', [])
        if isinstance(item, dict) and item.get('action') == 'investigate'), None)
    synthetic_investigator = choice == 'investigate' and (
        selected_plan_option is None or saved_investigator)
    if synthetic_investigator or projected_investigator or saved_investigator or investigation_action:
        instruction = ((investigation_action or {}).get('input') if investigation_action else answer)
        return _save_investigation_request(record, case, pending, identity=identity,
                                           instruction=instruction or note, note=note)

    # Prepared action choices are routed to the primary dispatcher.  They do
    # not grant authority, bind values, spend a recovery round, or complete a
    # node.  Keep the card visible until that dispatcher has performed the
    # requested research, re-preparation, maintenance, or manual path.
    if resolved is not None:
        actions = resolved.get('actions') or []
        values = resolved.get('values') or {}
        if actions:
            if len(actions) != 1 or values:
                raise ValueError('一次只能选择一个动作；动作不能同时绑定运行参数')
            action = actions[0]
            return _save_choice_action(
                record, case, pending, identity=identity,
                action=action.get('action'), action_input=action.get('input'),
                values={}, bundle=_choice_bundle_for(pending), note=note,
            )
        selected_option = _plan_option_from_values(case, pending, values)
        if selected_option is not None:
            plan = pending.get('plan') if isinstance(pending.get('plan'), dict) else case.get('plan')
            plan = copy.deepcopy(plan or {})
            plan['recommended_option_id'] = selected_option
            plan['round'] = len(case['rounds']) + 1
            case['plan'] = plan
            case['selected_recovery_option'] = {
                'option_id': selected_option, 'case_id': case_id, 'round': round,
            }
            if pending.get('kind') == 'business_decision':
                selected = next((item for item in pending.get('options', [])
                                 if isinstance(item, dict) and item.get('value') == selected_option), {})
                case['selected_business_option'] = {
                    'value': selected_option, 'label': selected.get('label', selected_option),
                    'case_id': case_id, 'round': round,
                }
            _record_context_note(record, case, identity=identity, pending=pending,
                                 text=selected_option, note=note,
                                 extra={'kind': 'recovery_option', 'option_id': selected_option})
            case.setdefault('events', []).append(identity)
            case.pop('pending_choice_action', None)
            case['status'] = 'open'
            case['pending'] = {}
            case.pop('closed_by_user', None)
            save_case(record, case)
            stage = record.setdefault('node_states', {}).setdefault(case['node_id'], {})
            stage.pop('pending_recovery_case', None)
            stage.pop('pending_choice_action', None)
            stage['status'] = 'pending'
            return case
        if values and pending.get('kind') != 'input' and set(values) == {'recovery_action'}:
            return _save_choice_action(record, case, pending, identity=identity,
                action='revise', action_input=values['recovery_action'], values={},
                bundle=_choice_bundle_for(pending), note=note)
        if values:
            # A prepared input bundle can contribute only values that are
            # present in its displayed contract.  Reuse the existing host
            # typed validator before binding any run input.
            if pending.get('input_definitions') or pending.get('questions'):
                typed_answer = json.dumps(values, ensure_ascii=False)
                typed = _typed_input_answer(record, case, pending, typed_answer)
                record['run_inputs'] = copy.deepcopy(typed['values'])
                _record_context_note(record, case, identity=identity, pending=pending,
                                     text=typed_answer, note=note,
                                     extra={'values': copy.deepcopy(typed['accepted'])})
                case.setdefault('events', []).append(identity)
                if typed['remaining']:
                    remaining = copy.deepcopy(typed['remaining'])
                    case['pending']['input_definitions'] = remaining
                    case['pending']['questions'] = copy.deepcopy(remaining)
                    _trim_input_choice_scope(case['pending'], remaining)
                    case['pending']['question'] = '仍需补充：' + '、'.join(
                        item.get('label') or item.get('name', '') for item in remaining)
                    case['status'] = 'awaiting_user'
                    save_case(record, case)
                    stage = record.setdefault('node_states', {}).setdefault(case['node_id'], {})
                    stage.update(status='paused', pending_recovery_case={
                        'case_id': case['case_id'], 'round': len(case['rounds'])})
                    return case
                case.update(status='open', pending={}, plan={})
                case.pop('pending_choice_action', None)
                save_case(record, case)
                stage = record.setdefault('node_states', {}).setdefault(case['node_id'], {})
                stage.pop('pending_recovery_case', None)
                stage.pop('pending_choice_action', None)
                stage['status'] = 'pending'
                return case
        raise ValueError('选项包没有可执行的值或动作')
    typed = _typed_input_answer(record, case, pending, answer) if choice == 'input' else None
    if typed is not None:
        # No authorization is inferred from a typed input answer.  ``_note``
        # is retained as evidence/context only; the contract values are the
        # sole fields bound to the run input snapshot.
        record['run_inputs'] = copy.deepcopy(typed['values'])
        record.setdefault('case_inputs', {})[identity] = {
            'case_id': case_id, 'text': answer,
            'question': pending.get('question', ''),
            'values': copy.deepcopy(typed['accepted']),
            **({'note': typed['note']} if typed['note'] is not None else {}),
            **({'context_note': note} if note else {}),
        }
        case.setdefault('events', []).append(identity)
        if typed['remaining']:
            remaining = copy.deepcopy(typed['remaining'])
            case['pending']['input_definitions'] = remaining
            case['pending']['questions'] = copy.deepcopy(remaining)
            _trim_input_choice_scope(case['pending'], remaining)
            case['pending']['question'] = '仍需补充：' + '、'.join(
                item.get('label') or item.get('name', '') for item in remaining)
            case['status'] = 'awaiting_user'
            save_case(record, case)
            stage = record.setdefault('node_states', {}).setdefault(case['node_id'], {})
            stage.update(status='paused', pending_recovery_case={
                'case_id': case['case_id'], 'round': len(case['rounds'])})
            return case
        case.update(status='open', pending={}, plan={})
        save_case(record, case)
        stage = record.setdefault('node_states', {}).setdefault(case['node_id'], {})
        stage.pop('pending_recovery_case', None)
        stage['status'] = 'pending'
        return case
    # Older cards expose the saved RecoveryOption ids directly without a
    # prepared choice bundle.  Preserve and execute the selected option rather
    # than clearing the plan and letting a later analysis pick the old
    # recommendation again.
    plan_for_card = pending.get('plan') if isinstance(pending.get('plan'), dict) else case.get('plan')
    plan_for_card = plan_for_card if isinstance(plan_for_card, dict) else {}
    option_ids = {item.get('option_id') for item in plan_for_card.get('options', [])
                  if isinstance(item, dict) and item.get('option_id')}
    if isinstance(choice, str) and choice in option_ids:
        selected_plan = copy.deepcopy(plan_for_card)
        selected_plan['recommended_option_id'] = choice
        selected_plan['round'] = len(case['rounds']) + 1
        case['plan'] = selected_plan
        case['selected_recovery_option'] = {'option_id': choice, 'case_id': case_id, 'round': round}
        if pending.get('kind') == 'business_decision':
            selected = next((item for item in pending.get('options', [])
                             if isinstance(item, dict) and item.get('value') == choice), {})
            case['selected_business_option'] = {
                'value': choice, 'label': selected.get('label', choice),
                'case_id': case_id, 'round': round,
            }
        _record_context_note(record, case, identity=identity, pending=pending,
                             text=choice, note=note,
                             extra={'kind': 'recovery_option', 'option_id': choice})
        case.setdefault('events', []).append(identity)
        case.pop('pending_choice_action', None)
        case.update(status='open', pending={})
        case.pop('closed_by_user', None)
        save_case(record, case)
        stage = record.setdefault('node_states', {}).setdefault(case['node_id'], {})
        stage.pop('pending_recovery_case', None)
        stage.pop('pending_choice_action', None)
        stage['status'] = 'pending'
        return case
    if isinstance(choice, str) and choice in {'research', 'revise', 'configure', 'retry_prepare', 'manual', 'defer'}:
        action_input = answer
        if isinstance(choice_inputs, dict):
            action_input = choice_inputs.get(choice, choice_inputs.get('action', action_input))
        if choice in {'defer', 'retry_prepare'} and action_input == '':
            action_input = None
        if choice in {'research', 'revise', 'configure', 'manual'}:
            if not isinstance(action_input, str) or not action_input.strip() or action_input != action_input.strip() or '\x00' in action_input:
                raise ValueError(f'动作 {choice} 需要补充输入')
        return _save_choice_action(
            record, case, pending, identity=identity, action=choice,
            action_input=action_input, values={}, bundle=_choice_bundle_for(pending), note=note,
        )
    if choice == 'investigate':
        return _save_investigation_request(record, case, pending, identity=identity,
                                           instruction=answer or note, note=note)
    elif choice == 'retry':
        direction = answer.strip() or note.strip()
        if not direction:
            raise ValueError('请选择一个具体处理方案或补充新方向；重复点击不会重新执行')
        case.pop('closed_by_user', None)
        save_case(record, case)
        case = apply_event(record, case_id, {'kind': 'extend', 'identity': identity,
                                           'user_confirmed': True, 'direction': direction})
        if note:
            record.setdefault('case_inputs', {})[identity] = {
                'case_id': case_id, 'text': choice,
                'question': pending.get('question', ''), 'context_note': note,
            }
    elif choice in {'approve', 'alternative', 'close', 'input'} or pending.get('kind') == 'business_decision':
        if pending.get('kind') != 'business_decision':
            for n, request in enumerate(pending.get('authority', [])):
                scope = copy.deepcopy(request['scope'])
                scope['operation_classes'] = [request['operation_class']]
                if choice == 'approve' and remember_scope == 'once':
                    scope.update(case_id=case_id, round=round + 1, epoch=case['epoch'], node_ids=[case['node_id']])
                if choice != 'approve':
                    scope.update(epoch=case['epoch'], node_ids=[case['node_id']])
                record_authorization(record, kind='elevation_answer' if choice == 'approve' else 'denial', scope=scope,
                    quote='批准卡片范围' if choice == 'approve' else '拒绝本次提权', source_ref=identity + f':{n}', host_verified=choice == 'approve')
        if choice == 'alternative':
            case['plan']['recommended_option_id'] = pending['alternative']['option_id']
            save_case(record, case)
        if choice == 'input':
            record_authorization(record, kind='user_statement', scope={'node_ids': [case['node_id']]},
                quote=answer, source_ref=identity)
            record.setdefault('case_inputs', {})[identity] = {'case_id': case_id, 'text': answer,
                'question': pending.get('question', ''),
                **({'context_note': note} if note else {})}
            # New materials can change tool arguments and the verification path.
            # Reuse the analyst session, not a plan made before this answer.
            case['plan'] = {}
            save_case(record, case)
        if pending.get('kind') == 'business_decision' and choice != 'close':
            selected = next((item for item in pending.get('options', []) if item.get('value') == choice), {})
            case['selected_business_option'] = {
                'value': choice, 'label': selected.get('label', choice),
                'case_id': case_id, 'round': round,
            }
            record.setdefault('case_inputs', {})[identity] = {
                'case_id': case_id, 'text': choice,
                'question': pending.get('question', ''), 'kind': 'business_decision',
                **({'context_note': note} if note else {}),
            }
            case['plan'] = {}
            save_case(record, case)
        elif note and choice != 'input':
            record.setdefault('case_inputs', {})[identity] = {
                'case_id': case_id, 'text': choice,
                'question': pending.get('question', ''),
                'context_note': note,
            }
        if choice == 'approve' and pending.get('input_requirements'):
            # Approval covers only the displayed typed operations. Re-analyze
            # remaining information, including fees; never silently discard it.
            case['plan'] = {}
            save_case(record, case)
        if case['status'] == 'awaiting_user':
            case = apply_event(record, case_id, {'kind': 'answer', 'identity': identity, 'status': 'reported' if choice == 'close' else 'open'})
        elif choice == 'close':
            case.update(pending={})
            save_case(record, case)
        elif case['status'] == 'reported':
            # Reports are answerable too. Re-open the same case after a typed
            # input, business choice, elevation approval, or alternative;
            # never treat the answer as a successful repair.
            case.update(status='open', pending={})
            save_case(record, case)
    else:
        raise ValueError('不支持的案件答复')
    stage = record.setdefault('node_states', {}).setdefault(case['node_id'], {})
    stage.pop('pending_recovery_case', None)
    stage.pop('pending_choice_action', None)
    stage['status'] = 'paused' if choice == 'close' else 'pending'
    if choice == 'close':
        case.update(status='reported', pending={}, closed_by_user=True)
        if identity not in case.get('events', []):
            case.setdefault('events', []).append(identity)
        case['report'].update(title='案件已停止', reason='你已停止本案件；节点尚未完成，检查点与产物已保留。')
        save_case(record, case)
    return case
