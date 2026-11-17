from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mewcode.usage.backend import UsageAdapter
from mewcode.usage.models import (
    EvidenceLevel,
    FinancialUsage,
    MetricProvenance,
    MetricValue,
    UsageAccount,
    UsagePeriod,
    UsageRun,
    UsageRunStatus,
    UsageSnapshot,
    UsageSnapshotStatus,
    QuotaWindow,
    RequestUsage,
    TokenUsage,
)
from mewcode.usage.runtime import UsageRuntime, current_month_period


UTC = timezone.utc


def test_current_month_period_uses_utc_natural_month_boundary():
    now = datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)
    period = current_month_period(now)
    assert period.start == datetime(2026, 8, 1, tzinfo=UTC)
    assert period.end == now
    assert period.timezone == "UTC"


def test_financial_metrics_keep_decimal_precision_and_only_derive_from_limit_minus_used():
    row = FinancialUsage(
        currency="usd",
        used=MetricValue.from_value(
            "0.100000000000000001", provenance=MetricProvenance.OFFICIAL_API
        ),
        limit=MetricValue.from_value(
            "1.000000000000000002", provenance=MetricProvenance.CONFIGURED
        ),
        balance=MetricValue.from_value(
            "7.50", provenance=MetricProvenance.OFFICIAL_API
        ),
    )
    row.derive_remaining()

    assert row.remaining is not None
    assert row.remaining.value == Decimal("0.900000000000000001")
    assert row.remaining.provenance is MetricProvenance.DERIVED
    assert row.to_dict()["used"]["value"] == "0.100000000000000001"

    balance_only = FinancialUsage(
        currency="CNY",
        balance=MetricValue.from_value(
            "20", provenance=MetricProvenance.OFFICIAL_API
        ),
    )
    balance_only.derive_remaining()
    assert balance_only.used is None
    assert balance_only.limit is None
    assert balance_only.remaining is None

    over_budget = FinancialUsage(
        currency="USD",
        used=MetricValue.from_value("12", provenance=MetricProvenance.OFFICIAL_API),
        limit=MetricValue.from_value("10", provenance=MetricProvenance.CONFIGURED),
    )
    over_budget.derive_remaining()
    assert over_budget.remaining.value == Decimal("-2")


def test_account_public_shape_never_contains_runtime_secrets(tmp_path):
    runtime = UsageRuntime(tmp_path)
    account = runtime.save_account(
        {
            "id": "usage:openai",
            "name": "OpenAI",
            "adapter_id": "openai",
            "has_secret": True,
        },
        secret_values={"admin_api_key": "sk-never-persist"},
    )

    assert "secret_values" not in account.to_dict()
    assert "sk-never-persist" not in runtime.ledger.path.read_text(
        encoding="utf-8", errors="ignore"
    )
    persisted = runtime.ledger.get_usage_account(account.id)
    assert persisted is not None
    assert persisted.has_secret is True
    assert persisted.secret_values == {}


def test_legacy_desktop_jsonl_import_is_idempotent(tmp_path):
    source = tmp_path / "usage.jsonl"
    source.write_text(
        '{"ts":"2026-08-02T03:04:05+00:00","model":"gpt-5",'
        '"in":11,"out":7,"sid":"session-a","msgs":2}\n'
        'not json\n',
        encoding="utf-8",
    )
    runtime = UsageRuntime(tmp_path / "data")

    assert runtime.import_legacy_desktop_usage(source) == 1
    assert runtime.import_legacy_desktop_usage(source) == 0
    events = runtime.ledger.activity_events()
    assert len(events) == 1
    assert events[0]["in"] == 11
    assert events[0]["out"] == 7
    assert events[0]["msgs"] == 2
    assert events[0]["approx"] is True


