from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from frontend import server
from mewcode.model_registry import PROVIDER_PRESETS, ProviderRegistry


@pytest.mark.asyncio
async def test_empty_default_model_returns_json_error_not_raise() -> None:
    result = await ProviderRegistry().test_profile(
        {
            "brand": "custom",
            "name": "deepseek",
            "protocol": "openai-compat",
            "base_url": "https://api.deepseek.com",
            "default_model": "",
            "api_key": "sk-test",
        }
    )
    assert result["ok"] is False
    assert "default_model" in result["error"]


def test_deepseek_preset_includes_default_model() -> None:
    preset = PROVIDER_PRESETS["deepseek"]
    assert preset["default_model"]
    assert preset["base_url"].rstrip("/").endswith("/v1")


@pytest.mark.asyncio
async def test_compat_profile_returns_detected_models(monkeypatch) -> None:
    from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete

    class FakeClient:
        async def list_models(self):
            return ["deepseek-chat", "deepseek-v4-flash"]

        async def stream(self, conversation, system="", tools=None):
            if tools:
                yield ToolCallComplete(
                    tool_id="t1",
                    tool_name="mewcode_connection_probe",
                    arguments={"value": "MEWCODE_OK"},
                )
                yield StreamEnd(stop_reason="tool_use")
                return
            yield TextDelta(text="MEWCODE_OK")
            yield StreamEnd(stop_reason="end_turn")

    async def fake_scheduled(client, conversation, system="", tools=None):
        async for event in client.stream(conversation, system=system, tools=tools):
            yield event

    registry = ProviderRegistry(client_factory=lambda _config: FakeClient())
    monkeypatch.setattr(
        "mewcode.model_registry.resolve_effective_provider_config",
        lambda config: config,
    )
    monkeypatch.setattr(
        "mewcode.model_registry.scheduled_client_stream",
        fake_scheduled,
    )
    result = await registry.test_profile(
        {
            "brand": "deepseek",
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com/v1",
            "default_model": "deepseek-chat",
            "api_key": "sk-test",
        }
    )
    assert result["ok"] is True
    assert result["models"] == ["deepseek-chat", "deepseek-v4-flash"]


def test_provider_manager_tests_via_same_origin_backend() -> None:
    app_js = Path("frontend/static/app.js").read_text(encoding="utf-8")
    helper = app_js.split("async function testProviderConnection", 1)[1].split(
        "const GROK_DESKTOP_PROVIDER_PRESET", 1
    )[0]
    assert 'fetch("/api/client/providers/test"' in helper
    assert "credentials: \"same-origin\"" in helper
    assert "desktop.testProvider" not in helper
    assert "result.error" in helper
    assert "Provider 测试未通过桌面会话校验" in helper


@pytest.mark.asyncio
async def test_provider_test_api_uses_desktop_cookie(monkeypatch) -> None:
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "desktop-test-token")
    seen: dict[str, object] = {}

    async def fake_test_profile(payload):
        seen["payload"] = payload
        return {
            "ok": True,
            "latency_ms": 12,
            "models": ["claude-sonnet-4-6"],
            "checks": {
                "authentication": True,
                "streaming": True,
                "tool_calling": True,
                "model_catalog": False,
            },
            "note": "鉴权、流式响应和工具调用均已验证。",
        }

    monkeypatch.setattr(server.MODEL_REGISTRY, "test_profile", fake_test_profile)
    transport = httpx.ASGITransport(app=server.app)
    profile = {
        "brand": "anthropic",
        "name": "Relay",
        "protocol": "anthropic",
        "base_url": "https://relay.example/v1",
        "default_model": "claude-sonnet-4-6",
        "api_key": "sk-test",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post("/api/client/providers/test", json={"profile": profile})
        assert denied.status_code == 401
        assert "desktop session token required" in denied.json()["error"]
        client.cookies.set(server.DESKTOP_COOKIE, "desktop-test-token")
        allowed = await client.post(
            "/api/client/providers/test",
            json={"profile": profile},
        )
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["ok"] is True
    assert seen["payload"]["default_model"] == "claude-sonnet-4-6"
    assert "少量模型费用" in body["cost_warning"]
