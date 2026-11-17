"""Host execution checks shared by maintenance diagnostics and dispatch."""
from __future__ import annotations

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.model_registry import ModelRef
from mewcode.orchestration.convert import from_expert_flow
from mewcode.orchestration.graph_lint import lint_graph


def graph_preflight_findings(flow, registry, *, checkpoint=None, profile=None):
    from mewcode.permissions.review_client import load_review_model_settings, resolve_visual_evidence_client

    parsed = ExpertFlowSpec.from_value(flow)
    capabilities = {}
    resolved_bindings = {}
    for node in parsed.nodes:
        binding = ((checkpoint or {}).get('locked_models') or {}).get(node.node_id)
        binding = binding or node.model or getattr(profile, node.role, None)
        if binding is None:
            continue
        try:
            ref = ModelRef.from_value(binding)
            key = (ref.provider_key, ref.model_id, ref.effort)
            if key not in resolved_bindings:
                resolved_bindings[key] = registry.resolve(ref)
            resolved = resolved_bindings[key]
            capabilities[node.node_id] = {
                'supports_vision': 'vision' in getattr(resolved, 'capabilities', ()),
                'review_only': getattr(getattr(resolved, 'client', None), 'review_only', False) is True}
        except (ValueError, KeyError):
            pass  # The separate model-binding gate reports an invalid route.
    settings = load_review_model_settings()
    observer_available = False
    if settings.get('vision_model_id') and settings.get('vision_provider_key'):
        try:
            resolve_visual_evidence_client(registry, settings)
            observer_available = True
        except (ValueError, KeyError):
            pass
    return lint_graph(from_expert_flow(parsed), model_capabilities=capabilities,
                      visual_evidence_available=observer_available,
                      required_predecessors=_bound_predecessors(parsed, checkpoint))


def _bound_predecessors(flow, checkpoint):
    """Runtime contracts require every bound input, including evidence branches.

    A design/candidate graph has no such proof. Metadata or unbound candidate
    dependencies must not make an optional gate look mandatory.
    """
    from mewcode.kernel import collaboration as ledger
    from mewcode.kernel.collaboration_graph import effective_flow

    if not checkpoint or not checkpoint.get('collaboration'):
        return {}
    try:
        value = ledger.state(checkpoint)
        if (ledger.flow_mode(checkpoint['locked_flow']) != 'collaborative'
                or effective_flow(checkpoint).as_dict() != flow.as_dict()):
            return {}
    except (KeyError, TypeError, ValueError):
        return {}
    return {key: row['bound_dependencies'] for key, row in value.get('contracts', {}).items()
            if isinstance(row.get('bound_dependencies'), list)
            and all(isinstance(item, str) for item in row['bound_dependencies'])}


def graph_preflight_error_text(findings):
    errors = [finding for finding in findings if finding.severity == 'error']
    if not errors:
        return ''
    return '编排检查未通过，未启动节点。\n' + '\n'.join(
        f'{finding.path}：{finding.message}' for finding in errors)
