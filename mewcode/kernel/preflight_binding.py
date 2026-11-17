"""Prepare flow capability declarations from existing, verifiable authority."""
from __future__ import annotations

import copy
import time
from pathlib import Path

from mewcode.kernel.capabilities import compile_manifest, declaration


_READ_ONLY_TOOLS = frozenset({'ReadFile', 'Glob', 'Grep'})
_WRITE_TOOLS = frozenset({'WriteFile', 'EditFile'})
_NETWORK_TOOLS = frozenset({'WebFetch', 'web_fetch'})
_UNBOUND_LEDGER_IDENTITIES = frozenset({'epoch', 'case_id', 'round', 'operation_digest'})


def _validated_task_authorization(value, task_id):
    if value is None:
        return None, ''
    from mewcode.permissions.task_authorization import TaskAuthorization

    try:
        raw = value.as_dict() if callable(getattr(value, 'as_dict', None)) else value
        if not isinstance(raw, dict):
            return None, ''
        authorization = TaskAuthorization.from_record(copy.deepcopy(raw))
    except (TypeError, ValueError, OSError):
        return None, ''
    bound_task_id = str(task_id or authorization.task_id)
    if not bound_task_id or authorization.task_id != bound_task_id:
        return None, ''
    return authorization, bound_task_id


def _node_allows_any(node, tools):
    denied = set(node.deny_tools or ())
    if node.tools is None:
        return bool(set(tools) - denied)
    configured = set(node.tools)
    if '*' in configured:
        return bool(set(tools) - denied)
    return bool((configured - denied) & set(tools))


def _task_authorization_can_bind(node, authorization):
    if authorization is None:
        return False
    write_tools = set(authorization.allowed_tools) & _WRITE_TOOLS
    network_tools = set(authorization.allowed_tools) & _NETWORK_TOOLS
    write = authorization.write_directories and _node_allows_any(node, write_tools)
    network = authorization.network_hosts and _node_allows_any(node, network_tools)
    # TaskAuthorization.allowed_behaviors has no exact command prefix, so it
    # cannot initialize Bash capability here.
    return bool(write or network)


def _ledger_can_bind(record, node):
    """Recognize broad live grants without dropping any call identity limit.

    These grants only justify an empty declaration. The runtime binder still
    checks the exact path, host, command, node, and current grant on each call.
    """
    node_id = node.node_id
    ledger = record.get('authorizations') or {}
    rows = ledger.get('entries', []) if isinstance(ledger, dict) else []
    now = time.time()
    for row in rows:
        if not isinstance(row, dict) or row.get('enforceable') is not True:
            continue
        if row.get('revoked_at') is not None:
            continue
        try:
            expires_at = row.get('expires_at')
            if expires_at is not None and float(expires_at) <= now:
                continue
        except (TypeError, ValueError):
            continue
        scope = row.get('scope')
        if not isinstance(scope, dict):
            continue
        # The operation request has no current recovery episode identity.
        # Keep those scopes null unless an already-saved exact call binding is
        # independently revalidated below.
        if _UNBOUND_LEDGER_IDENTITIES & scope.keys():
            continue
        scoped_tools = scope.get('tools')
        if scoped_tools is not None and (not isinstance(scoped_tools, list)
                                         or any(not isinstance(tool, str) for tool in scoped_tools)):
            continue
        node_ids = scope.get('node_ids')
        if node_ids is not None and (not isinstance(node_ids, list) or node_id not in node_ids):
            continue
        operations = scope.get('operation_classes')
        if not isinstance(operations, list):
            continue
        def tools_match(candidates):
            eligible = set(candidates)
            if scoped_tools is not None:
                eligible.intersection_update(scoped_tools)
            return any(_node_allows_any(node, {tool}) for tool in eligible)

        if ('write_outside_workspace' in operations and
                isinstance(scope.get('paths'), list) and scope['paths'] and
                tools_match(_WRITE_TOOLS)):
            return True
        if ('network' in operations and
                isinstance(scope.get('hosts'), list) and scope['hosts'] and
                tools_match(_NETWORK_TOOLS)):
            return True
        if ('run_command_prefix' in operations and
                isinstance(scope.get('command_prefix'), list) and scope['command_prefix'] and
                all(isinstance(part, str) and part for part in scope['command_prefix']) and
                tools_match({'Bash'})):
            return True
    return False


