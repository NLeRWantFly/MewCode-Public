from __future__ import annotations

from dataclasses import replace

import pytest

from mewcode.config import ProviderConfig
from mewcode.expert_group import ExpertProfile, relocate_expert_profile
from mewcode.expert_web import expert_flow_model_error, relocate_expert_flow
from mewcode.experts.flow import DEFAULT_EXPERT_FLOW
from mewcode.model_registry import ModelRef, ProviderRegistry, infer_brand
from mewcode.provider_sources import ProviderSlot


class _FakeClient:
    def __init__(self, models=None):
        self.models = models
        self.effort = ""
        self.recorder = None

    async def list_models(self):
        if isinstance(self.models, Exception):
            raise self.models
        return self.models

    def set_effort(self, effort):
        self.effort = effort

    def set_usage_recorder(self, recorder):
        self.recorder = recorder


def _provider(name="main", model="gpt-5.6-sol"):
    return ProviderConfig(
        name=name,
        protocol="openai-compat",
        base_url="https://example.test/v1",
        model=model,
        api_key="secret",
    )


@pytest.mark.asyncio
async def test_catalog_keeps_configured_model_when_list_endpoint_fails(monkeypatch):
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered(
        [ProviderSlot(provider=_provider(), source="config")]
    )
    monkeypatch.setattr(
        "mewcode.model_registry.create_client",
        lambda _config: _FakeClient(RuntimeError("no models endpoint")),
    )
    catalog = await registry.catalog()
    assert [item["model_id"] for item in catalog["models"]] == ["gpt-5.6-sol"]
    assert catalog["providers"][0]["available"] is True
    assert catalog["providers"][0]["models_authoritative"] is False
    assert "no models endpoint" in catalog["providers"][0]["catalog_error"]


@pytest.mark.asyncio
async def test_catalog_keeps_cc_switch_claude_models_without_flooding(monkeypatch):
    registry = ProviderRegistry(catalog_ttl=0)
    claude = ProviderConfig(
        name="cc-switch-claude",
        protocol="anthropic",
        base_url="https://relay.example/anthropic",
        model="claude-sonnet-4-5",
        api_key="sk-test",
    )
    explicit = [
        "claude-sonnet-4-5",
        "claude-opus-4-5",
        "claude-haiku-4-5",
        "claude-sonnet-4",
        "claude-opus-4",
        "claude-haiku-4",
    ]
    registry.refresh_discovered([
        ProviderSlot(
            provider=claude,
            source="cc-switch",
            origin="cc-switch:claude:relay",
            models=explicit,
        )
    ])
    monkeypatch.setattr(
        "mewcode.model_registry.create_client",
        lambda _config: (_ for _ in ()).throw(RuntimeError("should skip live list")),
    )
    catalog = await registry.catalog()
    model_ids = [item["model_id"] for item in catalog["models"]]
    assert model_ids == explicit


@pytest.mark.asyncio
async def test_catalog_live_fetches_when_cc_switch_only_has_role_mappings(monkeypatch):
    """Role env mappings (opus/sonnet) are not the full provider dropdown."""
    registry = ProviderRegistry(catalog_ttl=0)
    claude = ProviderConfig(
        name="cc-switch-claude",
        protocol="anthropic",
        base_url="https://relay.example/anthropic",
        model="claude-opus-5",
        api_key="sk-test",
    )
    registry.refresh_discovered([
        ProviderSlot(
            provider=claude,
            source="cc-switch",
            origin="cc-switch:claude:relay",
            models=["claude-opus-5", "claude-sonnet-5"],
        )
    ])
    live = [
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    ]
    monkeypatch.setattr(
        "mewcode.model_registry.create_client",
        lambda _config: _FakeClient(live),
    )
    catalog = await registry.catalog()
    model_ids = [item["model_id"] for item in catalog["models"]]
    assert model_ids == live
    assert catalog["providers"][0]["models_authoritative"] is True


