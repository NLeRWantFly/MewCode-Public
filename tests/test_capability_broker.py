from types import SimpleNamespace
from unittest.mock import Mock
from mewcode.experts.authorization_ledger import record_authorization
from mewcode.experts.capability_broker import CapabilityRequest, bind, model_inventory
from mewcode.model_registry import ModelRef


def service():
    configurations = {'p/text': (), 'p/vision': ('vision',), 'p/jev': ('vision',), 'p/review': ('vision',)}
    def resolve(ref):
        key = ref.provider_key + '/' + ref.model_id
        return SimpleNamespace(client=SimpleNamespace(review_only=ref.model_id == 'review', generate=Mock(side_effect=AssertionError('no generation'))),
            config=SimpleNamespace(base_url='https://configured.test'), protocol='typesafe' if ref.model_id == 'jev' else 'openai-compat',
            capabilities=configurations[key])
    return SimpleNamespace(records=lambda: [SimpleNamespace(key='p', models=['text', 'vision', 'jev', 'review'], provider=SimpleNamespace(model='text'))], resolve=resolve)


def test_configured_inventory_does_not_generate_excludes_jev_and_review_only():
    rows = model_inventory(service())
    assert next(r for r in rows if r['model_id'] == 'vision')['modalities']['vision'] is True
    assert not next(r for r in rows if r['model_id'] == 'jev')['modalities']['vision']
    assert not next(r for r in rows if r['model_id'] == 'review')['modalities']['vision']


def test_inventory_uses_advertised_routes_instead_of_stale_provider_default():
    providers = service()
    record = SimpleNamespace(key='p', models=['stale-default'], advertised_models=['vision'],
                             provider=SimpleNamespace(model='stale-default'))
    providers.records = lambda: [record]
    assert [row['model_id'] for row in model_inventory(providers)] == ['vision']


def test_inventory_is_incomplete_when_a_bound_route_is_missing_from_discovery():
    from mewcode.experts.capability_broker import inventory
    providers = service()
    record = {'locked_models': {'work': {'provider_key': 'p', 'model_id': 'new-route', 'effort': 'max'}}}
    value = inventory(SimpleNamespace(), providers, record)
    assert value['models_complete'] is False
    assert value['routes'] == record['locked_models']


def test_binding_no_recursion_without_authority_then_actual_resolver():
    providers = service()
    request = CapabilityRequest('vision', 'work')
    record = {'node_states': {'work': {}}, 'node_ledgers': {}}
    blocked = bind(request, session=object(), registry_service=providers, record=record, current_ref=ModelRef('p', 'text'))
    assert blocked.status == 'authority_required' and blocked.resolver is None
    wanted = blocked.authority_required[0]
    record_authorization(record, kind='elevation_answer', scope={**wanted['scope'], 'operation_classes': ['external_send']},
        quote='confirmed', source_ref='card:1', host_verified=True)
    resolved = bind(request, session=object(), registry_service=providers, record=record, current_ref=ModelRef('p', 'text'))
    assert resolved.status == 'bound' and resolved.probe_success
    client, route = resolved.resolver()
    assert route['supports_vision'] and not client.review_only


def test_explicit_route_is_never_silently_substituted():
    result = bind(CapabilityRequest('vision', 'work', {'model_routes': ['missing/model'], 'content_classes': ['page_image']}),
        session=object(), registry_service=service(), record={})
    assert result.status == 'capability_absent' and result.resolver is None


def test_visual_binding_retains_primary_usage_recorder():
    providers, recorder = service(), Mock()
    original = providers.resolve
    def resolve(ref, *, usage_recorder=None):
        resolved = original(ref)
        resolved.client.usage_recorder = usage_recorder
        return resolved
    providers.resolve = resolve
    record = {}
    record_authorization(record, kind='gate_answer', scope={'operation_classes': ['external_send'],
        'model_routes': ['p/vision'], 'content_classes': ['page_image'], 'cost_cap_tokens': 200000},
        quote='confirmed', source_ref='gate:1', host_verified=True)
    session = SimpleNamespace(agent=SimpleNamespace(client=SimpleNamespace(get_usage_recorder=lambda: recorder)))
    result = bind(CapabilityRequest('vision', 'work'), session=session, registry_service=providers, record=record)
    client, _ = result.resolver()
    assert client.usage_recorder is recorder


