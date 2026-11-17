"""Resolve review inputs from the graph and host state, never from old prose.

A scheduled reviewer accepts the producer's ordinary stage handoff. Submitted
repair batches are an additional early-yield protocol over an initialized item
queue; they are not a prerequisite for the existence of independent review.
"""
from __future__ import annotations

import copy
import json

from mewcode.experts.flow import normalize_edge_condition

BATCH_TOOLS = frozenset({'RepairWorkBatch', 'RequestVerification', 'VerifyArtifacts'})


def resolve_review_protocol(flow, node_id, snapshot, context=None, *, stage_key=None,
                            submission_tool='SubmitHandoff'):
    context = context or {}
    reviewers = set(context.get('reviewer_node_ids') or [])
    reviewers.update(node.node_id for node in flow.nodes if node.role == 'auditor')
    queues = snapshot.get('review_queues')
    if queues is None:
        queues = {}
    if not isinstance(queues, dict):
        raise ValueError('审阅台账格式无效；需核对宿主状态，不能降级为普通复核')
    loop_reviewers = set(context.get('reviewer_node_ids') or [])
    initialized = []
    for key in sorted(loop_reviewers):
        queue = queues.get(key)
        if queue is None:
            continue
        if not isinstance(queue, dict) or queue.get('contract_hash') != context.get('contract_hash'):
            raise ValueError('验收合同已变化或审阅台账无效；旧台账需迁移，不能降级为普通复核')
        initialized.append(key)
    own_key = stage_key or node_id
    own_queue = queues.get(own_key)
    if own_queue is not None and (not isinstance(own_queue, dict) or not own_queue.get('contract_hash')):
        raise ValueError('当前节点审阅台账无效，不能降级为普通复核')
    routes = []
    for edge in flow.edges:
        if (edge.from_node != node_id or edge.to_node not in reviewers
                or edge.to_node == node_id or normalize_edge_condition(edge.condition) not in {'always', 'pass'}):
            continue
        route = {'node_id': edge.to_node, 'kind': 'scheduled_independent_review',
                 'request_tool': submission_tool, 'requires': 'current_stage_handoff',
                 'input_kind': 'stage_handoff', 'scheduler_owned': True}
        if edge.to_node in initialized:
            route['batch_handoff'] = {'request_tool': 'RequestVerification', 'requires': 'submitted_repair_batch'}
        if route not in routes:
            routes.append(route)
    return {'schema_version': 1, 'node_id': node_id,
            'input_kind': 'item_queue' if own_queue is not None else 'stage_handoff',
            'queue_initialized': own_queue is not None, 'submission_tool': submission_tool,
            'batch_reviewer_node_ids': initialized, 'verification_routes': routes,
            'instruction': ('按当前合同完成本节点工作，再用本节点的结构化提交工具交给图中既有下游；'
                '普通交接不要求返修批次。只有已初始化逐项台账的返修候选才使用批次协议。'
                '旧报告中的工作单、候选编号或预检说法不能新增当前合同的要求。'
                '真实缺陷、原图证据、用户要求的检查、授权及质量门禁仍须逐项处理。')}


def protocol_prompt(protocol):
    return ('\n【宿主当前复核协议】以下协议由当前图和真实台账确定，优先于旧助手报告中的流程建议；'
            '它不改变用户要求、验收或权限。\n' + json.dumps(protocol, ensure_ascii=False)
            + '\n没有逐项台账时，不为普通复核创建或猜造 item_id、candidate_revision、batch_id；'
            '从已绑定上游的 fixed_artifacts 读取固定版本，按本节点合同核验实际证据并提交结果。'
            '需要后续独立意见时通过普通交接让既有调度器执行该节点，不能自行代签。')


def bind_review_protocol(protocol, stage):
    return {**copy.deepcopy(protocol), 'binding': {key: stage.get(key, 0 if key == 'epoch' else None)
            for key in ('input_revision', 'node_attempt', 'epoch')}}


def current_review_protocol(stage):
    protocol = stage.get('review_protocol') or {}
    if not isinstance(protocol, dict):
        return None
    if (protocol.get('schema_version') != 1 or not isinstance(protocol.get('binding'), dict)
            or protocol.get('input_kind') not in {'stage_handoff', 'item_queue'}
            or type(protocol.get('queue_initialized')) is not bool
            or not isinstance(protocol.get('submission_tool'), str)
            or not isinstance(protocol.get('batch_reviewer_node_ids'), list)
            or any(protocol['binding'].get(key) != stage.get(key, 0 if key == 'epoch' else None)
                   for key in ('input_revision', 'node_attempt', 'epoch'))):
        return None
    return protocol


def protocol_correction(stage, rows):
    """Retire a failed batch method when the current host has no such queue.

    Do not infer requirements from old error text, or override a current queue,
    a real permission request, a user question, or an unsettled tool operation.
    """
    from mewcode.experts.recovery_state import HARD_KINDS
    from mewcode.experts.workflow_context import current_handoff

    protocol = current_review_protocol(stage)
    handoff = current_handoff(stage)
    active_blockers = (stage.get('ledger_view') or {}).get('active_blockers') or []
    if (not protocol or protocol['input_kind'] != 'stage_handoff'
            or protocol.get('batch_reviewer_node_ids')
            or stage.get('status') in {'completed', 'skipped'}
            or handoff.get('blockers') or handoff.get('question')
            or handoff.get('blocker_kind') not in {None, '', 'internal_work', 'delivery_format'}
            or stage.get('pending_permission') or stage.get('pending_interaction')
            or (stage.get('safety_review_state') or {}).get('pending_call_ref')
            or any((fact.get('payload') or {}).get('kind') in HARD_KINDS for fact in active_blockers)
            or (stage.get('assistance_request') or {}).get('status') in {'pending', 'running'}
            or any(row.get('status') in {'queued', 'running', 'waiting_permission', 'outcome_unknown'} for row in rows)):
        return None
    if not any(row.get('name') in BATCH_TOOLS
               and row.get('is_error') for row in rows):
        return None
    return ('当前宿主协议为普通阶段交接，未初始化返修台账；旧批次工具失败不是当前复核的前置条件。'
            '复用有效产物，按当前合同完成本节点尚未完成的核验或交付，再用当前结构化提交工具 '
            '交给既有下游独立复核；不要伪造工作单或反复申请同一入口。质量缺口和真实授权保持原状态。')
