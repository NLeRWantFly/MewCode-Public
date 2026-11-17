"""One bounded host ladder; narrative claims never replenish its budget."""
import copy
import json
from dataclasses import asdict, dataclass
from typing import Literal

from mewcode.kernel.async_commit import durable_call
from mewcode.runtime_workers import io_call
from mewcode.kernel.blockers import policy
from mewcode.experts.blocking_policy import refresh_clearances
from mewcode.experts.node_ledger import NodeView, ledger_for_run, project_node_view

RecoveryAction = Literal['progress_continue', 'self_diagnose', 'assist', 'patch_node', 'ask_human', 'fail', 'open_case', 'run_case_round']
LEVELS = ('retry_same_method', 'alternate_route', 'replan_node', 'human')
OWNERS = ('executor', 'executor', 'planner', 'human')


@dataclass(frozen=True)
class RecoveryBudget:
    max_no_progress_segments: int = 2
    self_diagnose_per_epoch: int = 1
    assist_per_node_epoch: int = 1
    assist_per_run: int = 20
    patch_per_node_epoch: int = 1
    max_epochs_per_node: int = 5
    auto_continue_without_clearance: int = 1


@dataclass(frozen=True)
class RecoveryContext:
    node_view: NodeView
    run_assist_count: int
    trigger: str
    blocker_classes: frozenset[str]
    made_progress: bool
    cases_enabled: bool = False


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    assistance_kind: str | None = None
    owner: str = 'executor'
    level: str | None = None


def action_count(view, action):
    return sum(f.epoch == view.epoch and f.source == 'host' and f.payload.get('recovery_action') == action
               for f in view.history)


def no_progress_segments(view):
    count = 0
    for fact in reversed(view.history):
        if fact.epoch != view.epoch or fact.kind != 'progress' or 'made_progress' not in fact.payload:
            continue
        if fact.source == 'host' and fact.payload['made_progress'] and fact.payload.get('evidence_marker'):
            break
        count += 1
    return count


def automatic_continuations(view):
    return action_count(view, 'progress_continue')


def _decision(fact):
    value = fact.payload.get('decision')
    return value if isinstance(value, dict) else {}


def used_levels(view):
    used = set()
    # A cleared incident must not spend a later incident's retry ladder. For
    # legacy decisions, recover their exact blocker set from append-only order.
    # Decisions made without a blocker remain the node's bounded execution
    # ladder; merely changing an error label cannot replenish it.
    cleared = {f.payload.get('blocker_fact_id') for f in view.history
               if f.epoch == view.epoch and f.kind == 'clearance'
               and f.source in {'host', 'maintenance', 'migration'}}
    prior_blockers = set()
    for fact in view.history:
        if fact.epoch != view.epoch or fact.source != 'host':
            continue
        if fact.kind == 'blocker':
            prior_blockers.add(fact.fact_id)
        if fact.kind == 'clearance':
            prior_blockers.discard(fact.payload.get('blocker_fact_id'))
        incident = fact.payload.get('recovery_blocker_ids', sorted(prior_blockers))
        if incident and set(incident) <= cleared:
            continue
        level = _decision(fact).get('level')
        if level:
            used.add(level)
        elif fact.payload.get('recovery_action') in {'self_diagnose', 'assist', 'patch_node'}:
            used.update(LEVELS[:3] if fact.payload['recovery_action'] == 'patch_node' else LEVELS[:2])
    return used