def test_host_approval_route_label_is_never_resolved_as_provider_model():
    providers = SimpleNamespace(records=lambda: [SimpleNamespace(key='p', models=['codex-auto-review'],
        provider=SimpleNamespace(model='codex-auto-review'))], resolve=Mock(side_effect=AssertionError('route label is not a model')))
    assert model_inventory(providers) == []
    providers.resolve.assert_not_called()


def test_binding_passes_original_effort_and_exposes_host_authorization():
    providers, seen = service(), []
    original = providers.resolve
    def resolve(ref):
        seen.append(ref)
        result = original(ref)
        result.effort = ref.effort
        return result
    providers.resolve = resolve
    record = {}
    record_authorization(record, kind='gate_answer', scope={'operation_classes': ['external_send'],
        'model_routes': ['p/vision'], 'content_classes': ['page_image'], 'cost_cap_tokens': 200000},
        quote='confirmed', source_ref='gate:medium', host_verified=True)
    result = bind(CapabilityRequest('vision', 'work'), session=object(), registry_service=providers,
        record=record, current_ref=ModelRef('p', 'vision', 'medium'))
    _, route = result.resolver()
    assert seen[-1].effort == 'medium' and route['effort'] == 'medium'
    assert route['authorization']['auth_id'] == 'AUTH-01'


def test_expanded_child_uses_explicit_parent_authority_and_keeps_execution_identity():
    providers = service()
    record = {}
    record_authorization(record, kind='gate_answer', scope={
        'operation_classes': ['external_send'], 'model_routes': ['p/vision'],
        'content_classes': ['page_image'], 'node_ids': ['N01'], 'cost_cap_tokens': 120000,
        'tools': ['InspectVisualEvidence'],
    }, quote='confirmed', source_ref='gate:N01', host_verified=True)

    request = CapabilityRequest('vision', 'N01::exp::child',
        {'content_classes': ['page_image'], 'cost_cap_tokens': 120000},
        authorization_node_id='N01')
    bound = bind(request, session=object(), registry_service=providers, record=record,
        current_ref=ModelRef('p', 'text'))

    assert bound.status == 'bound'
    _, route = bound.resolver()
    assert route['execution_node_id'] == 'N01::exp::child'
    assert route['authorization_node_id'] == 'N01'
    assert route['authorization']['request']['scope']['node_ids'] == ['N01']
    assert route['authorization']['request']['scope']['tools'] == ['InspectVisualEvidence']
    assert route['authorization']['request']['scope']['content_classes'] == ['page_image']
    assert route['authorization']['request']['scope']['cost_cap_tokens'] == 120000


def test_child_cannot_borrow_parent_authority_without_explicit_identity_or_expand_its_scope():
    providers = service()
    record = {}
    record_authorization(record, kind='gate_answer', scope={
        'operation_classes': ['external_send'], 'model_routes': ['p/vision'],
        'content_classes': ['page_image'], 'node_ids': ['N01'], 'cost_cap_tokens': 120000,
    }, quote='confirmed', source_ref='gate:N01', host_verified=True)

    implicit = bind(CapabilityRequest('vision', 'N01::exp::child',
        {'content_classes': ['page_image']}), session=object(), registry_service=providers,
        record=record, current_ref=ModelRef('p', 'text'))
    over_scope = bind(CapabilityRequest('vision', 'N01::exp::child',
        {'content_classes': ['page_image', 'document_text'], 'cost_cap_tokens': 100000},
        authorization_node_id='N01'),
        session=object(), registry_service=providers, record=record, current_ref=ModelRef('p', 'text'))
    over_budget = bind(CapabilityRequest('vision', 'N01::exp::child',
        {'content_classes': ['page_image'], 'cost_cap_tokens': 120001}, authorization_node_id='N01'),
        session=object(), registry_service=providers, record=record, current_ref=ModelRef('p', 'text'))

    assert implicit.status == 'authority_required' and implicit.resolver is None
    assert over_scope.status == 'authority_required' and over_scope.resolver is None
    assert over_budget.status == 'authority_required' and over_budget.resolver is None


def test_explicit_parent_denial_does_not_bind_for_expanded_child():
    providers = service()
    record = {}
    record_authorization(record, kind='denial', scope={
        'operation_classes': ['external_send'], 'model_routes': ['p/vision'],
        'content_classes': ['page_image'], 'node_ids': ['N01'],
    }, quote='denied', source_ref='denial:N01')

    result = bind(CapabilityRequest('vision', 'N01::exp::child',
        {'content_classes': ['page_image']}, authorization_node_id='N01'),
        session=object(), registry_service=providers, record=record,
        current_ref=ModelRef('p', 'text'))

    assert result.status != 'bound' and result.resolver is None
