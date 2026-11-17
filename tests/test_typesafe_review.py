import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from mewcode.client import create_client, LLMError
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager, Message, ToolResultBlock
from mewcode.model_registry import ProviderRegistry, ProviderProfile, ModelRef
from mewcode.permissions.review_client import resolve_safety_review_client
from mewcode.permissions.review_routing import ReviewRouter
from mewcode.permissions.reviewer import REVIEWER_SYSTEM_PROMPT, SafetyReviewer
from mewcode.tools.base import TextDelta, StreamEnd
from mewcode.typesafe_client import TypeSafeClient
from mewcode.permissions.review_outcome import ReviewContextTooLarge


PROFILE = dict(brand='typesafe', name='TypeSafe', api_key='test-secret', provider_key='desktop:jev')
IMAGE = {'type': 'image', 'mime_type': 'image/png', 'data': 'cGl4ZWxz'}


def make_client(handler):
    config = ProviderConfig(name='TypeSafe', protocol='typesafe', model='jev-latest',
        base_url='https://api.typesafe.ai/v1', api_key='test-secret')
    client = TypeSafeClient(config, transport=httpx.MockTransport(handler))
    client.configure_usage_tracking(config, endpoint='/v1/systemone')
    return client


def test_typesafe_context_preflight_is_pure_and_reports_field_sizes():
    calls = []
    client = make_client(lambda request: calls.append(request))
    with pytest.raises(ReviewContextTooLarge) as raised:
        client.preflight_evaluation(
            {"excerpt": "x" * 31_000},
            {"impact": {"type": "noul", "instructions": "Check."}},
        )
    assert raised.value.sizes["field_bytes"]["excerpt"] > 30_000
    assert calls == []


def response_for(request, *, overrides=None, confidence=1):
    body = json.loads(request.content)
    chosen = {'risk': 'low', 'authorization': 'high', 'hard_deny': 'no', 'scope': 'yes', 'probe': 'MEWCODE_OK', **(overrides or {})}
    answers = {key: {'type': 'choice', 'choice': chosen[key], 'confidence': confidence,
        'probabilities': {option: int(option == chosen[key]) for option in q['criteria']}}
        for key, q in body['questions'].items()}
    return {'model': 'jev-1.13.0', 'answers': answers, 'usage': {'input_tokens': 120, 'output_tokens': 10}}


@pytest.mark.asyncio
async def test_native_protocol_and_usage_never_fake_chat_or_tool_call():
    calls, records = [], []
    def handler(request):
        calls.append(request)
        assert request.headers['authorization'] == 'Bearer test-secret'
        return httpx.Response(200, json=response_for(request))
    client = make_client(handler)
    client.set_usage_recorder(records.append)
    result = await client.review_safety({'authorized_task': 'inspect', 'planned_action': 'read'}, 'policy')
    assert result['outcome'] == 'allow' and result['review_route']['model'] == 'jev-1.13.0'
    assert calls[0].url.path == '/v1/systemone'
    assert set(json.loads(calls[0].content)) == {'state', 'model', 'questions'}
    assert records[0].tokens.input_tokens == 120
    with pytest.raises(LLMError, match='只支持类型化'):
        async for _ in client.stream(ConversationManager()):
            pass
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_safety_capsule_preserves_argument_structure_redacts_nested_secrets_and_reuses_policy_once(tmp_path):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=response_for(request))
    reviewer = SafetyReviewer(make_client(handler), str(tmp_path))
    reviewer.authorization_context = 'Only fingerprint files under the workspace.'
    reviewer.authorization_context_exclusive = True

    result = await reviewer.review(
        tool_name='Bash',
        arguments={
            'command': 'python -c "print(1)"',
            'options': [{'path': 'a.md', 'api_key': 'sk-nested-secret'}],
            'headers': {'Authorization': 'Bearer should-not-leak'},
        },
        risk={'side_effect': 'unknown'},
        conversation=None,
    )

    assert result.allowed
    body = calls[0]
    state = body['state']
    assert isinstance(state['planned_action']['arguments'], dict)
    assert isinstance(state['planned_action']['arguments']['options'], list)
    assert state['planned_action']['arguments']['options'][0]['api_key'] == '[REDACTED]'
    assert state['planned_action']['arguments']['headers']['Authorization'] == '[REDACTED]'
    assert 'sk-nested-secret' not in json.dumps(body, ensure_ascii=False)
    assert state['review_policy']['text'] == REVIEWER_SYSTEM_PROMPT
    assert all(q['instructions']['policy_ref'] == '$.review_policy.text'
               for q in body['questions'].values())
    assert all('policy' not in q['instructions'] for q in body['questions'].values())
    assert len(state['source_bindings']) == 4
    assert all(source['complete'] and source['digest'] for source in state['source_bindings'])


