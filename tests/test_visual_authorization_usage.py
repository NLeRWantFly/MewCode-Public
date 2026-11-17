import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from mewcode.experts.authorization_ledger import covers, record_authorization
from mewcode.experts.visual_authorization_usage import (
    JOURNAL, VisualAuthorizationMeter, authorization_account, authorization_accounts,
)
from mewcode.tools.base import StreamEnd, TextDelta
from tests.test_recovery_case_e2e import RUN, harness


def setup(tmp_path, *, cap=1000, scope=None):
    h = harness(tmp_path)
    request = {'operation_class': 'external_send', 'scope': {
        'model_routes': ['test/vision'], 'content_classes': ['page_image'], 'node_ids': ['work']}}
    def prepare(record):
        values = {**request['scope'], **(scope or {}), 'operation_classes': ['external_send']}
        if cap is not None:
            values['cost_cap_tokens'] = cap
        record_authorization(record, kind='elevation_answer', scope=values,
            quote='approved', source_ref='quota:approved', host_verified=True)
    h.store.mutate(RUN, prepare)
    grant = h.store.load(RUN)['authorizations']['entries'][0]
    coverage = covers(h.store.load(RUN), request)
    route = {'provider_key': 'test', 'model': 'vision', 'effort': 'medium', 'supports_vision': True,
             'authorization': {'auth_id': coverage.auth_id, 'status': coverage.status, 'request': request}}
    meter = VisualAuthorizationMeter(lambda: h.store.load(RUN), lambda fn: h.store.mutate(RUN, fn, durable=True),
                                    RUN, 'work', epoch=0, input_revision='rev')
    return h, grant, route, meter


def reserve(meter, route, *, input_tokens=100, output_tokens=800, sources=()):
    return meter.reserve(route, {'visual_evidence': {'packet': 1, 'plan_digest': 'pixels'}},
                         sources=sources, input_estimate=input_tokens, output_allowance=output_tokens)


def test_atomic_reservation_settlement_idempotence_and_actual_route(tmp_path):
    h, grant, route, meter = setup(tmp_path)
    row = reserve(meter, route)
    account = authorization_account(h.store.load(RUN), grant)
    assert account['reserved_tokens'] == 900 and account['remaining_tokens'] == 100
    meter.start(row['call_id'])
    assert authorization_account(h.store.load(RUN), grant)['remaining_tokens'] == 100
    usage = {'reported_total_tokens': 231}
    meter.settle(row['call_id'], usage=usage, reported_model='vision')
    meter.settle(row['call_id'], usage=usage, reported_model='vision')
    account = authorization_account(h.store.load(RUN), grant)
    assert account['used_tokens'] == 231 and account['remaining_tokens'] == 769 and account['reserved_tokens'] == 0
    saved = account['calls'][0]
    assert saved['route'] == {'provider_key': 'test', 'model': 'vision', 'effort': 'medium'}
    assert saved['scope']['cost_cap_tokens'] == 1000 and saved['reported_model'] == 'vision'
    with pytest.raises(ValueError, match='重复结算'):
        meter.settle(row['call_id'], usage={'reported_total_tokens': 0})
    with pytest.raises(ValueError, match='不得重复'):
        meter.start(row['call_id'])
    assert len(h.store.load(RUN)[JOURNAL]['calls']) == 1


def test_member_meter_validates_parent_scope_but_records_member_execution(tmp_path):
    h, grant, route, _ = setup(tmp_path)
    h.store.mutate(RUN, lambda record: record.setdefault('members', {}).update({
        'work::one': {'node_id': 'work', 'member_id': 'one'}}))
    meter = VisualAuthorizationMeter(lambda: h.store.load(RUN),
        lambda fn: h.store.mutate(RUN, fn, durable=True), RUN, 'work', epoch=0,
        input_revision='rev', execution_node_id='work::one')
    row = reserve(meter, route)
    assert row['node_id'] == 'work::one' and row['scope_node_id'] == 'work'
    meter.start(row['call_id'])
    meter.settle(row['call_id'], usage={'reported_total_tokens': 231})
    assert authorization_account(h.store.load(RUN), grant)['remaining_tokens'] == 769
    h.store.mutate(RUN, lambda record: record['members']['work::one'].update(node_id='other'))
    with pytest.raises(ValueError, match='执行成员'):
        reserve(meter, route)


