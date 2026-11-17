"""Anthropic Usage and Cost Admin API adapter."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx

from mewcode.usage.backend import (
    UsageAdapter,
    UsageAuthenticationError,
    UsageBackendError,
    UsageUnavailable,
)
from mewcode.usage.models import (
    EvidenceLevel,
    FinancialUsage,
    LocalUsageSummary,
    MetricProvenance,
    MetricValue,
    TokenUsage,
    UnavailableMetric,
    UsageAccount,
    UsagePeriod,
    UsageSnapshot,
    UsageSnapshotStatus,
    utc_now,
)


OFFICIAL_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


def is_official_anthropic_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == "api.anthropic.com"


def _items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


class AnthropicUsageAdapter(UsageAdapter):
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._http_client = http_client
        self.timeout = timeout

    @property
    def adapter_id(self) -> str:
        return "anthropic"

    async def _get(
        self,
        base_url: str,
        path: str,
        key: str,
        params: list[tuple[str, str]],
    ) -> dict[str, Any]:
        if not key:
            raise UsageUnavailable("缺少 Anthropic Admin API key")
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "accept": "application/json",
            "user-agent": "MewCode/0.2 usage-integration",
        }
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    f"{base_url.rstrip('/')}/{path.lstrip('/')}",
                    headers=headers,
                    params=params,
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(
                        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
                        headers=headers,
                        params=params,
                    )
        except httpx.HTTPError as exc:
            raise UsageBackendError(f"Anthropic usage request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise UsageAuthenticationError(
                "Anthropic Admin API 拒绝访问；组织 Usage/Cost 需要 Admin API key"
            )
        if response.status_code == 429:
            raise UsageBackendError("Anthropic Admin API rate limited the usage query")
        if response.is_error:
            raise UsageBackendError(
                f"Anthropic usage endpoint returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UsageBackendError("Anthropic usage endpoint returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise UsageBackendError("Anthropic usage endpoint returned a non-object payload")
        return payload

    async def _paginate(
        self,
        base_url: str,
        path: str,
        key: str,
        params: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = []
        page = ""
        seen: set[str] = set()
        for _ in range(1000):
            request_params = list(params)
            if page:
                request_params.append(("page", page))
            payload = await self._get(base_url, path, key, request_params)
            data.extend(_items(payload.get("data")))
            page = str(payload.get("next_page") or "")
            if not page:
                return data
            if page in seen:
                raise UsageBackendError("Anthropic usage pagination cursor repeated")
            seen.add(page)
        raise UsageBackendError("Anthropic usage pagination exceeded 1000 pages")

    @staticmethod
    def _usage_tokens(buckets: list[dict[str, Any]]) -> tuple[TokenUsage, int]:
        total = TokenUsage()
        requests = 0
        for bucket in buckets:
            for result in _items(bucket.get("results")):
                cache_creation = result.get("cache_creation") or {}
                if not isinstance(cache_creation, dict):
                    cache_creation = {}
                write_tokens = int(result.get("cache_creation_input_tokens") or 0)
                write_tokens += int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
                write_tokens += int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
                total = total.add(TokenUsage(
                    input_tokens=int(result.get("uncached_input_tokens") or result.get("input_tokens") or 0),
                    cached_input_tokens=int(result.get("cache_read_input_tokens") or 0),
                    cache_write_input_tokens=write_tokens,
                    output_tokens=int(result.get("output_tokens") or 0),
                ))
                requests += int(result.get("num_api_requests") or result.get("num_model_requests") or 0)
        return total, requests

    @staticmethod
    def _cost(buckets: list[dict[str, Any]]) -> Decimal:
        cents = Decimal("0")
        for bucket in buckets:
            for result in _items(bucket.get("results")):
                amount = Decimal(str(result.get("amount") or 0))
                if not amount.is_finite():
                    raise UsageBackendError("Anthropic cost result was not finite")
                cents += amount
                currency = str(result.get("currency") or "USD").upper()
                if currency != "USD":
                    raise UsageBackendError(
                        f"Anthropic cost result used unsupported currency {currency}"
                    )
        return cents / Decimal("100")

    async def fetch_snapshot(
        self,
        account: UsageAccount,
        period: UsagePeriod,
        *,
        local: LocalUsageSummary | None = None,
    ) -> UsageSnapshot:
        key = account.secret_values.get("admin_api_key") or account.secret_values.get("api_key", "")
        base_url = str(account.config.get("base_url") or OFFICIAL_ANTHROPIC_BASE_URL).rstrip("/")
        if not is_official_anthropic_url(base_url):
            raise UsageUnavailable(
                "Claude 官方适配器只允许 api.anthropic.com；中转站请使用自动化插件"
            )
        common: list[tuple[str, str]] = [
            ("starting_at", period.start.isoformat().replace("+00:00", "Z")),
            ("ending_at", period.end.isoformat().replace("+00:00", "Z")),
            ("bucket_width", "1d"),
            ("limit", "31"),
        ]
        for value in account.config.get("api_key_ids", []):
            common.append(("api_key_ids[]", str(value)))
        for value in account.config.get("workspace_ids", []):
            common.append(("workspace_ids[]", str(value)))
        usage_params = [*common, ("group_by[]", "model")]
        cost_params = [
            item for item in common if item[0] not in {"bucket_width", "limit", "api_key_ids[]"}
        ]
        cost_params.extend([("group_by[]", "workspace_id"), ("limit", "31")])

        usage_result, cost_result = await asyncio.gather(
            self._paginate(
                base_url, "v1/organizations/usage_report/messages", key, usage_params
            ),
            self._paginate(
                base_url, "v1/organizations/cost_report", key, cost_params
            ),
            return_exceptions=True,
        )
        if isinstance(usage_result, Exception) and isinstance(cost_result, Exception):
            raise usage_result
        unavailable = [
            UnavailableMetric(
                field="financials.USD.balance",
                code="not_supported",
                message="Claude Platform Usage/Cost API 不返回预付费余额。",
            )
        ]
        warnings: list[str] = []
        if isinstance(cost_result, Exception):
            finance = FinancialUsage(currency="USD")
            unavailable.append(UnavailableMetric(
                field="financials.USD.used",
                code="request_failed",
                message="Claude Cost Report 本次不可用，未将费用显示为 0。",
            ))
            warnings.append(f"Cost Report：{cost_result}"[:1000])
        else:
            spent = self._cost(cost_result)
            finance = FinancialUsage(
                currency="USD",
                used=MetricValue.from_value(
                    spent, provenance=MetricProvenance.OFFICIAL_API
                ),
            )
        override = account.budget_override.get("USD")
        if override is not None:
            finance.limit = MetricValue.from_value(
                override, provenance=MetricProvenance.CONFIGURED
            )
            finance.derive_remaining()
        else:
            unavailable.append(UnavailableMetric(
                field="financials.USD.limit",
                code="not_supported",
                message="Claude Platform Admin API 不提供组织月度总额度读取接口。",
            ))
        if isinstance(usage_result, Exception):
            tokens = None
            request_count = None
            unavailable.append(UnavailableMetric(
                field="tokens",
                code="request_failed",
                message="Claude Usage Report 本次不可用，Token 与请求数未显示为 0。",
            ))
            warnings.append(f"Usage Report：{usage_result}"[:1000])
        else:
            tokens, request_count = self._usage_tokens(usage_result)
        fetched = utc_now()
        return UsageSnapshot(
            account_id=account.id,
            status=(
                UsageSnapshotStatus.PARTIAL
                if isinstance(usage_result, Exception) or isinstance(cost_result, Exception)
                else UsageSnapshotStatus.OK
            ),
            period=period,
            financials=[finance],
            tokens=tokens,
            request_count=request_count,
            unavailable=unavailable,
            evidence=EvidenceLevel.OFFICIAL_ACCOUNT,
            fetched_at=fetched,
            stale_at=fetched + timedelta(minutes=5),
            warnings=warnings,
        )
