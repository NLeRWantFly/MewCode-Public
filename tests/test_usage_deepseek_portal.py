from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mewcode.usage.deepseek import DeepSeekPortalUsageAdapter
from mewcode.usage.models import MetricProvenance, UsageAccount, UsagePeriod


UTC = timezone.utc


def _period() -> UsagePeriod:
    return UsagePeriod(
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 2, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_deepseek_portal_reuses_session_without_login_when_balance_is_visible():
    calls = []

    async def request(_account, method, params):
        calls.append((method, params))
        if method == "browser.open":
            return {"url": "https://platform.deepseek.com/usage"}
        if method == "browser.evaluate":
            return "Account balance ¥ 12.34"
        raise AssertionError(method)

    snapshot = await DeepSeekPortalUsageAdapter(request).fetch_snapshot(
        UsageAccount(
            id="managed:deepseek-portal", name="DeepSeek", adapter_id="deepseek_portal"
        ),
        _period(),
    )
    assert snapshot.financials[0].currency == "CNY"
    assert str(snapshot.financials[0].balance.value) == "12.34"
    assert snapshot.financials[0].balance.provenance is MetricProvenance.PROVIDER_PORTAL
    assert [method for method, _ in calls] == ["browser.open", "browser.evaluate"]
    assert "cookie" not in repr(calls).lower()


@pytest.mark.asyncio
async def test_deepseek_portal_pauses_for_first_login_then_reuses_official_page():
    calls = []
    evaluations = iter(["Login 登录 verification", "Balance $8.50"])

    async def request(_account, method, params):
        calls.append((method, params))
        if method == "browser.open":
            return {"url": "https://platform.deepseek.com/sign_in"}
        if method == "browser.evaluate":
            return next(evaluations)
        if method in {"browser.pause_for_user", "browser.goto"}:
            return {"ok": True}
        raise AssertionError(method)

    snapshot = await DeepSeekPortalUsageAdapter(request).fetch_snapshot(
        UsageAccount(
            id="managed:deepseek-portal", name="DeepSeek", adapter_id="deepseek_portal"
        ),
        _period(),
    )
    assert snapshot.financials[0].currency == "USD"
    assert [method for method, _ in calls] == [
        "browser.open", "browser.evaluate", "browser.pause_for_user",
        "browser.goto", "browser.evaluate",
    ]
    assert all(
        not params.get("url") or params["url"].startswith("https://platform.deepseek.com/")
        for _, params in calls
    )