@pytest.mark.asyncio
@pytest.mark.parametrize('changes,allowed', [({}, True), ({'risk': 'critical'}, False),
    ({'risk': 'high', 'authorization': 'low'}, False), ({'risk': 'high', 'authorization': 'high'}, True),
    ({'hard_deny': 'yes'}, False), ({'scope': 'no'}, False)])
async def test_host_policy_composes_independent_judgments(changes, allowed):
    client = make_client(lambda req: httpx.Response(200, json=response_for(req, overrides=changes)))
    result = await client.review_safety({'authorized_task': 'task'}, 'policy')
    assert (result['outcome'] == 'allow') is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize('changes,expected', [
    ({'hard_deny': 'yes'}, 'policy_violation'),
    ({'risk': 'critical'}, 'policy_violation'),
    ({'risk': 'high', 'authorization': 'low'}, 'missing_authorization'),
    ({'scope': 'no', 'authorization': 'high'}, 'semantic_risk'),
])
async def test_typed_blocker_kind_is_host_composed(changes, expected):
    client = make_client(lambda req: httpx.Response(200, json=response_for(req, overrides=changes)))
    result = await client.review_safety({'authorized_task': 'task'}, 'policy')
    assert result['outcome'] == 'deny' and result['blocker_kind'] == expected


@pytest.mark.asyncio
async def test_irrelevant_authorization_confidence_does_not_block_low_risk_in_scope_action():
    def handler(req):
        value = response_for(req)
        value['answers']['authorization']['confidence'] = .1
        return httpx.Response(200, json=value)
    result = await make_client(handler).review_safety({'authorized_task': 'task'}, 'policy')
    assert result['outcome'] == 'allow'


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['confidence', 'missing', 'nan', 'wrong_choice', 'unknown', 'unauthorized', 'oversize'])
async def test_uncertain_invalid_or_failed_reviews_never_allow(tmp_path, defect):
    calls = []
    def handler(req):
        calls.append(req)
        value = response_for(req)
        if defect == 'confidence': value['answers']['risk']['confidence'] = .6
        if defect == 'missing': value['answers'].pop('scope')
        if defect == 'nan': value['answers']['risk']['confidence'] = 'NaN'
        if defect == 'wrong_choice': value['answers']['risk']['choice'] = 'critical'
        if defect == 'unknown': value = response_for(req, overrides={'scope': 'unknown'})
        return httpx.Response(401 if defect == 'unauthorized' else 200, json=value)
    reviewer = SafetyReviewer(ReviewRouter(make_client(handler)), str(tmp_path))
    result = await reviewer.review(tool_name='Bash', arguments={'command': 'x' * (40000 if defect == 'oversize' else 1)},
        risk={}, conversation=None)
    assert (result.inconclusive if defect in {'confidence', 'unknown'} else result.unavailable)
    assert not result.allowed and not result.escalatable
    assert len(calls) == (0 if defect == 'oversize' else 1 if defect in {'confidence', 'unknown'} else 2)


class VisionClient:
    capabilities = ('vision',)
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
    async def stream(self, conversation, system='', tools=None):
        self.calls.append((copy.deepcopy(conversation.history), system, tools))
        if self.fail:
            raise LLMError('visual offline')
        payload = json.loads(conversation.history[0].content)
        yield TextDelta(json.dumps({'observations': '页内标签可见；不能由图像确认授权',
            'uncertain': [], 'requires_joint_pixels': False,
            'covered_evidence_ids': payload.get('visual_evidence', {}).get('required_evidence_ids', [])}))
        yield StreamEnd('end_turn')


