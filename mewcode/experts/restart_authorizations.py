"""Read authority provenance only from a host-bound, explicitly replaced task."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import time

from mewcode.orchestration.run_inputs import resolve_run_inputs


def _files_match(project, versions):
    """Check the approved source bytes even when a new handoff is inline."""
    try:
        if not project or not Path(project).is_absolute() or not isinstance(versions, dict) or not versions:
            return False
        root = Path(project).resolve()
        for rows in versions.values():
            for row in rows:
                file = (root / row['path']).resolve()
                if not file.is_relative_to(root):
                    return False
                actual = hashlib.sha256()
                with file.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        actual.update(chunk)
                if actual.hexdigest() != row['sha256']:
                    return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _retire_redundant_question(record, node_id, scope):
    """Archive this gate's duplicate wait without recording a user answer."""
    stage = (record.get('node_states') or {}).get(node_id) or {}
    pending = stage.get('pending_interaction') or {}
    if not pending:
        return
    if (pending.get('kind') != 'human_oncall' or pending.get('tool') != 'human_oncall'
            or not pending.get('request_id') or not str(pending.get('scope_hash') or '').startswith(scope + ':')):
        raise ValueError('当前节点还有其他待答问题，不能复用签认跳过')
    if any(row.get('answered') and (row.get('pending') or {}).get('request_id') == pending['request_id']
           for row in stage.get('interaction_history') or []):
        raise ValueError('当前关口已有真实用户答复，不能归档为未答复问题')
    reason = 'restart_authorization_reused'
    stage.setdefault('interaction_history', []).append({'pending': copy.deepcopy(pending),
        'status': 'retired_not_approved', 'answered': False, 'reason': reason, 'at': time.time()})
    stage.pop('pending_interaction')
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    ledger = ledger_for_run(record)
    question_ids = {fact.fact_id for fact in ledger.view(node_id).history
                    if fact.kind == 'question' and fact.payload.get('request_id') == pending['request_id']}
    if any(fact.kind == 'answer' and fact.payload.get('question_fact_id') in question_ids
           for fact in ledger.view(node_id).history):
        raise ValueError('当前关口已有真实用户答复，不能归档为未答复问题')
    retired = ledger.record(node_id, 'maintenance', {'reason': reason,
        'request_id': pending['request_id'], 'approved': False}, source='host')
    for fact in ledger.view(node_id).history:
        if not fact.superseded_by and (
                fact.kind == 'question' and fact.payload.get('request_id') == pending['request_id']
                or fact.kind == 'blocker' and fact.payload.get('kind') in {'human_question', 'user_input'}
                and fact.payload.get('ref') == pending['request_id']):
            ledger.supersede(fact.fact_id, retired.fact_id)
    project_node_view(record, node_id)
    for question in ((record.get('collaboration') or {}).get('questions') or {}).values():
        if (question.get('node_id') == node_id and question.get('status') == 'waiting'
                and question.get('questions') == pending.get('questions')):
            question.update(status='superseded', retirement_reason=reason, approved=False)
    gate = (record.get('kernel_gates') or {}).get(pending['request_id']) or {}
    if gate.get('node_id') == node_id and gate.get('status') == 'waiting':
        gate.update(status='superseded', retirement_reason=reason, approved=False)


def source_for_restart(store, design, flow, *, conversation_id, task, run_inputs):
    restart = design.get('restart') or {}
    source_id = restart.get('source_run_id')
    if not source_id or not restart.get('source_task_id') or not restart.get('request_id'):
        raise ValueError('重新开始缺少宿主原任务绑定')
    source = store.read_execution_fields(source_id, 'run_id', 'conversation_id', 'task',
        'status', 'active_node_ids', 'locked_flow', 'run_inputs', 'authorizations', 'visual_authorization_usage')
    old_flow = source.get('locked_flow') or {}
    old_design = (old_flow.get('meta') or {}).get('task_design') or {}
    if (source.get('run_id') != source_id or source.get('conversation_id') != conversation_id
            or source.get('status') != 'terminated' or source.get('active_node_ids')
            or old_design.get('task_id') != restart['source_task_id']
            or (flow.meta.get('task_design') or {}).get('task_id') != design.get('task_id')):
        raise ValueError('原任务未安全结束或重新开始归属已变化，未继承授权')
    # The old task text was written before stage-time inputs were collected.
    # Compare the exact business goal and current typed values, rather than
    # mistaking those later answers for a different business task.
    marker = '\n\n本次运行参数：'
    if str(source.get('task') or '').split(marker, 1)[0] != str(task or '').split(marker, 1)[0]:
        raise ValueError('重新开始的业务目标已变化，未继承原授权')
    declarations = flow.meta.get('run_input_contract') or []
    current = resolve_run_inputs(declarations, run_inputs)
    previous = resolve_run_inputs(declarations, source.get('run_inputs') or {})
    if current != previous:
        raise ValueError('重新开始的参数范围已变化，未继承原授权')
    return source


