from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mewcode.client import AnthropicClient, model_effort_levels, model_supports_vision
from mewcode.cordis import Context
from mewcode.config import ProviderConfig
from mewcode.model_catalog import (
    MODEL_CATALOG_SERVICE,
    MODEL_METADATA_SLOT,
    ModelCatalogService,
    ModelDescriptor,
    canonicalize_selectable_model_id,
    is_official_gemini_endpoint,
    model_catalog_plugin,
    select_gemini_wire_model_id,
    split_gemini_baked_effort_sku,
)
from mewcode.model_registry import ProviderRegistry
from mewcode.provider_sources import ProviderSlot


def _provider(model: str = "gpt-5.6-sol") -> ProviderConfig:
    return ProviderConfig(
        name="catalog-test",
        protocol="openai-compat",
        base_url="https://example.test/v1",
        model=model,
        api_key="secret",
    )


class _CatalogClient:
    def __init__(self, models=None):
        self.models = models
        self.effort = ""

    async def list_models(self):
        return self.models

    def set_effort(self, effort: str):
        self.effort = effort


def test_builtin_catalog_is_public_json_and_has_sources() -> None:
    service = ModelCatalogService()
    rows = service.public_catalog()
    assert rows
    assert {row["model_id"] for row in rows} >= {
        "gpt-5.6-sol",
        "claude-sonnet-4-6",
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "deepseek-v4-pro",
        "grok-4.6",
        "grok-4.5",
        "kimi-k3",
        "glm-5.3-flash",
        "minimax-m3",
    }
    assert all(row["source_url"] and row["source_label"] and row["verified_at"] for row in rows)
    json.dumps(rows)


def test_exact_and_prefix_matching_prefers_specific_family() -> None:
    service = ModelCatalogService()
    assert service.describe("gpt-5.6-sol").model_id == "gpt-5.6-sol"
    assert service.describe("gpt-5.6-sol-latest").model_id == "gpt-5.6-sol"
    assert service.describe("openrouter/deepseek-v4-pro:free").model_id == "deepseek-v4-pro"
    assert service.describe("claude-sonnet-4-6-20260201").model_id == "claude-sonnet-4-6"
    assert service.describe("claude-legacy-test").efforts == (
        "off", "low", "medium", "high"
    )
    grok = service.describe("grok-4.6-latest")
    assert grok.model_id == "grok-4.6"
    assert grok.context_window == 500_000
    assert grok.default_effort == "high"


def test_effort_levels_distinguish_current_families_and_fallbacks() -> None:
    service = ModelCatalogService()
    assert service.effort_levels("gpt-5.6-terra") == [
        "none", "low", "medium", "high", "xhigh", "max"
    ]
    assert service.effort_levels("claude-sonnet-4-6") == [
        "low", "medium", "high", "max"
    ]
    assert service.effort_levels("claude-sonnet-5")[-2:] == ["xhigh", "max"]
    assert service.effort_levels("claude-opus-4-6")[-1:] == ["max"]
    assert service.effort_levels("gemini-3.7-flash") == ["low", "medium", "high"]
    assert service.effort_levels("gemini-3.8-flash") == ["low", "medium", "high"]
    assert service.describe("gemini-3.8-flash").model_id == "gemini-3.8-flash"
    assert model_effort_levels("openai-compat", "gemini-3.8-flash") == [
        "low", "medium", "high",
    ]
    assert split_gemini_baked_effort_sku("gemini-3.8-flash-high") == (
        "gemini-3.8-flash", "high",
    )
    assert split_gemini_baked_effort_sku("google/gemini-3.8-flash-medium") == (
        "google/gemini-3.8-flash", "medium",
    )
    assert split_gemini_baked_effort_sku("gemini-2.5-flash-lite") is None
    assert canonicalize_selectable_model_id("gemini-3.8-flash-high") == "gemini-3.8-flash"
    assert "gemini-3.8-flash-high" not in {
        row["model_id"] for row in service.public_catalog()
    }
    assert service.effort_levels("gemini-3.6-flash")[0] == "minimal"
    assert service.effort_levels("gemini-2.5-flash") == ["low", "medium", "high"]
    assert service.effort_levels("deepseek-v4-flash") == [
        "off", "low", "high", "max"
    ]
    assert service.effort_levels("grok-4.6") == [
        "low", "medium", "high", "xhigh"
    ]
    assert model_effort_levels("openai", "grok-4.6") == [
        "low", "medium", "high", "xhigh"
    ]
    assert model_effort_levels("openai", "grok-4.7") == [
        "low", "medium", "high", "xhigh"
    ]
    assert model_effort_levels("openai", "grok-4.7-build-fast") == [
        "low", "medium", "high", "xhigh"
    ]
    assert model_effort_levels("anthropic", "claude-sonnet-4") == [
        "off", "low", "medium", "high"
    ]


