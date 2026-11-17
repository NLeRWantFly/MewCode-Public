from types import SimpleNamespace

import pytest

from mewcode.client import OpenAIClient, create_client, model_supports_vision, _uses_gpt_5_6_prompt_cache
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.context.routing import ContextRouter
from mewcode.model_catalog import ModelCatalogService


def config(base_url="https://api.openai.com/v1", model="gpt-6-astra"):
    return ProviderConfig(name="test", protocol="openai-compat", base_url=base_url, model=model, api_key="test-only")


@pytest.mark.parametrize("family", ["gpt-6-astra", "gpt-6.1-sol"])
def test_astra_catalog_context_effort_and_vision(family):
    catalog = ModelCatalogService()
    for model in (family, family + "-latest", "openai/" + family):
        entry = catalog.describe(model)
        assert entry.family == family
        assert entry.context_window == 1_050_000 and entry.max_output_tokens == 128_000
        assert "none" not in entry.efforts and "max" in entry.efforts
        assert model_supports_vision("openai-compat", model)
        assert ContextRouter().resolve(model).name == ("openai-gpt-6-astra" if family == "gpt-6-astra" else "openai-gpt-6")
        assert _uses_gpt_5_6_prompt_cache(model)


@pytest.mark.parametrize("family", ["gpt-6-astra", "gpt-6.1-sol"])
def test_astra_uses_responses_without_changing_other_provider_models(family):
    from mewcode.opencode_go import resolve_effective_provider_config
    original = config(model=family)
    assert resolve_effective_provider_config(original).protocol == "openai"
    assert original.protocol == "openai-compat"  # Do not mutate the user's saved provider.
    assert isinstance(create_client(config(model=family)), OpenAIClient)
    assert not isinstance(create_client(config(model="gpt-5.6-sol")), OpenAIClient)
    direct = create_client(config(model=family))
    direct.set_effort("none")
    assert direct.effort == "low"
    direct.set_effort("ultra")
    assert direct.effort == "max"
    custom = create_client(config("https://provider.invalid/v1", model=family))
    custom.set_effort("ultra")
    assert custom.effort == "ultra"


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["gpt-6-astra", "gpt-6.1-sol"])
async def test_astra_wire_request_uses_responses_tools_and_output_cap(family):
    client = create_client(config(model=family))
    client.set_effort("high")
    client.response_token_limit = 2048
    captured = []
    async def empty_stream():
        if False:
            yield None
    async def create(**kwargs):
        captured.append(kwargs)
        return empty_stream()
    client._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    conversation = ConversationManager()
    conversation.add_user_message("核验结果")
    tools = [{"type": "function", "name": "check", "parameters": {"type": "object", "properties": {}}}]
    _ = [event async for event in client.stream(conversation, system="只核验", tools=tools)]
    wire = captured[0]
    assert wire["model"] == family and wire["tools"] == tools
    assert wire["max_output_tokens"] == 2048 and wire["reasoning"] == {"effort": "high"}
    assert not {"temperature", "top_p", "logprobs"} & wire.keys()