@pytest.mark.asyncio
@pytest.mark.parametrize('image_source', ['attachment', 'tool', 'openai'])
async def test_pixels_are_inspected_before_jev_makes_the_verdict(tmp_path, image_source):
    text = SimpleNamespace(review_only=True, review_safety=AsyncMock(return_value={'outcome': 'allow'}))
    vision = VisionClient()
    router = ReviewRouter(text, vision_resolver=lambda: (vision, {'provider_key': 'v', 'model': 'vision'}))
    conversation = ConversationManager()
    conversation.add_user_message('核对这张图后执行')
    if image_source == 'attachment': conversation.history[-1].content_blocks = [IMAGE]
    elif image_source == 'tool':
        conversation.history.append(Message(role='user', content='', tool_results=[ToolResultBlock('id', 'UNTRUSTED TOOL TEXT', content_blocks=[IMAGE])]))
    else: conversation.history[-1].content_blocks = [{'type': 'input_image', 'image_url': 'https://example.test/p.png'}]
    reviewer = SafetyReviewer(router, str(tmp_path))
    result = await reviewer.review(tool_name='Bash', arguments={'command': 'echo ok'},
                                   risk={'requires_vision': True}, conversation=conversation)
    assert result.allowed and result.review_route['pixel_access'] == 'inspected'
    assert text.review_safety.await_count == 1
    owner_payload = text.review_safety.call_args.args[0]
    assert 'InspectVisualEvidence' in json.dumps(owner_payload)
    assert 'pixel-bytes' not in json.dumps(owner_payload)
    blocks = vision.calls[0][0][0].content_blocks
    assert any(b['type'] == 'image' and (b.get('data') or b.get('url')) for b in blocks)
    payload = json.loads(vision.calls[0][0][0].content)
    assert 'UNTRUSTED TOOL TEXT' not in payload['authorized_task']
    assert vision.calls[0][2] == []  # The reviewer has no execution tools.


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing_model', 'missing_pixels', 'offline'])
async def test_visual_failure_never_falls_back_to_text(tmp_path, case):
    text = SimpleNamespace(review_only=True, review_safety=AsyncMock())
    vision = VisionClient(fail=case == 'offline')
    router = ReviewRouter(text, vision_resolver=None if case == 'missing_model' else lambda: (vision, {}))
    conversation = ConversationManager()
    conversation.history = [Message(role='user', content='check', content_blocks=[] if case == 'missing_pixels' else
        [{**IMAGE, 'data': str(i)} for i in range(9)] if case == 'too_many' else [IMAGE])]
    result = await SafetyReviewer(router, str(tmp_path)).review(tool_name='Bash', arguments={},
        risk={'requires_vision': True}, conversation=conversation)
    assert result.unavailable
    assert not result.allowed and not text.review_safety.called


@pytest.mark.asyncio
async def test_registry_catalog_and_provider_probe_use_evaluation_only():
    calls = []
    def handler(req):
        calls.append(req.url.path)
        if req.url.path == '/v1/models':
            return httpx.Response(200, json={'models': [{'name': 'jev-latest'}]})
        return httpx.Response(200, json=response_for(req))
    registry = ProviderRegistry(client_factory=lambda config: make_client(handler))
    registry.replace_desktop_profiles([PROFILE])
    catalog = await registry.catalog()
    model = catalog['models'][0]
    assert model['review_only'] and not model['vision'] and not model['tools']
    assert registry.resolve(ModelRef('desktop:jev', 'jev-latest')).capabilities == ('structured-review', 'structured-decision')
    probe = await registry.test_profile(PROFILE)
    assert probe['ok'] and probe['checks']['structured_evaluation']
    assert not probe['checks']['tool_calling'] and not probe['checks']['streaming']
    assert set(calls) == {'/v1/models', '/v1/systemone'}
    profile = ProviderProfile.from_payload(PROFILE)
    assert profile.protocol == 'typesafe' and profile.default_model == 'jev-latest'
    assert isinstance(create_client(ProviderConfig(name='j', protocol='typesafe', model='jev-latest',
        base_url='https://api.typesafe.ai/v1', api_key='fake')), TypeSafeClient)


