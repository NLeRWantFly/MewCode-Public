from dataclasses import replace
import json
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
from openai import AsyncOpenAI
import pytest

from frontend.fast_mode import FastModeSettings, install
from mewcode.client import LLMError, OpenAIClient, OpenAICompatClient
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.model_registry import ProviderRegistry
from mewcode.provider_sources import ProviderSlot
from mewcode.tools.base import StreamEnd


MODEL = 'gpt-6.1-sol'


class LocalClient:
    def __init__(self, config):
        self.service_tier = config.service_tier
        self.effort = ''

    def set_effort(self, effort):
        self.effort = effort


def registry(*, client_factory=LocalClient, desktop_source=None):
    value = ProviderRegistry(client_factory=client_factory, desktop_source=desktop_source)
    config = ProviderConfig(name='relay', protocol='openai', base_url='http://127.0.0.1:9191/v1',
                            model=MODEL, api_key='offline-test-only')
    slot = ProviderSlot(provider=config, source='config', registry_key='test:relay',
                        effort_levels=['max'], models=[MODEL])
    value.refresh_discovered([slot, replace(slot, registry_key='test:other')])
    return value, slot


def ref(key='test:relay'):
    return dict(provider_key=key, model_id=MODEL, effort='max')


def test_fast_preference_reaches_primary_nodes_and_scoped_clients_without_changing_effort(tmp_path):
    root, slot = registry()
    scoped, _ = registry(desktop_source=root)
    settings = FastModeSettings(tmp_path / 'speed.json', root)
    primary = LocalClient(slot.provider)
    primary.set_effort('max')
    root.track_service_tier(slot.key, MODEL, primary)
    node = scoped.resolve(ref())
    unrelated = root.resolve(ref('test:other'))
    settings.change(slot.key, MODEL, True)
    assert primary.service_tier == node.client.service_tier == 'fast'
    assert primary.effort == node.client.effort == 'max'
    assert unrelated.client.service_tier == ''
    future = scoped.resolve(ref())
    assert future.config.service_tier == future.client.service_tier == 'fast'
    assert future.ref.as_dict() == ref()
    assert root.apply_service_tier(slot.key, replace(slot.provider, model='other-model')).service_tier == ''
    assert slot.provider.service_tier == ''
    settings.change(slot.key, MODEL, False)
    assert primary.service_tier == node.client.service_tier == future.client.service_tier == 'default'
    assert node.client.effort == 'max'


def test_fast_override_survives_restart_and_an_explicit_off_overrides_provider_defaults(tmp_path):
    path = tmp_path / 'speed.json'
    original, slot = registry()
    FastModeSettings(path, original).change(slot.key, MODEL, True)
    restarted, _ = registry()
    settings = FastModeSettings(path, restarted)
    assert restarted.resolve(ref()).config.service_tier == 'fast'
    settings.change(slot.key, MODEL, False)
    assert restarted.apply_service_tier(slot.key, replace(slot.provider, service_tier='fast')).service_tier == 'default'
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved == {'models': [dict(provider_key=slot.key, model_id=MODEL, service_tier='default')]}


def test_failed_save_does_not_modify_a_live_request_tier(tmp_path, monkeypatch):
    root, slot = registry()
    settings = FastModeSettings(tmp_path / 'speed.json', root)
    client = root.resolve(ref()).client
    def fail(*_):
        raise OSError('disk unavailable')
    monkeypatch.setattr('frontend.fast_mode.os.replace', fail)
    with pytest.raises(OSError):
        settings.change(slot.key, MODEL, True)
    assert client.service_tier == '' and settings.values == {}
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol,base_url', [
    ('openai', 'https://chatgpt.com/backend-api/codex'),
    ('openai', 'http://127.0.0.1:9191/v1'),
    ('openai-compat', 'https://relay.invalid/v1'),
])
async def test_fast_and_standard_are_serialized_on_the_selected_route(protocol, base_url):
    requests = []
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if protocol == 'openai':
            event = dict(type='response.completed', response=dict(id='response', status='completed',
                service_tier='default', output=[], usage=dict(input_tokens=1, output_tokens=1)))
        else:
            event = dict(id='chat', choices=[], service_tier='default',
                         usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            content='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n')
    config = ProviderConfig(name='test', protocol=protocol, base_url=base_url, model=MODEL,
                            api_key='offline-test-only', service_tier='fast')
    client = (OpenAIClient if protocol == 'openai' else OpenAICompatClient)(config)
    await client._client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = AsyncOpenAI(api_key='offline-test-only', base_url=base_url,
                                     http_client=transport, max_retries=0)
        client.set_effort('max')
        chat = ConversationManager()
        chat.add_user_message('hello')
        for tier in ('fast', 'default'):
            client.service_tier = tier
            events = [event async for event in client.stream(chat)]
            assert requests[-1]['service_tier'] == tier and requests[-1]['model'] == MODEL
            assert requests[-1].get('reasoning', {}).get('effort', requests[-1].get('reasoning_effort')) == 'max'
            assert next(event for event in events if isinstance(event, StreamEnd)).service_tier == 'default'
    assert len(requests) == 2  # No fallback, probe or extra model request.


@pytest.mark.asyncio
async def test_relay_rejection_keeps_fast_intent_and_reports_the_provider_error():
    requests = []
    def reject(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={'error': {'message': 'Fast service_tier unavailable',
            'type': 'invalid_request_error', 'param': 'service_tier'}})
    base_url = 'http://127.0.0.1:9191/v1'
    client = OpenAIClient(ProviderConfig(name='relay', protocol='openai', base_url=base_url,
        model=MODEL, api_key='offline-test-only', service_tier='fast'))
    await client._client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as transport:
        client._client = AsyncOpenAI(api_key='offline-test-only', base_url=base_url,
                                     http_client=transport, max_retries=0)
        client.set_effort('max')
        chat = ConversationManager()
        chat.add_user_message('hello')
        with pytest.raises(LLMError, match='Fast service_tier unavailable'):
            _ = [event async for event in client.stream(chat)]
    assert len(requests) == 1 and requests[0]['service_tier'] == client.service_tier == 'fast'
    assert client.effort == 'max'


@pytest.mark.asyncio
async def test_fast_route_is_bound_to_current_conversation_and_model(tmp_path):
    root, slot = registry()
    settings = FastModeSettings(tmp_path / 'speed.json', root)
    live = root.resolve(ref()).client
    session = SimpleNamespace(slot=slot, status_payload=lambda: dict(conversation_id='c', model=MODEL,
        provider_key=slot.key, effort='max', **settings.status(slot, live)))
    server = SimpleNamespace(FAST_MODE_SETTINGS=settings, SESSION=session, _active_conversation_id=lambda: 'c')
    app = FastAPI()
    install(app, server)
    body = dict(conversation_id='c', provider_key=slot.key, model_id=MODEL, enabled=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post('/api/settings/fast-mode', json=body)).status_code == 403
        headers = {'X-Mew-Fast-Mode': '1'}
        for changed in (dict(conversation_id='other'), dict(provider_key='test:other'), dict(model_id='other'),
                        dict(enabled='true')):
            response = await client.post('/api/settings/fast-mode', headers=headers, json={**body, **changed})
            assert response.status_code == 409
        assert not settings.path.exists()
        response = await client.post('/api/settings/fast-mode', headers=headers, json=body)
        assert response.status_code == 200
        assert response.json()['status']['fast_mode'] is True
        assert response.json()['status']['effort'] == 'max'
        assert live.service_tier == 'fast'
