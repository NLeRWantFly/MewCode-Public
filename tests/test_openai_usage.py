from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from mewcode.config import ProviderConfig, UsageConfig
from mewcode.usage.backend import UsageUnavailable
from mewcode.usage.models import AttributionStatus, EvidenceLevel, UsageQuery
from mewcode.usage.openai import OpenAIUsageBackend
from mewcode.usage.service import UsageService


UTC = timezone.utc
START = datetime(2026, 8, 1, tzinfo=UTC)
END = datetime(2026, 8, 3, tzinfo=UTC)


def _json_response(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    headers = {
        "x-request-id": "admin_req_123",
        "openai-organization": "org_real",
    }
    if path.endswith("/organization/usage/completions"):
        return httpx.Response(
            200,
            headers=headers,
            json={
                "data": [
                    {
                        "start_time": int(START.timestamp()),
                        "end_time": int(END.timestamp()),
                        "results": [
                            {
                                "input_tokens": 1000,
                                "input_cached_tokens": 300,
                                "input_cache_write_tokens": 100,
                                "input_uncached_tokens": 600,
                                "output_tokens": 200,
                                "reasoning_tokens": 80,
                                "num_model_requests": 2,
                                "model": "gpt-5.6",
                                "project_id": "proj_mewcode",
                                "api_key_id": "key_mewcode",
                                "service_tier": "default",
                            }
                        ],
                    }
                ],
                "next_page": None,
            },
        )
    if path.endswith("/organization/costs"):
        filtered = bool(request.url.params.get_list("project_ids"))
        value = 1.25 if filtered else 2.50
        return httpx.Response(
            200,
            headers=headers,
            json={
                "data": [
                    {
                        "start_time": int(START.timestamp()),
                        "end_time": int(END.timestamp()),
                        "results": [
                            {"amount": {"value": value, "currency": "usd"}}
                        ],
                    }
                ],
                "next_page": None,
            },
        )
    if path.endswith("/organization/spend_limit"):
        return httpx.Response(
            200,
            headers=headers,
            json={
                "threshold_amount": 1000,
                "currency": "usd",
                "enforcement": {"status": "active"},
            },
        )
    if path.endswith("/organization/projects/proj_mewcode/spend_limit"):
        return httpx.Response(
            200,
            headers=headers,
            json={
                "threshold_amount": 500,
                "currency": "usd",
                "enforcement": {"status": "active"},
            },
        )
    return httpx.Response(404, headers=headers, json={"error": {"message": path}})


@pytest.mark.asyncio
async def test_gpt56_official_usage_cost_and_hard_limit_are_separate() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_json_response)
    ) as http_client:
        backend = OpenAIUsageBackend(
            admin_api_key="admin-key",
            generation_base_url="https://relay.example/v1",
            upstream_organization_id="org_real",
            project_ids=["proj_mewcode"],
            api_key_ids=["key_mewcode"],
            attribution_mode="byok-relay",
            http_client=http_client,
        )
        query = UsageQuery(
            start_time=START,
            end_time=END,
            project_ids=["proj_mewcode"],
            api_key_ids=["key_mewcode"],
            models=["gpt-5.6"],
            group_by=["model", "project_id", "api_key_id", "service_tier"],
        )
        usage = await backend.fetch_usage(query)
        snapshot = await backend.fetch_account_snapshot(query, provider_name="relay")

    assert usage.evidence is EvidenceLevel.OFFICIAL_ACCOUNT
    assert usage.request_count == 2
    assert usage.tokens.input_tokens == 600
    assert usage.tokens.cached_input_tokens == 300
    assert usage.tokens.cache_write_input_tokens == 100
    assert usage.tokens.output_tokens == 200
    assert usage.tokens.reasoning_tokens == 80

    assert snapshot.account_ref == "org_real"
    assert snapshot.scope_cost.amount == Decimal("1.25")
    assert snapshot.organization_cost.amount == Decimal("2.5")
    assert snapshot.organization_limit is not None
    assert snapshot.organization_limit.remaining.amount == Decimal("7.5")
    assert snapshot.project_limits[0].remaining.amount == Decimal("3.75")
    assert snapshot.attribution is AttributionStatus.AGGREGATE_VERIFIED
    assert snapshot.prepaid_credit_balance is None
    assert snapshot.approved_usage_limit is None


@pytest.mark.asyncio
async def test_official_organization_mismatch_is_reported() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_json_response)
    ) as http_client:
        backend = OpenAIUsageBackend(
            admin_api_key="admin-key",
            upstream_organization_id="org_wrong",
            generation_base_url="https://api.openai.com/v1",
            project_ids=["proj_mewcode"],
            http_client=http_client,
        )
        snapshot = await backend.fetch_account_snapshot(
            UsageQuery(start_time=START, end_time=END), provider_name="openai"
        )

    assert snapshot.account_ref == "org_real"
    assert snapshot.attribution is AttributionStatus.MISMATCH


def test_service_refuses_unbound_relay_account_claim(tmp_path) -> None:
    service = UsageService(tmp_path)
    provider = ProviderConfig(
        name="relay",
        protocol="openai-compat",
        base_url="https://relay.example/v1",
        model="gpt-5.6",
        api_key="relay-key",
        usage=UsageConfig(admin_api_key="admin-key"),
    )
    with pytest.raises(UsageUnavailable, match="中转"):
        service.backend_for(provider)
