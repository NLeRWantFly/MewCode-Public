"""Offline regression for loop output budgets on the ChatGPT Codex route."""
import json

import httpx
import pytest
from openai import AsyncOpenAI

from mewcode.client import OpenAIClient
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['gpt-6-astra', 'gpt-5.6-sol'])
@pytest.mark.parametrize('base_url, supports_cap', [
    ('https://chatgpt.com/backend-api/codex', False),
    ('https://chatgpt.com/backend-api/codex/', False),
    ('https://CHATGPT.COM/backend-api/codex', False),
    ('https://chatgpt.com/backend-api/codex/responses', False),
    ('https://api.openai.com/v1', True),
    ('https://provider.invalid/v1', True),
    ('https://chatgpt.com.example.invalid/backend-api/codex', True),
    ('https://chatgpt.com/other', True),
])
async def test_budget_cap_serialization_is_endpoint_specific(base_url, supports_cap, model):
    captured = []

    def handle(request):
        payload = json.loads(request.content)
        captured.append(payload)
        if not supports_cap and 'max_output_tokens' in payload:
            return httpx.Response(400, json={'detail': 'Unsupported parameter: max_output_tokens'})
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              content='data: [DONE]\n\n')

    config = ProviderConfig(name='codex-custom', protocol='openai', base_url=base_url,
                            model=model, api_key='offline-test-only')
    client = OpenAIClient(config)
    await client._client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as transport:
        client._client = AsyncOpenAI(api_key='offline-test-only', base_url=base_url,
                                     http_client=transport, max_retries=0)
        client.set_effort('high')
        conversation = ConversationManager()
        conversation.add_user_message('Extract the supplied paragraph only.')
        tools = [{'type': 'function', 'name': 'SubmitHandoff',
                  'parameters': {'type': 'object', 'properties': {}}}]
        # Initial loop budget and a smaller budget on the next request must
        # both follow the same route policy, without clearing local accounting.
        for limit in (4096, 2048):
            client.set_max_output_tokens(limit)
            client.response_token_limit = limit
            _ = [event async for event in client.stream(conversation, system='Bounded extraction', tools=tools)]
            assert client.max_output_tokens == client.response_token_limit == limit
            wire = captured[-1]
            assert wire['model'] == model and wire['reasoning'] == {'effort': 'high'}
            assert wire['tools'] == tools and wire['store'] is False and wire['stream'] is True
            if supports_cap:
                assert wire['max_output_tokens'] == limit
            else:
                assert 'max_output_tokens' not in wire and 'max_tokens' not in wire
        assert len(captured) == 2  # No failed request/retry or model fallback.