def gate_reuse_valid(node, flow, snapshot):
    """A historical approval remains a reference, never a new user answer."""
    from mewcode.experts.authorization_ledger import covers
    from mewcode.experts.gate_dialogue import digest, inputs
    from mewcode.experts.human_gate import gate_scope_hash
    from mewcode.kernel.collaboration import valid_certificate

    proof = (snapshot.get('gate_reuses') or {}).get(node.node_id) or {}
    restart = snapshot.get('task_restart') or {}
    inheritance = snapshot.get('authorization_inheritance') or {}
    draft = (snapshot.get('gate_dialogues') or {}).get(node.node_id) or {}
    if (proof.get('schema_version') != 2 or not restart.get('request_id')
            or inheritance.get('same_task_basis') != 'host_task_restart'
            or proof.get('source_run_id') != restart.get('source_run_id')
            or proof.get('source_run_id') != inheritance.get('source_run_id')
            or proof.get('scope_hash') != gate_scope_hash(node, flow.meta)
            or proof.get('current_inputs_hash') != digest(inputs(node, flow, snapshot))
            or not draft.get('proposal') or proof.get('proposal_hash') != digest(draft['proposal'])
            or not proof.get('source_certificate_hash') or not proof.get('source_request_id')
            or not proof.get('upstream_certificates')
            or set(proof['upstream_certificates']) != {
                edge.from_node for edge in flow.edges if edge.to_node == node.node_id}
            or set(proof.get('upstream_file_versions') or {}) != set(proof['upstream_certificates'])
            or any(not rows and not (proof.get('upstream_inline_versions') or {}).get(parent)
                   for parent, rows in proof.get('upstream_file_versions', {}).items())
            or not _files_match(proof.get('project_root'), proof.get('upstream_file_versions'))):
        return False
    value = snapshot.get('collaboration') or {}
    for parent, expected in (proof.get('upstream_certificates') or {}).items():
        if not valid_certificate(value, parent) or digest(value['certificates'][parent]) != expected:
            return False
    authorization = next((row for row in (snapshot.get('authorizations') or {}).get('entries', [])
        if row.get('kind') == 'gate_answer'
        and row.get('source_ref') == 'gate:' + proof['source_request_id']
        and (row.get('carried_from') or {}).get('auth_id') == proof.get('source_auth_id')), None)
    return bool(authorization and covers({'authorizations': {'entries': [authorization]}},
        {'operation_class': 'other', 'scope': {
        'node_ids': [node.node_id], 'operation_digest': proof['proposal_hash']}}).status == 'covered'
    )


