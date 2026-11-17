"""Version-bound downstream inputs, independent of scheduler-local caches."""
import copy
import json
from dataclasses import replace

from mewcode.kernel import collaboration as ledger


async def loop_decision_handoff(result, node_id, context):
    """Freeze a validated control submission as its actual inline deliverable."""
    if (result.status != 'completed' or not context or not result.loop_decision
            or node_id not in (context.get('reviewer_node_ids') or [])
            or any((result.handoff or {}).get(key) for key in ('artifacts', 'inline_outputs'))):
        return result
    from mewcode.tools.submit_loop_decision import SubmitLoopDecisionTool, SubmitLoopDecisionParams
    from mewcode.tools.submit_verdict import VerdictHolder
    try:
        params = SubmitLoopDecisionParams.model_validate(result.loop_decision)
    except ValueError:
        return result
    checked = await SubmitLoopDecisionTool(VerdictHolder(), context).execute(params)
    if checked.is_error:
        return result
    return replace(result, handoff={**(result.handoff or {}), 'inline_outputs': [
        {'name': '循环判定', 'content': json.dumps(result.loop_decision, ensure_ascii=False)}]})


async def retained_loop_decision(value, node_id, context, model, attempt_id):
    """Recheck an unchanged decision rejected solely for the old host envelope.

    Keep the rejected delivery and report immutable. The caller supplies these
    bytes to ordinary acceptance on the current attempt, without model replay.
    """
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.kernel.collaboration_recovery import gate_current
    if not context or node_id not in (context.get('reviewer_node_ids') or []):
        return None
    contract = value['contracts'][node_id]
    current = ledger.check_attempt(value, attempt_id)
    prior = [row for row in ledger.attempts_for(value, node_id) if row['number'] < current['number']]
    if (not prior or current.get('delivery') or contract.get('held')
            or contract.get('workspace_mode') or ledger.revision_pending(value, node_id)
            or value.get('rework_instructions', {}).get(node_id)
            or any(g.get('status') == 'waiting' and node_id in g.get('node_ids', [])
                and gate_current(value, g) for g in value.get('recovery_gates', {}).values())):
        return None
    source = prior[-1]
    report = source.get('report') or {}
    if (source['status'] != 'rejected' or source.get('patch')
            or (source.get('delivery') or {}).get('status') != 'completed'
            or source.get('execution_fault') or source.get('acceptance_fault')
            or source.get('executor_model') != model.as_dict()
            or source.get('retry_generation', 0) != value.get('retry_generations', {}).get(node_id, 0)
            or report.get('reason') != '交接缺少可回读的文件或内联产物'
            or report.get('artifacts') or report.get('execution_error')
            or report.get('artifacts_unchanged') is not True):
        return None
    try:
        ledger.check_attempt(value, source['id'])
    except ValueError:
        return None
    original = ExpertRoleResult(**source['delivery'])
    result = await loop_decision_handoff(original, node_id, context)
    if result is original:
        return None
    return source, replace(result, restored=True, input_tokens=0, output_tokens=0,
        billable_tokens=0, iterations_used=0, active_seconds=0)


def accepted_sources(current, keys):
    sources = {}
    for key in keys:
        if not ledger.valid_certificate(current, key):
            raise ValueError(f"上游 {key} 的固定交接版本已失效，不能派发下游")
        certificate = current['certificates'][key]
        delivery = current['attempts'][certificate['attempt_id']]['delivery']
        sources[key] = {
            'node_id': key, 'status': 'completed',
            'text': '上游自述（未验证补充）：' + str(delivery.get('text') or ''),
            'handoff': copy.deepcopy(delivery.get('handoff') or {}),
            'verdict': copy.deepcopy(delivery.get('verdict')),
            'loop_decision': copy.deepcopy(delivery.get('loop_decision')),
            'acceptance_certificate': copy.deepcopy(certificate),
            'fixed_artifacts': copy.deepcopy(certificate['artifacts']),
            'instruction': '读取 fixed_artifacts 中的 snapshot_path 固定版本；原路径仅为来源定位',
        }
    return sources


def loop_control_source(record, context, sources):
    """Frozen control facts; audit details stay in the original bound sources."""
    from mewcode.experts.review_evidence import queue_progress
    nodes = set(context.get('node_ids') or [])
    return {'node_id': '@loop_control', 'status': 'host_snapshot',
        'loop_id': context['loop_id'], 'round': context['round'],
        'contract_hash': context['contract_hash'], 'current_item': copy.deepcopy(context.get('current_item')),
        'check_node_id': context.get('check_node_id'),
        'entry_node_id': context.get('entry_node_id'),
        'repair_node_ids': list(context.get('repair_node_ids') or []),
        'upstream': [{'node_id': key, 'source_revision': value.get('source_revision'),
            'status': value.get('status'), 'verdict': copy.deepcopy(value.get('verdict')),
            'loop_decision': copy.deepcopy(value.get('loop_decision'))}
            for key, value in sources.items() if not key.startswith('@')],
        'queues': {key: queue_progress(queue) for key, queue in (record.get('review_queues') or {}).items()
                   if key in nodes},
        'instruction': '这是当前派发的宿主快照；按冻结条件判定，队列摘要不能代替实际审计证据。'}
