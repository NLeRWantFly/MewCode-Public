"""Explicit, exact-scope maintenance decisions. Assumptions are not receipts."""
import copy


def adjudicate(record, *, node_id, call_id, disposition, source_ref):
    from mewcode.kernel.events import digest
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    if disposition not in {'assume_not_applied', 'assume_applied'} or not source_ref:
        raise ValueError('unknown calls need an explicit scoped user disposition')
    row = record.get('kernel_calls', {}).get(call_id)
    if not row or row['node_id'] != node_id or row['state'] != 'unknown':
        raise ValueError('the selected call is not an unknown call of this node')
    decision = {'source': 'user', 'disposition': disposition, 'source_ref': source_ref,
                'call_digest': digest([call_id, row['invocation'], row['input_revision']])}
    if row.get('user_adjudication'):
        if row['user_adjudication'] != decision:
            raise ValueError('this unknown call already has a different user disposition')
        return {'status': 'no_effect', 'call_id': call_id}
    row['user_adjudication'] = decision
    ledger = ledger_for_run(record)
    evidence = ledger.record(node_id, 'receipt', {'request_id': call_id, 'status': 'adjudicated',
        'user_adjudication': decision, 'original_status': 'outcome_unknown', 'effect_verified': False},
        source='user', identity=['user_effect_disposition', call_id, source_ref])
    for fact in ledger.view(node_id).active_blockers:
        if fact.payload.get('kind') == 'unknown_effect' and fact.payload.get('ref') == call_id:
            ledger.record(node_id, 'clearance', {'blocker_fact_id': fact.fact_id,
                'clearance_type': 'explicit_user_effect_disposition', 'evidence_fact_id': evidence.fact_id,
                'disposition': disposition}, source='host', identity=['user_disposition_clearance', fact.fact_id, source_ref])
    stage = record.setdefault('node_states', {}).setdefault(node_id, {})
    for receipt in stage.get('command_receipts', []):
        if receipt.get('request_id') == call_id:
            receipt.update(status='adjudicated', original_status=receipt.get('status'), user_adjudication=decision)
    stage['blockers'] = [fact for fact in stage.get('blockers', []) if fact.get('ref') != call_id]
    project_node_view(record, node_id)
    return {'status': 'adjudicated', 'call_id': call_id, 'disposition': disposition, 'effect_verified': False}


def extend_manifest(record, *, node_id, capabilities, source_ref):
    from mewcode.kernel.capabilities import declaration
    from mewcode.kernel.events import digest
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    from mewcode.experts.authorization_ledger import record_authorization
    scope = declaration(capabilities)
    manifest = record.get('authorization_manifest')
    if not source_ref or not manifest or manifest.get('schema_version') != 2 or scope is None:
        raise ValueError('authorization expansion requires a concrete manifest and confirmed source')
    if manifest['nodes'].get(node_id) == scope:
        return {'status': 'no_effect', 'node_id': node_id}
    manifest['nodes'][node_id] = copy.deepcopy(scope)
    manifest['scope_digest'] = digest({'workspace': manifest['workspace'], 'nodes': manifest['nodes']})
    auth = record_authorization(record, kind='gate_answer', scope={
        'operation_classes': ['other'], 'node_ids': [node_id], 'operation_digest': digest(scope)},
        quote='明确确认维护预览展示的节点能力范围', source_ref=source_ref, host_verified=True)
    ledger = ledger_for_run(record)
    for fact in ledger.view(node_id).active_blockers:
        if fact.payload.get('kind') == 'missing_authorization' and fact.payload.get('requested_call'):
            from mewcode.kernel.capabilities import call_gap
            call = fact.payload['requested_call']
            if not call_gap(manifest, node_id, call['tool'], call['arguments']):
                ledger.record(node_id, 'clearance', {'blocker_fact_id': fact.fact_id,
                    'clearance_type': 'explicit_permission_receipt', 'auth_id': auth['auth_id']},
                    identity=['manifest_extension', fact.fact_id, source_ref])
    project_node_view(record, node_id)
    return {'status': 'authorization_extended', 'node_id': node_id, 'scope_digest': manifest['scope_digest']}


def current_blocker_actions(record, node_id):
    from mewcode.experts.node_ledger import ledger_for_run
    actions = []
    for fact in ledger_for_run(copy.deepcopy(record)).view(node_id).active_blockers:
        if fact.payload['kind'] == 'unknown_effect':
            actions.append({'action': 'adjudicate_unknown', 'node_id': node_id, 'call_id': fact.payload.get('ref'),
                'gate_kind': 'irreversible', 'options': [
                    {'value': 'assume_not_applied', 'label': '按未发生重做'},
                    {'value': 'assume_applied', 'label': '按已发生继续'}], 'requires_confirmation': True})
        elif fact.payload['kind'] == 'missing_authorization':
            actions.append({'action': 'extend_authorization', 'node_id': node_id,
                'requested_call': fact.payload.get('requested_call'), 'gate_kind': 'authorization_extension',
                'requires_confirmation': True})
    return actions