def reuse_gate(store, run_id, *, node, flow, snapshot, project):
    """Reuse only the unchanged gate of a verified, explicitly replaced task.

    The old approved proposal, current source certificate, live inherited
    authorization and unchanged physical upstream files must all agree.
    A changed gate, changed input, revocation or missing proof uses the normal
    human gate. No approval receipt or new capability is manufactured here.
    """
    from mewcode.experts.authorization_ledger import covers
    from mewcode.experts.gate_dialogue import digest, inputs
    from mewcode.experts.human_gate import gate_scope_hash
    from mewcode.kernel.collaboration_graph import effective_flow

    restart = snapshot.get('task_restart') or {}
    inheritance = snapshot.get('authorization_inheritance') or {}
    if not restart or inheritance.get('same_task_basis') != 'host_task_restart':
        return False
    task_id = (flow.meta.get('task_design') or {}).get('task_id')
    try:
        source = source_for_restart(store, {'task_id': task_id, 'restart': restart}, flow,
            conversation_id=snapshot.get('conversation_id'), task=snapshot.get('task'),
            run_inputs=snapshot.get('run_inputs') or {})
        source_flow = store.read_execution_flow(source['run_id'])
        old_node = source_flow.node_map.get(node.node_id)
        if old_node is None or (source_flow.meta.get('node_kinds') or {}).get(node.node_id) != 'gate':
            return False
        scope = gate_scope_hash(node, flow.meta)
        if scope != gate_scope_hash(old_node, source_flow.meta):
            return False
        certificate = store.read_current_certificate(source['run_id'], node.node_id)
        if not certificate or certificate['certificate'].get('quality_passed') is not True:
            return False
        facts = store.read_execution_fields(source['run_id'], 'gate_confirmations', 'gate_dialogues')
        receipt = (facts.get('gate_confirmations') or {}).get(node.node_id) or {}
        draft = (facts.get('gate_dialogues') or {}).get(node.node_id) or {}
        proposal_hash = digest(draft.get('proposal'))
        if (receipt.get('decision') != 'continue' or receipt.get('scope_hash') != scope
                or not draft.get('proposal') or receipt.get('proposal_hash') != proposal_hash
                or receipt.get('inputs_hash') != draft.get('inputs_hash')):
            return False
        request = {'operation_class': 'other', 'scope': {
            'node_ids': [node.node_id], 'operation_digest': proposal_hash}}
        authorization = next((row for row in (source.get('authorizations') or {}).get('entries', [])
            if receipt.get('request_id') and row.get('kind') == 'gate_answer'
            and row.get('source_ref') == 'gate:' + receipt['request_id']), None)
        if not authorization or covers({'authorizations': {'entries': [authorization]}}, request).status != 'covered':
            return False
        if covers(source, request).status != 'covered' or covers(snapshot, request).status != 'covered':
            return False
        root = Path(project).resolve()
        upstream, file_versions, inline_versions = {}, {}, {}
        old_parents = {edge.from_node for edge in source_flow.edges if edge.to_node == node.node_id}
        new_parents = {edge.from_node for edge in flow.edges if edge.to_node == node.node_id}
        if old_parents != new_parents or not old_parents:
            return False
        for parent in sorted(old_parents):
            old = store.read_current_certificate(source['run_id'], parent)
            current = store.read_current_certificate(run_id, parent)
            if not old or not current:
                return False
            old_files = {row['path']: row['sha256'] for row in old['certificate'].get('artifacts', [])
                         if row.get('kind') == 'file'}
            new_files = {row['path']: row['sha256'] for row in current['certificate'].get('artifacts', [])
                         if row.get('kind') == 'file'}
            prior_inline = [row['sha256'] for row in old['certificate'].get('artifacts', [])
                            if row.get('kind') == 'inline' and row.get('sha256')
                            and isinstance(row.get('content'), str) and row['content'].strip()]
            # Omitted references are not changed bytes. The host can bind the
            # original certificate's files directly; an explicit conflicting
            # current version still invalidates reuse.
            if (not old_files and not prior_inline
                    or any(path in new_files and new_files[path] != sha for path, sha in old_files.items())):
                return False
            file_versions[parent] = [{'path': path, 'sha256': sha} for path, sha in sorted(old_files.items())]
            # Inline audit findings are context, not a new capability grant.
            # Reusing the exact old approved proposal does not pass the new
            # audit or authorize an effect outside that original scope.
            inline_versions[parent] = sorted(prior_inline)
            upstream[parent] = digest(current['certificate'])
        if not _files_match(str(root), file_versions):
            return False
    except (ValueError, KeyError, OSError, TypeError):
        return False

    proof = {'schema_version': 2, 'source_run_id': source['run_id'],
        'source_request_id': receipt.get('request_id'), 'scope_hash': scope,
        'source_auth_id': authorization['auth_id'],
        'proposal_hash': proposal_hash, 'source_certificate_hash': digest(certificate['certificate']),
        'upstream_certificates': upstream, 'upstream_file_versions': file_versions, 'project_root': str(root),
        'upstream_inline_versions': inline_versions,
        'current_inputs_hash': digest(inputs(node, flow, snapshot)),
        'reused_at': time.time(), 'notice': 'Existing approval reused; no new user answer or grant.'}

    def apply(record):
        live_flow = effective_flow(record)
        if (live_flow.as_dict() != flow.as_dict() or record.get('task_restart') != restart
                or resolve_run_inputs(flow.meta.get('run_input_contract') or [], record.get('run_inputs') or {})
                != resolve_run_inputs(flow.meta.get('run_input_contract') or [], source.get('run_inputs') or {})
                or digest(inputs(node, flow, record)) != proof['current_inputs_hash']
                or covers(record, request).status != 'covered'
                or not _files_match(proof['project_root'], proof['upstream_file_versions'])):
            raise ValueError('原批准的范围或输入已变化，未复用签认')
        _retire_redundant_question(record, node.node_id, scope)
        record.setdefault('gate_dialogues', {})[node.node_id] = copy.deepcopy(draft)
        record.setdefault('gate_reuses', {})[node.node_id] = proof
        if not gate_reuse_valid(node, live_flow, record):
            raise ValueError('原批准的文件、交接或授权已变化，未复用签认')

    try:
        store.mutate(run_id, apply, durable=True)
    except (ValueError, KeyError, TypeError):
        return False
    return True