def retire_resolved_recovery(record, node_id):
    """Retire a host reservation only when its exact incident was cleared."""
    ledger = ledger_for_run(record)
    view = ledger.view(node_id)
    stage = (record.get('node_states') or {}).get(node_id) or {}
    pending = stage.get('pending_recovery_decision') or {}
    if pending.get('epoch') != view.epoch or not pending.get('decision'):
        return
    active, matched = set(), None
    cleared = {f.payload.get('blocker_fact_id') for f in view.history
               if f.epoch == view.epoch and f.kind == 'clearance'
               and f.source in {'host', 'maintenance', 'migration'}}
    for fact in view.history:
        if fact.epoch != view.epoch or fact.source != 'host':
            continue
        if fact.kind == 'blocker':
            active.add(fact.fact_id)
        elif fact.kind == 'clearance':
            active.discard(fact.payload.get('blocker_fact_id'))
        elif fact.payload.get('recovery_action') and fact.payload.get('decision'):
            matched = fact, set(fact.payload.get('recovery_blocker_ids', active))
    if not matched:
        return
    fact, incident = matched
    if fact.payload['decision'] != pending['decision'] or not incident or not incident <= cleared:
        return
    stage.pop('pending_recovery_decision', None)
    ledger.record(node_id, 'maintenance', {'resolved_recovery_fact_id': fact.fact_id,
        'cleared_blocker_ids': sorted(incident)}, identity=['resolved_recovery', fact.fact_id])


def decide(ctx, budget=RecoveryBudget()):
    view = ctx.node_view
    kinds = {str(f.payload.get('kind')) for f in view.active_blockers}
    if 'authority' in ctx.blocker_classes:
        if kinds and kinds <= {'unknown_effect', 'effect_unknown'}:
            if not any(f.epoch == view.epoch and f.source == 'host'
                       and _decision(f).get('assistance_kind') == 'effect_verification'
                       for f in view.history):
                return RecoveryDecision('assist', '先执行一次只读效果对账。', 'effect_verification', 'verifier', 'readonly_reconciliation')
            return RecoveryDecision('ask_human', '只读对账没有确定结果，等待你核销原调用。', owner='human', level='human')
        return RecoveryDecision('ask_human', '当前操作需要明确授权或有效审查结论。', owner='human', level='human')
    if 'business' in ctx.blocker_classes or view.epoch >= budget.max_epochs_per_node:
        return RecoveryDecision('ask_human', '当前节点需要你的具体决定。', owner='human', level='human')
    # An unfinished batch is expected during a long task. Host-observed new
    # output lets it continue without consuming a failure-recovery level.
    # Other technical faults still require their own recovery/clearance.
    if ctx.made_progress and (not ctx.blocker_classes or (kinds and kinds <= {'internal_work'})):
        return RecoveryDecision('progress_continue', '宿主确认新产物、验收或证据，继续当前任务。')
    used = used_levels(view)
    if (ctx.trigger in {'assistance_requested', 'supervisor_signal', 'replan_execution_method'}
            and 'alternate_route' not in used and ctx.run_assist_count < budget.assist_per_run):
        return RecoveryDecision('assist', '已收到明确的上交请求，在原授权内换处理路线一次。',
                                'diagnosis', 'executor', 'alternate_route')
    for level, action, reason, owner in (
        ('retry_same_method', 'progress_continue', '按原方法重试一次。', 'executor'),
        ('alternate_route', 'self_diagnose', '在现有模型池或已授权路线内换一种执行方式，仅一次。', 'executor'),
        ('replan_node', 'patch_node', '交给规划者重新规划当前节点，仅一次。', 'planner')):
        if level not in used:
            return RecoveryDecision(action, reason, owner=owner, level=level)
    return RecoveryDecision('ask_human', '三层自动处理机会已用完，等待你的决定。', owner='human', level='human')


def run_assist_count(record):
    ledger = ledger_for_run(record)
    return sum(f.source == 'host' and f.payload.get('recovery_action') == 'assist'
               for node_id in ledger.storage for f in ledger.view(node_id).history)


def context_for(record, node_id, *, trigger, made_progress=False):
    refresh_clearances(record, node_id)
    view = ledger_for_run(record).view(node_id)
    from mewcode.experts.recovery_state import observed_progress_marker, advance_work_cursor
    marker = observed_progress_marker(record, node_id)
    baseline = record.setdefault('kernel_progress', {}).get(node_id, {
        'review_complete': 0, 'review_recorded': 0, 'submitted_items': [], 'write_receipts': []})
    measured, cursor = advance_work_cursor({'before': baseline, 'after': marker}, baseline)
    record['kernel_progress'][node_id] = cursor
    classes = frozenset(policy(f.payload['kind']).category for f in view.active_blockers)
    return RecoveryContext(view, run_assist_count(record), trigger, classes, measured)


