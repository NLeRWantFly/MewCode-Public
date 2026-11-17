"""Explicit, reviewable choices when a saved graph cannot adopt an old run."""
from __future__ import annotations
import copy
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.node_ledger import bump_run_epoch, ledger_for_run
from mewcode.orchestration.revisions import execution_fingerprint


class MaintenanceChoiceRequired(ValueError):
    def __init__(self, run_adoption):
        self.run_adoption = run_adoption
        super().__init__('请为此运行选择维护接纳方式。')


def require_adopted_maintenance(snapshot, flow):
    value = flow.as_dict() if hasattr(flow, 'as_dict') else flow or {}
    binding = ((value.get('meta') or {}).get('maintenance_run_bindings') or {}).get(snapshot.get('run_id'))
    if not binding or any(row.get('maintenance_id') == binding.get('maintenance_id')
                          for row in snapshot.get('maintenance_adoptions') or []):
        return {'status': 'adopted', 'choices': []}
    if snapshot.get('maintenance_use_old_graph') == binding.get('maintenance_id'):
        return {'status': 'adopted', 'choices': [], 'use_old_graph': True}
    from mewcode.orchestration.maintenance_revision import changed_execution_nodes
    affected = binding.get('affected_node_ids') or sorted(changed_execution_nodes(snapshot.get('locked_flow'), value))
    return {'status': 'choice_required', 'maintenance_id': binding.get('maintenance_id'), 'choices': [
        {'id': 'rebase_node', 'label': '对受影响节点开新代际并接纳新版', 'node_ids': affected},
        {'id': 'use_old_graph', 'label': '本次继续用旧图，新版留给新运行'},
        {'id': 'new_run', 'label': '用新版新开运行，保留旧运行'}]}


def apply_maintenance_adoption_choice(snapshot, flow, choice, *, node_id=None):
    flow = ExpertFlowSpec.from_value(flow)
    candidate = copy.deepcopy(snapshot)
    state = require_adopted_maintenance(snapshot, flow)
    if state['status'] != 'choice_required':
        raise ValueError('该维护修订已接纳或不属于此运行')
    maintenance_id = state['maintenance_id']
    if choice == 'use_old_graph':
        candidate['maintenance_use_old_graph'] = maintenance_id
        selected = ExpertFlowSpec.from_value(snapshot['locked_flow'])
    elif choice == 'new_run':
        selected = flow
        candidate = {'task': snapshot.get('task', ''), 'attachments': copy.deepcopy(snapshot.get('attachments') or []),
                     'run_inputs': copy.deepcopy(snapshot.get('run_inputs') or {}), 'profile': copy.deepcopy(snapshot.get('locked_profile') or snapshot.get('profile') or {}),
                     'strategy': snapshot.get('strategy', 'stable'), 'conversation_id': snapshot.get('conversation_id')}
    elif choice == 'rebase_node':
        if snapshot.get('active_node_ids') or snapshot.get('status') == 'running':
            raise ValueError('原运行需要先停止再接纳')
        selected = flow
        affected = list(state['choices'][0]['node_ids'])
        if node_id and node_id not in affected:
            raise ValueError('所选节点不属于此次维护影响范围')
        from mewcode.expert_runs import successors_after_node, expert_flow_fingerprint
        for identity in tuple(affected):
            affected.extend(n for n in successors_after_node(snapshot['locked_flow'], identity) if n not in affected)
        for identity in affected:
            current = (snapshot.get('node_states') or {}).get(identity) or {}
            if current.get('status') == 'completed' or current.get('passed'):
                raise ValueError('修订影响已完成成果，请选择旧图或新运行：' + identity)
            from mewcode.experts.node_ledger import sync_node_facts
            from mewcode.experts.blocking_policy import BLOCKER_CLASS, refresh_clearances
            sync_node_facts(candidate, identity, include_legacy_blockers=True)
            refresh_clearances(candidate, identity)
            if any(BLOCKER_CLASS.get(f.payload.get('kind')) == 'authority'
                   for f in ledger_for_run(candidate).view(identity).active_blockers):
                raise ValueError('所选节点仍有授权或未知效果边界，请先完成核验：' + identity)
            bump_run_epoch(candidate, identity, reason='maintenance_applied', source_ref=maintenance_id)
        candidate.update(locked_flow=flow.as_dict(), flow_id=flow.flow_id, flow_version=flow.version,
            flow_fingerprint=expert_flow_fingerprint(flow), saved_plan_fingerprint=execution_fingerprint(flow),
            status='paused', active_node_ids=[], status_reason='维护已接纳，继续当前检查点')
        candidate.setdefault('maintenance_adoptions', []).append({'maintenance_id': maintenance_id, 'affected_node_ids': sorted(set(affected))})
        candidate.pop('plan_revision_pending', None)
        candidate.pop('flow_state', None)
    else:
        raise ValueError('未知的维护接纳选择')
    from mewcode.orchestration.convert import from_expert_flow
    from mewcode.orchestration.plan_session import build_plan_session
    plan = build_plan_session(from_expert_flow(selected)).as_dict()
    return {'run_snapshot': candidate, 'flow': selected.as_dict(), 'plan': plan, 'new_run': choice == 'new_run'}