@pytest.mark.asyncio
async def test_catalog_seeds_claude_only_when_slot_has_no_models(monkeypatch):
    registry = ProviderRegistry(catalog_ttl=0)
    claude = ProviderConfig(
        name="claude-code",
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-legacy-test",
        api_key="sk-test",
    )
    registry.refresh_discovered([
        ProviderSlot(provider=claude, source="claude-code", models=[])
    ])
    # Force the empty-list seed path (discovery normally keeps provider.model).
    key = next(iter(registry._discovered))
    record = registry._discovered[key]
    registry._discovered[key] = type(record)(
        **{**record.__dict__, "models": []}
    )
    monkeypatch.setattr(
        "mewcode.model_registry.create_client",
        lambda _config: (_ for _ in ()).throw(RuntimeError("should skip live list")),
    )
    catalog = await registry.catalog()
    model_ids = {item["model_id"] for item in catalog["models"]}
    assert model_ids
    assert "claude" not in model_ids
    assert any(item.startswith("claude-") for item in model_ids)


@pytest.mark.asyncio
async def test_catalog_marks_successful_model_listing_authoritative(monkeypatch):
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered(
        [ProviderSlot(provider=_provider(), source="config")]
    )
    monkeypatch.setattr(
        "mewcode.model_registry.create_client",
        lambda _config: _FakeClient(["gpt-5.6-sol", "gpt-5.6-terra"]),
    )
    catalog = await registry.catalog()
    assert catalog["providers"][0]["models_authoritative"] is True
    assert {model["model_id"] for model in catalog["models"]} == {
        "gpt-5.6-sol", "gpt-5.6-terra"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6.1-sol-latest", "openai/gpt-6.1-sol"])
async def test_gpt_61_sol_fetched_catalog_and_selected_route_agree(model):
    created = []

    def factory(config):
        created.append(config)
        return _FakeClient([model])

    provider = _provider()
    registry = ProviderRegistry(catalog_ttl=0, client_factory=factory)
    registry.refresh_discovered([ProviderSlot(provider=provider, source="config")])
    catalog = await registry.catalog()
    row = next(item for item in catalog["models"] if item["model_id"] == model)
    assert row["provider_key"] == "config:main" and row["available"]
    assert row["protocol"] == "openai"
    assert row["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert row["default_effort"] == "medium"
    assert row["context_window"] == 1_050_000 and row["max_output_tokens"] == 128_000
    assert row["vision"] and row["tools"]

    resolved = registry.resolve(ModelRef(row["provider_key"], model, "max"))
    assert resolved.protocol == row["protocol"]
    assert resolved.config.model == model and created[-1].model == model
    assert resolved.ref.model_id == model and resolved.effort == "max"
    assert resolved.client.effort == "max" and "vision" in resolved.capabilities
    assert provider.model == "gpt-5.6-sol" and provider.protocol == "openai-compat"


@pytest.mark.asyncio
async def test_catalog_keeps_codex_cached_models_and_per_model_efforts(monkeypatch):
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered([
        ProviderSlot(
            provider=_provider(),
            source="codex",
            models=["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
            model_efforts={
                "gpt-5.6-terra": ["low", "medium", "high", "xhigh", "ultra"],
                "gpt-5.6-luna": ["low", "medium", "high", "xhigh", "max"],
            },
        )
    ])
    def boom(_config):
        raise AssertionError("live list_models should not run")

    monkeypatch.setattr("mewcode.model_registry.create_client", boom)

    catalog = await registry.catalog()
    by_model = {item["model_id"]: item for item in catalog["models"]}

    assert set(by_model) == {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
    assert by_model["gpt-5.6-terra"]["efforts"][-1] == "ultra"
    assert by_model["gpt-5.6-luna"]["efforts"][-1] == "max"


def test_desktop_profile_public_payload_never_contains_secret():
    registry = ProviderRegistry()
    public = registry.replace_desktop_profiles(
        [
            {
                "id": "kimi-main",
                "name": "Kimi",
                "brand": "kimi",
                "base_url": "https://api.moonshot.cn/v1",
                "default_model": "kimi-k3",
                "api_key": "do-not-leak",
                "context_channel": "deepseek",
            }
        ]
    )
    assert public[0]["provider_key"] == "desktop:kimi-main"
    assert public[0]["has_secret"] is True
    assert public[0]["context_channel"] == "deepseek"
    assert registry.get("desktop:kimi-main").provider.context_channel == "deepseek"
    assert "api_key" not in public[0]
    assert "do-not-leak" not in repr(public)


def test_xai_desktop_profile_uses_grok_preset_without_shell_upgrade():
    registry = ProviderRegistry()
    public = registry.replace_desktop_profiles(
        [{"id": "grok-main", "brand": "xai", "api_key": "xai-secret"}]
    )

    record = registry.get("desktop:grok-main")
    provider = record.provider
    assert public[0]["brand"] == "xai"
    assert provider.protocol == "openai"
    assert provider.base_url == "https://api.x.ai/v1"
    assert provider.model == "grok-4.6"
    assert provider.get_context_window() == 500_000
    assert record.model_efforts["grok-4.6"] == [
        "low", "medium", "high", "xhigh"
    ]
    assert record.default_effort == "high"
    assert infer_brand("Grok", "grok-4.6", "https://api.x.ai/v1") == "xai"
    assert infer_brand("xAI") == "xai"


def test_resolve_rejects_model_outside_the_bound_provider_catalog(monkeypatch):
    registry = ProviderRegistry()
    registry.replace_desktop_profiles([{
        "id": "codex-only",
        "name": "Codex relay",
        "brand": "custom",
        "protocol": "openai-compat",
        "base_url": "https://relay.example/v1",
        "default_model": "gpt-6-astra",
        "models": ["codex-auto-review", "gpt-6-astra", "gpt-5.6-sol"],
        "api_key": "secret",
    }])
    monkeypatch.setattr(
        "mewcode.model_registry.create_client", lambda _config: _FakeClient()
    )

    with pytest.raises(ValueError, match='grok-4.6.*Codex relay'):
        registry.resolve(ModelRef("desktop:codex-only", "grok-4.6", "high"))
    assert "codex-auto-review" not in registry.get("desktop:codex-only").models
    with pytest.raises(ValueError, match="审批路由标签"):
        ModelRef.from_value({
            "provider_key": "desktop:codex-only",
            "model_id": "codex-auto-review",
        })

    nodes = list(DEFAULT_EXPERT_FLOW.nodes)
    nodes[0] = replace(
        nodes[0], model=ModelRef("desktop:codex-only", "grok-4.6", "high")
    )
    nodes[1] = replace(
        nodes[1], model=ModelRef("desktop:codex-only", "gpt-6-astra", "high")
    )
    flow = replace(DEFAULT_EXPERT_FLOW, nodes=tuple(nodes))
    assert "grok-4.6" in expert_flow_model_error(registry, flow)
    assert expert_flow_model_error(
        registry, flow, node_ids={"planner"}
    ) == ""


def test_relocate_moves_stale_model_onto_provider_that_lists_it(monkeypatch):
    registry = ProviderRegistry()
    registry.replace_desktop_profiles([
        {
            "id": "desktop:example_provider_1",
            "name": "desktop:example_provider_1",
            "brand": "custom",
            "protocol": "openai-compat",
            "base_url": "https://relay.example/v1",
            "default_model": "gpt-6-astra",
            "models": [
                "gpt-5.5",
                "gpt-5.6-luna",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-6-astra",
            ],
            "api_key": "secret",
        },
        {
            "id": "grok-main",
            "name": "desktop:example_provider_2",
            "brand": "xai",
            "api_key": "xai-secret",
        },
    ])
    monkeypatch.setattr(
        "mewcode.model_registry.create_client", lambda _config: _FakeClient()
    )

    stale = ModelRef("desktop:example_provider_1", "grok-4.6", "xhigh")
    with pytest.raises(ValueError, match="desktop:example_provider_1"):
        registry.resolve(stale)

    relocated = registry.relocate_model_ref(stale)
    assert relocated.provider_key == "desktop:grok-main"
    assert relocated.model_id == "grok-4.6"
    assert relocated.effort == "xhigh"
    resolved = registry.resolve(relocated)
    assert resolved.ref.provider_key == "desktop:grok-main"

    profile = ExpertProfile(
        manager=stale,
        executor=stale,
        auditor=stale,
        capability_executor=stale,
    )
    healed = relocate_expert_profile(registry, profile)
    assert healed.manager.provider_key == "desktop:grok-main"
    assert healed.executor.effort == "xhigh"
    # An explicit assistance slot is owned by the user and cannot silently
    # move to another provider while ordinary role bindings are healed.
    assert healed.capability_executor == stale

    nodes = list(DEFAULT_EXPERT_FLOW.nodes)
    nodes[0] = replace(nodes[0], model=stale)
    flow = replace(DEFAULT_EXPERT_FLOW, nodes=tuple(nodes))
    assert "grok-4.6" in expert_flow_model_error(registry, flow)
    healed_flow = relocate_expert_flow(registry, flow)
    assert healed_flow.nodes[0].model.provider_key == "desktop:grok-main"
    assert expert_flow_model_error(registry, healed_flow) == ""


def test_stale_gemini_does_not_relocate_to_unadvertised_example_provider_4():
    registry = ProviderRegistry()
    registry.refresh_discovered([
        ProviderSlot(provider=_provider(name="desktop:example_provider_4", model="gpt-5.6-sol"), source="config")
    ])
    registry.replace_desktop_profiles([{
        "id": "gemini", "name": "Gemini", "brand": "custom",
        "protocol": "openai-compat", "base_url": "https://gemini.example/v1",
        "default_model": "gemini-3-flash", "models": ["gemini-3-flash"],
        "api_key": "test",
    }])
    stale = ModelRef("desktop:gemini", "gemini-3.8-flash", "high")
    assert registry.relocate_model_ref(stale) == stale
    assert registry.find_provider_key_for_model(stale.model_id) == ""
    with pytest.raises(ValueError, match="Gemini"):
        registry.resolve(stale)


def test_ambiguous_relocation_preserves_binding_for_user_choice():
    registry = ProviderRegistry()
    registry.replace_desktop_profiles([{
        "id": name, "name": name, "brand": "custom",
        "protocol": "openai-compat", "base_url": f"https://{name}.example/v1",
        "default_model": "gemini-3-flash", "models": ["gemini-3-flash"],
        "api_key": "test",
    } for name in ("gemini-a", "gemini-b")])
    stale = ModelRef("desktop:removed", "gemini-3-flash", "high")
    assert registry.relocate_model_ref(stale) == stale
    explicit = ModelRef("desktop:gemini-b", "gemini-3-flash", "high")
    assert registry.relocate_model_ref(explicit) == explicit


def test_resolve_uses_referenced_provider_protocol_model_and_effort(monkeypatch):
    registry = ProviderRegistry()
    registry.refresh_discovered(
        [ProviderSlot(provider=_provider(), source="config")]
    )
    client = _FakeClient()
    monkeypatch.setattr("mewcode.model_registry.create_client", lambda _config: client)
    resolved = registry.resolve(
        ModelRef("config:main", "gpt-5.6-sol", "high")
    )
    assert resolved.protocol == "openai-compat"
    assert resolved.config.model == "gpt-5.6-sol"
    assert resolved.effort == "high"
    assert client.effort == "high"


def test_resolve_rewrites_baked_gemini_sku_to_base_and_effort(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr("mewcode.model_registry.create_client", lambda _config: client)
    registry = ProviderRegistry()
    registry.refresh_discovered([
        ProviderSlot(
            provider=_provider(model="gemini-3.8-flash"),
            source="config",
        )
    ])
    resolved = registry.resolve(
        ModelRef("config:main", "gemini-3.8-flash-high", "")
    )
    assert resolved.ref.model_id == "gemini-3.8-flash"
    assert resolved.config.model == "gemini-3.8-flash"
    assert resolved.effort == "high"
    assert client.effort == "high"


def test_resolve_keeps_effort_for_gemini_38_flash_family(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr("mewcode.model_registry.create_client", lambda _config: client)
    registry = ProviderRegistry()
    registry.refresh_discovered([
        ProviderSlot(
            provider=_provider(model="gemini-3.8-flash"),
            source="config",
        )
    ])
    resolved = registry.resolve(
        ModelRef("config:main", "gemini-3.8-flash", "high")
    )
    assert resolved.ref.model_id == "gemini-3.8-flash"
    assert resolved.config.model == "gemini-3.8-flash"
    assert resolved.effort == "high"
    assert client.effort == "high"


def test_resolve_keeps_base_gemini_on_official_google_endpoint(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr("mewcode.model_registry.create_client", lambda _config: client)
    google = ProviderConfig(
        name="main",
        protocol="openai-compat",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-3.8-flash",
        api_key="secret",
    )
    registry = ProviderRegistry()
    registry.refresh_discovered([ProviderSlot(provider=google, source="config")])
    resolved = registry.resolve(
        ModelRef("config:main", "gemini-3.8-flash", "high")
    )
    assert resolved.config.model == "gemini-3.8-flash"
    assert resolved.effort == "high"
    assert client.effort == "high"


@pytest.mark.asyncio
async def test_gemini_group_only_offers_its_advertised_effort_and_rejects_missing_sku(monkeypatch):
    client = _FakeClient(['gemini-3.8-flash-high'])
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered([ProviderSlot(provider=_provider(model='gemini-3.8-flash-high'), source='config')])
    constructed = []

    def build(config):
        constructed.append(config.model)
        return client

    monkeypatch.setattr('mewcode.model_registry.create_client', build)
    catalog = await registry.catalog()
    row = next(row for row in catalog['models'] if row['model_id'] == 'gemini-3.8-flash')
    assert row['efforts'] == ['high'] and row['default_effort'] == 'high'
    constructed.clear()
    with pytest.raises(ValueError, match='medium'):
        registry.resolve(ModelRef('config:main', 'gemini-3.8-flash', 'medium'))
    assert constructed == []
    resolved = registry.resolve(ModelRef('config:main', 'gemini-3.8-flash', 'high'))
    assert resolved.ref == ModelRef('config:main', 'gemini-3.8-flash', 'high')
    assert resolved.config.model == 'gemini-3.8-flash-high' and client.effort == ''


def test_base_only_gemini_relay_keeps_model_id_and_sends_effort_as_parameter(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr('mewcode.model_registry.create_client', lambda _: client)
    registry = ProviderRegistry()
    registry.refresh_discovered([ProviderSlot(provider=_provider(model='gemini-3.8-flash'), source='config')])
    resolved = registry.resolve(ModelRef('config:main', 'gemini-3.8-flash', 'medium'))
    assert resolved.config.model == 'gemini-3.8-flash' and client.effort == 'medium'


@pytest.mark.asyncio
async def test_catalog_hides_baked_gemini_effort_skus(monkeypatch):
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered([
        ProviderSlot(
            provider=_provider(model="gemini-3.8-flash"),
            source="config",
        )
    ])
    seen_models: list[str] = []

    def _factory(config):
        seen_models.append(config.model)
        return _FakeClient([
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash",
        ])

    monkeypatch.setattr("mewcode.model_registry.create_client", _factory)
    catalog = await registry.catalog()
    ids = [row["model_id"] for row in catalog["models"]]
    assert ids == ["gemini-3.8-flash"]
    assert catalog["models"][0]["efforts"] == ["low", "medium", "high"]
    resolved = registry.resolve(
        ModelRef("config:main", "gemini-3.8-flash", "high")
    )
    assert resolved.ref.model_id == "gemini-3.8-flash"
    assert resolved.config.model == "gemini-3.8-flash-high"
    assert seen_models[-1] == "gemini-3.8-flash-high"


def test_gpt_56_official_efforts_and_provider_declared_ultra(monkeypatch):
    monkeypatch.setattr("mewcode.model_registry.create_client", lambda _config: _FakeClient())
    official = ProviderRegistry()
    official.refresh_discovered([ProviderSlot(provider=_provider(), source="config")])
    assert official.resolve(ModelRef("config:main", "gpt-5.6-sol", "max")).effort == "max"
    assert official.resolve(ModelRef("config:main", "gpt-5.6-sol", "ultra")).effort == "xhigh"

    declared = ProviderRegistry()
    declared.refresh_discovered([
        ProviderSlot(
            provider=_provider(), source="config",
            effort_levels=["low", "high", "xhigh", "ultra"],
        )
    ])
    assert declared.resolve(ModelRef("config:main", "gpt-5.6-sol", "ultra")).effort == "ultra"


def test_desktop_profile_validation_rejects_missing_secret():
    registry = ProviderRegistry()
    with pytest.raises(ValueError, match="required"):
        registry.replace_desktop_profiles(
            [
                {
                    "id": "broken",
                    "brand": "openai",
                    "default_model": "gpt-5.6-sol",
                }
            ]
        )