def record_decision(record, node_id, decision, *, trigger, identity):
    ledger = ledger_for_run(record)
    blocker_ids = [fact.fact_id for fact in ledger.view(node_id).active_blockers]
    if decision.level == 'alternate_route' and trigger in {'assistance_requested', 'supervisor_signal', 'replan_execution_method'}:
        ledger.record(node_id, 'maintenance', {'recovery_action': 'skip_inapplicable_retry',
            'decision': {'level': 'retry_same_method'}, 'reason': 'explicit_handoff',
            'recovery_blocker_ids': blocker_ids}, identity=['skip_retry', identity])
    fact = ledger.record(node_id, 'maintenance', {'recovery_action': decision.action, 'trigger': trigger,
        'decision': asdict(decision), 'claim_id': identity, 'recovery_blocker_ids': blocker_ids}, identity=['recovery', identity])
    project_node_view(record, node_id)
    ensure_owners(record)
    return fact


def decide_and_record(record, node_id, *, trigger, made_progress=False, identity, budget=RecoveryBudget()):
    view = ledger_for_run(record).view(node_id)
    prior = next((f for f in view.history if f.epoch == view.epoch
                  and f.payload.get('claim_id') == identity and f.source == 'host'), None)
    if prior is not None:
        return RecoveryDecision(**prior.payload['decision'])
    context = context_for(record, node_id, trigger=trigger)
    ledger_for_run(record).record(node_id, 'progress', {'made_progress': context.made_progress,
        'evidence_marker': record['kernel_progress'][node_id], 'trigger': trigger}, identity=['segment', identity])
    choice = decide(context, budget)
    record_decision(record, node_id, choice, trigger=trigger, identity=identity)
    return choice


def ensure_owners(record):
    ledger = ledger_for_run(record)
    if 'node_states' not in record:
        record['node_states'] = {node_id: {} for node_id in ledger.storage}
    for node_id in ledger.storage:
        if node_id not in record['node_states']:
            continue
        view = ledger.view(node_id)
        stage = record.setdefault('node_states', {}).setdefault(node_id, {})
        pending = stage.get('pending_recovery_decision') or {}
        reserved = pending.get('decision') if pending.get('epoch') == view.epoch else None
        if reserved and not view.active_blockers:
            ledger.record(node_id, 'blocker', {'kind': 'internal_work', 'ref': 'reserved_recovery',
                          'owner': reserved.get('owner', 'executor')}, identity=['reserved_recovery', view.epoch])
            view = ledger.view(node_id)
        level = next((index for index, name in enumerate(LEVELS[:3]) if name not in used_levels(view)), 3)
        rows = []
        for blocker in view.active_blockers:
            contract = policy(blocker.payload['kind'])
            owner = (contract.owner if level < 2 else OWNERS[level]) if contract.category == 'auto' else contract.owner
            if contract.owner == 'verifier' and any(
                    f.epoch == view.epoch and _decision(f).get('level') == 'readonly_reconciliation'
                    for f in view.history):
                owner = 'human'
            if reserved and blocker.payload.get('ref') == 'reserved_recovery':
                owner = reserved.get('owner') or owner
            rows.append({'fact_id': blocker.fact_id, 'kind': blocker.payload['kind'], 'owner': owner,
                         'next': reserved.get('level') if reserved and blocker.payload.get('ref') == 'reserved_recovery'
                            else LEVELS[level] if contract.category == 'auto' else contract.clearance,
                         'remaining_opportunities': 0 if owner == 'human' else 1})
        stage['blocker_resolution'] = rows


