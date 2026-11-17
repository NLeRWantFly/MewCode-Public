import asyncio
import copy
import json

import pytest

from mewcode.kernel.models import RoleModels
from mewcode.orchestration.context_budget import ContextBudget, estimate_text_tokens, model_context_budget
from mewcode.orchestration.task_context import ContextBudgetExceeded, build_task_context
from mewcode.orchestration.context_partition import context_packets
from tests.test_autonomous_context import make_ledger
from tests.test_collaboration_mode import CATALOG, EXECUTOR, REVIEWER, RUN_ID, delivery, flow, store_for
from tests.test_autonomous_execute_context import make_policy
from mewcode.orchestration.collab_runtime import CollaborationRuntime
from mewcode.orchestration.collab_coordinator import CollaborationCoordinator


def catalog(executor_window=256000, reviewer_window=32000):
    result = copy.deepcopy(CATALOG)
    for row in result['models']:
        row.update(context_window=executor_window if row['model_id'] == EXECUTOR.model_id else reviewer_window,
                   max_output_tokens=8192)
    return result


def test_bound_model_windows_and_fixed_reserves_override_legacy_char_fallback():
    pool = RoleModels(catalog())
    old_policy = {'context_char_budget': 64000}
    before = copy.deepcopy(old_policy)
    work = model_context_budget(pool, EXECUTOR, old_policy, system='fixed', tools=True)
    review = model_context_budget(pool, REVIEWER, old_policy, system='fixed')
    assert work.limit > 64000 and work.window == 256000
    assert review.window == 32000 and review.limit < work.limit
    assert work.limit + work.output_reserve + work.host_reserve + work.safety_margin + estimate_text_tokens('fixed') == work.window
    assert old_policy == before
    assert not review.fits('汉' * 16000)
    assert work.fits('汉' * 40000)


def test_unknown_catalog_uses_validated_legacy_fallback_without_guessing_window():
    pool = RoleModels(CATALOG)
    assert model_context_budget(pool, EXECUTOR, {'context_char_budget': 12345}) == ContextBudget(12345)
    for invalid in (None, 0, True, '64000'):
        with pytest.raises(ValueError):
            model_context_budget(pool, EXECUTOR, {'context_char_budget': invalid})


def test_invalid_model_output_reserve_does_not_invent_extra_capacity():
    rows = catalog()
    rows['models'][0]['max_output_tokens'] = 300000
    with pytest.raises(ValueError, match='输出预留'):
        model_context_budget(RoleModels(rows), EXECUTOR, {'context_char_budget': 64000})


def test_actual_request_output_cap_is_used_instead_of_model_maximum():
    rows = catalog(32768)
    rows['models'][0].update(max_output_tokens=32768, request_output_tokens=8192)
    budget = model_context_budget(RoleModels(rows), EXECUTOR, {'context_char_budget': 64000})
    assert budget.output_reserve == 8192 and budget.limit > 16000


@pytest.mark.asyncio
async def test_live_catalog_distinguishes_configured_request_cap_from_model_capability():
    from mewcode.config import ProviderConfig
    from mewcode.model_registry import ProviderRegistry
    from mewcode.provider_sources import ProviderSlot
    from tests.test_model_registry import _FakeClient
    provider = ProviderConfig(name='main', protocol='openai-compat', base_url='https://example.test/v1',
                              model='deepseek-v4.1-flash', api_key='test-key', max_output_tokens=4096)
    registry = ProviderRegistry(catalog_ttl=0, client_factory=lambda config: _FakeClient([config.model]))
    registry.refresh_discovered([ProviderSlot(provider=provider, source='config')])
    rows = await registry.catalog()
    row = next(item for item in rows['models'] if item['model_id'] == provider.model)
    assert row['protocol'] == 'openai-compat'
    assert row['request_output_tokens'] == provider.get_max_output_tokens() == 4096


def test_unicode_counting_and_wrapper_subtraction_are_not_character_comparisons():
    allowance = ContextBudget(100, 'estimated_tokens')
    assert allowance.fits('a' * 299)
    assert not allowance.fits('汉' * 51)
    assert not allowance.fits('🙂' * 26)
    assert allowance.subtract('汉' * 10).limit == 80


def test_large_model_projects_full_requirements_beyond_old_cap_without_mutation():
    value = make_ledger()
    entry = value['entries'][value['contracts']['work']['requirement_ids'][0]]
    entry['body'] = '精确需求不得丢弃。' * 9000
    before = copy.deepcopy(value)
    with pytest.raises(ContextBudgetExceeded):
        build_task_context(value, 'work', char_budget=64000)
    budget = model_context_budget(RoleModels(catalog(1000000)), EXECUTOR, value['policy'], tools=True)
    projected = build_task_context(value, 'work', context_budget=budget)
    encoded = json.dumps(projected, ensure_ascii=False)
    assert entry['body'] in encoded
    assert len(encoded) > 64000 and budget.fits(encoded)
    assert projected['budget']['model_context']['context_window_tokens'] == 1000000
    assert value == before