def _authorized_vision_routes(record, node_id, node):
    """Return routes with live page-image authority for this exact node/tool."""
    if not _node_allows_any(node, {'InspectVisualEvidence'}):
        return []
    from mewcode.experts.authorization_ledger import covers, entries

    routes = set()
    for row in entries(record):
        scope = row.get('scope') if isinstance(row, dict) else None
        operations = scope.get('operation_classes') if isinstance(scope, dict) else None
        content_classes = scope.get('content_classes') if isinstance(scope, dict) else None
        if (not isinstance(scope, dict)
                or not isinstance(operations, list) or 'external_send' not in operations
                or not isinstance(content_classes, list) or 'page_image' not in content_classes):
            continue
        candidates = scope.get('model_routes')
        if not isinstance(candidates, list):
            continue
        for route in candidates:
            if (not isinstance(route, str) or not route.strip() or route != route.strip()
                    or '*' in route):
                continue
            request = {'operation_class': 'external_send', 'scope': {
                'model_routes': [route], 'content_classes': ['page_image'],
                'node_ids': [node_id], 'tools': ['InspectVisualEvidence'],
            }}
            # covers gives denials precedence and checks expiry, revocation,
            # node/tool restrictions, and any epoch/case/round/digest limits.
            # The omitted episode identities intentionally make those scoped
            # approvals ineligible for preflight route availability.
            try:
                result = covers(record, request)
            except (KeyError, TypeError, ValueError):
                continue
            if result.status == 'covered':
                routes.add(route)
    return sorted(routes)


def _scope_contains(existing, required):
    """Whether an approved extension still includes the current declaration."""
    if required is None:
        return True
    if existing is None:
        return False
    try:
        old = declaration(existing)
        current = declaration(required)
    except (TypeError, ValueError):
        return False
    if current['command_execution'] and not old['command_execution']:
        return False
    for field in ('interpreters', 'network_hosts', 'external_targets', 'write_paths',
                  'vision_routes', 'possible_blockers'):
        if not set(current[field]).issubset(old[field]):
            return False
    for prefix in current['command_prefixes']:
        if not any(prefix[:len(old_prefix)] == old_prefix for old_prefix in old['command_prefixes']):
            return False
    return all(old['resolvers'].get(key) == value for key, value in current['resolvers'].items())


def _has_extend_manifest_receipt(record, node_id, scope):
    """Check the exact live gate_answer receipt written by repairs.extend_manifest."""
    from mewcode.experts.authorization_ledger import covers
    from mewcode.kernel.events import digest

    wanted_scope = {'operation_classes': ['other'], 'node_ids': [node_id],
                    'operation_digest': digest(scope)}
    request = {'operation_class': 'other', 'scope': wanted_scope}
    entries = (record.get('authorizations') or {}).get('entries', [])
    for row in entries if isinstance(entries, list) else ():
        if (not isinstance(row, dict) or row.get('kind') != 'gate_answer'
                or row.get('enforceable') is not True or not row.get('source_ref')
                or row.get('scope') != wanted_scope):
            continue
        try:
            receipt = covers({'authorizations': {'entries': [row]}}, request)
            effective = covers(record, request)
        except (KeyError, TypeError, ValueError):
            continue
        if (receipt.status == 'covered' and receipt.auth_id == row.get('auth_id')
                and effective.status == 'covered'):
            return True
    return False


def _same_workspace(old, workspace):
    if not isinstance(old, dict) or old.get('schema_version') != 2:
        return False
    old_workspace = str(old.get('workspace') or '')
    try:
        normalized_old = str(Path(old_workspace).resolve()) if old_workspace else ''
    except (OSError, RuntimeError, ValueError):
        return False
    return normalized_old == workspace