def adopt_assistance_patch(record, request, patch):
    """Validate a node-scoped patch on a copy before altering the live record."""
    if not isinstance(patch, dict) or set(patch) != {'nodes'} or not isinstance(patch['nodes'], list):
        raise ValueError('协助 patch 只接受 nodes 列表')
    candidate = copy.deepcopy(record)
    flow = copy.deepcopy(record['locked_flow'])
    from mewcode.expert_runs import successors_after_node, expert_flow_fingerprint
    allowed = {request.node_id, *successors_after_node(flow, request.node_id)}
    # A not-yet-entered loop owns repair nodes that have no forward DAG path
    # from its entry. They are still downstream work, under the same guards.
    loops = (flow.get('meta') or {}).get('loops') or []
    for _ in range(len(loops)):
        before = len(allowed)
        for loop in loops:
            if loop.get('entry_node_id') in allowed or request.node_id in (loop.get('node_ids') or []):
                allowed.update(loop.get('node_ids') or [])
        if len(allowed) == before:
            break
    mutable = {'responsibility', 'tools', 'deny_tools', 'failure_policy'}
    for change in patch['nodes']:
        if not isinstance(change, dict):
            raise ValueError('协助 patch 的每个节点修订必须是对象')
        identity = change.get('node_id')
        stage = (record.get('node_states') or {}).get(identity) or {}
        if identity not in allowed or (identity != request.node_id and stage.get('status') not in {None, 'pending', 'ready'}):
            raise ValueError('协助 patch 只能影响本节点与尚未开始的下游')
        if set(change) - mutable - {'node_id'}:
            raise ValueError('协助 patch 不能更改模型、预算或验收合同')
        target = next((n for n in flow['nodes'] if n['node_id'] == identity), None)
        if target is None:
            raise ValueError('协助 patch 引用了不存在的节点')
        if 'deny_tools' in change:
            from mewcode.tools import expand_tool_aliases
            previous_denials = expand_tool_aliases(set(target.get('deny_tools') or []))
            new_denials = expand_tool_aliases(set(change.get('deny_tools') or []))
            if not previous_denials <= new_denials:
                raise ValueError('协助 patch 不能移除显式禁用的工具；扩大授权需逐项用户确认')
        target.update(copy.deepcopy(change))
    parsed = ExpertFlowSpec.from_value(flow)
    if expert_flow_fingerprint(parsed) == expert_flow_fingerprint(record['locked_flow']):
        raise ValueError('协助 patch 没有实际节点变更，不能据此重置原检查点')
    from mewcode.experts.node_ledger import sync_node_facts
    from mewcode.experts.blocking_policy import BLOCKER_CLASS, refresh_clearances
    sync_node_facts(candidate, request.node_id, include_legacy_blockers=True)
    refresh_clearances(candidate, request.node_id)
    view = ledger_for_run(candidate).view(request.node_id)
    if (any(BLOCKER_CLASS.get(f.payload.get('kind'), 'business') in {'authority', 'business'}
            for f in view.active_blockers)
            or any(f.payload.get('status') in {'in_flight', 'outcome_unknown', 'effect_unknown', 'effect_partial'}
                   for f in view.current_receipts)):
        raise ValueError('协助 patch 接纳前须核验原操作效果并处理原问题；原检查点已保留')
    bump_run_epoch(candidate, request.node_id, reason='assistance_patch', source_ref=request.request_id)
    candidate.update(locked_flow=parsed.as_dict(), flow_fingerprint=expert_flow_fingerprint(parsed),
                     assistance_flow_revision=request.request_id)
    ledger_for_run(candidate).record(request.node_id, 'maintenance', {'assistance_patch': patch, 'request_id': request.request_id})
    record.clear()
    record.update(candidate)
