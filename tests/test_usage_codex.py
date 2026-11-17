from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from mewcode.usage.backend import (
    UsageAuthenticationError,
    UsageBackendError,
    UsageUnavailable,
)
from mewcode.usage.codex import (
    CodexOAuthUsageAdapter,
    discover_codex_usage_account,
    read_codex_oauth_credentials,
)
from mewcode.usage.models import UsagePeriod


UTC = timezone.utc


def _write_auth(tmp_path, value):
    root = tmp_path / ".codex"
    root.mkdir()
    (root / "auth.json").write_text(json.dumps(value), encoding="utf-8")


def test_codex_discovery_accepts_only_chatgpt_oauth(tmp_path):
    assert read_codex_oauth_credentials(tmp_path) is None
    _write_auth(tmp_path, {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-api"})
    assert read_codex_oauth_credentials(tmp_path) is None
    (tmp_path / ".codex" / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"access_token": "oauth-secret", "account_id": "acct-1"},
    }), encoding="utf-8")
    account = discover_codex_usage_account(tmp_path)
    assert account is not None and account.managed
    assert account.source_kind == "auto_discovered"
    assert "oauth-secret" not in json.dumps(account.to_dict())
    assert account.secret_values["oauth_access_token"] == "oauth-secret"


def _client(status: int, payload=None):
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload or {}, request=request)
    ))


@pytest.mark.asyncio
async def test_codex_adapter_parses_primary_secondary_and_headers(tmp_path):
    captured = {}
    def handler(request):
        captured.update(request.headers)
        return httpx.Response(200, json={"rate_limit": {
            "primary_window": {"used_percent": 12.5, "limit_window_seconds": 18000, "reset_at": 1786500000},
            "secondary_window": {"used_percent": 34, "limit_window_seconds": 604800},
        }}, request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    account = discover_codex_usage_account.__globals__["UsageAccount"](
        id="managed:codex", name="Codex", adapter_id="codex",
        config={"account_id": "acct-1"}, secret_values={"oauth_access_token": "secret"},
    )
    period = UsagePeriod(start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 8, 2, tzinfo=UTC))
    try:
        snapshot = await CodexOAuthUsageAdapter(http_client=client).fetch_snapshot(account, period)
    finally:
        await client.aclose()
    assert [row.name for row in snapshot.quota_windows] == ["5 小时", "7 天"]
    assert float(snapshot.quota_windows[0].used_percent) == 12.5
    assert captured["chatgpt-account-id"] == "acct-1"
    assert captured["authorization"] == "Bearer secret"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [
    (401, UsageAuthenticationError), (403, UsageAuthenticationError),
    (500, UsageUnavailable), (502, UsageUnavailable), (429, UsageBackendError),
])
async def test_codex_adapter_classifies_http_failures(status, error):
    from mewcode.usage.models import UsageAccount
    client = _client(status)
    account = UsageAccount(
        id="managed:codex", name="Codex", adapter_id="codex",
        secret_values={"oauth_access_token": "secret"},
    )
    period = UsagePeriod(start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 8, 2, tzinfo=UTC))
    try:
        with pytest.raises(error):
            await CodexOAuthUsageAdapter(http_client=client).fetch_snapshot(account, period)
    finally:
        await client.aclose()