def authorization_request_for(blocker, *, workspace=''):
    """Turn one recorded manifest gap into the typed request it stands for.

    A node whose capability list does not cover a call is not a missing user
    decision: the user already said what the task is. These requests exist so the
    host can check the recorded scope first and only ask when it is uncovered.
    Paths name the actual target, never its parent directory.
    """
    call = (blocker.payload or {}).get('requested_call') or {}
    tool, arguments = call.get('tool'), call.get('arguments') or {}
    if tool in {'WriteFile', 'EditFile'}:
        from pathlib import Path
        if not arguments.get('file_path') or not workspace:
            return None
        root = Path(workspace).resolve()
        file_path = Path(str(arguments['file_path']))
        file_path = (file_path if file_path.is_absolute() else root / file_path).resolve()
        if not file_path.is_relative_to(root):
            return None
        # One spelling for authorization scopes: backslashes in a granted path
        # would never match the same directory recorded with forward slashes.
        return {'operation_class': 'write_outside_workspace', 'scope': {'paths': [file_path.as_posix()]}}
    if tool == 'Bash':
        from mewcode.permissions.command_rules import parse_command
        import os as _os
        parsed = parse_command(str(arguments.get('command') or ''), 'powershell' if _os.name == 'nt' else 'bash')
        if (not parsed.safe_for_prefix_rules or len(parsed.segments) != 1
                or any(segment.redirections for segment in parsed.segments)):
            return None
        return {'operation_class': 'run_command_prefix', 'scope': {
            'command_prefix': list(parsed.segments[0].arguments)}}
    if tool in {'WebFetch', 'web_fetch'}:
        from urllib.parse import urlsplit
        host = urlsplit(str(arguments.get('url') or '')).hostname
        return {'operation_class': 'network', 'scope': {'hosts': [host]}} if host else None
    return None


def authorization_choice_options(current, node_id):
    """Concrete choices for the recorded gaps of one node; no filler options."""
    from mewcode.kernel.authorization_binding import request_for_call, coverage
    workspace = str((current.get('authorization_manifest') or {}).get('workspace') or '')
    options, pending = [], []
    for fact in ledger_for_run(copy.deepcopy(current)).view(node_id).active_blockers:
        if fact.payload.get('kind') != 'missing_authorization' or not fact.payload.get('requested_call'):
            continue
        call = fact.payload['requested_call']
        request = request_for_call(node_id, call['tool'], call.get('arguments') or {}, workspace)
        if request is None:
            continue
        if coverage(current, request).status == 'covered':
            continue
        pending.append((fact, request))
        tool = str(call.get('tool') or '')
        target = ''
        if tool in {'WriteFile', 'EditFile'}:
            target = str((call.get('arguments') or {}).get('file_path') or '')
        elif tool in {'WebFetch', 'web_fetch'}:
            target = str((call.get('arguments') or {}).get('url') or '')
        elif tool == 'Bash':
            target = str((call.get('arguments') or {}).get('command') or '')[:80]
        label = ('允许这次操作：' + tool) if not target else ('允许这次操作：' + tool + ' → ' + target[:60])
        options.append({'value': 'allow:' + fact.fact_id, 'label': label,
                        'description': '确认后按最小范围授权本次操作，并只解除这一条阻塞。'})
    if not pending:
        return options, pending
    options.append({'value': 'deny_scope', 'label': '不允许，停止当前节点',
                    'description': '不授权这些操作，保留已有成果；后续由流程的失败分支处理。'})
    return options, pending


