from __future__ import annotations

import httpx
import pytest

from mewcode.client import create_client
from mewcode.config import ProviderConfig
from mewcode.model_registry import ProviderRegistry
from mewcode.openrouter import (
    OPENROUTER_AUTH_ERROR,
    OPENROUTER_DEFAULT_NOT_IN_CATALOG,
    OPENROUTER_EMPTY_ERROR,
    OPENROUTER_NETWORK_ERROR,
    OpenRouterCatalogError,
    fetch_openrouter_user_models,
    is_official_openrouter,
    merge_llm_headers,
    openrouter_request_headers,
)


def test_official_openrouter_requires_brand_and_host():
    assert is_official_openrouter(
        brand="openrouter", base_url="https://openrouter.ai/api/v1"
    )
    assert is_official_openrouter(
        brand="openrouter", base_url="https://www.openrouter.ai/api/v1"
    )
    assert not is_official_openrouter(
        brand="custom", base_url="https://openrouter.ai/api/v1"
    )
    assert not is_official_openrouter(
        brand="openrouter", base_url="https://relay.example/v1"
    )


def test_openrouter_headers_set_explicit_bearer():
    headers = openrouter_request_headers("sk-or-v1-test", {"X-Test": "1"})
    assert headers["Authorization"] == "Bearer sk-or-v1-test"
    assert headers["X-Test"] == "1"
    assert headers["HTTP-Referer"]
    assert headers["X-OpenRouter-Title"] == "MewCode"


def test_compat_client_sends_explicit_openrouter_authorization(monkeypatch):
    captured: dict = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("mewcode.client.AsyncOpenAI", FakeAsyncOpenAI)
    create_client(
        ProviderConfig(
            name="OpenRouter",
            protocol="openai-compat",
            base_url="https://openrouter.ai/api/v1",
            model="anthropic/claude-sonnet-4",
            api_key="sk-or-v1-test",
        )
    )
    assert captured["api_key"] == "sk-or-v1-test"
    assert captured["default_headers"]["Authorization"] == "Bearer sk-or-v1-test"


def test_non_openrouter_client_does_not_force_authorization(monkeypatch):
    captured: dict = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("mewcode.client.AsyncOpenAI", FakeAsyncOpenAI)
    create_client(
        ProviderConfig(
            name="DeepSeek",
            protocol="openai-compat",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            api_key="sk-deepseek",
        )
    )
    assert captured["default_headers"]["User-Agent"] == "MewCode/0.1"
    assert "Authorization" not in captured["default_headers"]
    assert merge_llm_headers("https://api.deepseek.com", "sk-deepseek", None) is None