def test_concurrent_reservations_cannot_spend_same_balance(tmp_path):
    h, grant, route, meter = setup(tmp_path, cap=500)
    def attempt(_):
        try:
            return reserve(meter, route, input_tokens=300, output_tokens=300)
        except ValueError:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(row is not None for row in results) == 1
    account = authorization_account(h.store.load(RUN), grant)
    assert account['reserved_tokens'] == 500 and account['remaining_tokens'] == 0


@pytest.mark.parametrize('change', ['revoked', 'denied', 'expired', 'scope', 'revision', 'epoch', 'terminated'])
def test_revalidates_identity_and_authority_before_dispatch(tmp_path, change):
    h, grant, route, meter = setup(tmp_path)
    row = reserve(meter, route)
    def edit(record):
        if change == 'revoked': record['authorizations']['entries'][0]['revoked_at'] = 1
        elif change == 'expired': record['authorizations']['entries'][0]['expires_at'] = 1
        elif change == 'scope': record['authorizations']['entries'][0]['scope']['cost_cap_tokens'] = 100
        elif change == 'denied':
            record_authorization(record, kind='denial', scope=grant['scope'], quote='no', source_ref='denied:quota')
        elif change == 'revision': record['node_states']['work']['input_revision'] = 'changed'
        elif change == 'epoch':
            from mewcode.experts.node_ledger import ledger_for_run
            ledger_for_run(record).bump_epoch('work', reason='explicit_rebase', source_ref='test:rebase')
        else: record['status'] = 'terminated'
    h.store.mutate(RUN, edit)
    with pytest.raises(ValueError): meter.start(row['call_id'])
    meter.settle(row['call_id'], outcome='not_started')
    assert authorization_account(h.store.load(RUN), grant)['reserved_tokens'] == 0
    assert h.store.load(RUN)[JOURNAL]['calls'][row['call_id']]['state'] == 'released'


def test_specific_paths_are_checked_against_actual_rendered_sources(tmp_path):
    h, _, route, meter = setup(tmp_path, scope={'paths': [str(tmp_path / 'allowed')]})
    with pytest.raises(ValueError, match='未覆盖实际'):
        reserve(meter, route, sources=[{'file_path': str(tmp_path / 'other' / 'book.pdf')}])
    assert JOURNAL not in h.store.load(RUN)


