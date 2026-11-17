from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.usage.provider_meters import classify_provider, collect_provider_meters


def test_classify_official_hosts():
    assert classify_provider("OpenAI", "https://api.openai.com/v1")[0] == "official"
    assert classify_provider("Claude", "https://api.anthropic.com")[1] == "anthropic"
    assert classify_provider("DS", "https://api.deepseek.com")[1] == "deepseek"
    assert classify_provider("Grok", "https://api.x.ai/v1")[1] == "xai"


def test_classify_relay():
    kind, vendor = classify_provider("示例中转", "https://relay.example.test/v1")
    assert kind == "relay"
    assert vendor == "relay"


@pytest.mark.asyncio
async def test_collect_relay_card_without_key():
    provider = SimpleNamespace(
        name="中转",
        base_url="https://relay.example/v1",
        model="gpt-x",
        protocol="openai",
        resolve_api_key=lambda: "",
    )
    record = SimpleNamespace(key="relay-1", provider=provider)
    payload = await collect_provider_meters([record])
    assert payload["count"] == 1
    card = payload["providers"][0]
    assert card["kind"] == "relay"
    assert card["status"] == "missing_auth"


def test_usage_providers_route_uses_session_usage_service():
    """Regression: undefined `_usage_service` made the whole panel say unavailable."""
    server = (Path(__file__).resolve().parents[1] / "frontend" / "server.py").read_text(
        encoding="utf-8"
    )
    assert "async def api_usage_providers" in server
    assert "_session_usage_service(SESSION.work_dir)" in server
    assert "_usage_service()" not in server