@pytest.mark.asyncio
async def test_fetch_user_models_reads_account_catalog():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "authorization": request.headers.get("Authorization"),
            }
        )
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"label": "test", "limit": 1}})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "anthropic/claude-sonnet-4"},
                    {"id": "openai/gpt-5"},
                    {"id": "anthropic/claude-sonnet-4"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await fetch_openrouter_user_models(
            api_key="sk-or-v1-test",
            base_url="https://openrouter.ai/api/v1",
            client=client,
        )
    assert models == ["anthropic/claude-sonnet-4", "openai/gpt-5"]
    assert [item["url"].rsplit("/", 1)[-1] for item in seen] == ["key", "models"]
    assert all(item["authorization"] == "Bearer sk-or-v1-test" for item in seen)


@pytest.mark.asyncio
async def test_fetch_user_models_rejects_invalid_key_on_key_endpoint():
    """/key 的 401（真实报文 "User not found."）必须阻止继续拉目录。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "User not found.", "code": 401}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpenRouterCatalogError, match="User not found"):
            await fetch_openrouter_user_models(
                api_key="sk-or-v1-bad",
                base_url="https://openrouter.ai/api/v1",
                client=client,
            )


@pytest.mark.asyncio
async def test_fetch_user_models_maps_missing_auth_header():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "Missing Authentication header", "code": 401}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpenRouterCatalogError, match=OPENROUTER_AUTH_ERROR):
            await fetch_openrouter_user_models(
                api_key="sk-or-v1-test",
                base_url="https://openrouter.ai/api/v1",
                client=client,
            )


@pytest.mark.asyncio
async def test_fetch_user_models_surfaces_unauthorized_body():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "Unauthorized", "code": 401}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpenRouterCatalogError, match="Unauthorized"):
            await fetch_openrouter_user_models(
                api_key="sk-or-v1-test",
                base_url="https://openrouter.ai/api/v1",
                client=client,
            )


@pytest.mark.asyncio
async def test_fetch_user_models_strips_pasted_bearer_prefix():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization") or ""
        return httpx.Response(200, json={"data": [{"id": "anthropic/claude-sonnet-4"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await fetch_openrouter_user_models(
            api_key="Bearer sk-or-v1-test",
            base_url="https://openrouter.ai/api/v1",
            client=client,
        )
    assert models == ["anthropic/claude-sonnet-4"]
    assert seen["authorization"] == "Bearer sk-or-v1-test"


@pytest.mark.asyncio
async def test_fetch_user_models_maps_empty_catalog():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpenRouterCatalogError, match=OPENROUTER_EMPTY_ERROR):
            await fetch_openrouter_user_models(
                api_key="sk-or-v1-test",
                base_url="https://openrouter.ai/api/v1",
                client=client,
            )


@pytest.mark.asyncio
async def test_fetch_user_models_maps_network_failure():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpenRouterCatalogError, match=OPENROUTER_NETWORK_ERROR):
            await fetch_openrouter_user_models(
                api_key="sk-or-v1-test",
                base_url="https://openrouter.ai/api/v1",
                client=client,
            )


@pytest.mark.asyncio
async def test_openrouter_test_profile_is_catalog_only(monkeypatch):
    async def fake_fetch(**kwargs):
        assert kwargs["api_key"] == "sk-or-v1-test"
        return ["anthropic/claude-sonnet-4", "openai/gpt-5"]

    monkeypatch.setattr(
        "mewcode.model_registry.fetch_openrouter_user_models", fake_fetch
    )
    registry = ProviderRegistry()
    result = await registry.test_profile(
        {
            "brand": "openrouter",
            "name": "OpenRouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-v1-test",
        }
    )
    assert result["ok"] is True
    assert result["models"] == ["anthropic/claude-sonnet-4", "openai/gpt-5"]
    assert result["checks"]["model_catalog"] is True
    assert result["checks"]["streaming"] is False
    assert result["checks"]["tool_calling"] is False
    assert "已验证 Key 并拉取模型目录" in result["note"]


@pytest.mark.asyncio
async def test_openrouter_test_profile_reports_auth_failure(monkeypatch):
    async def fake_fetch(**_kwargs):
        raise OpenRouterCatalogError("auth", OPENROUTER_AUTH_ERROR)

    monkeypatch.setattr(
        "mewcode.model_registry.fetch_openrouter_user_models", fake_fetch
    )
    result = await ProviderRegistry().test_profile(
        {
            "brand": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-v1-bad",
        }
    )
    assert result["ok"] is False
    assert OPENROUTER_AUTH_ERROR in result["error"]


def test_openrouter_legacy_profile_without_catalog_still_syncs():
    registry = ProviderRegistry()
    public = registry.replace_desktop_profiles(
        [
            {
                "id": "or1",
                "brand": "openrouter",
                "name": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "default_model": "anthropic/claude-sonnet-4",
                "api_key": "sk-or-v1-test",
            }
        ]
    )
    assert public[0]["models"] == ["anthropic/claude-sonnet-4"]
    assert public[0]["default_model"] == "anthropic/claude-sonnet-4"


def test_openrouter_save_rejects_default_outside_catalog():
    registry = ProviderRegistry()
    with pytest.raises(ValueError, match=OPENROUTER_DEFAULT_NOT_IN_CATALOG):
        registry.replace_desktop_profiles(
            [
                {
                    "id": "or1",
                    "brand": "openrouter",
                    "name": "OpenRouter",
                    "base_url": "https://openrouter.ai/api/v1",
                    "default_model": "missing/model",
                    "models": ["anthropic/claude-sonnet-4"],
                    "api_key": "sk-or-v1-test",
                }
            ]
        )


def test_custom_openrouter_url_still_allows_manual_models():
    registry = ProviderRegistry()
    public = registry.replace_desktop_profiles(
        [
            {
                "id": "relay",
                "brand": "openrouter",
                "name": "Relay",
                "base_url": "https://relay.example/v1",
                "default_model": "gpt-4o",
                "api_key": "relay-key",
            }
        ]
    )
    assert public[0]["models"] == ["gpt-4o"]


@pytest.mark.asyncio
async def test_desktop_openrouter_catalog_uses_snapshot_without_listing(monkeypatch):
    registry = ProviderRegistry(catalog_ttl=0)
    registry.replace_desktop_profiles(
        [
            {
                "id": "or1",
                "brand": "openrouter",
                "name": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "default_model": "anthropic/claude-sonnet-4",
                "models": ["anthropic/claude-sonnet-4", "openai/gpt-5"],
                "api_key": "sk-or-v1-test",
            }
        ]
    )

    def boom(_config):
        raise AssertionError("live list_models should not run")

    monkeypatch.setattr("mewcode.model_registry.create_client", boom)
    catalog = await registry.catalog()
    assert {item["model_id"] for item in catalog["models"]} == {
        "anthropic/claude-sonnet-4",
        "openai/gpt-5",
    }
    assert catalog["providers"][0]["models_authoritative"] is True
    assert catalog["providers"][0]["available"] is True