def apply_authorization_choice(record, *, node_id, blocked, choice, source_ref):
    """Record the authorization the user actually picked, then clear that gap."""
    from mewcode.experts.authorization_ledger import record_authorization
    from mewcode.kernel.authorization_binding import bind_call, clear_call_blockers
    approved = [row for row in blocked if choice == 'allow:' + row[0].fact_id]
    if choice == 'deny_scope' or not approved:
        return {'status': 'no_effect'}
    active = {fact.fact_id for fact in ledger_for_run(record).view(node_id).active_blockers}
    recorded = []
    for fact, request in approved:
        if fact.fact_id not in active:
            continue
        scope = {**copy.deepcopy(request['scope']), 'operation_classes': [request['operation_class']]}
        auth = record_authorization(record, kind='gate_answer', scope=scope,
            quote='用户在缺口关口确认这次操作：' + json.dumps(request['scope'], ensure_ascii=False),
            source_ref='gate:' + source_ref + ':' + fact.fact_id, host_verified=True)
        call = fact.payload['requested_call']
        if bind_call(record, node_id, call['tool'], call.get('arguments') or {}):
            clear_call_blockers(record, node_id, call['tool'], call.get('arguments') or {})
        recorded.append(auth['auth_id'])
    return {'status': 'authorized', 'auth_ids': recorded}


def unstarted_assistance_decision(record, node_id):
    """Finish an existing reservation that never reached its helper.

    Old hosts could record an assist decision with no assistance coordinator
    attached. Keep that decision and its budget charge; do not buy another
    recovery rung or replay an already started helper.
    """
    stage = record.get('node_states', {}).get(node_id, {})
    request = stage.get('assistance_request') or {}
    request_id = request.get('request_id')
    view = ledger_for_run(record).view(node_id)
    if (not request_id or request.get('status') != 'pending'
            or request.get('epoch') != view.epoch or request.get('node_id') != node_id
            or stage.get('pending_permission') or view.pending_question
            or any(policy(f.payload['kind']).category in {'authority', 'business'} for f in view.active_blockers)
            or (stage.get('assistance_checkpoint') or {}).get('request_id') == request_id
            or any(row.get('request_id') == request_id for row in stage.get('assistance_receipts', []))
            or node_id + '__assist_' + request_id[:12] in record.get('node_states', {})):
        return None
    requested = next((f for f in view.history if f.epoch == view.epoch and not f.superseded_by
                      and f.kind == 'assistance' and f.source == 'node'
                      and f.payload.get('request_id') == request_id and f.payload.get('status') == 'requested'), None)
    if requested is None:
        return None
    reserved = next((f for f in view.history if f.epoch == view.epoch and not f.superseded_by
                     and f.source == 'host' and f.created_at >= requested.created_at
                     and f.payload.get('trigger') == 'assistance_requested'
                     and _decision(f).get('action') == 'assist'
                     and _decision(f).get('level') == 'alternate_route'), None)
    return RecoveryDecision(**reserved.payload['decision']) if reserved else None


