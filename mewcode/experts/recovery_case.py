"""Host-owned, append-only recovery cases and goal-directed checkpoints."""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Literal

from mewcode.experts.node_ledger import ledger_for_run

RootCauseClass = Literal['transient', 'environment_fixable', 'capability_bindable',
    'capability_absent', 'authority_gap', 'input_defect', 'contract_defect',
    'business_decision', 'unknown_effect', 'unclassified']
RETRYABLE = {'transient', 'environment_fixable', 'capability_bindable', 'unknown_effect', 'unclassified'}
FUNDAMENTAL = {'capability_absent', 'contract_defect', 'repeat_without_progress'}
USER_ROUTED = {'authority_gap', 'input_defect', 'business_decision'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def error_head_hash(text):
    text = re.sub(r'(?:[A-Za-z]:[\\/]|/)[^\s,;，；]+|\b[0-9a-f]{8,}\b|\d+', '<value>', str(text)[:200], flags=re.I)
    return digest(text)


def method_fingerprint(strategy):
    return digest([strategy['kind'], sorted(strategy['tools']), strategy['target']])


@dataclass(frozen=True)
class ProblemSignature:
    node_id: str
    blocker_kinds: tuple[str, ...]
    capability: str
    error_head_hash: str


@dataclass
class CaseRound:
    n: int
    option_id: str
    method_fingerprint: str
    root_cause: str
    authority: str = 'none'
    executor_receipt_ref: str | None = None
    verification: dict = field(default_factory=dict)
    evidence_digest: str = ''
    tokens: int = 0
    seconds: float = 0.


@dataclass
class RecoveryCase:
    case_id: str
    node_id: str
    epoch: int
    input_revision: str
    signature: ProblemSignature
    status: str = 'open'
    rounds: list[dict] = field(default_factory=list)
    boundary: dict = field(default_factory=dict)
    analyst_session: str = ''
    opened_by: str = 'host'
    events: list[str] = field(default_factory=list)
    pending: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)


def cases(record, node_id=None):
    values = {}
    ledger = ledger_for_run(record)
    for identity in ([node_id] if node_id else list(ledger.storage)):
        for fact in ledger.view(identity).history:
            if fact.kind == 'case' and fact.source == 'host' and isinstance(fact.payload.get('case'), dict):
                row = fact.payload['case']
                values[row['case_id']] = copy.deepcopy(row)
    return list(values.values())


def get_case(record, case_id):
    return next((row for row in cases(record) if row['case_id'] == case_id), None)


def active_case(record, node_id):
    epoch = ledger_for_run(record).view(node_id).epoch
    revision = record.get('node_states', {}).get(node_id, {}).get('input_revision', '')
    return next((row for row in reversed(cases(record, node_id)) if row['epoch'] == epoch
        and row['input_revision'] == revision
        and row['status'] not in {'resolved', 'superseded'}), None)


def decision_version(case):
    """A consumed answer starts a new decision, even if report prose repeats."""
    answered = [value for value in case.get('events', []) if value.startswith('answer:')]
    return digest([case['case_id'], case['epoch'], case.get('input_revision', ''),
                   answered[-1:] or case.get('events', [])[:1]])


def reconcile_wait_state(record, case_id):
    """Repair the legacy open+pending contradiction without answering a gate."""
    case = get_case(record, case_id)
    if not case or case.get('status') != 'open' or not case.get('pending'):
        return case
    stage = record.get('node_states', {}).get(case['node_id'], {})
    if (case.get('closed_by_user') or stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}
            or ledger_for_run(record).view(case['node_id']).epoch != case['epoch']
            or stage.get('input_revision', '') != case.get('input_revision', '')):
        return case
    selected = case.get('selected_recovery_option') or {}
    answered = any(value.startswith('answer:') for value in case.get('events', []))
    if answered and selected.get('case_id') == case_id and selected.get('round') == len(case['rounds']):
        case.setdefault('retired_questions', []).append({'reason': 'answered_report_replayed',
                                                        'pending': copy.deepcopy(case['pending'])})
        case['pending'] = {}
        stage.pop('pending_recovery_case', None)
        stage['status'] = 'pending'
        ledger_for_run(record).set_phase(case['node_id'], 'ready', reason='answered_report_reconciled')
    else:
        case['status'] = 'reported' if case.get('report') else 'awaiting_user'
    case['events'].append('wait_state_reconciled:' + decision_version(case))
    save_case(record, case)
    return case


