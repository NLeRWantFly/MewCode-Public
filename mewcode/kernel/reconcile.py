"""Bound, read-only host probes for uncertain invocation effects."""
import hashlib
import base64
import os
import stat
from pathlib import Path


def pre_dispatch_evidence(record, node_id, stage, tool_id, tool):
    """An exact durable gateway proposal proves dispatch never happened.

    Call only after the owning execution has stopped. A UI start event includes
    permission review; it is not the gateway's executor-acceptance event.
    """
    from mewcode.kernel.calls import canonical_invocation, invocation_id
    from mewcode.kernel.events import digest
    if (tool.get('name') not in {'Bash', 'WriteFile', 'EditFile', 'RunVerification'}
            or not isinstance(tool.get('args'), dict) or not stage.get('input_revision')
            or tool.get('tool_id', tool_id) != tool_id):
        return None
    invocation = canonical_invocation(tool['name'], tool['args'])
    call_id = invocation_id(node_id, stage['input_revision'], invocation)
    call = record.get('kernel_calls', {}).get(call_id)
    allowed = {'proposed', 'reviewed', 'rejected', 'not_started'}
    if (not call or call.get('call_id') != call_id or call.get('node_id') != node_id
            or call.get('input_revision') != stage['input_revision'] or call.get('invocation') != invocation
            or call.get('state') not in allowed or not call.get('history')
            or call['history'][0] != 'proposed' or call['history'][-1] != call['state']
            or any(state not in allowed for state in call['history'])
            or call.get('origin') or call.get('user_adjudication')
            or any(item.get('executor_accepted') is True for item in call.get('evidence') or [])):
        return None
    return {'source': 'kernel_pre_dispatch_reconciliation', 'host_verified': True,
            'request_id': tool_id, 'kernel_call_id': call_id, 'input_revision': stage['input_revision'],
            'invocation_sha256': digest(invocation), 'gateway_state': call['state'],
            'gateway_history': list(call['history']), 'command_started': False,
            'effect': 'not_applied', 'status': 'not_started',
            'reason': '精确匹配的宿主调用仅进入派发前阶段，没有 executor_accepted；原操作未执行。'}


def reconcile_pre_dispatch_effects(record):
    """Clear only quiescent, exact-call false unknowns; preserve old archives."""
    if (record.get('status') not in {'paused', 'blocked', 'needs_attention'}
            or record.get('active_node_ids') or not record.get('node_ledgers')):
        return []
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    from mewcode.kernel.events import digest
    ledger, repaired = ledger_for_run(record), []
    for node_id in list(record.get('node_ledgers') or {}):
        view = ledger.view(node_id)
        blockers = [fact for fact in view.active_blockers
                    if fact.payload.get('kind') == 'unknown_effect' and fact.payload.get('ref')]
        if not blockers:
            continue
        current = record.get('node_states', {}).get(node_id) or {}
        for blocker in blockers:
            tool_id = blocker.payload['ref']
            sources = [(None, current)] if tool_id in current.get('tools', {}) else []
            invalid = False
            for archive_id, archive in record.get('kernel_attempt_archives', {}).items():
                data = archive.get('data') or {}
                if archive.get('node_id') != node_id or tool_id not in data.get('tools', {}):
                    continue
                if archive.get('data_digest') != digest(data):
                    invalid = True
                    break
                sources.append((archive_id, data))
            proofs = []
            for archive_id, stage in sources:
                tool = stage['tools'][tool_id]
                if (stage.get('epoch') != view.epoch or tool.get('epoch') != view.epoch
                        or tool.get('status') not in {'running', 'outcome_unknown', 'not_executed',
                                                    'interrupted_before_execution'}):
                    invalid = True
                    break
                proof = pre_dispatch_evidence(record, node_id, stage, tool_id, tool)
                if proof is None:
                    invalid = True
                    break
                proofs.append(proof)
            if invalid or not proofs or any(proof != proofs[0] for proof in proofs[1:]):
                continue
            proof = {**proofs[0], 'blocker_fact_id': blocker.fact_id,
                     'archive_ids': sorted(key for key, _ in sources if key)}
            receipt = ledger.record(node_id, 'receipt', proof,
                identity=['kernel_pre_dispatch_reconciliation', blocker.fact_id, digest(proof)])
            ledger.record(node_id, 'clearance', {'blocker_fact_id': blocker.fact_id,
                'evidence_ref': receipt.fact_id, 'clearance_kind': 'independent_effect_receipt'},
                identity=['kernel_pre_dispatch_clearance', blocker.fact_id, receipt.fact_id])
            if tool_id in current.get('tools', {}):
                current['tools'][tool_id].update(status='not_executed',
                    recovery_reason='kernel_pre_dispatch_reconciliation')
            current['blockers'] = [row for row in current.get('blockers', [])
                                   if not isinstance(row, dict) or row.get('ref') != tool_id]
            repaired.append({'node_id': node_id, 'tool_id': tool_id, 'receipt_id': receipt.fact_id})
        if any(row['node_id'] == node_id for row in repaired):
            project_node_view(record, node_id)
    return repaired


