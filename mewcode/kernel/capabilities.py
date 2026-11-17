"""Declared node capabilities are an additional boundary, never a permission bypass."""
import copy
import os
from pathlib import Path
from urllib.parse import urlsplit

from mewcode.kernel.blockers import REGISTRY, canonical_blocker


LIST_FIELDS = ('interpreters', 'network_hosts', 'external_targets', 'write_paths', 'vision_routes',
               'possible_blockers')
FIELDS = frozenset((*LIST_FIELDS, 'command_execution', 'command_prefixes', 'resolvers'))


def _declared_blocker(value, field):
    """Normalize a declared blocker without leaking the registry's KeyError."""
    try:
        return canonical_blocker(value).value
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'{field} contains unknown blocker {value!r}') from exc


def declaration(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError('capabilities must contain only declared capability fields')
    result = {'command_execution': value.get('command_execution', False)}
    if type(result['command_execution']) is not bool:
        raise ValueError('command_execution must be boolean')
    for key in LIST_FIELDS:
        items = value.get(key, [])
        if not isinstance(items, (list, tuple)) or len(items) > 100 or any(
                not isinstance(item, str) or not item.strip() or '\x00' in item for item in items):
            raise ValueError(f'{key} must be a bounded list of nonempty strings')
        result[key] = sorted(set(item.strip() for item in items))
    result['possible_blockers'] = sorted({_declared_blocker(item, 'possible_blockers')
                                          for item in result['possible_blockers']})
    prefixes = value.get('command_prefixes', [])
    if not isinstance(prefixes, (list, tuple)) or len(prefixes) > 100 or any(
            not isinstance(row, (list, tuple)) or not row or any(
                not isinstance(part, str) or not part or '\x00' in part for part in row) for row in prefixes):
        raise ValueError('command_prefixes must be token arrays')
    result['command_prefixes'] = [list(row) for row in prefixes]
    resolvers = value.get('resolvers', {})
    if not isinstance(resolvers, dict) or any(not isinstance(target, str) or not target for target in resolvers.values()):
        raise ValueError('resolvers must map blockers to node IDs or human')
    result['resolvers'] = {_declared_blocker(key, 'resolvers'): target
                           for key, target in resolvers.items()}
    return result


def compile_manifest(flow, *, workspace='', approved=False):
    from mewcode.kernel.events import digest
    nodes = {node.node_id: declaration(node.capabilities) for node in flow.nodes}
    result = {'schema_version': 2, 'workspace': str(Path(workspace).resolve()) if workspace else '',
              'nodes': nodes, 'approved': bool(approved)}
    result['scope_digest'] = digest({'workspace': result['workspace'], 'nodes': nodes})
    return result


def _inside(path, root):
    candidate = Path(path)
    candidate = candidate if candidate.is_absolute() else root / candidate
    candidate = candidate.resolve()
    return candidate


def call_gap(manifest, node_id, tool_name, arguments):
    """Fail closed for new manifests; legacy migration is explicit and separate."""
    if not manifest or manifest.get('schema_version') != 2:
        return None
    node_id = node_id.split('::', 1)[0]
    needs_scope = tool_name in {'Bash', 'WriteFile', 'EditFile', 'WebFetch', 'web_fetch', 'InspectVisualEvidence'}
    if not needs_scope:
        return None
    if tool_name == 'Bash' and arguments.get('network') == 'proxy':
        # A command-prefix grant cannot also grant a different egress origin.
        # Check the declared targets even when an exact command binding exists.
        from mewcode.tools.command_proxy import proxy_origins
        scope = manifest.get('nodes', {}).get(node_id)
        if not manifest.get('approved') or scope is None:
            return '当前节点没有已确认的能力声明。'
        try:
            origins = proxy_origins(arguments.get('proxy_targets', []))
            declared = [urlsplit(target) for target in scope['external_targets']]
            hosts = {item.lower() for item in scope['network_hosts']}
            if any(host.lower() not in hosts or (declared and not any(
                    value.scheme == 'https' and value.hostname == host and (value.port or 443) == port
                    for value in declared)) for host, port in origins):
                return '代理目标超出当前节点已确认的网络与外发范围。'
        except ValueError:
            return '代理目标必须是已确认的 HTTPS 服务源站。'
    from mewcode.kernel.authorization_binding import call_key
    if manifest.get('approved') and call_key(node_id, tool_name, arguments, manifest.get('workspace', '')) in manifest.get('call_bindings', {}):
        return None
    scope = manifest.get('nodes', {}).get(node_id)
    if not manifest.get('approved') or scope is None:
        return '当前节点没有已确认的能力声明。'
    root = Path(manifest['workspace']).resolve()
    if tool_name in {'WriteFile', 'EditFile'}:
        path = _inside(str(arguments.get('file_path') or ''), root)
        if not any(path.is_relative_to(_inside(target, root)) for target in scope['write_paths']):
            return '写入路径超出已确认的授权清单。'
    if tool_name == 'Bash':
        from mewcode.permissions.command_rules import parse_command
        parsed = parse_command(str(arguments.get('command') or ''), 'powershell' if os.name == 'nt' else 'bash')
        if not scope['command_execution'] or not parsed.safe_for_prefix_rules or not parsed.segments:
            return '命令能力未声明，或命令结构不能与授权清单核对。'
        for segment in parsed.segments:
            if segment.redirections or not any(segment.arguments[:len(prefix)] == tuple(prefix)
                                               for prefix in scope['command_prefixes']):
                return '命令超出已确认的命令范围。'
    if tool_name in {'WebFetch', 'web_fetch'}:
        host = urlsplit(str(arguments.get('url') or '')).hostname
        if not host or host.lower() not in {item.lower() for item in scope['network_hosts']}:
            return '网络目标超出已确认的授权清单。'
    if tool_name == 'InspectVisualEvidence' and not scope['vision_routes']:
        return '当前节点未声明看图路线；需要确认外发服务与页面范围。'
    return None


class ManifestGuard:
    def __init__(self, store, run_id, node_id, *, execution_workspace=None, task_authorization_reader=None):
        self.store, self.run_id, self.node_id = store, run_id, node_id
        self.execution_workspace = execution_workspace
        self.task_authorization_reader = task_authorization_reader

    def check(self, tool_name, arguments):
        owner = self.node_id.split('::', 1)[0]
        scoped_read = getattr(self.store, 'read_authorization_state', None)
        record = (scoped_read(self.run_id, owner) if callable(scoped_read)
                  else self.store.load(self.run_id)) or {}
        manifest = record.get('authorization_manifest')
        checked_arguments = arguments
        if (self.execution_workspace and manifest and manifest.get('workspace')
                and tool_name in {'WriteFile', 'EditFile'}):
            execution_root = Path(self.execution_workspace).resolve()
            target = _inside(str(arguments.get('file_path') or ''), execution_root)
            if not target.is_relative_to(execution_root):
                gap = '调查写入路径超出当前隔离工作区。'
            else:
                checked_arguments = {**arguments, 'file_path': str(
                    Path(manifest['workspace']) / target.relative_to(execution_root))}
                gap = call_gap(manifest, self.node_id, tool_name, checked_arguments)
        else:
            gap = call_gap(manifest, self.node_id, tool_name, checked_arguments)
        # Grants and the executable binding must move together. Recheck live
        # grants on every call, including retries after a restart or revocation.
        from mewcode.kernel.authorization_binding import (
            bind_call, clear_call_blockers, reconcile_bindings, request_for_call, coverage,
        )
        outside_snapshot = bool(self.execution_workspace and gap == '调查写入路径超出当前隔离工作区。')
        if manifest and not outside_snapshot:
            def reconcile(value):
                if self.task_authorization_reader is not None:
                    authorization, task_id = self.task_authorization_reader()
                    value['authorization_manifest']['task_authorization'] = authorization.as_dict() if authorization else None
                    value['authorization_manifest']['task_id'] = task_id
                reconcile_bindings(value)
                if call_gap(value.get('authorization_manifest'), owner, tool_name, checked_arguments):
                    bind_call(value, owner, tool_name, checked_arguments)
                clear_call_blockers(value, owner, tool_name, checked_arguments)
            updated = copy.deepcopy(record)
            reconcile(updated)
            if updated != record:
                if callable(scoped_read):
                    self.store.mutate_tool_boundary(self.run_id, owner, reconcile,
                                                    fields=('authorization_manifest',))
                    record = scoped_read(self.run_id, owner)
                else:
                    record = self.store.mutate(self.run_id, reconcile, durable=True)
            else:
                record = updated
            manifest = record.get('authorization_manifest')
            gap = call_gap(manifest, owner, tool_name, checked_arguments)
        if not gap:
            return gap
        # A covered scope means the gap is only the missing capability entry, not
        # a missing user decision: the executor fills it in without asking again.
        request = request_for_call(owner, tool_name, checked_arguments,
                                  str((manifest or {}).get('workspace') or ''))
        kind = 'capability_missing' if request is not None and coverage(record, request).status == 'covered' \
            else 'missing_authorization'

        def save(value):
            from mewcode.experts.node_ledger import ledger_for_run, project_node_view
            ledger = ledger_for_run(value)
            view = ledger.view(owner)
            call = {'tool': tool_name, 'arguments': copy.deepcopy(checked_arguments)}
            cleared_calls = {fact.fact_id for fact in view.history if fact.kind == 'blocker'
                             and fact.payload.get('requested_call') == call}
            last_clearance = next((fact.fact_id for fact in reversed(view.history)
                if fact.kind == 'clearance' and fact.payload.get('blocker_fact_id') in cleared_calls), None)
            ledger.record(self.node_id.split('::', 1)[0], 'blocker', {
                'kind': kind, 'reason': gap, 'gate_kind': 'authorization_extension',
                'requested_call': call,
                'scope_digest': (value.get('authorization_manifest') or {}).get('scope_digest')},
                source='host', identity=['manifest_gap', self.node_id, tool_name, arguments, kind,
                    (value.get('authorizations') or {}).get('version'),
                    (value.get('authorization_manifest') or {}).get('scope_digest'), last_clearance])
            project_node_view(value, self.node_id.split('::', 1)[0])
        if callable(scoped_read):
            self.store.mutate_tool_boundary(self.run_id, owner, save, fields=('authorization_manifest',))
        else:
            self.store.mutate(self.run_id, save, durable=True)
        return gap


def resolver_diagnostics(flow):
    adjacency = {node.node_id: set() for node in flow.nodes}
    for edge in flow.edges:
        adjacency[edge.from_node].add(edge.to_node)
    diagnostics = []
    for node in flow.nodes:
        scope = declaration(node.capabilities)
        if scope is None:
            continue
        reachable, pending = {node.node_id}, [node.node_id]
        while pending:
            for target in adjacency[pending.pop()] - reachable:
                reachable.add(target)
                pending.append(target)
        for blocker in scope['possible_blockers']:
            owner = REGISTRY[canonical_blocker(blocker)].owner
            target = scope['resolvers'].get(blocker)
            # Host role resolvers are run-wide; a declared graph resolver must
            # actually be reachable through a dependency or failure edge.
            valid = (target == 'human' or (target in reachable if target else owner in {
                'human', 'executor', 'planner', 'verifier', 'reviewer', 'arbiter'}))
            if not valid:
                diagnostics.append({'code': 'unreachable_blocker_resolver', 'path': f'nodes/{node.node_id}',
                    'entry_ids': [node.node_id], 'blocker': blocker, 'resolver': target,
                    'message': f'节点 {node.node_id} 的阻塞 {blocker} 无法到达解决者 {target}。'})
    return diagnostics