def save_case(record, case):
    ledger_for_run(record).record(case['node_id'], 'case', {'case': copy.deepcopy(case)},
        epoch=case['epoch'], identity=[case['case_id'], case['events'], case['status'], digest(case)])
    record.setdefault('node_states', {}).setdefault(case['node_id'], {})['recovery_case'] = copy.deepcopy(case)


def handoff_gate_recovery(record, node_id):
    """Move an obsolete gate-repair loop to its ordinary, unapproved gate."""
    flow = record.get('locked_flow') or {}
    if (flow.get('meta') or {}).get('node_kinds', {}).get(node_id) != 'gate':
        return None
    stage = (record.get('node_states') or {}).get(node_id)
    case = active_case(record, node_id)
    if (not stage or not case or case.get('closed_by_user')
            or stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}):
        return None
    case = reconcile_wait_state(record, case['case_id']) or case
    if case.get('pending'):
        case.setdefault('retired_questions', []).append({
            'reason': 'normal_gate_confirmation', 'pending': copy.deepcopy(case['pending'])})
        save_case(record, case)
    case = apply_event(record, case['case_id'], {'kind': 'supersede',
        'identity': 'normal_gate_handoff:' + decision_version(case)})
    stage.update(status='pending', handling={'owner': 'user', 'handling_state': 'deciding',
        'reason_kind': 'authorization', 'reason': '正在整理具体方案和需要你批准的范围。'})
    stage.pop('pending_recovery_case', None)
    ledger_for_run(record).set_phase(node_id, 'ready', reason='normal_gate_handoff')
    # Repair the old running-with-no-worker contradiction, without interrupting
    # any active node or helper. Normal resume performs the later scheduling.
    if (record.get('status') == 'running' and not record.get('active_node_ids')
            and not any(row.get('status') == 'running' for row in (record.get('node_states') or {}).values())):
        record['status'] = 'paused'
        record.setdefault('flow_state', {}).update(phase='paused', status='paused')
    return case


def open_case(record, node_id, *, error='', capability='', identity='host'):
    supersede_cases(record, node_id)
    previous = active_case(record, node_id)
    if previous:
        return previous
    view = ledger_for_run(record).view(node_id)
    signature = ProblemSignature(node_id, tuple(sorted({f.payload.get('kind', '') for f in view.active_blockers})),
        capability, error_head_hash(error))
    stage = record.setdefault('node_states', {}).setdefault(node_id, {})
    ordinal = sum(row['epoch'] == view.epoch for row in cases(record, node_id)) + 1
    case_id = digest([node_id, view.epoch, stage.get('input_revision', ''), asdict(signature), identity, ordinal])[:24]
    case = asdict(RecoveryCase(case_id, node_id, view.epoch, stage.get('input_revision', ''), signature,
        analyst_session=f'{node_id}__analyst__{case_id}', events=['open:' + str(identity)]))
    from mewcode.experts.recovery_context import failed_step
    case['failed_step'] = failed_step(record, node_id)
    case['phases'] = ['发现问题']
    if any(f.epoch == view.epoch and f.source == 'host' and f.kind == 'maintenance'
           and f.payload.get('recovery_action') in {'self_diagnose', 'progress_continue'} for f in view.history):
        case['phases'].append('节点自救')
    save_case(record, case)
    return case


