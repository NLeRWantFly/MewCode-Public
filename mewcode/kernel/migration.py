"""Explicit, idempotent legacy import. Original bytes never become mutable runs."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import zipfile


def migrate(store, source, *, workspace='', now=None):
    from mewcode.kernel.events import canonical, digest
    from mewcode.orchestration.write_lease import WriteLeaseStore
    from mewcode.experts.flow import ExpertFlowSpec
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    from mewcode.kernel.capabilities import compile_manifest
    if source.get('kernel_revision'):
        return copy.deepcopy(source)
    source_id = source.get('run_id')
    store._path(source_id)  # Validate identity before accessing any archive path.
    source_hash = digest(source)
    run_id = digest(['legacy_import', source_id, source_hash])[:32]
    directory = store.root / 'legacy-archives'
    directory.mkdir(exist_ok=True)
    with WriteLeaseStore(directory / 'writer.json', sandbox=None)._locked():
        existing = store.load(run_id)
        if existing and existing.get('migration', {}).get('source_digest') != source_hash:
            raise ValueError('legacy destination identity conflict')
        if existing and existing.get('migration', {}).get('status') == 'complete':
            return existing
        archive = directory / (source_hash + '.zip')
        if not archive.is_file():
            fd, pending = tempfile.mkstemp(prefix='.import-', suffix='.zip', dir=directory)
            os.close(fd)
            try:
                with zipfile.ZipFile(pending, 'w', compression=zipfile.ZIP_DEFLATED) as output:
                    output.writestr('source.json', canonical(source))
                    paths = [store._path(source_id), store._event_log_path(source_id)]
                    paths += list((store.root / 'blobs' / source_id).glob('*.json'))
                    for path in paths:
                        if path.is_file():
                            if not path.resolve().is_relative_to(store.root.resolve()):
                                raise ValueError('legacy archive source escaped run storage')
                            output.write(path, 'original/' + path.relative_to(store.root).as_posix())
                with open(pending, 'r+b') as stream:
                    os.fsync(stream.fileno())
                os.replace(pending, archive)
                archive.chmod(0o444)
            finally:
                Path(pending).unlink(missing_ok=True)
        with zipfile.ZipFile(archive) as archived:
            if digest(json.loads(archived.read('source.json'))) != source_hash or archived.testzip() is not None:
                raise ValueError('legacy archive integrity mismatch')
        from mewcode.experts.flow import DEFAULT_EXPERT_FLOW
        from mewcode.expert_runs import LEGACY_NODE_IDS
        flow = ExpertFlowSpec.from_value(source.get('locked_flow') or DEFAULT_EXPERT_FLOW)
        metadata = {'source_run_id': source_id, 'source_digest': source_hash, 'archive': str(archive),
                    'status': 'building', 'model_calls': 0}
        if existing is None:
            store.create(run_id=run_id, conversation_id=source.get('conversation_id') or 'fixture',
                task=source.get('task') or '', attachments=source.get('attachments') or [],
                profile=source.get('locked_profile') or {}, strategy=source.get('strategy') or 'stable', flow=flow,
                initial_state={'status': 'paused', 'migration': metadata})
        states = source.get('node_states') or source.get('stages') or {}
        if not source.get('locked_flow') and int(source.get('version') or 0) < 2:
            states = {LEGACY_NODE_IDS.get(key, key): value for key, value in states.items()}
        completed = {key: copy.deepcopy(stage) for key, stage in states.items()
                     if stage.get('status') == 'completed' or stage.get('passed') is True}
        first_unfinished = next((node.node_id for node in flow.nodes if node.node_id not in completed), '')
        current_time = time.time() if now is None else now
        def initialize(record):
            record['migration'] = {**metadata, 'status': 'complete', 'reused_node_ids': sorted(completed)}
            record.update(status='paused', active_node_ids=[], stage_cursor=first_unfinished,
                status_reason='旧运行已只读归档；复用明确成果，从首个未完成节点继续。',
                run_inputs=copy.deepcopy(source.get('run_inputs') or {}), node_states=completed)
            # Completed histories remain in the immutable source archive;
            # active attempts need artifacts and handoffs, never old prompts.
            for stage in record['node_states'].values():
                for field in ('messages', 'pending_interaction', 'pending_permission', 'scheduler_recovery'):
                    stage.pop(field, None)
            record['authorizations'] = {'version': 1, 'entries': [copy.deepcopy(row) for row in
                (source.get('authorizations') or {}).get('entries', []) if row.get('enforceable') is True
                and row.get('revoked_at') is None and (row.get('expires_at') is None or row['expires_at'] > current_time)]}
            record['authorization_manifest'] = compile_manifest(flow, workspace=workspace, approved=False)
            ledger = ledger_for_run(record)
            for node_id, stage in states.items():
                if node_id in completed:
                    ledger.set_phase(node_id, 'done', reason='explicit_legacy_import')
                for fact in stage.get('known_facts', []):
                    if isinstance(fact, dict) and fact.get('host_verified') is True:
                        ledger.record(node_id, 'artifact', fact, source='host', identity=['import_fact', digest(fact)])
                for receipt in stage.get('command_receipts', []):
                    status = receipt.get('status')
                    definite = status in {'completed', 'exited', 'failed'} and receipt.get('command_started') is not False and (
                        receipt.get('exit_code') is not None or receipt.get('effect_verified') is True)
                    if definite or status == 'not_started' or receipt.get('command_started') is False:
                        ledger.record(node_id, 'receipt', copy.deepcopy(receipt), source='host',
                                      identity=['import_receipt', digest(receipt)])
                        import_call(record, node_id, receipt, source_hash)
                    elif status in {'outcome_unknown', 'in_flight', 'effect_partial'}:
                        if import_call(record, node_id, receipt, source_hash):
                            continue
                        # A redacted or legacy label is not dispatch evidence.
                        # Quarantine it and ask for evidence before a new call.
                        ledger.record(node_id, 'blocker', {'kind': 'insufficient_evidence',
                            'reason': '历史调用缺少可绑定的完整派发与效果证据，需要先核对存档。',
                            'legacy_receipt_ref': receipt.get('request_id'), 'archive_digest': source_hash},
                            source='host', identity=['legacy_quarantine', digest(receipt)])
                        record.setdefault('legacy_unresolved_effects', []).append({'node_id': node_id,
                            'receipt': copy.deepcopy(receipt), 'source_digest': source_hash})
                project_node_view(record, node_id)
            record['stages'] = record['node_states']
            from mewcode.kernel.briefs import brief
            record['migration']['brief'] = brief(record, first_unfinished) if first_unfinished else ''
        return store.mutate(run_id, initialize, durable=True)


def import_call(record, node_id, receipt, source_hash):
    from mewcode.kernel.calls import propose, transition
    from mewcode.kernel.events import digest
    request = receipt.get('request') or {}
    if not isinstance(request, dict):
        return False
    name = request.get('tool') or request.get('tool_name')
    arguments = request.get('arguments')
    if name is None and isinstance(request.get('command'), str):
        name, arguments = 'Bash', {key: request[key] for key in ('command', 'timeout') if key in request}
    if name not in {'Bash', 'WriteFile', 'EditFile'} or not isinstance(arguments, dict):
        return False
    if receipt.get('command_started') is not True:
        return False
    invocation = {'tool': name, 'arguments': copy.deepcopy(arguments)}
    if name == 'WriteFile':
        invocation['arguments'].setdefault('encoding', 'utf-8')
        invocation['arguments'].setdefault('mode', None)
    row = propose(record, node_id, receipt.get('input_revision') or source_hash, invocation)
    transition(row, 'reviewed')
    transition(row, 'dispatched', evidence={'executor_accepted': True, 'source': 'legacy_host_receipt', 'source_digest': source_hash})
    definite = receipt.get('exit_code') is not None and receipt.get('status') in {'completed', 'exited', 'failed'}
    state = ('failed' if receipt['exit_code'] != 0 else 'succeeded') if definite else 'unknown'
    transition(row, state, evidence={'host_verified': True, 'command_started': True, 'source_digest': source_hash})
    row['origin'] = 'legacy'
    row['import_scope'] = digest([record.get('task'), record.get('run_inputs')])
    row['result'] = {'output': str(receipt.get('output') or receipt.get('stdout') or '') + str(receipt.get('stderr') or ''),
                     'is_error': state != 'succeeded', 'host_observation': {'legacy_host_receipt': copy.deepcopy(receipt)}}
    if state == 'unknown':
        from mewcode.experts.node_ledger import ledger_for_run
        ledger_for_run(record).record(node_id, 'blocker', {'kind': 'unknown_effect', 'ref': row['call_id'],
            'reason': '原运行有明确的派发证据，需要只读对账或人工核销。'}, identity=['import_unknown', row['call_id']])
    return True
