from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from mewcode.config import ProviderConfig
from mewcode.usage.models import (
    AccountSnapshot,
    AttributionStatus,
    Money,
    RequestUsage,
    UsageQuery,
)
from mewcode.usage.service import UsageService


UTC = timezone.utc


def test_admin_account_must_match_model_response_account(tmp_path) -> None:
    service = UsageService(tmp_path)
    service.record_request(
        RequestUsage(
            occurred_at=datetime(2026, 8, 2, tzinfo=UTC),
            provider_name="openai",
            organization_id="org_generation",
        )
    )
    provider = ProviderConfig(
        name="openai",
        protocol="openai",
        base_url="https://api.openai.com/v1",
        model="gpt-5.6",
        api_key="key",
    )
    query = UsageQuery(
        start_time=datetime(2026, 8, 1, tzinfo=UTC),
        end_time=datetime(2026, 9, 1, tzinfo=UTC),
    )
    snapshot = AccountSnapshot(
        provider_name="openai",
        account_ref="org_admin",
        period_start=query.start_time,
        period_end=query.end_time,
        scope_cost=Money(),
        organization_cost=Money(),
    )

    matched = service._apply_identity_check(
        provider,
        SimpleNamespace(observed_organization_id="org_admin"),
        query,
        snapshot,
    )

    assert matched is False
    assert snapshot.attribution is AttributionStatus.MISMATCH
    assert "不一致" in snapshot.attribution_reason