def advance(case, event):
    """Only host callers supply events; replay is idempotent and never spends a round."""
    case = copy.deepcopy(case)
    identity = event.get('identity')
    if not isinstance(identity, str) or not identity:
        raise ValueError('案件事件需要稳定 identity')
    if identity in case['events']:
        return case
    kind = event['kind']
    if case['status'] == 'superseded':
        raise ValueError('旧代际案件不可继续')
    if kind == 'start':
        if case['status'] != 'open' or case.get('closed_by_user'):
            raise ValueError('案件不能开始新轮次')
        case.update(status='executing', plan=copy.deepcopy(event['plan']), pending={})
    elif kind == 'finish':
        if case['status'] not in {'open', 'executing'} or case.get('closed_by_user'):
            raise ValueError('案件状态不匹配')
        row = copy.deepcopy(event['round'])
        if row['n'] != len(case['rounds']) + 1:
            raise ValueError('案件轮次不匹配')
        case['rounds'].append(row)
        outcome = row['verification'].get('result')
        case['status'] = 'resolved' if outcome == 'resolved' else 'open'
        if isinstance(event.get('boundary'), dict):
            case['boundary'] = copy.deepcopy(event['boundary'])
    elif kind == 'wait':
        if case['status'] not in {'open', 'executing', 'reported'}:
            raise ValueError('案件不可等待回答')
        case.update(status='awaiting_user', pending=copy.deepcopy(event['pending']))
    elif kind == 'answer':
        if case['status'] != 'awaiting_user':
            raise ValueError('案件没有待回答卡片')
        case.update(status=event.get('status', 'open'), pending={})
    elif kind == 'extend':
        if case['status'] not in {'reported', 'awaiting_user'} or event.get('user_confirmed') is not True:
            raise ValueError('继续处理需要用户明确选择')
        direction = str(event.get('direction') or '').strip()
        if not direction:
            raise ValueError('请提供具体的新方向，或选择诊断中的处理方案')
        case.update(status='open', pending={}, plan={})
        case['continuation_direction'] = direction
        # Keep prior evidence consumed; a user decision is not progress.
        case.setdefault('boundary', {})['needs_review'] = True
    elif kind == 'report':
        report = copy.deepcopy(event['report'])
        pending = copy.deepcopy(report.get('pending')) if isinstance(report.get('pending'), dict) else None
        if pending and pending.get('kind') in {'input', 'elevation', 'business_decision', 'incident'}:
            pending.setdefault('round', len(case['rounds']))
            pending.setdefault('options', copy.deepcopy(report.get('options', [])))
        else:
            pending = {'kind': 'incident', 'round': len(case['rounds']),
                       'options': copy.deepcopy(report.get('options', []))}
        # A report may be the first durable snapshot carrying model-generated
        # RecoveryOptions.  Preserve the executable plan alongside its card so
        # selecting an option can run that exact strategy after a restart.
        saved_plan = report.get('recovery_plan') or (pending or {}).get('plan')
        if isinstance(saved_plan, dict) and saved_plan.get('options'):
            case['plan'] = copy.deepcopy(saved_plan)
        case.update(status='reported', report=report, pending=pending)
    elif kind == 'reconcile':
        if event.get('case_digest') != digest(case):
            raise ValueError('宿主重核验未绑定当前案件版本')
        if case['status'] not in {'reported', 'awaiting_user', 'executing', 'open'}:
            raise ValueError('案件当前状态不能进行宿主重核验')
        evidence = copy.deepcopy(event.get('evidence') or {})
        if evidence.get('callback_eligible') is not True or not evidence.get('evidence_digest'):
            raise ValueError('宿主重核验证据不完整')
        case.setdefault('revalidations', []).append({
            'kind': 'input_discovery', 'evidence_digest': evidence['evidence_digest'],
            'evidence_tool_id': evidence.get('evidence_tool_id'),
            'helper_node_id': evidence.get('helper_node_id'),
            'executor_receipt_ref': evidence.get('executor_receipt_ref'),
            'relative_paths': copy.deepcopy(evidence.get('relative_paths') or []),
            'absolute_paths': copy.deepcopy(evidence.get('absolute_paths') or []),
            'case_digest': event.get('case_digest') or '',
            'recorded_at': event.get('recorded_at'),
        })
        case.update(status='resolved', pending={})
    elif kind == 'supersede':
        case.update(status='superseded', pending={})
    else:
        raise ValueError('未知案件事件')
    case['events'].append(identity)
    return case


def apply_event(record, case_id, event):
    case = get_case(record, case_id)
    if case is None:
        raise ValueError('案件不存在')
    if event.get('kind') == 'reconcile':
        # Recheck the proof at the mutation boundary.  A caller cannot turn a
        # copied ``callback_eligible`` object or foreign path list into a
        # resolved case by bypassing CaseCoordinator's preflight.
        if event.get('case_digest') != digest(case):
            raise ValueError('宿主重核验未绑定当前案件版本')
        from mewcode.experts.recovery_context import verify_input_discovery
        proof = event.get('evidence') or {}
        checked = verify_input_discovery(record, case['node_id'], case=case,
            case_id=case_id)
        if not checked.get('callback_eligible'):
            raise ValueError('输入发现回执未通过宿主重核验')
        bound = ('node_id', 'case_id', 'epoch', 'input_revision', 'evidence_digest',
                 'evidence_tool_id', 'helper_node_id', 'executor_receipt_ref',
                 'relative_paths', 'absolute_paths', 'query')
        if any(proof.get(key) != checked.get(key) for key in bound):
            raise ValueError('宿主重核验证据与当前来源不一致')
        event = {**event, 'evidence': checked}
    case = advance(case, event)
    save_case(record, case)
    if event['kind'] == 'finish':
        ledger_for_run(record).record(case['node_id'], 'case_round',
            {'case_id': case_id, 'round': case['rounds'][-1]}, epoch=case['epoch'], identity=event['identity'])
    elif event['kind'] == 'reconcile':
        # Keep this independent host fact in the existing maintenance ledger
        # vocabulary; no new node-ledger fact kind is needed for a read-only
        # revalidation.
        ledger_for_run(record).record(case['node_id'], 'maintenance',
            {'recovery_action': 'input_discovery_reconcile', 'case_id': case_id,
             'kind': 'input_discovery', 'evidence': copy.deepcopy(event.get('evidence') or {}),
             'case_digest': event.get('case_digest') or ''},
            epoch=case['epoch'], identity=event['identity'])
    return case