def test_usage_run_persists_plugin_version_and_hash(tmp_path):
    runtime = UsageRuntime(tmp_path)
    run = UsageRun(
        id="run-plugin",
        account_id="usage:relay",
        status=UsageRunStatus.SUCCEEDED,
        plugin_id="relay.example",
        plugin_version="1.2.3",
        plugin_sha256="a" * 64,
    )
    runtime.ledger.save_usage_run(run)

    restored = runtime.ledger.get_usage_run(run.id)
    assert restored is not None
    assert restored.plugin_id == "relay.example"
    assert restored.plugin_version == "1.2.3"
    assert restored.plugin_sha256 == "a" * 64


class _BlockingAdapter(UsageAdapter):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    @property
    def adapter_id(self) -> str:
        return "blocking"

    async def fetch_snapshot(self, account, period, *, local=None):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return UsageSnapshot(
            account_id=account.id,
            status=UsageSnapshotStatus.OK,
            period=period,
            evidence=EvidenceLevel.OFFICIAL_ACCOUNT,
        )


@pytest.mark.asyncio
async def test_runtime_deduplicates_concurrent_refresh_and_reuses_fresh_cache(tmp_path):
    runtime = UsageRuntime(tmp_path, cache_seconds=60)
    adapter = _BlockingAdapter()
    runtime.adapters.register(adapter)
    runtime.save_account({
        "id": "usage:dedupe",
        "name": "Dedupe",
        "adapter_id": "blocking",
    })

    first = await runtime.start_refresh("usage:dedupe")
    await adapter.started.wait()
    second = await runtime.start_refresh("usage:dedupe")
    assert second.id == first.id
    assert adapter.calls == 1

    adapter.release.set()
    await runtime._tasks[first.id]
    cached = await runtime.start_refresh("usage:dedupe")
    assert cached.id != first.id
    assert cached.status.value == "succeeded"
    assert "缓存" in cached.message
    assert adapter.calls == 1


def test_usage_snapshot_rejects_duplicate_currency_rows():
    period = UsagePeriod(
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 2, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="duplicate financial currency"):
        UsageSnapshot(
            account_id="usage:duplicate",
            status=UsageSnapshotStatus.OK,
            period=period,
            financials=[FinancialUsage("USD"), FinancialUsage("usd")],
        )


def test_usage_snapshot_round_trips_quota_windows_and_managed_source():
    period = UsagePeriod(
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 2, tzinfo=UTC),
    )
    account = UsageAccount(
        id="managed:codex", name="Codex", adapter_id="codex",
        managed=True, source_kind="auto_discovered",
    )
    snapshot = UsageSnapshot(
        account_id=account.id,
        status=UsageSnapshotStatus.OK,
        period=period,
        quota_windows=[QuotaWindow(
            name="5 小时", used_percent=Decimal("21.5"), window_seconds=18000,
        )],
    )
    restored = UsageSnapshot.from_dict(snapshot.to_dict())
    assert account.to_dict()["managed"] is True
    assert account.to_dict()["source_kind"] == "auto_discovered"
    assert restored.quota_windows[0].window_seconds == 18000
    assert restored.quota_windows[0].used_percent == Decimal("21.5")


def test_activity_aggregate_matches_event_totals(tmp_path):
    runtime = UsageRuntime(tmp_path)
    runtime.ledger.append(RequestUsage(
        occurred_at=datetime(2026, 8, 2, 3, tzinfo=UTC),
        configured_model="gpt-test", response_model="gpt-test",
        session_id="session-aggregate",
        tokens=TokenUsage(
            input_tokens=11, cached_input_tokens=3, output_tokens=7,
        ),
        evidence=EvidenceLevel.LOCAL_ESTIMATE,
        metadata={"legacy_messages": 2},
    ))
    aggregate = runtime.ledger.activity_aggregate()
    assert aggregate["totals"] == {"tokens": 21, "in": 14, "out": 7, "messages": 2}
    assert aggregate["models"]["gpt-test"] == 21
    assert aggregate["sessions"] == ["session-aggregate"]