def test_unknown_usage_holds_reservation_overage_is_recorded_uncapped_has_no_default_cap(tmp_path):
    h, grant, route, meter = setup(tmp_path)
    row = reserve(meter, route)
    meter.start(row['call_id'])
    meter.settle(row['call_id'], outcome='cancelled')
    account = authorization_account(h.store.load(RUN), grant)
    assert account['used_tokens'] == 900 and account['estimated_used_tokens'] == 900 and account['reserved_tokens'] == 0
    with pytest.raises(ValueError, match='剩余额度不足'): reserve(meter, route)
    # A later provider receipt can reconcile an interrupted call, once.
    meter.settle(row['call_id'], usage={'reported_total_tokens': 1500})
    account = authorization_account(h.store.load(RUN), grant)
    assert account['state'] == 'over_cap' and account['known_used_tokens'] == 1500
    with pytest.raises(ValueError, match='不足'): reserve(meter, route)
    h2, grant2, route2, meter2 = setup(tmp_path / 'uncapped', cap=None)
    row2 = reserve(meter2, route2)
    meter2.start(row2['call_id'])
    meter2.settle(row2['call_id'], usage={'reported_total_tokens': 250001})
    account2 = authorization_account(h2.store.load(RUN), grant2)
    assert account2['state'] == 'reconciled' and account2['limit_tokens'] is None
    assert account2['used_tokens'] == 250001 and account2['remaining_tokens'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('behavior', ['success', 'invalid_json', 'truncated', 'no_usage', 'provider_error', 'cancel'])
async def test_real_request_lifecycle_accounts_even_on_failure_or_cancellation(tmp_path, monkeypatch, behavior):
    from mewcode.tools import inspect_visual_evidence as module
    h, grant, route, meter = setup(tmp_path, cap=20000)
    started = asyncio.Event()
    async def stream(*args, **kwargs):
        started.set()
        if behavior == 'cancel': await asyncio.Event().wait()
        if behavior == 'provider_error': raise RuntimeError('provider failed')
        yield TextDelta('not json' if behavior == 'invalid_json' else '{"observations":"visible"}')
        yield StreamEnd('max_tokens' if behavior == 'truncated' else 'end_turn',
            input_tokens=0 if behavior == 'no_usage' else 100,
            output_tokens=0 if behavior == 'no_usage' else 30,
            cache_read=0 if behavior == 'no_usage' else 70, model='vision')
    monkeypatch.setattr(module, 'scheduled_client_stream', stream)
    receipts = []
    task = asyncio.create_task(module._observation_request(SimpleNamespace(max_output_tokens=10000), {}, 'observe', [],
        timeout_seconds=1, remaining=lambda: 1, accounting=meter, route=route, accounting_receipts=receipts))
    if behavior == 'cancel':
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    elif behavior in {'invalid_json', 'truncated', 'provider_error'}:
        with pytest.raises((ValueError, RuntimeError)): await task
    else:
        response = await task
        assert response['accounting_call_id']
    account = authorization_account(h.store.load(RUN), grant)
    if behavior in {'no_usage', 'provider_error', 'cancel'}:
        assert account['state'] == 'estimated' and account['estimated_used_tokens'] > 0 and account['reserved_tokens'] == 0
    else:
        assert account['used_tokens'] == 200 and account['remaining_tokens'] == 19800
    assert len(receipts) == 1 and len(account['calls']) == 1


@pytest.mark.asyncio
async def test_preflight_and_failed_persistence_do_not_start_provider(tmp_path, monkeypatch):
    from mewcode.tools import inspect_visual_evidence as module
    h, _, route, meter = setup(tmp_path)
    def rejected(*args): raise ValueError('preflight rejected')
    monkeypatch.setattr(module, 'preflight_review_text', rejected)
    with pytest.raises(ValueError, match='preflight'):
        await module._observation_request(SimpleNamespace(max_output_tokens=800), {}, '', [],
            timeout_seconds=1, remaining=lambda: 1, accounting=meter, route=route)
    assert JOURNAL not in h.store.load(RUN)
    def failed_write(fn): raise OSError('durability failed')
    meter.mutate_record = failed_write
    with pytest.raises(OSError): reserve(meter, route)
    assert JOURNAL not in h.store.load(RUN)


@pytest.mark.asyncio
async def test_expired_request_deadline_does_not_reserve_or_send(tmp_path):
    from mewcode.tools import inspect_visual_evidence as module
    h, _, route, meter = setup(tmp_path)
    with pytest.raises(ValueError, match='时间已用完'):
        await module._observation_request(SimpleNamespace(max_output_tokens=800), {}, '', [],
            timeout_seconds=1, remaining=lambda: 0, accounting=meter, route=route)
    assert JOURNAL not in h.store.load(RUN)


@pytest.mark.asyncio
async def test_provider_wire_limit_and_client_configuration_are_preserved(tmp_path, monkeypatch):
    from mewcode.client import OpenAICompatClient
    from mewcode.tools import inspect_visual_evidence as module
    h, grant, route, meter = setup(tmp_path)
    client = OpenAICompatClient.__new__(OpenAICompatClient)
    client.max_output_tokens = 10000
    async def stream(*args, **kwargs):
        assert client.max_output_tokens < 1000
        yield TextDelta('{"observations":"visible"}')
        yield StreamEnd('end_turn', input_tokens=100, output_tokens=30, model='vision')
    monkeypatch.setattr(module, 'scheduled_client_stream', stream)
    await module._observation_request(client, {}, '', [], timeout_seconds=1, remaining=lambda: 1, accounting=meter, route=route)
    account = authorization_account(h.store.load(RUN), grant)
    assert account['calls'][0]['provider_output_bound'] is True and client.max_output_tokens == 10000


@pytest.mark.asyncio
async def test_legacy_unknown_is_not_zero_known_receipt_is_not_double_charged_and_read_tools_agree(tmp_path):
    from mewcode.tools.read_authorizations import ReadAuthorizations, ReadAuthorizationsParams
    from mewcode.experts.recovery_context import context_sections
    h, grant, route, meter = setup(tmp_path)
    row = reserve(meter, route)
    meter.start(row['call_id'])
    meter.settle(row['call_id'], usage={'reported_total_tokens': 200})
    def legacy(record):
        record['node_states']['work']['tools'] = {
            'new': {'name': 'InspectVisualEvidence', 'host_observation': {'visual_call': {
                'accounting_call_ids': [row['call_id']], 'authorization': {'auth_id': grant['auth_id']},
                'usage': {'reported_total_tokens': 200}}}},
            'old': {'name': 'InspectVisualEvidence', 'host_observation': {'pixel_coverage': {'complete': True, 'model': 'vision'}}}}
    h.store.mutate(RUN, legacy)
    before = h.store.load(RUN)
    account = authorization_accounts(before)[0]
    assert account['known_used_tokens'] == 200 and account['used_tokens'] == 1200 and account['remaining_tokens'] == 0
    assert account['unknown_call_ids'] == ['work/old']
    result = await ReadAuthorizations(lambda: h.store.load(RUN)).execute(ReadAuthorizationsParams())
    displayed = json.loads(result.output)['entries'][0]['accounting']
    assert displayed['known_used_tokens'] == 200 and displayed['remaining_tokens'] == 0 and displayed['has_estimates']
    assert context_sections(before, 'work')['accounting']['authorization_limits'][0] == account
    assert h.store.load(RUN) == before
    with pytest.raises(ValueError, match='剩余额度不足'): reserve(meter, route)
    # The existing host observation is preserved; reconciliation never alters business evidence.
    assert h.store.load(RUN)['node_states']['work']['tools'] == before['node_states']['work']['tools']


def test_known_legacy_usage_is_counted_once_and_an_older_call_is_not_assigned_to_new_grant(tmp_path):
    h, grant, _, _ = setup(tmp_path)
    def legacy(record):
        record['node_states']['work']['tools'] = {
            'known': {'name': 'InspectVisualEvidence', 'host_observation': {'visual_call': {
                'authorization': {'auth_id': grant['auth_id']}, 'usage': {'reported_total_tokens': 150}}}},
            'before_grant': {'name': 'InspectVisualEvidence', 'completed_at': grant['created_at'] - 1,
                            'host_observation': {'pixel_coverage': {'complete': True, 'model': 'vision'}}}}
    h.store.mutate(RUN, legacy)
    snapshot = h.store.load(RUN)
    assert authorization_account(snapshot, grant)['used_tokens'] == 150
    assert authorization_account(copy.deepcopy(snapshot), grant)['used_tokens'] == 150


@pytest.mark.asyncio
async def test_external_send_quota_is_not_reset_as_helper_role_budget(tmp_path):
    from tests.test_recovery_case_e2e import AUTH, option, run
    h = harness(tmp_path, make_options=lambda n: [option(n, authority=True, vision=True)])
    h.store.mutate(RUN, lambda record: record_authorization(record, kind='gate_answer',
        scope={**AUTH['scope'], 'operation_classes': ['external_send']},
        quote='approved', source_ref='gate:quota', host_verified=True))
    await run(h)
    helper_policies = [policy for _, _, policy in h.calls if policy.assistance_session]
    assert helper_policies and all(policy.token_budget is None for policy in helper_policies)


@pytest.mark.asyncio
@pytest.mark.parametrize('count,invalid', [(1, False), (9, False), (1, True)])
async def test_inspection_wires_packet_page_mapping_and_failed_result_accounting(tmp_path, monkeypatch, count, invalid):
    from PIL import Image
    from mewcode.tools import inspect_visual_evidence as module
    h, grant, route, meter = setup(tmp_path, cap=200000)
    images = []
    for index in range(count):
        path = tmp_path / f'page{index}.png'
        Image.new('RGB', (20, 20), color=(index, 0, 0)).save(path)
        images.append({'file_path': str(path)})
    async def stream(client, conversation, **kwargs):
        payload = json.loads(conversation.history[0].content)
        value = {'observations': 'visible page', 'uncertain': [], 'requires_joint_pixels': False,
                 'covered_evidence_ids': payload['visual_evidence']['required_evidence_ids']}
        yield TextDelta('not json' if invalid else json.dumps(value))
        yield StreamEnd('end_turn', input_tokens=100, output_tokens=30, model='vision')
    monkeypatch.setattr(module, 'scheduled_client_stream', stream)
    inspector = module.InspectVisualEvidence([str(tmp_path)],
        vision_resolver=lambda: (SimpleNamespace(max_output_tokens=10000), route), authorization_meter=meter)
    result = await inspector.execute(module.InspectVisualEvidenceParams(image_refs=images, question='observe'))
    assert result.is_error is invalid
    account = authorization_account(h.store.load(RUN), grant)
    assert account['used_tokens'] == 130 * len(account['calls'])
    sources = [source for call in account['calls'] for source in call['sources']]
    assert {source['file_path'] for source in sources} == {image['file_path'] for image in images}
    assert all(source['source_digest'] and source['pixel_digest'] for source in sources)
    assert result.host_observation['visual_call']['accounting_call_ids'] == [call['call_id'] for call in account['calls']]
    h.store.mutate(RUN, lambda record: record['node_states']['work'].setdefault('tools', {}).update(
        inspection={'name': 'InspectVisualEvidence', 'status': 'failed' if invalid else 'completed',
                    'host_observation': result.host_observation}))
    assert authorization_account(h.store.load(RUN), grant)['used_tokens'] == account['used_tokens']
    if not invalid:
        assert result.host_observation['pixel_coverage']['complete'] is True
        assert result.host_observation['visual_call']['route']['effort'] == 'medium'


@pytest.mark.asyncio
async def test_same_page_crops_keep_packet_pixel_mapping(tmp_path, monkeypatch):
    from PIL import Image
    from mewcode.tools import inspect_visual_evidence as module
    h, grant, route, meter = setup(tmp_path, cap=200000)
    path = tmp_path / 'page.png'
    image = Image.new('RGB', (100, 100))
    image.putdata([(x, y, 0) for y in range(100) for x in range(100)])
    image.save(path)
    refs = [{'file_path': str(path), 'crop': f'{i},0,10,10', 'criterion_id': f'item-{i}'} for i in range(9)]
    async def stream(client, conversation, **kwargs):
        payload = json.loads(conversation.history[0].content)
        yield TextDelta(json.dumps({'observations': 'visible crop', 'uncertain': [], 'requires_joint_pixels': False,
            'covered_evidence_ids': payload['visual_evidence']['required_evidence_ids']}))
        yield StreamEnd('end_turn', input_tokens=100, output_tokens=30, model='vision')
    monkeypatch.setattr(module, 'scheduled_client_stream', stream)
    result = await module.InspectVisualEvidence([str(tmp_path)],
        vision_resolver=lambda: (SimpleNamespace(max_output_tokens=10000), route), authorization_meter=meter).execute(
            module.InspectVisualEvidenceParams(image_refs=refs, question='observe crops'))
    assert not result.is_error, result.output
    calls = authorization_account(h.store.load(RUN), grant)['calls']
    assert [len(call['sources']) for call in calls] == [8, 1]
    assert len({(source['crop'], source['criterion_id']) for call in calls for source in call['sources']}) == 9