@pytest.mark.asyncio
async def test_configured_route_is_explicit_and_broken_provider_never_uses_executor(tmp_path):
    settings = tmp_path / 'review.json'
    settings.write_text(json.dumps({'model_id': 'jev-latest', 'provider_key': 'jev',
        'vision_model_id': 'gemini-test', 'vision_provider_key': 'visual'}), encoding='utf-8')
    text = SimpleNamespace(review_only=True, review_safety=AsyncMock(return_value={'outcome': 'allow'}))
    vision = VisionClient()
    calls = []
    def resolve(ref, **kwargs):
        calls.append(ref.provider_key)
        return SimpleNamespace(client=text if ref.provider_key == 'jev' else vision, capabilities=('vision',))
    router = resolve_safety_review_client(SimpleNamespace(resolve=resolve), settings_path=settings)
    await router.review_safety({}, '', images=[IMAGE])
    assert calls == ['jev', 'visual'] and text.review_safety.await_count == 1
    def broken(*args, **kwargs): raise ValueError('offline')
    fallback = SimpleNamespace(review_safety=AsyncMock())
    router = resolve_safety_review_client(SimpleNamespace(resolve=broken), fallback_client=fallback, settings_path=settings)
    with pytest.raises(ValueError, match='未切换'):
        await router.review_safety({}, '')
    assert not fallback.review_safety.called


@pytest.mark.asyncio
async def test_pure_text_routes_only_to_jev(tmp_path):
    text = SimpleNamespace(review_only=True, review_safety=AsyncMock(return_value={'outcome': 'allow'}))
    vision_resolver = AsyncMock()
    result = await SafetyReviewer(ReviewRouter(text, vision_resolver=vision_resolver), str(tmp_path)).review(
        tool_name='ReadFile', arguments={'file_path': 'README.md'}, risk={}, conversation=None)
    assert result.allowed and text.review_safety.await_count == 1
    assert not vision_resolver.called


def test_vision_settings_validation_preservation_and_live_rebinding(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from frontend import server
    monkeypatch.setattr(server, 'REVIEW_MODEL_FILE', tmp_path / 'review.json')
    text = SimpleNamespace(review_only=True)
    def resolve(ref, **kwargs):
        if ref.provider_key == 'jev':
            return SimpleNamespace(client=text, capabilities=('structured-review',))
        return SimpleNamespace(client=VisionClient(), capabilities=('vision',))
    monkeypatch.setattr(server, 'MODEL_REGISTRY', SimpleNamespace(resolve=resolve))
    reviewer = SimpleNamespace(client=None, _denials={'old': 'denial'})
    monkeypatch.setattr(server, 'SESSION', SimpleNamespace(agent=SimpleNamespace(
        safety_reviewer=reviewer, auxiliary_client=None), client=None))
    client = TestClient(server.app)
    base = {'model_id': 'jev-latest', 'provider_key': 'jev',
        'vision_model_id': 'vision', 'vision_provider_key': 'visual'}
    assert client.post('/api/settings/review-model', json=base).status_code == 200
    assert isinstance(reviewer.client, ReviewRouter) and reviewer._denials == {}
    saved = server.REVIEW_MODEL_FILE.read_bytes()
    invalid = client.post('/api/settings/review-model', json={**base, 'vision_provider_key': 'jev', 'vision_model_id': 'jev-latest'})
    assert invalid.status_code == 400 and server.REVIEW_MODEL_FILE.read_bytes() == saved
    # An older client changing the text model must not erase the vision binding.
    changed = client.post('/api/settings/review-model', json={'model_id': 'jev-latest', 'provider_key': 'jev'})
    assert changed.json()['vision_provider_key'] == 'visual'
    cleared = client.post('/api/settings/review-model', json={**base, 'vision_model_id': '', 'vision_provider_key': ''})
    assert cleared.status_code == 200 and not cleared.json()['vision_model_id']
