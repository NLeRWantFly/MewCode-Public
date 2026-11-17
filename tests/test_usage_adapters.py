from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from mewcode.usage.anthropic import AnthropicUsageAdapter
from mewcode.usage.adapters import OpenAIAccountAdapter
from mewcode.usage.backend import UsageAuthenticationError
from mewcode.usage.deepseek import DeepSeekUsageAdapter
from mewcode.usage.models import (
    AccountUsageSummary,
    LocalUsageSummary,
    TokenUsage,
    UsageAccount,
    UsagePeriod,
)


UTC = timezone.utc


def _period() -> UsagePeriod:
    return UsagePeriod(
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 12, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_deepseek_returns_multicurrency_balance_without_fake_monthly_spend():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user/balance"
        assert request.headers["authorization"] == "Bearer ds-key"
        return httpx.Response(200, json={
            "is_available": True,
            "balance_infos": [
                {"currency": "CNY", "total_balance": "12.3400"},
                {"currency": "USD", "total_balance": "1.25"},
            ],
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = DeepSeekUsageAdapter(http_client=client)
    account = UsageAccount(
        id="usage:deepseek",
        name="DeepSeek",
        adapter_id="deepseek",
        budget_override={"CNY": "100"},
        secret_values={"api_key": "ds-key"},
    )
    snapshot = await adapter.fetch_snapshot(
        account,
        _period(),
        local=LocalUsageSummary(
            request_count=3, tokens=TokenUsage(input_tokens=9, output_tokens=4)
        ),
    )
    await client.aclose()

    assert [item.currency for item in snapshot.financials] == ["CNY", "USD"]
    assert snapshot.financials[0].balance.value.as_tuple().exponent == -4
    assert snapshot.financials[0].used is None
    assert snapshot.financials[0].remaining is None
    assert snapshot.tokens.total == 13
    assert snapshot.request_count == 3
    assert any(item.field.endswith("used") for item in snapshot.unavailable)


@pytest.mark.asyncio
async def test_deepseek_maps_403_to_authentication_error():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(403, json={}))
    )
    adapter = DeepSeekUsageAdapter(http_client=client)
    account = UsageAccount(
        id="usage:denied", name="Denied", adapter_id="deepseek",
        secret_values={"api_key": "bad"},
    )
    with pytest.raises(UsageAuthenticationError):
        await adapter.fetch_snapshot(account, _period())
    await client.aclose()


@pytest.mark.asyncio
async def test_anthropic_paginates_usage_and_cost_and_preserves_configured_budget():
    usage_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal usage_calls
        assert request.headers["x-api-key"] == "admin-key"
        if request.url.path.endswith("usage_report/messages"):
            usage_calls += 1
            if usage_calls == 1:
                return httpx.Response(200, json={
                    "data": [{"results": [{
                        "uncached_input_tokens": 10,
                        "cache_read_input_tokens": 4,
                        "cache_creation_input_tokens": 2,
                        "output_tokens": 3,
                        "num_api_requests": 2,
                    }]}],
                    "next_page": "next-usage",
                })
            assert request.url.params["page"] == "next-usage"
            return httpx.Response(200, json={
                "data": [{"results": [{
                    "uncached_input_tokens": 1,
                    "output_tokens": 1,
                    "num_api_requests": 1,
                }]}],
                "next_page": None,
            })
        if request.url.path.endswith("cost_report"):
            return httpx.Response(200, json={
                "data": [{"results": [{"amount": "123.45", "currency": "USD"}]}],
                "next_page": None,
            })
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AnthropicUsageAdapter(http_client=client)
    account = UsageAccount(
        id="usage:claude",
        name="Claude",
        adapter_id="anthropic",
        budget_override={"USD": "25.00"},
        secret_values={"admin_api_key": "admin-key"},
        config={"workspace_ids": ["wrk_1"], "api_key_ids": ["key_1"]},
    )
    snapshot = await adapter.fetch_snapshot(account, _period())
    await client.aclose()

    assert usage_calls == 2
    assert snapshot.tokens.input_tokens == 11
    assert snapshot.tokens.cached_input_tokens == 4
    assert snapshot.tokens.cache_write_input_tokens == 2
    assert snapshot.tokens.output_tokens == 4
    assert snapshot.request_count == 3
    assert str(snapshot.financials[0].used.value) == "1.2345"
    assert str(snapshot.financials[0].remaining.value) == "23.7655"
    assert snapshot.financials[0].limit.provenance.value == "configured"


@pytest.mark.asyncio
async def test_anthropic_keeps_usage_as_partial_when_cost_report_fails():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("usage_report/messages"):
            return httpx.Response(200, json={
                "data": [{"results": [{
                    "uncached_input_tokens": 5,
                    "output_tokens": 2,
                    "num_api_requests": 1,
                }]}],
                "next_page": None,
            })
        return httpx.Response(500, json={"error": "temporary"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AnthropicUsageAdapter(http_client=client)
    account = UsageAccount(
        id="usage:claude-partial",
        name="Claude partial",
        adapter_id="anthropic",
        secret_values={"admin_api_key": "admin-key"},
    )
    snapshot = await adapter.fetch_snapshot(account, _period())
    await client.aclose()

    assert snapshot.status.value == "partial"
    assert snapshot.tokens.total == 7
    assert snapshot.financials[0].used is None
    assert any(item.field.endswith("used") for item in snapshot.unavailable)


@pytest.mark.asyncio
async def test_openai_keeps_official_tokens_when_costs_api_is_temporarily_unavailable(
    monkeypatch,
):
    class FakeBackend:
        def __init__(self, **_kwargs):
            pass

        async def fetch_usage(self, query):
            return AccountUsageSummary(
                start_time=query.start_time,
                end_time=query.end_time,
                tokens=TokenUsage(input_tokens=6, output_tokens=2),
                request_count=1,
            )

        async def fetch_cost(self, query, **_kwargs):
            raise httpx.ReadTimeout("cost timeout")

    monkeypatch.setattr("mewcode.usage.adapters.OpenAIUsageBackend", FakeBackend)
    account = UsageAccount(
        id="usage:openai-partial",
        name="OpenAI partial",
        adapter_id="openai",
        budget_override={"USD": "20"},
        secret_values={"admin_api_key": "admin-key"},
    )
    snapshot = await OpenAIAccountAdapter().fetch_snapshot(account, _period())

    assert snapshot.status.value == "partial"
    assert snapshot.tokens.total == 8
    assert snapshot.request_count == 1
    assert snapshot.financials[0].used is None
    assert snapshot.financials[0].remaining is None
    assert snapshot.financials[0].limit.value == 20