def supersede_cases(record, node_id):
    current = ledger_for_run(record).view(node_id).epoch
    revision = record.get('node_states', {}).get(node_id, {}).get('input_revision', '')
    for case in cases(record, node_id):
        if (case['epoch'] < current or case['input_revision'] != revision) and case['status'] not in {'resolved', 'superseded'}:
            apply_event(record, case['case_id'], {'kind': 'supersede', 'identity': f'epoch:{current}:input:{revision}'})


def migrate_legacy(record, node_id):
    stage = record.get('node_states', {}).get(node_id, {})
    if stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}:
        return None
    receipts = [r for r in stage.get('assistance_receipts', []) if r.get('next_step') == 'ask_human' and not r.get('question')]
    if not receipts or cases(record, node_id):
        return None
    case = open_case(record, node_id, identity='legacy_empty_questions', error=stage.get('error_kind', ''))
    attempts = [f for f in ledger_for_run(record).view(node_id).history if f.payload.get('recovery_action') in {'assist', 'patch_node'}]
    for n, receipt in enumerate(receipts, 1):
        row = asdict(CaseRound(n, 'legacy', digest(['legacy', receipt.get('request_id', n)]), 'unclassified',
            executor_receipt_ref=receipt.get('request_id'), verification={'result': 'unchanged',
                'reason': '旧协助没有具体问题或可核验的解决结果', 'action_ref': attempts[n-1].fact_id if n <= len(attempts) else ''}))
        case = apply_event(record, case['case_id'], {'kind': 'finish', 'identity': f'legacy:{n}', 'round': row})
    from mewcode.experts.problem_dossier import incident_report
    legacy_root = next((str(receipt.get('root_cause') or receipt.get('cause') or '').strip()
                        for receipt in receipts
                        if str(receipt.get('root_cause') or receipt.get('cause') or '').strip()
                        in {'input_defect', 'business_decision', 'authority_gap', 'contract_defect'}),
                       'unclassified')
    legacy_conclusion = next((str(receipt.get('reason') or receipt.get('conclusion') or '').strip()
                              for receipt in receipts
                              if str(receipt.get('reason') or receipt.get('conclusion') or '').strip()),
                             '此前协助没有完成，需要选择下一步。')
    case = apply_event(record, case['case_id'], {'kind': 'report', 'identity': 'legacy_report',
        'report': incident_report(record, case, conclusion=legacy_conclusion, root_cause=legacy_root)})
    return _route_saved_case(record, case)


def _route_saved_case(record, case):
    """Classify an old report once; empty legacy questions are not user facts."""
    from mewcode.experts.interaction_routing import derive_handling
    pending = case.get('pending') or {}
    report = case.get('report') or {}
    root = report.get('root_cause', '')
    fields = pending.get('input_definitions') or []
    route = derive_handling(purpose='auto', root_cause=root,
        explicit_user_gate=root in {'authority_gap', 'business_decision'},
        unmet_authority=pending.get('authority') or [], missing_user_fields=fields)
    stage = record['node_states'][case['node_id']]
    if route['owner'] == 'system':
        route.update(handling_state='blocked', reason=report.get('reason') or
            '此前协助没有交付可执行方案；检查点已保留，没有需要你回答的技术选择。')
        if pending:
            case.setdefault('retired_questions', []).append({'reason': 'internal_decision_owned_by_system',
                                                            'pending': copy.deepcopy(pending)})
        case['pending'] = {}
        stage.update(status='needs_attention')
        stage.pop('pending_recovery_case', None)
        ledger_for_run(record).set_phase(case['node_id'], 'escalated', reason='system_blocked')
    node = next((node for node in (record.get('locked_flow') or {}).get('nodes', [])
                 if node.get('node_id') == case['node_id']), {})
    route['summary'] = {'goal': node.get('label') or stage.get('label') or case['node_id'],
        'progress': '原检查点、已填参数和此前证据均已保留，当前步骤尚未完成。',
        'why_user': route['reason'] if route['owner'] == 'user' else '',
        'recommendation': '确认具体范围或补充所缺信息。' if route['owner'] == 'user' else '由系统核对具体故障和继续路径。',
        'impact': '原失败步骤尚未恢复，后续步骤暂不能继续。'}
    case['handling'] = route
    case['events'].append('ownership_migration:' + decision_version(case))
    save_case(record, case)
    return case