def _preserve_call_bindings(record, manifest, old_manifest):
    if not old_manifest.get('approved'):
        return
    old_bindings = old_manifest.get('call_bindings')
    if isinstance(old_bindings, dict) and old_bindings:
        candidates = {}
        for key, row in old_bindings.items():
            if not isinstance(row, dict) or row.get('node_id') not in manifest['nodes']:
                continue
            request = row.get('request')
            scope = request.get('scope') if isinstance(request, dict) else None
            if (not isinstance(scope, dict) or scope.get('node_ids') != [row['node_id']]
                    or scope.get('operation_digest') != key):
                continue
            candidates[key] = copy.deepcopy(row)
        if candidates:
            manifest['call_bindings'] = candidates
    from mewcode.kernel.authorization_binding import reconcile_bindings

    candidate = dict(record)
    candidate['authorization_manifest'] = manifest
    try:
        reconcile_bindings(candidate)
    except (KeyError, TypeError, ValueError, AttributeError):
        manifest.pop('call_bindings', None)
        return


def prepare_manifest(record, flow, workspace, task_authorization=None, task_id=''):
    """Compile and update the run manifest without creating new authorization.

    Missing node declarations become empty only for explicitly read-only/gate
    nodes, host-restricted loop checkpoints, valid task scopes, existing live
    ledger grants, or revalidated exact call bindings. Exact write/network/
    command decisions remain runtime-bound.
    """
    from mewcode.experts.flow import ExpertFlowSpec

    flow = ExpertFlowSpec.from_value(flow)
    manifest = compile_manifest(flow, workspace=workspace, approved=True)
    canonical_workspace = manifest['workspace']
    authorization, bound_task_id = _validated_task_authorization(task_authorization, task_id)
    if authorization is None:
        manifest.pop('task_authorization', None)
        manifest.pop('task_id', None)
    else:
        manifest['task_authorization'] = authorization.as_dict()
        manifest['task_id'] = bound_task_id

    old_manifest = record.get('authorization_manifest') or {}
    same_workspace = _same_workspace(old_manifest, canonical_workspace)
    if same_workspace and old_manifest.get('approved') is True:
        old_nodes = old_manifest.get('nodes')
        if isinstance(old_nodes, dict):
            for node in flow.nodes:
                previous = old_nodes.get(node.node_id)
                current = manifest['nodes'].get(node.node_id)
                if (previous is None or previous == current or
                        not _scope_contains(previous, current) or
                        not _has_extend_manifest_receipt(record, node.node_id, previous)):
                    continue
                manifest['nodes'][node.node_id] = declaration(previous)
        _preserve_call_bindings(record, manifest, old_manifest)

    node_kinds = flow.meta.get('node_kinds') or {}
    # The loop runtime gives auditor checkpoints only evidence readers and
    # SubmitLoopDecision. They need no external capability just to evaluate
    # current inputs. An empty scope still denies every scoped side effect.
    loop_checkpoints = {loop['check_node_id'] for loop in flow.meta.get('loops') or []}
    for node in flow.nodes:
        node_id = node.node_id
        if manifest['nodes'].get(node_id) is not None:
            continue
        vision_routes = _authorized_vision_routes(record, node_id, node)
        if vision_routes:
            manifest['nodes'][node_id] = declaration({'vision_routes': vision_routes})
            continue
        explicit_read_only = (node.tools is not None and
                              (not node.tools or set(node.tools).issubset(_READ_ONLY_TOOLS)))
        explicit_gate = node_kinds.get(node_id) == 'gate'
        host_checkpoint = node.role == 'auditor' and node_id in loop_checkpoints
        has_binding = any(isinstance(row, dict) and row.get('node_id') == node_id
                          for row in (manifest.get('call_bindings') or {}).values())
        if (explicit_gate or explicit_read_only or host_checkpoint or has_binding or
                _task_authorization_can_bind(node, authorization) or
                _ledger_can_bind(record, node)):
            manifest['nodes'][node_id] = declaration({})

    from mewcode.kernel.authorization_binding import refresh_digest
    refresh_digest(manifest)
    record['authorization_manifest'] = manifest
    return manifest
