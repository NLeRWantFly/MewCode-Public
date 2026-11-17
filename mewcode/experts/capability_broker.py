"""Bind configured capabilities without interpreting registration as authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from mewcode.experts.authorization_ledger import covers
from mewcode.model_catalog import canonicalize_selectable_model_id
from mewcode.model_registry import ModelRef
from mewcode.permissions.review_evidence import supports_vision

ALTERNATIVES = [
    {'kind': 'alt_extraction', 'method': '提取 PDF 文本层并保留待视觉核验页'},
    {'kind': 'request_input', 'method': '请用户提供样本页的人工核验结果'},
    {'kind': 'capability_probe', 'method': '只读核验配置与已挂载能力',
     'strategy': {'kind': 'capability_probe', 'tools': ['ProbeCapabilities'], 'target': 'configured_routes'},
     'verification': {'probe': '只读能力清单', 'expected_evidence': '宿主保存的能力观察',
                      'tool': 'ProbeCapabilities', 'arguments': {}}},
]


@dataclass(frozen=True)
class CapabilityRequest:
    capability: str
    node_id: str
    scope: dict = field(default_factory=lambda: {'content_classes': ['page_image']})
    authorization_node_id: str = ''


@dataclass
class BindingResult:
    status: str
    route: dict = field(default_factory=dict)
    authority_required: list = field(default_factory=list)
    alternatives: list = field(default_factory=list)
    probe_success: bool = False
    resolver: object = None


def model_inventory(registry_service):
    rows = []
    for provider in getattr(registry_service, 'records', lambda: [])():
        declared = (getattr(provider, 'advertised_models', None) or getattr(provider, 'models', [])
                    or [getattr(provider.provider, 'model', '')])
        for model in dict.fromkeys(canonicalize_selectable_model_id(item) for item in declared):
            if not model or model == 'codex-auto-review':
                continue
            try:
                resolved = registry_service.resolve(ModelRef(provider.key, model))
                client = resolved.client
                review_only = getattr(client, 'review_only', False) is True
                is_jev = resolved.protocol == 'typesafe' or model.lower() == 'jev'
                vision = 'vision' in resolved.capabilities and not review_only and not is_jev
                external = urlparse(str(getattr(resolved.config, 'base_url', ''))).hostname not in {'localhost', '127.0.0.1', '::1'}
                rows.append({'route_key': provider.key + '/' + model, 'provider_key': provider.key,
                    'model_id': model, 'modalities': {'vision': vision}, 'review_only': review_only,
                    'is_jev': is_jev, 'external': external, 'available': True})
            except Exception:
                rows.append({'route_key': provider.key + '/' + model, 'provider_key': provider.key,
                    'model_id': model, 'modalities': {'vision': 'unknown'}, 'review_only': False,
                    'is_jev': model.lower() == 'jev', 'external': True, 'available': False})
    return rows


def inventory(session, registry_service, record, *, node=None):
    agent = getattr(session, 'agent', session)
    registry = getattr(agent, 'registry', None)
    tools = []
    for tool in getattr(registry, 'list_tools', lambda: [])():
        enabled = registry.is_enabled(tool.name)
        node_tools = getattr(node, 'tools', None)
        mounted = enabled and tool.name not in getattr(node, 'deny_tools', ()) and (node_tools is None or '*' in node_tools or tool.name in node_tools)
        usable = mounted
        reason = '' if enabled else '节点工具策略禁止'
        if tool.name == 'InspectVisualEvidence':
            resolver = getattr(tool, 'vision_resolver', None)
            try:
                usable = mounted and resolver is not None and supports_vision(*resolver())
                reason = '' if usable else '没有已绑定且获授权的视觉路线'
            except Exception as exc:
                usable, reason = False, type(exc).__name__
        tools.append({'name': tool.name, 'registered': True, 'deferred': tool.should_defer,
            'mounted_in_node': mounted, 'usable': usable, 'unusable_reason': reason})
    models = model_inventory(registry_service)
    known = {(row['provider_key'], row['model_id']) for row in models}
    bindings = record.get('locked_models') or {}
    complete = registry_service is not None and all(
        (ref.get('provider_key'), ref.get('model_id')) in known for ref in bindings.values())
    return {'tools': tools, 'models': models, 'models_complete': complete,
        'env': record.get('runtime_diagnostics', {}), 'routes': record.get('locked_models', {}),
        'alternatives': ALTERNATIVES}


def bind_command(session):
    """Read native readiness and original host scope without starting a command."""
    agent = getattr(session, 'agent', session)
    checker = getattr(agent, 'permission_checker', None)
    registry = getattr(agent, 'registry', None)
    if registry is None or registry.get('Bash') is None or not registry.is_enabled('Bash'):
        return BindingResult('capability_absent', alternatives=[{'method': '原节点未挂载可用 Bash；先修复宿主工具配置。'}])
    try:
        from mewcode.sandbox.native_gate import native_gate_status
        from mewcode.sandbox.lpac_runtime import NativeScope
        gate = native_gate_status()
        if not gate.ready:
            return BindingResult('capability_absent', route={'capability': 'command', 'reason': gate.reason},
                alternatives=[{'method': '原生命令环境不可用：' + gate.reason}])
        scope = NativeScope.from_checker(checker)
        if not scope.current():
            raise ValueError('原节点的宿主命令任务身份或工作区已变化')
        return BindingResult('bound', probe_success=True, route={'capability': 'command',
            'workspace': str(scope.workspace), 'task_id': scope.task_id, 'publication': scope.publication_mode,
            'native_gate': gate.state.value, 'execution_confirmed': False,
            'rule': '仅确认宿主执行入口；具体命令仍须独立审查、创建隔离事务并由真实回执核验。'})
    except (OSError, ValueError, AttributeError, RuntimeError) as exc:
        return BindingResult('capability_absent', route={'capability': 'command', 'reason': str(exc)},
            alternatives=[{'method': '原节点命令范围预检未通过：' + str(exc)}])


def bind(request, *, session, registry_service, record, current_ref=None, review_settings=None, grant_store=None):
    if request.capability not in {'vision', 'InspectVisualEvidence'}:
        return BindingResult('capability_absent', alternatives=[{'kind': 'request_input', 'method': '提供可核验的替代材料'}])
    rows = [r for r in model_inventory(registry_service) if r['available'] and r['modalities']['vision'] is True
            and not r['is_jev'] and not r['review_only']]
    requested_routes = request.scope.get('model_routes')
    if requested_routes:
        rows = [r for r in rows if r['route_key'] in requested_routes]
    settings = review_settings or {}
    priorities = []
    if current_ref:
        ref = ModelRef.from_value(current_ref)
        priorities.append(ref.provider_key + '/' + ref.model_id)
    if settings.get('vision_provider_key') and settings.get('vision_model_id'):
        priorities.append(settings['vision_provider_key'] + '/' + settings['vision_model_id'])
    rows.sort(key=lambda r: (priorities.index(r['route_key']) if r['route_key'] in priorities else len(priorities), r['route_key']))
    if not rows:
        return BindingResult('capability_absent', alternatives=ALTERNATIVES)
    row = rows[0]
    # node_id remains the executing node for audit. Authorization may be
    # explicitly inherited from a parent, but is never inferred from an ID
    # prefix: only the trusted policy field can name that principal.
    authorization_node_id = request.authorization_node_id or request.node_id
    authority = {'operation_class': 'external_send', 'scope': {
        **request.scope, 'model_routes': [row['route_key']]}}
    from mewcode.experts.recovery_case import active_case
    case = active_case(record, authorization_node_id)
    scope = {**authority['scope'], 'node_ids': [authorization_node_id], 'tools': ['InspectVisualEvidence']}
    if case:
        scope.update(epoch=case['epoch'], case_id=case['case_id'], round=len(case['rounds']) + 1)
    coverage = covers(record, {**authority, 'scope': scope}, grant_store=grant_store) if row['external'] else None
    if coverage and coverage.status != 'covered':
        return BindingResult('authority_required', route=row, authority_required=[authority],
            alternatives=ALTERNATIVES)
    owner = getattr(session, 'agent', session)
    recorder = getattr(getattr(owner, 'client', None), 'get_usage_recorder', lambda: None)()
    original = ModelRef.from_value(current_ref) if current_ref else None
    effort = original.effort if original and original.provider_key == row['provider_key'] and original.model_id == row['model_id'] else None
    ref = ModelRef(row['provider_key'], row['model_id'], effort)
    resolved = registry_service.resolve(ref, usage_recorder=recorder) if recorder is not None else registry_service.resolve(ref)
    route = {'provider_key': row['provider_key'], 'model': row['model_id'],
             'configured_model': row['model_id'], 'supports_vision': True,
             'execution_node_id': request.node_id,
             'authorization_node_id': authorization_node_id,
             'effort': getattr(resolved, 'effort', None),
             'authorization': {'auth_id': coverage.auth_id, 'status': coverage.status,
                 'request': {**authority, 'scope': scope}} if coverage else {'status': 'local_route'}}
    if not supports_vision(resolved.client, route):
        return BindingResult('capability_absent', route=row)
    return BindingResult('bound', route=row, probe_success=True, resolver=lambda: (resolved.client, route))


def binding_receipt(record, node_id, result, *, request_id):
    if result.status != 'bound' or not result.probe_success or result.resolver is None:
        raise ValueError('能力绑定未通过宿主探针')
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.experts.blocking_policy import refresh_clearances
    ledger = ledger_for_run(record)
    for blocker in ledger.view(node_id).active_blockers:
        if blocker.payload.get('kind') == 'capability_missing':
            ledger.record(node_id, 'receipt', {'probe_success': True, 'required_tools_available': True,
                'blocker_fact_id': blocker.fact_id, 'request_id': request_id, 'route': result.route})
    refresh_clearances(record, node_id)


def binding_summary(result):
    return {key: value for key, value in asdict(result).items() if key != 'resolver'}
