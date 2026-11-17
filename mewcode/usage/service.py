"""Application-facing usage tracking service."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from mewcode.usage.backend import UsageUnavailable
from mewcode.usage.adapters import UsageAdapterRegistry
from mewcode.usage.ledger import UsageLedger
from mewcode.usage.models import (
    AccountSnapshot,
    AccountUsageSummary,
    AttributionStatus,
    LocalUsageSummary,
    ReconciliationReport,
    RequestUsage,
    UsageQuery,
    UsageAccount,
)
from mewcode.usage.openai import OpenAIUsageBackend, is_official_openai_url

if TYPE_CHECKING:
    from mewcode.client import LLMClient
    from mewcode.config import ProviderConfig


UTC = timezone.utc


@dataclass(frozen=True)
class UsageTraceScope:
    session_id: str = ""
    agent_id: str = ""
    trace_id: str = ""


_USAGE_SCOPE: ContextVar[UsageTraceScope] = ContextVar(
    "mewcode_usage_scope", default=UsageTraceScope()
)


def current_usage_scope() -> UsageTraceScope:
    return _USAGE_SCOPE.get()


@contextmanager
def usage_trace_scope(
    *, session_id: str = "", agent_id: str = "", trace_id: str = ""
) -> Iterator[None]:
    token = _USAGE_SCOPE.set(
        UsageTraceScope(
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
        )
    )
    try:
        yield
    finally:
        _USAGE_SCOPE.reset(token)


def current_month_query(
    *, project_ids: list[str] | None = None, api_key_ids: list[str] | None = None
) -> UsageQuery:
    now = datetime.now(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    return UsageQuery(
        start_time=start,
        end_time=now,
        project_ids=list(project_ids or []),
        api_key_ids=list(api_key_ids or []),
        group_by=["model", "project_id", "api_key_id", "service_tier"],
    )


class UsageService:
    def __init__(
        self,
        work_dir: str | Path,
        *,
        ledger_path: str | Path | None = None,
    ) -> None:
        self.ledger = UsageLedger(work_dir, ledger_path)
        # New account-oriented callers use this registry.  ``backend_for`` is
        # retained below as the compatibility bridge for the existing /usage
        # command and legacy provider.usage YAML configuration.
        self.adapters = UsageAdapterRegistry.with_builtins()

    def account_from_legacy_provider(
        self, provider: "ProviderConfig", *, provider_key: str = ""
    ) -> UsageAccount:
        """Convert one legacy provider.usage block without persisting secrets."""

        lower = " ".join((provider.name, provider.model, provider.base_url)).lower()
        if provider.protocol == "anthropic" or "anthropic" in lower or "claude" in lower:
            adapter_id = "anthropic"
            secret_values = {
                "admin_api_key": provider.usage.resolve_admin_api_key()
            }
            config = {"base_url": provider.base_url}
        elif "deepseek" in lower:
            adapter_id = "deepseek"
            secret_values = {"api_key": provider.resolve_api_key()}
            config = {"base_url": provider.base_url}
        else:
            adapter_id = "openai"
            secret_values = {
                "admin_api_key": provider.usage.resolve_admin_api_key()
            }
            config = {
                "base_url": provider.usage.admin_base_url,
                "project_ids": list(provider.usage.project_ids),
                "api_key_ids": list(provider.usage.api_key_ids),
            }
        identity = provider_key or provider.name
        safe_name = "".join(
            character if character.isalnum() or character in "._-" else "-"
            for character in identity.lower()
        ).strip("-")[:160] or "provider"
        return UsageAccount(
            id=f"legacy:{safe_name}",
            name=provider.name,
            adapter_id=adapter_id,
            linked_provider_keys=[provider_key] if provider_key else [],
            config=config,
            has_secret=bool(next(iter(secret_values.values()), "")),
            secret_values=secret_values,
        )

    def adapter_for(self, provider: "ProviderConfig"):
        account = self.account_from_legacy_provider(provider)
        return self.adapters.get(account.adapter_id)

    def bind_client(self, client: "LLMClient | None") -> None:
        if client is not None:
            client.set_usage_recorder(self.record_request)

    def record_request(self, record: RequestUsage) -> None:
        self.ledger.append(record)

    def local_summary(
        self,
        *,
        provider_name: str = "",
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> LocalUsageSummary:
        return self.ledger.summarize(
            provider_name=provider_name,
            start_time=start_time,
            end_time=end_time,
        )

    def backend_for(self, provider: "ProviderConfig") -> OpenAIUsageBackend:
        if provider.protocol not in {"openai", "openai-compat"}:
            raise UsageUnavailable(
                f"{provider.protocol} 尚未实现账户级用量后端；本地请求账本仍可使用"
            )
        direct = is_official_openai_url(provider.base_url)
        usage = provider.usage
        if not direct and not usage.account_tracking:
            raise UsageUnavailable(
                "当前是中转/兼容端点。为防止把无关 OpenAI 账户误当成上游账户，"
                "请显式设置 provider.usage.account_tracking: true，并配置上游 "
                "project_ids 或 api_key_ids"
            )
        admin_key = usage.resolve_admin_api_key()
        if not admin_key:
            raise UsageUnavailable(
                "缺少 OpenAI Admin API key；普通调用 key 只能返回请求 token，"
                "不能读取真实组织 Usage/Costs"
            )
        return OpenAIUsageBackend(
            admin_api_key=admin_key,
            admin_base_url=usage.admin_base_url,
            generation_base_url=provider.base_url,
            generation_api_key=provider.resolve_api_key(),
            upstream_organization_id=usage.upstream_organization_id,
            project_ids=usage.project_ids,
            api_key_ids=usage.api_key_ids,
            attribution_mode=(
                "direct" if usage.attribution_mode == "auto" and direct
                else usage.attribution_mode
            ),
        )

    async def account_snapshot(self, provider: "ProviderConfig") -> AccountSnapshot:
        backend = self.backend_for(provider)
        query = current_month_query(
            project_ids=provider.usage.project_ids,
            api_key_ids=provider.usage.api_key_ids,
        )
        snapshot = await backend.fetch_account_snapshot(
            query, provider_name=provider.name
        )
        self._apply_identity_check(provider, backend, query, snapshot)
        self.ledger.save_account_snapshot(snapshot)
        return snapshot

    def _apply_identity_check(
        self,
        provider: "ProviderConfig",
        backend: OpenAIUsageBackend,
        query: UsageQuery,
        snapshot: AccountSnapshot,
    ) -> bool:
        identities = self.ledger.identities(
            provider_name=provider.name,
            start_time=query.start_time,
            end_time=query.end_time,
        )
        official_org = backend.observed_organization_id
        if (
            official_org
            and identities.organization_ids
            and identities.organization_ids != {official_org}
        ):
            snapshot.attribution = AttributionStatus.MISMATCH
            snapshot.attribution_reason = (
                "模型响应记录的 organization 与 Admin API 官方账单 organization 不一致"
            )
            return False
        return True

    async def account_report(
        self, provider: "ProviderConfig"
    ) -> tuple[AccountUsageSummary, AccountSnapshot]:
        """Fetch official token usage and billing facts from one backend."""

        backend = self.backend_for(provider)
        query = current_month_query(
            project_ids=provider.usage.project_ids,
            api_key_ids=provider.usage.api_key_ids,
        )
        usage_task = backend.fetch_usage(query)
        snapshot_task = backend.fetch_account_snapshot(
            query, provider_name=provider.name
        )
        usage, snapshot = await asyncio.gather(usage_task, snapshot_task)
        self._apply_identity_check(provider, backend, query, snapshot)
        self.ledger.save_account_snapshot(snapshot)
        return usage, snapshot

    async def reconcile(self, provider: "ProviderConfig") -> ReconciliationReport:
        backend = self.backend_for(provider)
        query = current_month_query(
            project_ids=provider.usage.project_ids,
            api_key_ids=provider.usage.api_key_ids,
        )
        official = await backend.fetch_usage(query)
        local = self.local_summary(
            provider_name=provider.name,
            start_time=query.start_time,
            end_time=query.end_time,
        )
        bound = bool(provider.usage.project_ids or provider.usage.api_key_ids)
        direct = is_official_openai_url(provider.base_url)
        identities = self.ledger.identities(
            provider_name=provider.name,
            start_time=query.start_time,
            end_time=query.end_time,
        )
        official_org = backend.observed_organization_id
        identity_match = not (
            official_org
            and identities.organization_ids
            and identities.organization_ids != {official_org}
        )
        aggregate_verified = (
            official.evidence.value == "official_account"
            and bound
            and identity_match
        )
        # The public aggregate Usage API has no request-id dimension.  Even a
        # dedicated API key proves the aggregate, not a one-to-one row match.
        exact = False
        if not identity_match:
            explanation = (
                "本地模型响应与 Admin API 账单属于不同 organization，已拒绝核验。"
            )
        elif direct and bound:
            explanation = (
                "官方聚合已按独占 project/API-key 核验；Usage API 不返回逐请求 ID，"
                "因此逐条关联依赖本地 x-request-id 账本。"
            )
        elif not direct and bound:
            explanation = (
                "官方上游聚合可核验；中转到单请求的关联还需要回传上游 request/API-key ID 回执。"
            )
        else:
            explanation = (
                "未使用独占 project/API-key，官方总量可能包含同账户的其他应用调用。"
            )
        return ReconciliationReport(
            local=local,
            official=official,
            input_token_delta=official.tokens.input_total - local.tokens.input_total,
            output_token_delta=official.tokens.output_tokens - local.tokens.output_tokens,
            aggregate_verified=aggregate_verified,
            exact_request_linkage=exact,
            explanation=explanation,
        )


def apply_scope(record: RequestUsage) -> RequestUsage:
    """Attach the current agent/session trace to a newly normalized record."""

    scope = current_usage_scope()
    record.session_id = scope.session_id
    record.agent_id = scope.agent_id
    record.trace_id = scope.trace_id
    return record
