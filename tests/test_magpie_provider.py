import json

import pytest

from mewcode.config import ProviderConfig
from mewcode.model_registry import ModelRef, ProviderRegistry, infer_brand
from mewcode.provider_sources import ProviderSlot, discover_provider_slots


@pytest.mark.parametrize("environment", [False, True])
def test_magpie_uses_own_credentials_and_catalog(tmp_path, monkeypatch, environment):
    home = tmp_path / "home"
    config = home / ".codex"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "max"\n'
        '[model_providers.magpie]\nname = "magpie"\n'
        'base_url = "http://127.0.0.1:3425/v1"\nwire_api = "responses"\n'
        'requires_openai_auth = true\nexperimental_bearer_token = "magpie-secret"\n'
        'env_key = "MAGPIE_TEST_KEY"\n', encoding="utf-8")
    (config / "auth.json").write_text(json.dumps({"tokens": {
        "access_token": "oauth-secret", "account_id": "chatgpt-account"}}), encoding="utf-8")
    (config / "models_cache.json").write_text(json.dumps({"models": [{
        "slug": "unrelated-codex-cache", "visibility": "list", "supported_in_api": True}]}), encoding="utf-8")
    if environment:
        monkeypatch.setenv("MAGPIE_TEST_KEY", "environment-secret")
    else:
        monkeypatch.delenv("MAGPIE_TEST_KEY", raising=False)
    slots = discover_provider_slots([], home=home, work_dir=tmp_path, cc_switch_bindings=[{
        "app_type": "codex", "name": "other", "api_key": "cc-switch-secret",
        "base_url": "http://127.0.0.1:3425/backend-api/codex", "model": "other-model",
        "protocol": "openai"}])
    slot = next(row for row in slots if row.key == "codex:codex-magpie")
    assert slot.provider.api_key == ("environment-secret" if environment else "magpie-secret")
    assert not slot.provider.default_headers
    assert not slot.provider._codex_auth_path
    assert slot.provider.base_url == "http://127.0.0.1:3425/v1"
    assert slot.models == ["codex/gpt-6.1-sol"]
    assert slot.default_effort == "max"
    assert infer_brand(slot.provider.name, slot.provider.model, slot.provider.base_url) == "magpie"


@pytest.mark.asyncio
async def test_magpie_catalog_uses_advertised_routes_and_keeps_wire_identity(monkeypatch):
    calls = []
    class Client:
        effort = ""
        async def list_models(self):
            calls.append("catalog")
            return ["codex/gpt-6.1-sol", "desktop:example_provider_4-ai/grok-4.7", "desktop:example_provider_4/glm-5.3"]
        def set_effort(self, effort): self.effort = effort
        def set_usage_recorder(self, recorder): pass
    def create(config):
        calls.append(config.model)
        return Client()
    monkeypatch.setattr("mewcode.model_registry.create_client", create)
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered([ProviderSlot(source="codex", models=["gpt-6.1-sol", "cache-only"],
        provider=ProviderConfig(name="codex-magpie", protocol="openai", base_url="http://localhost:3425/v1",
                                model="codex/gpt-6.1-sol", api_key="test"))])
    catalog = await registry.catalog()
    assert "catalog" in calls
    assert {row["model_id"] for row in catalog["models"]} == {
        "codex/gpt-6.1-sol", "desktop:example_provider_4-ai/grok-4.7", "desktop:example_provider_4/glm-5.3"}
    assert catalog["providers"][0]["brand"] == "magpie"
    assert catalog["providers"][0]["models_authoritative"]
    sol = next(row for row in catalog["models"] if row["model_id"] == "codex/gpt-6.1-sol")
    assert "max" in sol["efforts"] and sol["vision"]
    resolved = registry.resolve(ModelRef("codex:codex-magpie", "codex/gpt-6.1-sol", "max"))
    assert resolved.config.model == "codex/gpt-6.1-sol"


@pytest.mark.asyncio
async def test_magpie_offline_catalog_reports_stale_without_other_provider(monkeypatch):
    class Client:
        async def list_models(self): return None
    monkeypatch.setattr("mewcode.model_registry.create_client", lambda _: Client())
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered([ProviderSlot(source="codex", provider=ProviderConfig(
        name="codex-magpie", protocol="openai", base_url="http://localhost:3425/v1",
        model="codex/gpt-6.1-sol", api_key="test"))])
    catalog = await registry.catalog()
    provider = catalog["providers"][0]
    assert provider["catalog_stale"] and not provider["models_authoritative"]
    assert "Magpie" in provider["catalog_error"]
    assert [row["model_id"] for row in catalog["models"]] == ["codex/gpt-6.1-sol"]
