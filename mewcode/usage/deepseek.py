"""DeepSeek official account balance adapter."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
import re
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
    UnavailableMetric,
    UsageAccount,
    UsagePeriod,
    UsageSnapshot,
    UsageSnapshotStatus,
    utc_now,
)


OFFICIAL_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def is_official_deepseek_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == "api.deepseek.com"


class DeepSeekUsageAdapter(UsageAdapter):
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
        return "deepseek"

    async def _balance(self, base_url: str, key: str) -> dict[str, Any]:
        if not key:
            raise UsageUnavailable("缺少 DeepSeek API key")
        url = f"{base_url.rstrip('/')}/user/balance"
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url, headers={"Authorization": f"Bearer {key}"}
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(
                        url, headers={"Authorization": f"Bearer {key}"}
                    )
        except httpx.HTTPError as exc:
            raise UsageBackendError(f"DeepSeek balance request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise UsageAuthenticationError("DeepSeek API 拒绝了余额查询")
        if response.status_code == 429:
            raise UsageBackendError("DeepSeek API rate limited the balance query")
        if response.is_error:
            raise UsageBackendError(
                f"DeepSeek balance endpoint returned HTTP {response.status_code}"
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise UsageBackendError("DeepSeek balance endpoint returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise UsageBackendError("DeepSeek balance endpoint returned a non-object payload")
        return value

    async def fetch_snapshot(
        self,
        account: UsageAccount,
        period: UsagePeriod,
        *,
        local: LocalUsageSummary | None = None,
    ) -> UsageSnapshot:
        key = account.secret_values.get("api_key", "")
        base_url = str(account.config.get("base_url") or OFFICIAL_DEEPSEEK_BASE_URL)
        if not is_official_deepseek_url(base_url):
            raise UsageUnavailable(
                "DeepSeek 官方适配器只允许 api.deepseek.com；中转站请使用 Provider 额度探测"
            )
        payload = await self._balance(base_url, key)
        financials: list[FinancialUsage] = []
        seen: set[str] = set()
        unavailable = [
            UnavailableMetric(
                field="financials.*.used",
                code="not_supported",
                message="DeepSeek 官方公开 API 只返回当前余额，不返回历史或本月费用。",
            ),
            UnavailableMetric(
                field="financials.*.remaining",
                code="not_derivable",
                message="缺少同周期官方已用金额，不能从余额反推总额度或剩余。",
            ),
        ]
        for raw in payload.get("balance_infos") or []:
            if not isinstance(raw, dict):
                continue
            currency = str(raw.get("currency") or "").upper()
            if not currency or currency in seen:
                continue
            seen.add(currency)
            balance_value = raw.get("total_balance")
            row = FinancialUsage(
                currency=currency,
                balance=(
                    MetricValue.from_value(
                        balance_value,
                        provenance=MetricProvenance.OFFICIAL_API,
                    )
                    if balance_value is not None else None
                ),
            )
            if balance_value is None:
                unavailable.append(UnavailableMetric(
                    field=f"financials.{currency}.balance",
                    code="invalid_response",
                    message=f"DeepSeek 余额响应缺少 {currency} total_balance。",
                ))
            override = account.budget_override.get(currency)
            if override is not None:
                row.limit = MetricValue.from_value(
                    override, provenance=MetricProvenance.CONFIGURED
                )
            financials.append(row)
        for currency, override in account.budget_override.items():
            if currency not in seen:
                financials.append(FinancialUsage(
                    currency=currency,
                    limit=MetricValue.from_value(
                        override, provenance=MetricProvenance.CONFIGURED
                    ),
                ))
        if not any(row.balance is not None for row in financials):
            unavailable.append(UnavailableMetric(
                field="financials.*.balance",
                code="unavailable",
                message="DeepSeek 余额响应没有可用的币种明细。",
            ))
        fetched = utc_now()
        warnings = []
        if payload.get("is_available") is False:
            warnings.append("DeepSeek 报告当前余额不足，API 调用不可用。")
        return UsageSnapshot(
            account_id=account.id,
            status=UsageSnapshotStatus.OK,
            period=period,
            financials=financials,
            tokens=local.tokens if local is not None else None,
            request_count=local.request_count if local is not None else None,
            unavailable=unavailable,
            evidence=EvidenceLevel.OFFICIAL_ACCOUNT,
            fetched_at=fetched,
            stale_at=fetched + timedelta(minutes=5),
            warnings=warnings,
        )


class DeepSeekPortalUsageAdapter(UsageAdapter):
    """Official portal collector using the Electron-owned persistent session."""

    def __init__(self, browser_request: Any) -> None:
        self._browser_request = browser_request

    @property
    def adapter_id(self) -> str:
        return "deepseek_portal"

    @staticmethod
    def _extract_balances(text: str) -> list[FinancialUsage]:
        patterns = {
            "CNY": r"(?:¥|￥|CNY|RMB)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
            "USD": r"(?:\$|USD)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        }
        rows: list[FinancialUsage] = []
        for currency, pattern in patterns.items():
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            rows.append(FinancialUsage(
                currency=currency,
                balance=MetricValue.from_value(
                    match.group(1).replace(",", ""),
                    provenance=MetricProvenance.PROVIDER_PORTAL,
                ),
            ))
        return rows

    async def fetch_snapshot(
        self,
        account: UsageAccount,
        period: UsagePeriod,
        *,
        local: LocalUsageSummary | None = None,
    ) -> UsageSnapshot:
        await self._browser_request(
            account, "browser.open",
            {"url": "https://platform.deepseek.com/usage", "visible": False},
        )
        text = str(await self._browser_request(
            account, "browser.evaluate",
            {"script": "String(document.body && document.body.innerText || '').slice(0, 200000)"},
        ) or "")
        balances = self._extract_balances(text)
        if not balances:
            await self._browser_request(
                account, "browser.pause_for_user",
                {"prompt": "请在 DeepSeek 官方窗口完成登录，然后回到用量页并点击继续。"},
            )
            await self._browser_request(
                account, "browser.goto", {"url": "https://platform.deepseek.com/usage"}
            )
            text = str(await self._browser_request(
                account, "browser.evaluate",
                {"script": "String(document.body && document.body.innerText || '').slice(0, 200000)"},
            ) or "")
            balances = self._extract_balances(text)
        if not balances:
            raise UsageUnavailable(
                "deepseek_portal_unavailable: 官方页面未返回可识别的余额，可重新登录或继续使用 API Key /user/balance"
            )
        fetched = utc_now()
        return UsageSnapshot(
            account_id=account.id,
            status=UsageSnapshotStatus.OK,
            period=period,
            financials=balances,
            tokens=local.tokens if local else None,
            request_count=local.request_count if local else None,
            unavailable=[UnavailableMetric(
                field="financials.*.used",
                code="not_supported",
                message="DeepSeek 官方门户采集当前余额，不把余额伪装为本月用量。",
            )],
            evidence=EvidenceLevel.OFFICIAL_ACCOUNT,
            fetched_at=fetched,
            stale_at=fetched + timedelta(minutes=5),
            warnings=["Cookie 仅保存在按账户隔离的 Electron 持久会话中。"],
        )
