from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

import frontend.server as server
from mewcode.usage.backend import UsageAdapter
from mewcode.usage.models import (
    EvidenceLevel,
    FinancialUsage,
    MetricProvenance,
    MetricValue,
    UsageSnapshot,
    UsageSnapshotStatus,
    utc_now,
)
from mewcode.usage.runtime import UsageRuntime


class _ApiAdapter(UsageAdapter):
    @property
    def adapter_id(self) -> str:
        return "api-test"

    async def fetch_snapshot(self, account, period, *, local=None):
        fetched = utc_now()
        return UsageSnapshot(
            account_id=account.id,
            status=UsageSnapshotStatus.PARTIAL,
            period=period,
            financials=[FinancialUsage(
                currency="USD",
                balance=MetricValue.from_value(
                    "8.25", provenance=MetricProvenance.OFFICIAL_API
                ),
            )],
            evidence=EvidenceLevel.OFFICIAL_ACCOUNT,
            fetched_at=fetched,
            stale_at=fetched + timedelta(minutes=5),
        )


@pytest.mark.asyncio
async def test_usage_account_summary_refresh_and_run_api(tmp_path, monkeypatch):
    runtime = UsageRuntime(tmp_path)
    runtime.adapters.register(_ApiAdapter())
    monkeypatch.setattr(server, "USAGE_RUNTIME", runtime)
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/api/usage/accounts", json={
            "id": "usage:api",
            "name": "API account",
            "adapter_id": "api-test",
        })
        assert created.status_code == 201
        assert "secret_values" not in created.json()

        before = await client.get("/api/usage/summary?period=current_month")
        assert before.status_code == 200
        assert before.json()["accounts"][0]["snapshot"] is None

        refresh = await client.post("/api/usage/accounts/usage:api/refresh")
        assert refresh.status_code == 202
        run_id = refresh.json()["run_id"]
        for _ in range(100):
            if runtime.get_run(run_id).status.value not in {
                "queued", "running", "action_required",
            }:
                break
            await asyncio.sleep(0.01)

        run = await client.get(f"/api/usage/runs/{run_id}")
        assert run.status_code == 200
        assert run.json()["status"] == "partial"
        after = (await client.get("/api/usage/summary")).json()
        assert after["accounts"][0]["snapshot"]["financials"][0]["balance"]["value"] == "8.25"


@pytest.mark.asyncio
async def test_desktop_token_protects_usage_mutations(tmp_path, monkeypatch):
    runtime = UsageRuntime(tmp_path)
    runtime.adapters.register(_ApiAdapter())
    monkeypatch.setattr(server, "USAGE_RUNTIME", runtime)
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "desktop-secret")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post("/api/usage/accounts", json={
            "id": "usage:api", "name": "API", "adapter_id": "api-test",
        })
        assert denied.status_code == 401
        allowed = await client.post(
            "/api/usage/accounts",
            headers={"Authorization": "Bearer desktop-secret"},
            json={"id": "usage:api", "name": "API", "adapter_id": "api-test"},
        )
        assert allowed.status_code == 201


@pytest.mark.asyncio
async def test_public_account_api_rejects_plaintext_secrets(tmp_path, monkeypatch):
    runtime = UsageRuntime(tmp_path)
    monkeypatch.setattr(server, "USAGE_RUNTIME", runtime)
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/usage/accounts", json={
            "id": "usage:unsafe",
            "name": "Unsafe",
            "adapter_id": "openai",
            "secret_values": {"admin_api_key": "must-not-cross-this-route"},
        })
    assert response.status_code == 400
    assert "safeStorage" in response.json()["error"]
    assert not runtime.ledger.path.exists()