def test_select_gemini_wire_model_id_bakes_unprefixed_relay_skus() -> None:
    assert is_official_gemini_endpoint(
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    assert not is_official_gemini_endpoint("https://example.test/v1")
    assert select_gemini_wire_model_id(
        "gemini-3.8-flash",
        "high",
        ["gemini-3.8-flash-high", "gemini-3.8-flash-medium"],
    ) == "gemini-3.8-flash-high"
    assert select_gemini_wire_model_id(
        "gemini-3.8-flash",
        "high",
        ["gemini-3.8-flash"],
    ) == "gemini-3.8-flash"
    assert select_gemini_wire_model_id(
        "gemini-3.8-flash",
        "high",
        ["gemini-3.8-flash"],
        official_gemini_endpoint=True,
    ) == "gemini-3.8-flash"
    assert select_gemini_wire_model_id(
        "google/gemini-3.8-flash",
        "high",
        ["google/gemini-3.8-flash"],
    ) == "google/gemini-3.8-flash"
    assert select_gemini_wire_model_id(
        "google/gemini-3.8-flash",
        "high",
        ["google/gemini-3.8-flash", "google/gemini-3.8-flash-high"],
    ) == "google/gemini-3.8-flash-high"
    with pytest.raises(ValueError, match='medium'):
        select_gemini_wire_model_id('gemini-3.8-flash', 'medium', ['gemini-3.8-flash-high'])


def test_deepseek_vision_experiment_has_exact_catalog_metadata() -> None:
    service = ModelCatalogService()
    descriptor = service.describe("deepseek-v4-flash-vision-exp")
    assert descriptor is not None
    assert descriptor.model_id == "deepseek-v4-flash-vision-exp"
    assert descriptor.vision is True
    assert descriptor.context_window == 1_000_000
    assert descriptor.max_output_tokens == 384_000
    assert descriptor.efforts == ("off", "low", "high", "max")
    assert model_supports_vision(
        "openai-compat",
        "deepseek-v4-flash-vision-exp",
        catalog_service=service,
    ) is True
    flash = service.describe("deepseek-flash")
    assert flash is not None
    assert flash.model_id == "deepseek-v4-flash"
    assert flash.vision is True
    assert model_supports_vision("openai-compat", "deepseek-v4-flash", catalog_service=service)
    assert model_supports_vision("openai-compat", "deepseek-v4-pro", catalog_service=service) is False


def test_official_catalog_context_and_vision_flags() -> None:
    """Pin vendor-documented windows and image support (checked 2026-09-17)."""

    service = ModelCatalogService()
    expected = {
        "gpt-6.1-sol": (1_050_000, True),
        "gpt-6-astra": (1_050_000, True),
        "gpt-6-sol": (1_050_000, True),
        "gpt-6-luna": (1_050_000, True),
        "gpt-5.6-sol": (1_050_000, True),
        "claude-sonnet-4-6": (1_000_000, True),
        "claude-opus-4-6": (1_000_000, True),
        "claude-sonnet-4": (200_000, True),
        "gemini-3.8-flash": (1_048_576, True),
        "gemini-3-flash": (1_048_576, True),
        "gemini-3.1-flash-image": (131_072, True),
        "deepseek-v4-flash": (1_000_000, True),
        "deepseek-v4-pro": (1_000_000, False),
        "grok-4.6": (500_000, True),
        "grok-4.5": (500_000, True),
        "kimi-k3": (1_000_000, True),
        "kimi-k3-1": (300_000, True),
        "kimi-k2.7-code": (256_000, True),
        "kimi-k2.7": (256_000, True),
        "glm-5.3": (1_000_000, False),
        "glm-5.3-flash": (1_000_000, True),
        "glm-5.2": (1_000_000, False),
        "glm-5v-turbo": (200_000, True),
        "glm-4.6v": (128_000, True),
        "minimax-m3": (1_000_000, True),
        "minimax-m2.7": (204_800, False),
        "minimax-m2.5": (204_800, False),
        "qwen3.8-max": (1_000_000, True),
        "qwen3.8-flash": (1_000_000, True),
        "mimo-v2.5": (1_000_000, True),
        "mimo-v2.5-pro": (1_000_000, False),
        "mimo-v2-omni": (256_000, True),
        "hy3": (256_000, False),
        "hy3-preview": (256_000, False),
    }
    for model_id, (window, vision) in expected.items():
        descriptor = service.describe(model_id)
        assert descriptor is not None, model_id
        assert descriptor.context_window == window, model_id
        assert descriptor.vision is vision, model_id
        assert model_supports_vision(
            "openai-compat", model_id, catalog_service=service
        ) is vision, model_id


def test_contribution_register_and_unload() -> None:
    service = ModelCatalogService()
    descriptor = ModelDescriptor(
        model_id="vendor-next",
        family="vendor-next",
        provider_brand="vendor",
        efforts=("low", "high"),
        default_effort="high",
        context_window=123,
        source_url="https://vendor.example/models",
        source_label="Vendor model docs",
    )
    token = service.register_contribution(descriptor)
    assert service.describe("vendor-next").context_window == 123
    assert service.unregister_contribution(token)
    assert service.describe("vendor-next") is None


def test_plugin_service_and_metadata_slot_lifecycle() -> None:
    service = ModelCatalogService()
    ctx = Context()
    ctx.load(model_catalog_plugin(service))
    assert ctx.get(MODEL_CATALOG_SERVICE) is service
    extension = ModelDescriptor(
        model_id="plugin-model",
        family="plugin-model",
        provider_brand="plugin",
        efforts=("medium",),
        default_effort="medium",
    )
    registration = ctx.slots.register(MODEL_METADATA_SLOT, extension)
    assert service.describe("plugin-model") is extension
    registration.dispose()
    ctx.unload(MODEL_CATALOG_SERVICE)
    assert service.describe("plugin-model") is None


@pytest.mark.asyncio
async def test_provider_registry_enriches_only_configured_or_reported_models(monkeypatch) -> None:
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered(
        [ProviderSlot(provider=_provider(), source="config", models=["gpt-5.6-sol"])]
    )
    monkeypatch.setattr(
        "mewcode.model_registry.create_client",
        lambda _config: _CatalogClient(["gpt-5.6-sol"]),
    )
    catalog = await registry.catalog()
    rows = catalog["models"]
    assert [row["model_id"] for row in rows] == ["gpt-5.6-sol"]
    row = rows[0]
    assert row["available"] is True
    assert row["context_window"] == 1_050_000
    assert row["max_output_tokens"] == 128_000
    assert row["source_url"]
    assert all(row["model_id"] != "claude-sonnet-4-6" for row in rows)


@pytest.mark.asyncio
async def test_provider_registry_exposes_grok_47_efforts_without_stable_catalog_row() -> None:
    registry = ProviderRegistry(client_factory=lambda _config: _CatalogClient())
    registry.replace_desktop_profiles([{
        "provider_key": "desktop:grok-47",
        "name": "Grok Build",
        "brand": "xai",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:8000/v1",
        "default_model": "grok-4.7",
        "models": ["grok-4.7", "grok-4.7-build-fast"],
        "api_key": "test-only",
    }])

    catalog = await registry.catalog(local_only=True)
    efforts = {
        row["model_id"]: row["efforts"]
        for row in catalog["models"]
        if row["provider_key"] == "desktop:grok-47"
    }
    assert efforts == {
        "grok-4.7": ["low", "medium", "high", "xhigh"],
        "grok-4.7-build-fast": ["low", "medium", "high", "xhigh"],
    }


def test_modern_anthropic_accepts_catalog_effort_and_keeps_legacy_budget_mode() -> None:
    modern = AnthropicClient(ProviderConfig(
        name="anthropic",
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-sonnet-5",
        api_key="secret",
        thinking=True,
    ))
    modern.set_effort("xhigh")
    assert modern.effort == "xhigh"
    assert modern._uses_catalog_effort_wire is True

    legacy = AnthropicClient(ProviderConfig(
        name="anthropic",
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-legacy-test",
        api_key="secret",
        thinking=True,
    ))
    legacy.set_effort("max")
    assert legacy.effort == "high"
    assert legacy._uses_catalog_effort_wire is False


@pytest.mark.asyncio
async def test_modern_anthropic_wire_uses_output_config_effort(monkeypatch) -> None:
    import mewcode.client as client_module
    from mewcode.conversation import ConversationManager

    captured: dict = {}

    class FakeStream:
        response = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def __aiter__(self):
            async def events():
                yield SimpleNamespace(type="message_stop")

            return events()

        async def get_final_message(self):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                stop_reason="end_turn",
                id="msg_test",
                model="claude-sonnet-5",
            )

    class FakeMessages:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return FakeStream()

    class FakeAnthropic:
        def __init__(self, **_kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(client_module, "AsyncAnthropic", FakeAnthropic)
    client = AnthropicClient(ProviderConfig(
        name="anthropic",
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-sonnet-5",
        api_key="secret",
        thinking=True,
    ))
    client.set_effort("xhigh")
    conversation = ConversationManager()
    conversation.add_user_message("hello")
    _ = [event async for event in client.stream(conversation)]
    assert captured["output_config"] == {"effort": "xhigh"}
    assert "thinking" not in captured
