from types import SimpleNamespace

import pytest

from mewcode.kernel.models import RoleModels, rebind_role
from mewcode.model_registry import ModelRef


def catalog(*models):
    return {'models': [{'provider_key': provider, 'model_id': model, 'available': True,
                        'protocol': 'openai', 'tools': True, 'brand': provider} for provider, model in models]}


def profile(provider='a', model='one'):
    ref = ModelRef(provider, model)
    return SimpleNamespace(manager=ref, executor=ref, auditor=ref)


def test_default_verification_uses_another_configured_provider_and_overrides_are_preserved():
    pool = RoleModels(catalog(('a', 'one'), ('b', 'two')))
    binding = pool.bind(profile())
    assert binding['roles']['executor']['provider_key'] == 'a'
    assert binding['roles']['verifier']['provider_key'] == 'b'
    assert binding['verification_independent']
    same = pool.bind(profile(), {'verifier': {'provider_key': 'a', 'model_id': 'one'}})
    assert same['notice'] == '核查不独立'


def test_one_provider_uses_another_model_and_never_claims_independence():
    pool = RoleModels(catalog(('a', 'one'), ('a', 'two')))
    binding = pool.bind(profile())
    assert binding['roles']['verifier']['model_id'] == 'two'
    assert not binding['verification_independent']


def test_unavailable_user_selection_is_not_silently_substituted():
    pool = RoleModels(catalog(('b', 'two')))
    with pytest.raises(ValueError, match='executor'):
        pool.bind(SimpleNamespace(manager=ModelRef('b', 'two'), executor=ModelRef('a', 'one'), auditor=ModelRef('b', 'two')))
    with pytest.raises(ValueError):
        RoleModels(catalog(('a', 'codex-auto-review'))).bind(profile('a', 'codex-auto-review'))


def test_model_changes_and_fallback_are_constrained_to_the_same_pool():
    pool = RoleModels(catalog(('a', 'one'), ('b', 'two')))
    record = {'role_models': pool.bind(profile())}
    assert pool.alternate(record['role_models'], 'executor') == ModelRef('b', 'two')
    assert rebind_role(record, pool=pool, role='executor', model=ModelRef('b', 'two'), source_ref='fixture-user')
    assert record['role_model_changes'][0]['source_ref'] == 'fixture-user'


@pytest.mark.asyncio
async def test_scheduler_consumes_only_one_pool_fallback_after_same_method_retry(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.escalation import decide_and_record
    from tests.test_scheduler_recovery import controller
    handler, flow, _, store, _, _, _ = controller(tmp_path, monkeypatch)
    configured = catalog(('a', 'one'), ('b', 'two'))
    handler.registry.catalog = AsyncMock(return_value=configured)
    def seed(record):
        record['role_models'] = RoleModels(configured).bind(profile())
        record['locked_models'] = {'extract': ModelRef('a', 'one').as_dict()}
        record['node_states']['extract'].update(input_revision='v1', node_attempt=1, completed_at=2)
        decide_and_record(record, 'extract', trigger='failure', identity='prior-retry')
    store.mutate(handler.run_id, seed, durable=True)
    request = dict(node=flow.node_map['extract'], result=ExpertRoleResult(text='unavailable', status='needs_attention',
        error_kind='provider_interrupted'), policy=ExpertExecutionPolicy('stable', False, 0),
        allowed=['continue_checkpoint', 'diagnose_path'], host_facts={'trigger': 'provider_interrupted'}, account=AsyncMock())
    result = await handler(**request)
    assert result['action'] == 'diagnose_path'
    record = store.load(handler.run_id)
    assert record['role_models']['roles']['executor']['provider_key'] == 'b'
    assert len(record['role_model_changes']) == 1
    assert len([f for f in ledger_for_run(record).view('extract').history
                if (f.payload.get('decision') or {}).get('level') == 'alternate_route']) == 1