def file_probe(call, work_dir, checker=None):
    invocation = call['invocation']
    args = invocation['arguments']
    observation = {'call_id': call['call_id'], 'readonly': True, 'host_verified': True,
                   'state': 'unknown', 'kind': 'file_digest'}
    if invocation['tool'] != 'WriteFile' or not isinstance(args.get('content'), str):
        return {**observation, 'reason': '没有能核销该调用的只读文件凭证'}
    try:
        root = Path(work_dir).resolve()
        target = Path(args['file_path'])
        target = target if target.is_absolute() else root / target
        target = target.resolve(strict=True)
        target.relative_to(root)
        sandbox = getattr(checker, 'sandbox', None)
        if sandbox is not None and not sandbox.check(str(target), read_only=True)[0]:
            return {**observation, 'reason': '核验路径不在当前读取范围内'}
        if not target.is_file() or target.stat().st_size > 50 * 1024 * 1024:
            return {**observation, 'reason': '文件超出只读摘要探针范围'}
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        content = (base64.b64decode(args['content'], validate=True) if args.get('encoding') == 'base64'
                   else args['content'].replace('\n', os.linesep).encode('utf-8'))
        intended = hashlib.sha256(content).hexdigest()
        actual_mode = stat.S_IMODE(target.stat().st_mode)
        mode_matches = args.get('mode') is None or args['mode'] == actual_mode
        return {**observation, 'sha256': actual, 'expected_sha256': intended, 'mode': actual_mode,
                'state': 'succeeded' if actual == intended and mode_matches else 'unknown'}
    except (OSError, ValueError, KeyError):
        return {**observation, 'reason': '当前文件状态不能确定原调用是否生效'}


def apply_probe(record, call_id, observation):
    from mewcode.kernel.calls import transition
    if (observation.get('call_id') != call_id or observation.get('readonly') is not True
            or observation.get('host_verified') is not True):
        raise ValueError('reconciliation must come from a bound read-only host probe')
    row = record['kernel_calls'][call_id]
    row.setdefault('reconciliation_checks', []).append(dict(observation))
    if observation['state'] in {'succeeded', 'failed'}:
        if row['state'] == 'dispatched':
            transition(row, 'unknown')
        transition(row, observation['state'], evidence=observation)
        row['result'] = {'output': '宿主只读探针已确认原调用效果。', 'is_error': observation['state'] == 'failed',
                         'host_observation': {'reconciliation': dict(observation)}}
        from mewcode.experts.node_ledger import ledger_for_run
        ledger = ledger_for_run(record)
        receipt = ledger.record(row['node_id'], 'receipt', {'request_id': call_id, 'state': row['state'],
            'status': 'completed', 'effect_class': 'side_effecting', 'host_verified': True,
            'effect': 'applied' if row['state'] == 'succeeded' else 'not_applied',
            'effect_verified': row['state'] == 'succeeded'}, identity=['kernel_probe', call_id, observation])
        for blocker in ledger.view(row['node_id']).active_blockers:
            if blocker.payload.get('ref') == call_id and blocker.payload['kind'] == 'unknown_effect':
                ledger.record(row['node_id'], 'clearance', {'blocker_fact_id': blocker.fact_id,
                    'evidence_ref': receipt.fact_id, 'clearance_kind': 'independent_effect_receipt'},
                    identity=['kernel_probe_clearance', blocker.fact_id, receipt.fact_id])
    return row