def test_smaller_reviewer_pages_full_entries_using_its_own_token_allowance():
    from tests.test_context_partition import large_flow, value_for
    value = value_for(large_flow())
    before = copy.deepcopy(value)
    pool = RoleModels(catalog(1000000, 64000))
    work = model_context_budget(pool, EXECUTOR, value['policy'])
    review = model_context_budget(pool, REVIEWER, value['policy'])
    assert len(context_packets(value, 'work', budget=work)) == 1
    packets = context_packets(value, 'work', budget=review)
    assert len(packets) > 1
    assert all(review.fits(json.dumps(packet, ensure_ascii=False)) for packet in packets)
    assert [key for packet in packets for key in packet['scope']['included_requirement_ids']] == value['contracts']['work']['requirement_ids']
    assert value == before


@pytest.mark.asyncio
async def test_coordinator_accepts_large_model_input_but_rejects_small_bound_model_before_dispatch(tmp_path):
    store = store_for(tmp_path)
    calls = []
    async def ask(model, payload, system):
        calls.append(model)
        return {'passed': True}
    runtime = CollaborationRuntime(store=store, run_id=RUN_ID, flow=flow(), catalog=catalog(), work_dir=tmp_path)
    coordinator = CollaborationCoordinator(store, RUN_ID, catalog(), ask=ask)
    payload = {'body': '原文' * 20000}
    assert len(json.dumps(payload, ensure_ascii=False)) < 64000
    with pytest.raises(ValueError, match='Token'):
        await coordinator.call(kind='semantic_review', identity='small', payload=payload, model=REVIEWER, system='复核')
    assert runtime.read()['calls'] == {} and calls == []
    payload = {'body': 'exact requirement ' * 6000}
    assert len(json.dumps(payload)) > 64000
    await coordinator.call(kind='coordinator', identity='large', payload=payload, model=EXECUTOR, system='规划')
    assert calls == [EXECUTOR]


@pytest.mark.asyncio
async def test_actual_executor_receives_complete_large_contract_instead_of_false_partition(tmp_path):
    store = store_for(tmp_path)
    async def review(*args):
        return {'passed': True, 'evidence': ['actual output checked']}
    runtime = CollaborationRuntime(store=store, run_id=RUN_ID, flow=flow(), catalog=catalog(1000000, 1000000),
                                   work_dir=tmp_path, review=review)
    text = '必须原样保留的需求。' * 8000
    runtime.mutate(lambda value: value['entries']['goal'].update(body=text))
    seen = []
    async def invoke(node, model, prompt, policy, member):
        assert model == EXECUTOR and text in prompt
        assert len(prompt) > 64000
        seen.append(node.node_id)
        return delivery()
    result = await runtime.execute(flow().node_map['work'], EXECUTOR, '完成任务', make_policy(), None, invoke)
    assert result.status == 'completed', result.text
    assert seen == ['work']
    assert not runtime.read().get('graph_publications')


@pytest.mark.asyncio
async def test_actual_verifier_checks_own_window_before_reserving_or_calling_and_reuses_finished_result(tmp_path):
    from mewcode.kernel.events import digest
    from mewcode.kernel import collaboration as ledger
    calls = []
    async def review(*args):
        calls.append(True)
        return {'passed': True, 'evidence': ['verified']}
    runtime = CollaborationRuntime(store=store_for(tmp_path), run_id=RUN_ID, flow=flow(),
        catalog=catalog(1000000, 32000), work_dir=tmp_path, review=review)
    runtime.mutate(lambda value: value['entries']['goal'].update(body='汉' * 30000))
    attempt = runtime.mutate(lambda value: ledger.dispatch(value, 'work'))
    payload = {'contract': runtime.read()['contracts']['work'], 'check': {'id': 'large-check'}, 'artifacts': []}
    context = {'node_id': 'work', 'attempt_id': attempt['id'], 'executor_model': EXECUTOR.as_dict()}
    with pytest.raises(ContextBudgetExceeded, match='model-b.*Token'):
        await runtime.review(payload, context)
    assert calls == [] and runtime.read()['calls'] == {}
    identity = digest(['acceptance', attempt['id'], 0, payload['check'], payload['artifacts']])
    saved = {'passed': True, 'evidence': ['earlier completed response']}
    runtime.mutate(lambda value: value['calls'].update({identity: {'kind': 'acceptance', 'status': 'finished', 'result': saved}}))
    assert await runtime.review(payload, context) == saved
    assert calls == []
