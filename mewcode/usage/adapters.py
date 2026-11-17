"""Registry and built-in account adapters for normalized usage snapshots."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Iterable

from mewcode.usage.anthropic import AnthropicUsageAdapter
from mewcode.usage.backend import UsageAdapter, UsageUnavailable
from mewcode.usage.deepseek import DeepSeekUsageAdapter
from mewcode.usage.codex import CodexOAuthUsageAdapter
from mewcode.usage.models import (
    EvidenceLevel,
    FinancialUsage,
    LocalUsageSummary,
    MetricProvenance,
    MetricValue,
    UnavailableMetric,
    UsageAccount,
    UsagePeriod,
    UsageQuery,
    UsageSnapshot,
    UsageSnapshotStatus,
    utc_now,
)
from mewcode.usage.openai import (
    OFFICIAL_OPENAI_BASE_URL,
    OpenAIUsageBackend,
    is_official_openai_url,
)


class OpenAIAccountAdapter(UsageAdapter):
    @property
    def adapter_id(self) -> str:
        return "openai"

    async def fetch_snapshot(
        self,
        account: UsageAccount,
        period: UsagePeriod,
        *,
        local: LocalUsageSummary | None = None,
    ) -> UsageSnapshot:
        key = account.secret_values.get("admin_api_key") or account.secret_values.get("api_key", "")
        base_url = str(account.config.get("base_url") or OFFICIAL_OPENAI_BASE_URL)
        if not is_official_openai_url(base_url):
            raise UsageUnavailable(
                "OpenAI 官方适配器只允许 api.openai.com；中转站请使用 Provider 额度探测"
            )
        project_ids = [str(item) for item in account.config.get("project_ids", [])]
        api_key_ids = [str(item) for item in account.config.get("api_key_ids", [])]
        backend = OpenAIUsageBackend(
            admin_api_key=key,
            admin_base_url=base_url,
            generation_base_url=OFFICIAL_OPENAI_BASE_URL,
            project_ids=project_ids,
            api_key_ids=api_key_ids,
        )
        query = UsageQuery(
            start_time=period.start,
            end_time=period.end,
            bucket_width="1d",
            project_ids=project_ids,
            api_key_ids=api_key_ids,
            group_by=["model", "project_id", "api_key_id", "service_tier"],
        )
        usage_task = backend.fetch_usage(query)
        cost_task = backend.fetch_cost(
            query, project_ids=project_ids, api_key_ids=api_key_ids
        )
        usage_result, cost_result = await asyncio.gather(
            usage_task, cost_task, return_exceptions=True
        )
        if isinstance(usage_result, Exception) and isinstance(cost_result, Exception):
            raise usage_result
        unavailable = [
            UnavailableMetric(
                field="financials.*.balance",
                code="not_supported",
                message="OpenAI 公共 Admin API 不提供预付费信用余额读取接口。",
            )
        ]
        warnings: list[str] = []
        financials: list[FinancialUsage] = []
        if isinstance(cost_result, Exception):
            unavailable.append(UnavailableMetric(
                field="financials.USD.used",
                code="request_failed",
                message="OpenAI Costs API 本次不可用，未将费用显示为 0。",
            ))
            warnings.append(f"Costs API：{cost_result}"[:1000])
            finance = FinancialUsage(currency="USD")
        else:
            cost, _request_ids = cost_result
            finance = FinancialUsage(
                currency=cost.currency.upper(),
                used=MetricValue.from_value(
                    cost.amount, provenance=MetricProvenance.OFFICIAL_API
                ),
            )
        override = account.budget_override.get(finance.currency)
        if override is not None:
            finance.limit = MetricValue.from_value(
                override, provenance=MetricProvenance.CONFIGURED
            )
            finance.derive_remaining()
        else:
            unavailable.append(UnavailableMetric(
                field=f"financials.{finance.currency}.limit",
                code="not_supported",
                message="未读取到公开且稳定的组织总额度接口；可配置本地月度预算。",
            ))
        financials.append(finance)
        if isinstance(usage_result, Exception):
            tokens = None
            request_count = None
            unavailable.append(UnavailableMetric(
                field="tokens",
                code="request_failed",
                message="OpenAI Usage API 本次不可用，Token 与请求数未显示为 0。",
            ))
            warnings.append(f"Usage API：{usage_result}"[:1000])
        else:
            tokens = usage_result.tokens
            request_count = usage_result.request_count
        fetched = utc_now()
        return UsageSnapshot(
            account_id=account.id,
            status=(
                UsageSnapshotStatus.PARTIAL
                if isinstance(usage_result, Exception) or isinstance(cost_result, Exception)
                else UsageSnapshotStatus.OK
            ),
            period=period,
            financials=financials,
            tokens=tokens,
            request_count=request_count,
            unavailable=unavailable,
            evidence=EvidenceLevel.OFFICIAL_ACCOUNT,
            fetched_at=fetched,
            stale_at=fetched + timedelta(minutes=5),
            warnings=warnings,
        )


class UsageAdapterRegistry:
    def __init__(self, adapters: Iterable[UsageAdapter] | None = None) -> None:
        self._adapters: dict[str, UsageAdapter] = {}
        for adapter in adapters or ():
            self.register(adapter)

    @classmethod
    def with_builtins(cls) -> "UsageAdapterRegistry":
        return cls([
            OpenAIAccountAdapter(),
            CodexOAuthUsageAdapter(),
            AnthropicUsageAdapter(),
            DeepSeekUsageAdapter(),
        ])

    def register(self, adapter: UsageAdapter) -> None:
        if not adapter.adapter_id:
            raise ValueError("usage adapter id is required")
        self._adapters[adapter.adapter_id] = adapter

    def get(self, adapter_id: str) -> UsageAdapter:
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise UsageUnavailable(f"未知用量适配器：{adapter_id}") from exc

    def ids(self) -> list[str]:
        return sorted(self._adapters)
