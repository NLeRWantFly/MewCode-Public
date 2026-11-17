import httpx
import pytest

from mewcode.usage.relay_probe import (
    NEWAPI_QUOTA_PER_USD,
    normalize_api_root,
    probe_newapi_token,
    probe_relay_quota,
    probe_v1_usage,
)


def test_normalize_api_root_strips_v1():
    assert normalize_api_root("https://relay.example.test/v1") == "https://relay.example.test"
    assert normalize_api_root("https://relay.example.test/v1/") == "https://relay.example.test"


@pytest.mark.asyncio
async def test_probe_newapi_token_parses_quota():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.rstrip("/") == "/api/usage/token"
        return httpx.Response(
            200,
            json={
                "data": {
                    "total_available": NEWAPI_QUOTA_PER_USD * 2.79,
                    "total_used": NEWAPI_QUOTA_PER_USD * 1.0,
                    "total_granted": NEWAPI_QUOTA_PER_USD * 3.79,
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await probe_newapi_token("https://example.test/v1", "sk-test", client=client)
    await client.aclose()
    assert result.ok
    assert result.remaining == pytest.approx(2.79)
    assert result.used == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_probe_newapi_token_unlimited():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"unlimited_quota": True, "total_available": -1}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await probe_newapi_token("https://example.test", "sk-test", client=client)
    await client.aclose()
    assert result.ok
    assert result.unlimited


@pytest.mark.asyncio
async def test_probe_v1_usage_parses_remaining():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/usage"
        return httpx.Response(
            200,
            json={
                "balance": 2.55,
                "remaining": 2.55,
                "unit": "USD",
                "planName": "钱包余额",
                "mode": "unrestricted",
                "usage": {"today": {"actual_cost": 0.12}},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await probe_v1_usage("https://hgapi.example/v1", "sk-test", client=client)
    await client.aclose()
    assert result.ok
    assert result.remaining == pytest.approx(2.55)
    assert result.used == pytest.approx(0.12)
    assert result.plan_name == "钱包余额"
    assert result.source == "v1_usage"


@pytest.mark.asyncio
async def test_probe_relay_falls_through():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/usage":
            return httpx.Response(404, json={"error": "no"})
        if request.url.path.rstrip("/").endswith("/api/usage/token"):
            return httpx.Response(404, json={"error": "no"})
        if request.url.path.endswith("/user/balance"):
            return httpx.Response(200, json={"balance": 12.5})
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await probe_relay_quota("https://example.test", api_key="sk-test", client=client)
    await client.aclose()
    assert result.ok
    assert result.remaining == pytest.approx(12.5)
    assert result.source == "generic_balance"
