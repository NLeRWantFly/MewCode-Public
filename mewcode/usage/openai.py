"""OpenAI account usage implementation.

Request token telemetry comes from Responses/Chat Completions and is recorded
by ``mewcode.client``.  This backend independently queries OpenAI's Admin Usage,
Costs, and Spend Limit endpoints.  Keeping the admin credential separate from
the generation credential is what makes the account snapshot useful as an
independent billing source.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import httpx

from mewcode.usage.backend import (
    UsageAuthenticationError,
    UsageBackend,
    UsageBackendCapabilities,
    UsageBackendError,
    UsageUnavailable,
)
from mewcode.usage.models import (
    AccountSnapshot,
    AccountUsageSummary,
    AttributionStatus,
    EvidenceLevel,
    Money,
    SpendLimitSnapshot,
    TokenUsage,
    UsageGroup,
    UsageQuery,
    credential_fingerprint,
)


UTC = timezone.utc
OFFICIAL_OPENAI_BASE_URL = "https://api.openai.com/v1"


def is_official_openai_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == "api.openai.com"


def _unix(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp())


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string(value: Any) -> str:
    return str(value) if value is not None else ""


def _money_sum(results: Iterable[dict[str, Any]]) -> Money:
    totals: dict[str, Decimal] = {}
    for result in results:
        amount = result.get("amount") or {}
        currency = _string(amount.get("currency") or "usd").lower()
        totals[currency] = totals.get(currency, Decimal("0")) + Decimal(
            str(amount.get("value") or 0)
        )
    if not totals:
        return Money()
    if len(totals) != 1:
        currencies = ", ".join(sorted(totals))
        raise UsageBackendError(
            f"OpenAI Costs API returned multiple currencies ({currencies}); "
            "a single remaining balance cannot be computed safely"
        )
    currency, amount = next(iter(totals.items()))
    return Money(amount, currency)


class OpenAIUsageBackend(UsageBackend):
    def __init__(
        self,
        *,
        admin_api_key: str,
        admin_base_url: str = OFFICIAL_OPENAI_BASE_URL,
        generation_base_url: str = OFFICIAL_OPENAI_BASE_URL,
        generation_api_key: str = "",
        upstream_organization_id: str = "",
        project_ids: list[str] | None = None,
        api_key_ids: list[str] | None = None,
        attribution_mode: str = "auto",
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.admin_api_key = admin_api_key
        self.admin_base_url = admin_base_url.rstrip("/")
        self.generation_base_url = generation_base_url
        self.generation_api_key = generation_api_key
        self.upstream_organization_id = upstream_organization_id
        self.project_ids = list(project_ids or [])
        self.api_key_ids = list(api_key_ids or [])
        self.attribution_mode = attribution_mode
        self._http_client = http_client
        self.timeout = timeout
        self._observed_organization_id = ""
        self._evidence = (
            EvidenceLevel.OFFICIAL_ACCOUNT
            if is_official_openai_url(self.admin_base_url)
            else EvidenceLevel.RELAY_REPORTED
        )

    @property
    def backend_id(self) -> str:
        return "openai"

    @property
    def observed_organization_id(self) -> str:
        return self._observed_organization_id

    def capabilities(self) -> UsageBackendCapabilities:
        return UsageBackendCapabilities(
            request_tokens=True,
            account_usage=True,
            actual_costs=True,
            hard_spend_limit=True,
            # No documented numeric read endpoint is available for these two
            # account concepts.  They must remain unknown rather than being
            # conflated with hard-spend or rate-limit remaining values.
            prepaid_credit_balance=False,
            approved_usage_limit=False,
            evidence=self._evidence,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.admin_api_key}",
            "Content-Type": "application/json",
        }

    async def _get(
        self,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        missing_ok: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        if not self.admin_api_key:
            raise UsageUnavailable(
                "缺少 OpenAI Admin API key；请设置 OPENAI_ADMIN_KEY 或 provider.usage.admin_api_key"
            )
        url = f"{self.admin_base_url}/{path.lstrip('/')}"
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url, headers=self._headers(), params=params
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(
                        url, headers=self._headers(), params=params
                    )
        except httpx.HTTPError as exc:
            raise UsageBackendError(f"OpenAI usage request failed: {exc}") from exc

        request_id = response.headers.get("x-request-id", "")
        observed_organization = (
            response.headers.get("openai-organization", "")
            or response.headers.get("x-openai-organization", "")
        )
        if observed_organization:
            self._observed_organization_id = observed_organization
        if response.status_code == 404 and missing_ok:
            return None, request_id
        if response.status_code in (401, 403):
            raise UsageAuthenticationError(
                "OpenAI Admin API 拒绝访问；普通项目 API key 不能读取组织 Usage/Costs，"
                "需要组织 Admin API key"
            )
        if response.status_code == 429:
            raise UsageBackendError("OpenAI Admin API rate limited the usage query")
        if response.is_error:
            detail = ""
            try:
                body = response.json()
                detail = _string((body.get("error") or {}).get("message"))
            except (ValueError, AttributeError):
                detail = response.text[:300]
            raise UsageBackendError(
                f"OpenAI usage endpoint returned HTTP {response.status_code}"
                + (f": {detail}" if detail else "")
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UsageBackendError("OpenAI usage endpoint returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise UsageBackendError("OpenAI usage endpoint returned a non-object payload")
        return payload, request_id

    @staticmethod
    def _usage_params(query: UsageQuery) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = [
            ("start_time", str(_unix(query.start_time))),
            ("bucket_width", query.bucket_width),
        ]
        if query.end_time is not None:
            params.append(("end_time", str(_unix(query.end_time))))
        for key, values in (
            ("project_ids", query.project_ids),
            ("api_key_ids", query.api_key_ids),
            ("models", query.models),
            ("group_by", query.group_by),
        ):
            params.extend((key, value) for value in values)
        # 31 one-day buckets is the documented maximum and keeps pagination
        # efficient for month-to-date queries.
        params.append(("limit", "31" if query.bucket_width == "1d" else "168"))
        return params

    async def _paginate(
        self, path: str, params: list[tuple[str, str]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        data: list[dict[str, Any]] = []
        request_ids: list[str] = []
        page = ""
        seen_pages: set[str] = set()
        for _ in range(1000):
            page_params = list(params)
            if page:
                page_params.append(("page", page))
            payload, request_id = await self._get(path, params=page_params)
            assert payload is not None
            if request_id:
                request_ids.append(request_id)
            data.extend(item for item in _as_list(payload.get("data")) if isinstance(item, dict))
            next_page = _string(payload.get("next_page"))
            if not next_page:
                break
            if next_page in seen_pages:
                raise UsageBackendError("OpenAI usage pagination cursor repeated")
            seen_pages.add(next_page)
            page = next_page
        else:
            raise UsageBackendError("OpenAI usage pagination exceeded 1000 pages")
        return data, request_ids

    async def fetch_usage(self, query: UsageQuery) -> AccountUsageSummary:
        buckets, request_ids = await self._paginate(
            "organization/usage/completions", self._usage_params(query)
        )
        groups: list[UsageGroup] = []
        total = TokenUsage()
        request_count = 0
        end_time = query.end_time or datetime.now(UTC)
        for bucket in buckets:
            bucket_start = datetime.fromtimestamp(int(bucket.get("start_time") or 0), UTC)
            bucket_end = datetime.fromtimestamp(int(bucket.get("end_time") or 0), UTC)
            for result in _as_list(bucket.get("results")):
                if not isinstance(result, dict):
                    continue
                cached = int(result.get("input_cached_tokens") or 0)
                cache_write = int(result.get("input_cache_write_tokens") or 0)
                input_total = int(result.get("input_tokens") or 0)
                uncached_value = result.get("input_uncached_tokens")
                uncached = (
                    int(uncached_value)
                    if uncached_value is not None
                    else max(input_total - cached - cache_write, 0)
                )
                tokens = TokenUsage(
                    input_tokens=uncached,
                    cached_input_tokens=cached,
                    cache_write_input_tokens=cache_write,
                    output_tokens=int(result.get("output_tokens") or 0),
                    reasoning_tokens=int(result.get("reasoning_tokens") or 0),
                )
                count = int(result.get("num_model_requests") or 0)
                groups.append(
                    UsageGroup(
                        start_time=bucket_start,
                        end_time=bucket_end,
                        tokens=tokens,
                        num_model_requests=count,
                        model=_string(result.get("model")),
                        project_id=_string(result.get("project_id")),
                        api_key_id=_string(result.get("api_key_id")),
                        service_tier=_string(result.get("service_tier")),
                    )
                )
                total = total.add(tokens)
                request_count += count
        return AccountUsageSummary(
            start_time=query.start_time,
            end_time=end_time,
            groups=groups,
            tokens=total,
            request_count=request_count,
            evidence=self._evidence,
            source_request_ids=request_ids,
        )

    async def _fetch_cost(
        self,
        query: UsageQuery,
        *,
        project_ids: list[str] | None = None,
        api_key_ids: list[str] | None = None,
    ) -> tuple[Money, list[str]]:
        params: list[tuple[str, str]] = [
            ("start_time", str(_unix(query.start_time))),
            ("bucket_width", "1d"),
            ("limit", "180"),
        ]
        if query.end_time is not None:
            params.append(("end_time", str(_unix(query.end_time))))
        for value in project_ids or []:
            params.append(("project_ids", value))
        for value in api_key_ids or []:
            params.append(("api_key_ids", value))
        buckets, request_ids = await self._paginate("organization/costs", params)
        results = [
            result
            for bucket in buckets
            for result in _as_list(bucket.get("results"))
            if isinstance(result, dict)
        ]
        return _money_sum(results), request_ids

    async def fetch_cost(
        self,
        query: UsageQuery,
        *,
        project_ids: list[str] | None = None,
        api_key_ids: list[str] | None = None,
    ) -> tuple[Money, list[str]]:
        """Public Costs API operation for normalized account adapters."""

        return await self._fetch_cost(
            query, project_ids=project_ids, api_key_ids=api_key_ids
        )

    async def _fetch_limit(
        self, path: str, *, scope: str, scope_id: str, spent: Money
    ) -> tuple[SpendLimitSnapshot | None, str]:
        payload, request_id = await self._get(path, missing_ok=True)
        if payload is None:
            return None, request_id
        if payload.get("threshold_amount") is None:
            return None, request_id
        threshold = Money.from_cents(
            payload.get("threshold_amount"), _string(payload.get("currency") or "usd")
        )
        if threshold.currency != spent.currency:
            raise UsageBackendError(
                f"{scope} spend limit currency differs from Costs API currency"
            )
        enforcement = payload.get("enforcement") or {}
        return (
            SpendLimitSnapshot(
                scope=scope,
                scope_id=scope_id,
                threshold=threshold,
                spent=spent,
                remaining=threshold.minus(spent, floor_zero=True),
                enforcement_status=_string(enforcement.get("status")),
            ),
            request_id,
        )

    def _attribution(self) -> tuple[AttributionStatus, str]:
        direct = is_official_openai_url(self.generation_base_url)
        bound = bool(self.project_ids or self.api_key_ids)
        if (
            self.upstream_organization_id
            and self._observed_organization_id
            and self.upstream_organization_id != self._observed_organization_id
        ):
            return (
                AttributionStatus.MISMATCH,
                "配置的上游 organization 与 OpenAI 官方响应头不一致；已拒绝视为同一账户",
            )
        if self._evidence != EvidenceLevel.OFFICIAL_ACCOUNT:
            return (
                AttributionStatus.UNVERIFIABLE,
                "账单端点本身不是 api.openai.com，只能视为 provider/中转站声明",
            )
        if direct:
            if bound:
                return (
                    AttributionStatus.AGGREGATE_VERIFIED,
                    "生成请求直连 OpenAI，且官方 Usage/Costs 按 project/API-key 绑定聚合",
                )
            return (
                AttributionStatus.CONFIGURED,
                "生成请求直连 OpenAI，账户总账来自官方；未用独占 project/API-key，无法证明总账只属于 MewCode 请求",
            )
        if self.attribution_mode == "byok-relay" and bound:
            return (
                AttributionStatus.AGGREGATE_VERIFIED,
                "中转使用 BYOK 且配置了上游 project/API-key 过滤；官方聚合可核验，"
                "但单请求仍需中转回执关联",
            )
        if bound:
            return (
                AttributionStatus.CONFIGURED,
                "官方账户数据真实，project/API-key 绑定来自本地配置；中转未提供可独立验证的单请求证明",
            )
        return (
            AttributionStatus.UNVERIFIABLE,
            "中转站未暴露上游 project/API-key 绑定；无法证明请求扣在哪个真实 OpenAI 账户",
        )

    async def fetch_account_snapshot(
        self, query: UsageQuery, *, provider_name: str = ""
    ) -> AccountSnapshot:
        if not self.admin_api_key:
            raise UsageUnavailable(
                "未配置 OPENAI_ADMIN_KEY，无法读取真实 OpenAI 组织 Usage/Costs"
            )

        source_ids: list[str] = []
        organization_cost, ids = await self._fetch_cost(query)
        source_ids.extend(ids)
        if self.project_ids or self.api_key_ids:
            scope_cost, ids = await self._fetch_cost(
                query,
                project_ids=self.project_ids,
                api_key_ids=self.api_key_ids,
            )
            source_ids.extend(ids)
        else:
            scope_cost = organization_cost

        organization_limit, request_id = await self._fetch_limit(
            "organization/spend_limit",
            scope="organization",
            scope_id=self.upstream_organization_id,
            spent=organization_cost,
        )
        if request_id:
            source_ids.append(request_id)

        project_limits: list[SpendLimitSnapshot] = []
        for project_id in self.project_ids:
            project_cost, ids = await self._fetch_cost(
                query, project_ids=[project_id]
            )
            source_ids.extend(ids)
            limit, request_id = await self._fetch_limit(
                f"organization/projects/{quote(project_id, safe='')}/spend_limit",
                scope="project",
                scope_id=project_id,
                spent=project_cost,
            )
            if request_id:
                source_ids.append(request_id)
            if limit is not None:
                project_limits.append(limit)

        attribution, reason = self._attribution()
        limitations = [
            "公开 Admin API 未提供可读取的 prepaid credit balance 数值；耗尽时只能从 credit_balance_exhausted 错误确认。",
            "OpenAI 批准的 usage-tier 月度上限与用户配置的 hard spend limit 是不同概念；前者没有在此公开读取接口中返回。",
            "x-ratelimit-remaining-* 只表示短时请求/token 限流余量，不是账户余额。",
        ]
        if not (self.project_ids or self.api_key_ids):
            limitations.append(
                "未配置独占 project_ids/api_key_ids，scope_cost 包含该组织内其他调用。"
            )
        if not is_official_openai_url(self.generation_base_url):
            limitations.append(
                "OpenAI Usage API 是聚合接口且不返回逐请求 ID；中转请求若无上游回执，不能做逐请求核销。"
            )

        account_ref = (
            self._observed_organization_id
            or self.upstream_organization_id
            or (
            "openai-admin:" + credential_fingerprint(self.admin_api_key)
            )
        )
        return AccountSnapshot(
            provider_name=provider_name,
            account_ref=account_ref,
            period_start=query.start_time,
            period_end=query.end_time or datetime.now(UTC),
            scope_cost=scope_cost,
            organization_cost=organization_cost,
            organization_limit=organization_limit,
            project_limits=project_limits,
            prepaid_credit_balance=None,
            approved_usage_limit=None,
            evidence=self._evidence,
            attribution=attribution,
            attribution_reason=reason,
            source_request_ids=list(dict.fromkeys(source_ids)),
            limitations=limitations,
        )