class KernelScheduler:
    """Adapt the single host ladder to graph continuations and primary questions."""
    def __init__(self, *, session, registry, store, run_id, emit, before_resume, task,
                 primary_repair=None, **unused):
        self.session, self.registry, self.store, self.run_id = session, registry, store, run_id
        self.emit, self.before_resume, self.task, self.primary_repair = emit, before_resume, task, primary_repair

    def stage(self, node_id):
        return (self.store.load(self.run_id) or {}).get('node_states', {}).get(node_id) or {}

    def checkpoint_result(self, node_id, result):
        from dataclasses import replace
        from mewcode.experts.recovery_state import work_progress
        record, stage = self.store.load(self.run_id), self.stage(node_id)
        return replace(result, review_progress=stage.get('review_progress') or result.review_progress,
            work_progress=work_progress(record, node_id), billable_tokens=stage.get('billable_tokens', result.billable_tokens),
            iterations_used=stage.get('iterations_used', result.iterations_used))

    async def record_result(self, event):
        from mewcode.experts.primary_repair import record_primary_verification
        await durable_call(record_primary_verification, self.store, self.run_id, event)

    async def __call__(self, *, node, result, policy, allowed, host_facts, account=None):
        import asyncio
        import uuid
        from mewcode.agent import AskUserRequest
        from mewcode.experts.node_ledger import sync_node_facts
        from mewcode.kernel.events import digest
        from mewcode.experts.waits import BranchWaitPaused, wait_for_interaction
        await self.before_resume()
        if (host_facts.get('hard_boundary') or not allowed
                or (policy.token_budget is not None and policy.token_budget < 1024)
                or (policy.timeout_seconds is not None and policy.timeout_seconds <= 0)):
            return {'action': 'stop', 'source': 'resource_boundary', 'chosen': {
                'next_step': host_facts.get('hard_boundary') or '当前资源边界已达到；保留检查点。'}}
        choice = []
        def reserve(record):
            stage = record.setdefault('node_states', {}).setdefault(node.node_id, {})
            sync_node_facts(record, node.node_id, include_legacy_blockers=False)
            if host_facts.get('hard_boundary') == '全局运行时限已耗尽':
                ledger_for_run(record).record(node.node_id, 'blocker', {'kind': 'run_deadline'}, identity='deadline')
            pending = stage.pop('pending_recovery_decision', None)
            unstarted = (unstarted_assistance_decision(record, node.node_id)
                         if self.primary_repair is not None
                         and host_facts.get('preserve_collaboration_contract')
                         and host_facts.get('trigger') == 'assistance_requested' else None)
            if unstarted is not None:
                selected = unstarted
            elif pending and pending.get('epoch') == ledger_for_run(record).view(node.node_id).epoch:
                selected = RecoveryDecision(**pending['decision'])
            else:
                selected = decide_and_record(record, node.node_id,
                    trigger=host_facts.get('trigger') or result.error_kind or result.status,
                    identity=digest(['scheduler', node.node_id, stage.get('node_attempt'), stage.get('input_revision'),
                                     record.get('kernel_revision'), len(ledger_for_run(record).view(node.node_id).history)]))
            choice.append(selected)
            stage.setdefault('scheduler_recovery', {}).update(controller=asdict(selected))
        snapshot = (await durable_call(self.store.mutate, self.run_id, reserve, durable=True))
        selected = choice[0]
        decision = {'action': 'human', 'source': 'kernel', 'controller': asdict(selected),
                    'host_check': selected.reason, 'chosen': {'next_step': selected.reason}}
        if allowed and not host_facts.get('hard_boundary'):
            if selected.level == 'readonly_reconciliation':
                from mewcode.kernel.reconcile import apply_probe, file_probe
                checker = getattr(getattr(self.session, 'agent', None), 'permission_checker', None)
                for call in list(snapshot.get('kernel_calls', {}).values()):
                    if call['node_id'] == node.node_id and call['state'] == 'unknown' and not call.get('user_adjudication'):
                        observation = file_probe(call, self.session.work_dir, checker)
                        (await durable_call(self.store.mutate, self.run_id, lambda record, key=call['call_id'], proof=observation:
                            apply_probe(record, key, proof), durable=True))
                current = (await io_call(self.store.load, self.run_id))
                if not any(f.payload['kind'] == 'unknown_effect' for f in ledger_for_run(current).view(node.node_id).active_blockers):
                    return {**decision, 'action': 'continue_checkpoint'}
            elif selected.action in {'progress_continue', 'self_diagnose'}:
                if selected.level == 'alternate_route':
                    if host_facts.get('preserve_collaboration_contract') and 'switch_declared_route' not in allowed:
                        return {**decision, 'action': 'diagnose_path', 'source': 'same_authorized_model'}
                    from mewcode.kernel.models import RoleModels, rebind_role
                    pool = RoleModels(await self.registry.catalog(local_only=True))
                    current = (snapshot.get('locked_models') or {}).get(node.node_id) or node.model
                    current = current or (snapshot.get('role_models') or {}).get('roles', {}).get('executor')
                    try:
                        target = pool.alternate({'roles': {'executor': current}}, 'executor') if current else None
                    except ValueError:
                        target = None
                    if target:
                        def bind(record):
                            rebind_role(record, pool=pool, role='executor', model=target, source_ref='alternate:' + node.node_id + ':' + str(
                                ledger_for_run(record).view(node.node_id).epoch))
                            record.setdefault('locked_models', {})[node.node_id] = target.as_dict()
                        (await durable_call(self.store.mutate, self.run_id, bind, durable=True))
                return {**decision, 'action': 'continue_checkpoint' if selected.action == 'progress_continue' else 'diagnose_path'}
            elif selected.action in {'assist', 'patch_node'} and self.primary_repair is not None:
                if (host_facts.get('preserve_collaboration_contract')
                        and not (selected.action == 'assist' and host_facts.get('trigger') == 'assistance_requested')):
                    return {**decision, 'action': 'human', 'chosen': {
                        'next_step': '原合同内的自动恢复机会已用完，请在任务关口决定重试、换模型或修改要求。'}}
                assisted = await self.primary_repair(node=node, result=result, policy=policy, allowed=allowed,
                    host_facts={**host_facts, 'assistance_kind': selected.assistance_kind or 'diagnosis',
                                'patch_required': selected.action == 'patch_node'})
                if assisted and assisted.get('action') in allowed:
                    return assisted
                if (assisted and assisted.get('action') == 'human'
                        and host_facts.get('preserve_collaboration_contract')):
                    return assisted
        if host_facts.get('defer_human'):
            return decision
        current = (await io_call(self.store.load, self.run_id)) or {}
        if not getattr(self.session, 'work_dir', None):
            return decision
        unknown = [row for row in current.get('kernel_calls', {}).values() if row['node_id'] == node.node_id
                   and row['state'] == 'unknown' and not row.get('user_adjudication')]
        options = ([{'value': 'assume_not_applied', 'label': '按未发生重做'},
                    {'value': 'assume_applied', 'label': '按已发生继续'}, {'value': 'pause', 'label': '保留进度等待'}]
                   if len(unknown) == 1 else [{'value': 'pause', 'label': '保留进度等待', 'recommended': True},
                                            {'value': 'continue', 'label': '在原范围继续一次'}])
        # A recorded authorization gap has a concrete, answerable choice: the
        # user already said what the task is, so only the uncovered scope is asked.
        grant_options, pending_grants = authorization_choice_options(current, node.node_id)
        if grant_options:
            options = grant_options
        from mewcode.kernel.briefs import brief
        request = AskUserRequest(uuid.uuid4().hex, 'KernelEscalation', [{'name': 'direction',
            'question': str((result.handoff or {}).get('question') or selected.reason), 'context': brief(current, node.node_id),
            'options': options}], asyncio.get_running_loop().create_future())
        answers = await wait_for_interaction(request, kind='ask_user', session=self.session, emit=self.emit,
            store=self.store, run_id=self.run_id, stage_key=node.node_id, role=node.role)
        value = answers.get('direction') if isinstance(answers, dict) else None
        if len(unknown) == 1 and value in {'assume_not_applied', 'assume_applied'}:
            from mewcode.kernel.repairs import adjudicate
            (await durable_call(self.store.mutate, self.run_id, lambda record: adjudicate(record, node_id=node.node_id,
                call_id=unknown[0]['call_id'], disposition=value, source_ref=request.request_id), durable=True))
            return {**decision, 'action': 'continue_checkpoint', 'source': 'explicit_user_disposition'}
        if value == 'continue' and allowed and not ledger_for_run((await io_call(self.store.load, self.run_id))).view(node.node_id).active_blockers:
            return {**decision, 'action': 'continue_checkpoint', 'source': 'explicit_user_choice'}
        if pending_grants and isinstance(value, str) and (value.startswith('allow:') or value == 'deny_scope'):
            (await durable_call(self.store.mutate, self.run_id, lambda record: apply_authorization_choice(record,
                node_id=node.node_id, blocked=pending_grants, choice=value,
                source_ref=request.request_id), durable=True))
            if value == 'deny_scope':
                return {**decision, 'action': 'fail', 'source': 'explicit_user_denial'}
            return {**decision, 'action': 'continue_checkpoint', 'source': 'explicit_authorization'}
        raise BranchWaitPaused(node.node_id, request.request_id, reason=selected.reason)
