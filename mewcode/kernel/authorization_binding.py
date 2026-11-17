"""Bind host-confirmed authority to one concrete call, without creating grants."""
import copy
from types import SimpleNamespace

from mewcode.experts.authorization_ledger import Coverage, covers
from mewcode.kernel.events import digest


def canonical_arguments(tool, arguments, workspace):
    from pathlib import Path
    result = dict(arguments)
    if tool in {'WriteFile', 'EditFile'} and workspace and arguments.get('file_path'):
        path = Path(str(arguments['file_path']))
        result['file_path'] = str((path if path.is_absolute() else Path(workspace) / path).resolve())
    return result


def call_key(node_id, tool, arguments, workspace=''):
    return digest([node_id, tool, canonical_arguments(tool, arguments, workspace)])


def request_for_call(node_id, tool, arguments, workspace):
    from mewcode.kernel.escalation import authorization_request_for
    request = authorization_request_for(SimpleNamespace(payload={
        'requested_call': {'tool': tool, 'arguments': arguments}}), workspace=workspace)
    if request is not None:
        request['scope'].update(node_ids=[node_id], tools=[tool], operation_digest=call_key(node_id, tool, arguments, workspace))
    return request


def coverage(record, request):
    result = covers(record, request)
    # A directory/host grant can cover a concrete call. An exact-call grant cannot
    # match this fallback because the ledger requires its operation_digest.
    broad = copy.deepcopy(request)
    broad['scope'].pop('operation_digest', None)
    broader = covers(record, broad)
    if result.status == 'denied' or broader.status == 'denied':
        return Coverage('denied', result.auth_id if result.status == 'denied' else broader.auth_id)
    return result if result.status == 'covered' else broader


def task_coverage(record, tool, arguments):
    from pathlib import Path
    from mewcode.permissions.task_authorization import TaskAuthorization
    manifest = record.get('authorization_manifest') or {}
    saved = manifest.get('task_authorization')
    if not saved:
        return Coverage('uncovered')
    try:
        scope = TaskAuthorization.from_record(saved)
        checked = dict(arguments)
        if tool in {'WriteFile', 'EditFile'}:
            path = Path(str(checked.get('file_path') or ''))
            checked['file_path'] = str((path if path.is_absolute() else Path(manifest['workspace']) / path).resolve())
        target = SimpleNamespace(name=tool, category='write' if tool in {'WriteFile', 'EditFile'} else 'read')
        if scope.covers(target, checked, task_id=manifest.get('task_id', '')):
            return Coverage('covered', 'task:' + scope.version)
    except (ValueError, OSError):
        pass
    return Coverage('uncovered')


def bind_call(record, node_id, tool, arguments):
    """Return true only after a live grant has been checked and durably bindable."""
    manifest = record.get('authorization_manifest') or {}
    if not manifest.get('approved') or node_id not in manifest.get('nodes', {}):
        return False
    request = request_for_call(node_id, tool, arguments, manifest.get('workspace', ''))
    if request is None:
        return False
    covered = coverage(record, request)
    if covered.status not in {'covered', 'denied'}:
        covered = task_coverage(record, tool, arguments)
    key = call_key(node_id, tool, arguments, manifest.get('workspace', ''))
    bindings = manifest.setdefault('call_bindings', {})
    if covered.status != 'covered':
        bindings.pop(key, None)
        return False
    from mewcode.kernel.capabilities import declaration
    if manifest['nodes'][node_id] is None:
        manifest['nodes'][node_id] = declaration({})
    # Only target data needed for checking a task scope is retained, not file
    # contents or command text. The operation digest binds the original call.
    bindings[key] = {'node_id': node_id, 'request': request, 'auth_id': covered.auth_id,
                     'tool': tool, 'target': {k: v for k, v in arguments.items() if k in {'file_path', 'url'}}}
    refresh_digest(manifest)
    return True


def refresh_digest(manifest):
    manifest['scope_digest'] = digest({key: value for key, value in manifest.items() if key != 'scope_digest'})


def reconcile_bindings(record):
    manifest = record.get('authorization_manifest') or {}
    bindings = manifest.get('call_bindings', {})
    for key, row in list(bindings.items()):
        covered = coverage(record, row['request'])
        if covered.status not in {'covered', 'denied'} and row.get('auth_id', '').startswith('task:'):
            covered = task_coverage(record, row['tool'], row.get('target', {}))
        if (row.get('node_id') not in manifest.get('nodes', {})
                or covered.status != 'covered'):
            del bindings[key]
    refresh_digest(manifest)


def clear_call_blockers(record, node_id, tool, arguments):
    """Only clear manifest facts after the same concrete call can pass."""
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    from mewcode.kernel.capabilities import call_gap
    if call_gap(record.get('authorization_manifest'), node_id, tool, arguments):
        return
    ledger = ledger_for_run(record)
    workspace = record['authorization_manifest'].get('workspace', '')
    for fact in ledger.view(node_id).active_blockers:
        old_call = fact.payload.get('requested_call') or {}
        if (fact.payload.get('kind') in {'missing_authorization', 'capability_missing'}
                and old_call.get('tool') == tool
                and canonical_arguments(tool, old_call.get('arguments') or {}, workspace)
                    == canonical_arguments(tool, arguments, workspace)):
            ledger.record(node_id, 'clearance', {'blocker_fact_id': fact.fact_id,
                'clearance_type': 'explicit_permission_receipt',
                'operation_digest': call_key(node_id, tool, arguments, workspace)},
                source='host', identity=['bound_call', fact.fact_id,
                                         record['authorization_manifest']['scope_digest']])
    project_node_view(record, node_id)