def needs_case_card_migration(record):
    idle_running_legacy = (record.get('status') == 'running'
                           and record.get('active_node_ids') == [])
    if (record.get('status') not in {'paused', 'blocked', 'needs_attention', 'failed', 'stalled'}
            and not idle_running_legacy):
        return False
    for stage in record.get('node_states', {}).values():
        if stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}:
            continue
        case = stage.get('recovery_case') or {}
        if (case.get('status') == 'reported' and not case.get('closed_by_user')
                and not case.get('handling')):
            return True
        if (case.get('handling') or {}).get('owner') == 'system':
            continue
        from mewcode.experts.recovery_decisions import misrouted_elevation
        if case and misrouted_elevation(record, case):
            return True
        if (case.get('status') == 'reported' and not case.get('closed_by_user')
                and not case.get('pending', {}).get('options') and case.get('report', {}).get('options')):
            return True
        if (case.get('status') == 'reported' and not case.get('closed_by_user')
                and case.get('report', {}).get('root_cause') == 'input_defect'
                and (case.get('pending', {}).get('kind') != 'input'
                     or not case.get('pending', {}).get('input_definitions'))):
            from mewcode.experts.problem_dossier import _input_defect_projection
            report = case.get('report', {})
            projection = report.get('card_projection') or {}
            expected = _input_defect_projection(
                record, case,
                input_definitions=report.get('input_definitions') or [],
                deferred_inputs=report.get('deferred_inputs') or [],
                reason=report.get('reason') or '',
            )
            if projection == expected:
                continue
            return True
        if not case and any(r.get('next_step') == 'ask_human' and not r.get('question')
                            for r in stage.get('assistance_receipts', [])):
            return True
    return False


def restore_case_cards(record):
    """Expose saved legacy waits without retrying models or granting authority."""
    if not needs_case_card_migration(record):
        return
    for node_id in list(record.get('node_states', {})):
        migrate_legacy(record, node_id)
        stage = record['node_states'][node_id]
        if stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}:
            continue
        case = stage.get('recovery_case') or {}
        if (case.get('handling') or {}).get('owner') == 'system':
            continue
        from mewcode.experts.recovery_decisions import misrouted_elevation
        if (case.get('status') == 'reported' and not case.get('closed_by_user')
                and case.get('report', {}).get('root_cause') == 'input_defect'
                and (case.get('pending', {}).get('kind') != 'input'
                     or not case.get('pending', {}).get('input_definitions'))):
            from mewcode.experts.problem_dossier import incident_report
            normalized = incident_report(
                record, case,
                conclusion=case.get('report', {}).get('reason') or '当前阶段仍缺少具体输入。',
                root_cause='input_defect',
            )
            identity = 'restore_input_card:' + digest([case['case_id'], normalized.get('input_definitions'), normalized.get('reason')])
            if identity not in case.get('events', []):
                case = apply_event(record, case['case_id'], {
                    'kind': 'report', 'identity': identity, 'report': normalized,
                })
            # ``incident_report`` carries the stable input-defect marker.
            # Save it even when the report event was already present in an
            # older snapshot, so the migration converges on the next read.
            projection = normalized.get('card_projection')
            if (projection and case.get('report', {}).get('card_projection') != projection):
                case.setdefault('report', {})['card_projection'] = copy.deepcopy(projection)
                save_case(record, case)
        pending = misrouted_elevation(record, case) if case else None
        if pending:
            original = copy.deepcopy(case['pending'])
            case['pending'] = pending
            case.setdefault('card_migrations', []).append({'kind': 'typed_authority_before_input',
                'original_pending': original})
            save_case(record, case)
            stage['pending_recovery_case'] = {'case_id': case['case_id'], 'round': len(case['rounds'])}
            stage['recovery_phase'] = '待你提权'
            if not case.get('handling'):
                _route_saved_case(record, case)
            continue
        if (case.get('status') == 'reported' and not case.get('closed_by_user')
                and not case.get('pending', {}).get('options') and case.get('report', {}).get('options')):
            case = apply_event(record, case['case_id'], {'kind': 'report',
                'identity': 'restore_card:' + digest([case['events'], case['report']]),
                'report': case['report']})
        if case.get('status') == 'reported' and not case.get('closed_by_user') and not case.get('handling'):
            _route_saved_case(record, case)
