"""Unified interface implemented by account-usage providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from mewcode.usage.models import (
    AccountSnapshot,
    AccountUsageSummary,
    EvidenceLevel,
    LocalUsageSummary,
    UsageAccount,
    UsagePeriod,
    UsageQuery,
    UsageSnapshot,
)


class UsageBackendError(RuntimeError):
    pass


class UsageAuthenticationError(UsageBackendError):
    pass


class UsageUnavailable(UsageBackendError):
    pass


@dataclass(frozen=True)
class UsageBackendCapabilities:
    request_tokens: bool = True
    account_usage: bool = False
    actual_costs: bool = False
    hard_spend_limit: bool = False
    prepaid_credit_balance: bool = False
    approved_usage_limit: bool = False
    evidence: EvidenceLevel = EvidenceLevel.UNAVAILABLE


class UsageBackend(ABC):
    """Provider-neutral account usage and quota contract."""

    @property
    @abstractmethod
    def backend_id(self) -> str: ...

    @abstractmethod
    def capabilities(self) -> UsageBackendCapabilities: ...

    @abstractmethod
    async def fetch_usage(self, query: UsageQuery) -> AccountUsageSummary: ...

    @abstractmethod
    async def fetch_account_snapshot(
        self, query: UsageQuery, *, provider_name: str = ""
    ) -> AccountSnapshot: ...


class UsageAdapter(ABC):
    """Account-oriented adapter used by the desktop usage runtime."""

    @property
    @abstractmethod
    def adapter_id(self) -> str: ...

    @abstractmethod
    async def fetch_snapshot(
        self,
        account: UsageAccount,
        period: UsagePeriod,
        *,
        local: LocalUsageSummary | None = None,
    ) -> UsageSnapshot: ...
