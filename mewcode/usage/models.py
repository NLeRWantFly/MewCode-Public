"""Provider-neutral usage, quota, and attribution models.

The key design rule is that token telemetry, account billing, rate-limit
headroom, and prepaid credit are different facts.  They intentionally have
separate fields and evidence levels so callers cannot accidentally present a
relay's claim as an upstream account balance.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def _config_contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in {
                "api_key", "admin_api_key", "password", "secret", "token",
                "cookie", "authorization", "credential", "credentials",
            } or normalized.endswith(("_password", "_secret", "_token", "_cookie")):
                return True
            if _config_contains_secret(item):
                return True
    elif isinstance(value, list):
        return any(_config_contains_secret(item) for item in value)
    return False


def credential_fingerprint(secret: str) -> str:
    """Return a stable, non-secret identifier for a credential.

    API keys are high-entropy values, so a truncated SHA-256 digest is useful
    for distinguishing local configurations without persisting the key itself.
    """

    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


class EvidenceLevel(str, Enum):
    """Strength and provenance of a usage fact."""

    OFFICIAL_ACCOUNT = "official_account"
    OFFICIAL_RESPONSE = "official_response"
    RELAY_ATTESTED = "relay_attested"
    RELAY_REPORTED = "relay_reported"
    LOCAL_ESTIMATE = "local_estimate"
    UNAVAILABLE = "unavailable"


class AttributionStatus(str, Enum):
    """How confidently a request is tied to a billed upstream account."""

    REQUEST_VERIFIED = "request_verified"
    AGGREGATE_VERIFIED = "aggregate_verified"
    CONFIGURED = "configured"
    UNVERIFIABLE = "unverifiable"
    MISMATCH = "mismatch"


class MetricProvenance(str, Enum):
    """Where one normalized metric came from."""

    OFFICIAL_API = "official_api"
    PROVIDER_PORTAL = "provider_portal"
    LOCAL_LEDGER = "local_ledger"
    CONFIGURED = "configured"
    DERIVED = "derived"


class MetricQuality(str, Enum):
    EXACT = "exact"
    ESTIMATE = "estimate"


class UsageSnapshotStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    ACTION_REQUIRED = "action_required"
    ERROR = "error"


class UsageRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    ACTION_REQUIRED = "action_required"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Money:
    amount: Decimal = Decimal("0")
    currency: str = "usd"

    @classmethod
    def from_value(cls, value: Any, currency: str = "usd") -> "Money":
        return cls(Decimal(str(value or 0)), (currency or "usd").lower())

    @classmethod
    def from_cents(cls, value: Any, currency: str = "usd") -> "Money":
        return cls(Decimal(str(value or 0)) / Decimal("100"), currency.lower())

    def minus(self, other: "Money", *, floor_zero: bool = False) -> "Money":
        if self.currency != other.currency:
            raise ValueError("cannot subtract different currencies")
        amount = self.amount - other.amount
        if floor_zero and amount < 0:
            amount = Decimal("0")
        return Money(amount, self.currency)

    def to_dict(self) -> dict[str, str]:
        return {"amount": str(self.amount), "currency": self.currency}


@dataclass(frozen=True)
class MetricValue:
    """A decimal metric with field-level provenance.

    Values are serialized as strings so billing amounts never pass through a
    binary float on their way to the desktop UI or a third-party plugin.
    """

    value: Decimal
    provenance: MetricProvenance
    quality: MetricQuality = MetricQuality.EXACT

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        provenance: MetricProvenance | str,
        quality: MetricQuality | str = MetricQuality.EXACT,
    ) -> "MetricValue":
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise ValueError("metric value must be finite")
        return cls(
            value=decimal_value,
            provenance=MetricProvenance(provenance),
            quality=MetricQuality(quality),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "MetricValue | None":
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("metric value must be an object or null")
        return cls.from_value(
            value.get("value"),
            provenance=value.get("provenance"),
            quality=value.get("quality", MetricQuality.EXACT.value),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "value": str(self.value),
            "provenance": self.provenance.value,
            "quality": self.quality.value,
        }


@dataclass
class FinancialUsage:
    currency: str
    used: MetricValue | None = None
    limit: MetricValue | None = None
    remaining: MetricValue | None = None
    balance: MetricValue | None = None

    def __post_init__(self) -> None:
        self.currency = self.currency.upper().strip()
        if not self.currency or len(self.currency) > 12:
            raise ValueError("financial currency must be a short non-empty code")
        for name in ("used", "limit", "balance"):
            metric = getattr(self, name)
            if metric is not None and metric.value < 0:
                raise ValueError(f"financial {name} cannot be negative")

    def derive_remaining(self) -> None:
        """Derive remaining only from a same-row period limit and spend."""

        if self.remaining is not None or self.limit is None or self.used is None:
            return
        self.remaining = MetricValue.from_value(
            self.limit.value - self.used.value,
            provenance=MetricProvenance.DERIVED,
            quality=(
                MetricQuality.ESTIMATE
                if MetricQuality.ESTIMATE in {self.limit.quality, self.used.quality}
                else MetricQuality.EXACT
            ),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "FinancialUsage":
        if not isinstance(value, dict):
            raise ValueError("financial entry must be an object")
        result = cls(
            currency=str(value.get("currency") or ""),
            used=MetricValue.from_dict(value.get("used")),
            limit=MetricValue.from_dict(value.get("limit")),
            remaining=MetricValue.from_dict(value.get("remaining")),
            balance=MetricValue.from_dict(value.get("balance")),
        )
        result.derive_remaining()
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "used": self.used.to_dict() if self.used else None,
            "limit": self.limit.to_dict() if self.limit else None,
            "remaining": self.remaining.to_dict() if self.remaining else None,
            "balance": self.balance.to_dict() if self.balance else None,
        }


@dataclass(frozen=True)
class UsagePeriod:
    start: datetime
    end: datetime
    timezone: str = "UTC"
    policy: str = "current_month"

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("usage period end must be after start")
        if self.timezone != "UTC":
            raise ValueError("official usage periods must use UTC")

    @classmethod
    def from_dict(cls, value: Any) -> "UsagePeriod":
        if not isinstance(value, dict):
            raise ValueError("period must be an object")
        start = datetime.fromisoformat(str(value.get("start") or ""))
        end = datetime.fromisoformat(str(value.get("end") or ""))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        return cls(
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
            timezone=str(value.get("timezone") or "UTC"),
            policy=str(value.get("policy") or "current_month"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "start": self.start.astimezone(UTC).isoformat(),
            "end": self.end.astimezone(UTC).isoformat(),
            "timezone": self.timezone,
            "policy": self.policy,
        }


@dataclass(frozen=True)
class UnavailableMetric:
    field: str
    code: str
    message: str

    @classmethod
    def from_dict(cls, value: Any) -> "UnavailableMetric":
        if not isinstance(value, dict):
            raise ValueError("unavailable entry must be an object")
        field_name = str(value.get("field") or "").strip()
        code = str(value.get("code") or "unavailable").strip()
        message = str(value.get("message") or "").strip()
        if not field_name or not message:
            raise ValueError("unavailable entry requires field and message")
        if len(field_name) > 200 or len(code) > 100 or len(message) > 2000:
            raise ValueError("unavailable entry is too large")
        return cls(field=field_name, code=code, message=message)

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass
class UsageAccount:
    id: str
    name: str
    adapter_id: str
    linked_provider_keys: list[str] = field(default_factory=list)
    period_policy: str = "current_month"
    budget_override: dict[str, str] = field(default_factory=dict)
    plugin_id: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    has_secret: bool = False
    managed: bool = False
    source_kind: str = "manual"
    secret_values: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.id = self.id.strip()
        self.name = self.name.strip()
        self.adapter_id = self.adapter_id.strip().lower()
        self.plugin_id = self.plugin_id.strip().lower()
        self.source_kind = self.source_kind.strip().lower() or "manual"
        if not self.id or len(self.id) > 180:
            raise ValueError("usage account id is required and must be <= 180 chars")
        if not self.name or len(self.name) > 120:
            raise ValueError("usage account name is required and must be <= 120 chars")
        if not self.adapter_id:
            raise ValueError("usage account adapter_id is required")
        if self.period_policy != "current_month":
            raise ValueError("only current_month period_policy is supported in v1")
        normalized_budget: dict[str, str] = {}
        for currency, raw in self.budget_override.items():
            metric = MetricValue.from_value(
                raw, provenance=MetricProvenance.CONFIGURED
            )
            if metric.value < 0:
                raise ValueError("budget override cannot be negative")
            normalized_budget[str(currency).upper()] = str(metric.value)
        self.budget_override = normalized_budget
        self.linked_provider_keys = list(dict.fromkeys(
            str(item).strip() for item in self.linked_provider_keys if str(item).strip()
        ))
        if _config_contains_secret(self.config):
            raise ValueError(
                "usage account config cannot contain credentials; use safeStorage secret_values"
            )
        try:
            config_size = len(json.dumps(
                self.config, ensure_ascii=False, separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("usage account config must be JSON serializable") from exc
        if config_size > 100_000:
            raise ValueError("usage account config exceeds 100000 bytes")
        self.has_secret = self.has_secret or bool(self.secret_values)

    @classmethod
    def from_dict(
        cls, value: Any, *, secret_values: dict[str, str] | None = None
    ) -> "UsageAccount":
        if not isinstance(value, dict):
            raise ValueError("usage account must be an object")

        def parsed_time(name: str) -> datetime:
            raw = value.get(name)
            if not raw:
                return utc_now()
            result = datetime.fromisoformat(str(raw))
            if result.tzinfo is None:
                result = result.replace(tzinfo=UTC)
            return result.astimezone(UTC)

        return cls(
            id=str(value.get("id") or ""),
            name=str(value.get("name") or ""),
            adapter_id=str(value.get("adapter_id") or ""),
            linked_provider_keys=list(value.get("linked_provider_keys") or []),
            period_policy=str(value.get("period_policy") or "current_month"),
            budget_override=dict(value.get("budget_override") or {}),
            plugin_id=str(value.get("plugin_id") or ""),
            config=dict(value.get("config") or {}),
            enabled=bool(value.get("enabled", True)),
            has_secret=bool(value.get("has_secret", False)),
            managed=bool(value.get("managed", False)),
            source_kind=str(value.get("source_kind") or "manual"),
            secret_values=dict(secret_values or value.get("secret_values") or {}),
            created_at=parsed_time("created_at"),
            updated_at=parsed_time("updated_at"),
        )

    def to_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "name": self.name,
            "adapter_id": self.adapter_id,
            "linked_provider_keys": list(self.linked_provider_keys),
            "period_policy": self.period_policy,
            "budget_override": dict(self.budget_override),
            "plugin_id": self.plugin_id,
            "config": self.config,
            "enabled": self.enabled,
            "has_secret": self.has_secret,
            "managed": self.managed,
            "source_kind": self.source_kind,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "updated_at": self.updated_at.astimezone(UTC).isoformat(),
        }
        if include_secrets:
            payload["secret_values"] = dict(self.secret_values)
        return payload


@dataclass(frozen=True)
class QuotaWindow:
    name: str
    used_percent: Decimal
    window_seconds: int | None = None
    resets_at: datetime | None = None
    provenance: MetricProvenance = MetricProvenance.OFFICIAL_API

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip())
        decimal_percent = Decimal(str(self.used_percent))
        if not decimal_percent.is_finite():
            raise ValueError("quota window used_percent must be finite")
        object.__setattr__(self, "used_percent", decimal_percent)
        if not self.name or len(self.name) > 120:
            raise ValueError("quota window name is required and must be <= 120 chars")
        if self.used_percent < 0:
            raise ValueError("quota window used_percent cannot be negative")
        if self.window_seconds is not None and self.window_seconds <= 0:
            raise ValueError("quota window window_seconds must be positive")

    @classmethod
    def from_dict(cls, value: Any) -> "QuotaWindow":
        if not isinstance(value, dict):
            raise ValueError("quota window must be an object")
        reset = value.get("resets_at")
        resets_at = None
        if reset:
            resets_at = datetime.fromisoformat(str(reset))
            if resets_at.tzinfo is None:
                resets_at = resets_at.replace(tzinfo=UTC)
            resets_at = resets_at.astimezone(UTC)
        seconds = value.get("window_seconds")
        return cls(
            name=str(value.get("name") or ""),
            used_percent=Decimal(str(value.get("used_percent") or 0)),
            window_seconds=int(seconds) if seconds is not None else None,
            resets_at=resets_at,
            provenance=MetricProvenance(value.get("provenance") or "official_api"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "used_percent": float(self.used_percent),
            "window_seconds": self.window_seconds,
            "resets_at": self.resets_at.astimezone(UTC).isoformat() if self.resets_at else None,
            "provenance": self.provenance.value,
        }


@dataclass
class UsageSnapshot:
    account_id: str
    status: UsageSnapshotStatus
    period: UsagePeriod
    financials: list[FinancialUsage] = field(default_factory=list)
    tokens: TokenUsage | None = None
    request_count: int | None = None
    quota_windows: list[QuotaWindow] = field(default_factory=list)
    unavailable: list[UnavailableMetric] = field(default_factory=list)
    evidence: EvidenceLevel = EvidenceLevel.UNAVAILABLE
    fetched_at: datetime = field(default_factory=utc_now)
    stale_at: datetime | None = None
    warnings: list[str] = field(default_factory=list)
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        if self.schema_version != "1.0":
            raise ValueError("unsupported usage snapshot schema_version")
        if not self.account_id:
            raise ValueError("usage snapshot account_id is required")
        if self.request_count is not None and self.request_count < 0:
            raise ValueError("request_count cannot be negative")
        if len(self.financials) > 50 or len(self.quota_windows) > 20 or len(self.unavailable) > 100:
            raise ValueError("usage snapshot contains too many entries")
        if len(self.warnings) > 50 or any(len(item) > 2000 for item in self.warnings):
            raise ValueError("usage snapshot warnings are too large")
        if self.tokens is not None and any(
            value < 0 for value in (
                self.tokens.input_tokens,
                self.tokens.cached_input_tokens,
                self.tokens.cache_write_input_tokens,
                self.tokens.output_tokens,
                self.tokens.reasoning_tokens,
            )
        ):
            raise ValueError("usage snapshot token counts cannot be negative")
        seen: set[str] = set()
        for item in self.financials:
            if item.currency in seen:
                raise ValueError(f"duplicate financial currency: {item.currency}")
            seen.add(item.currency)
            item.derive_remaining()

    @classmethod
    def from_dict(cls, value: Any) -> "UsageSnapshot":
        if not isinstance(value, dict):
            raise ValueError("usage snapshot must be an object")
        raw_tokens = value.get("tokens")
        tokens = None
        if raw_tokens is not None:
            if not isinstance(raw_tokens, dict):
                raise ValueError("tokens must be an object or null")
            tokens = TokenUsage(
                input_tokens=int(raw_tokens.get("input_tokens") or 0),
                cached_input_tokens=int(raw_tokens.get("cached_input_tokens") or 0),
                cache_write_input_tokens=int(raw_tokens.get("cache_write_input_tokens") or 0),
                output_tokens=int(raw_tokens.get("output_tokens") or 0),
                reasoning_tokens=int(raw_tokens.get("reasoning_tokens") or 0),
            )

        def parsed_time(name: str, *, optional: bool = False) -> datetime | None:
            raw = value.get(name)
            if not raw:
                return None if optional else utc_now()
            result = datetime.fromisoformat(str(raw))
            if result.tzinfo is None:
                result = result.replace(tzinfo=UTC)
            return result.astimezone(UTC)

        return cls(
            schema_version=str(value.get("schema_version") or ""),
            account_id=str(value.get("account_id") or ""),
            status=UsageSnapshotStatus(value.get("status") or "error"),
            period=UsagePeriod.from_dict(value.get("period")),
            financials=[
                FinancialUsage.from_dict(item)
                for item in (value.get("financials") or [])
            ],
            tokens=tokens,
            request_count=(
                int(value["request_count"])
                if value.get("request_count") is not None
                else None
            ),
            quota_windows=[
                QuotaWindow.from_dict(item)
                for item in (value.get("quota_windows") or [])
            ],
            unavailable=[
                UnavailableMetric.from_dict(item)
                for item in (value.get("unavailable") or [])
            ],
            evidence=EvidenceLevel(value.get("evidence") or "unavailable"),
            fetched_at=parsed_time("fetched_at") or utc_now(),
            stale_at=parsed_time("stale_at", optional=True),
            warnings=[str(item) for item in (value.get("warnings") or [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "account_id": self.account_id,
            "status": self.status.value,
            "period": self.period.to_dict(),
            "financials": [item.to_dict() for item in self.financials],
            "tokens": self.tokens.to_dict() if self.tokens else None,
            "request_count": self.request_count,
            "quota_windows": [item.to_dict() for item in self.quota_windows],
            "unavailable": [item.to_dict() for item in self.unavailable],
            "evidence": self.evidence.value,
            "fetched_at": self.fetched_at.astimezone(UTC).isoformat(),
            "stale_at": (
                self.stale_at.astimezone(UTC).isoformat() if self.stale_at else None
            ),
            "warnings": list(self.warnings),
        }

    def canonical_hash(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class UsageRun:
    id: str
    account_id: str
    status: UsageRunStatus = UsageRunStatus.QUEUED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    message: str = ""
    interaction_id: str = ""
    snapshot_hash: str = ""
    plugin_id: str = ""
    plugin_version: str = ""
    plugin_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account_id": self.account_id,
            "status": self.status.value,
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "updated_at": self.updated_at.astimezone(UTC).isoformat(),
            "message": self.message,
            "interaction_id": self.interaction_id,
            "snapshot_hash": self.snapshot_hash,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "plugin_sha256": self.plugin_sha256,
        }


@dataclass
class TokenUsage:
    """Normalized request token counts.

    ``input_tokens`` is the uncached input portion.  Cached reads and cache
    writes are separate so GPT-5.6 pricing and cache effectiveness can be
    reconciled without double-counting the full prompt.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def input_total(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.cache_write_input_tokens
        )

    @property
    def total(self) -> int:
        return self.input_total + self.output_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        if not self.input_total:
            return None
        return self.cached_input_tokens / self.input_total

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=(
                self.cached_input_tokens + other.cached_input_tokens
            ),
            cache_write_input_tokens=(
                self.cache_write_input_tokens
                + other.cache_write_input_tokens
            ),
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    def to_dict(self) -> dict[str, int | float | None]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "input_total": self.input_total,
            "cache_hit_rate": self.cache_hit_rate,
            "total_tokens": self.total,
        }


def _header_int(headers: dict[str, str], name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(value.replace(",", "").strip())
    except (TypeError, ValueError):
        return None


@dataclass
class RateLimitSnapshot:
    """Per-route rate-limit headroom, never an account credit balance."""

    request_limit: int | None = None
    requests_remaining: int | None = None
    request_reset: str = ""
    token_limit: int | None = None
    tokens_remaining: int | None = None
    token_reset: str = ""

    @classmethod
    def from_headers(cls, raw_headers: dict[str, str]) -> "RateLimitSnapshot | None":
        headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
        snapshot = cls(
            request_limit=_header_int(headers, "x-ratelimit-limit-requests"),
            requests_remaining=_header_int(
                headers, "x-ratelimit-remaining-requests"
            ),
            request_reset=headers.get("x-ratelimit-reset-requests", ""),
            token_limit=_header_int(headers, "x-ratelimit-limit-tokens"),
            tokens_remaining=_header_int(
                headers, "x-ratelimit-remaining-tokens"
            ),
            token_reset=headers.get("x-ratelimit-reset-tokens", ""),
        )
        if all(
            value in (None, "")
            for value in (
                snapshot.request_limit,
                snapshot.requests_remaining,
                snapshot.request_reset,
                snapshot.token_limit,
                snapshot.tokens_remaining,
                snapshot.token_reset,
            )
        ):
            return None
        return snapshot

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_limit": self.request_limit,
            "requests_remaining": self.requests_remaining,
            "request_reset": self.request_reset,
            "token_limit": self.token_limit,
            "tokens_remaining": self.tokens_remaining,
            "token_reset": self.token_reset,
        }


@dataclass
class RequestUsage:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=utc_now)
    provider_name: str = ""
    protocol: str = ""
    base_url: str = ""
    endpoint: str = ""
    configured_model: str = ""
    response_model: str = ""
    response_id: str = ""
    request_id: str = ""
    upstream_request_id: str = ""
    service_tier: str = ""
    organization_id: str = ""
    project_id: str = ""
    api_key_id: str = ""
    credential_fingerprint: str = ""
    tokens: TokenUsage = field(default_factory=TokenUsage)
    cache_usage_available: bool = False
    rate_limits: RateLimitSnapshot | None = None
    evidence: EvidenceLevel = EvidenceLevel.UNAVAILABLE
    attribution: AttributionStatus = AttributionStatus.UNVERIFIABLE
    receipt_sha256: str = ""
    receipt_signature_present: bool = False
    session_id: str = ""
    agent_id: str = ""
    trace_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
            "provider_name": self.provider_name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "endpoint": self.endpoint,
            "configured_model": self.configured_model,
            "response_model": self.response_model,
            "response_id": self.response_id,
            "request_id": self.request_id,
            "upstream_request_id": self.upstream_request_id,
            "service_tier": self.service_tier,
            "organization_id": self.organization_id,
            "project_id": self.project_id,
            "api_key_id": self.api_key_id,
            "credential_fingerprint": self.credential_fingerprint,
            "tokens": self.tokens.to_dict(),
            "cache_usage_available": self.cache_usage_available,
            "rate_limits": self.rate_limits.to_dict() if self.rate_limits else None,
            "evidence": self.evidence.value,
            "attribution": self.attribution.value,
            "receipt_sha256": self.receipt_sha256,
            "receipt_signature_present": self.receipt_signature_present,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "trace_id": self.trace_id,
            "metadata": self.metadata,
        }


@dataclass
class UsageQuery:
    start_time: datetime
    end_time: datetime | None = None
    bucket_width: str = "1d"
    project_ids: list[str] = field(default_factory=list)
    api_key_ids: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    group_by: list[str] = field(default_factory=lambda: ["model"])


@dataclass
class UsageGroup:
    start_time: datetime
    end_time: datetime
    tokens: TokenUsage = field(default_factory=TokenUsage)
    num_model_requests: int = 0
    model: str = ""
    project_id: str = ""
    api_key_id: str = ""
    service_tier: str = ""


@dataclass
class AccountUsageSummary:
    start_time: datetime
    end_time: datetime
    groups: list[UsageGroup] = field(default_factory=list)
    tokens: TokenUsage = field(default_factory=TokenUsage)
    request_count: int = 0
    evidence: EvidenceLevel = EvidenceLevel.UNAVAILABLE
    source_request_ids: list[str] = field(default_factory=list)


@dataclass
class SpendLimitSnapshot:
    scope: str
    scope_id: str
    threshold: Money
    spent: Money
    remaining: Money
    enforcement_status: str = ""


@dataclass
class AccountSnapshot:
    provider_name: str
    account_ref: str
    period_start: datetime
    period_end: datetime
    scope_cost: Money
    organization_cost: Money
    organization_limit: SpendLimitSnapshot | None = None
    project_limits: list[SpendLimitSnapshot] = field(default_factory=list)
    prepaid_credit_balance: Money | None = None
    approved_usage_limit: Money | None = None
    evidence: EvidenceLevel = EvidenceLevel.UNAVAILABLE
    attribution: AttributionStatus = AttributionStatus.UNVERIFIABLE
    attribution_reason: str = ""
    source_request_ids: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        def limit_dict(limit: SpendLimitSnapshot) -> dict[str, Any]:
            return {
                "scope": limit.scope,
                "scope_id": limit.scope_id,
                "threshold": limit.threshold.to_dict(),
                "spent": limit.spent.to_dict(),
                "remaining": limit.remaining.to_dict(),
                "enforcement_status": limit.enforcement_status,
            }

        return {
            "provider_name": self.provider_name,
            "account_ref": self.account_ref,
            "period_start": self.period_start.astimezone(UTC).isoformat(),
            "period_end": self.period_end.astimezone(UTC).isoformat(),
            "scope_cost": self.scope_cost.to_dict(),
            "organization_cost": self.organization_cost.to_dict(),
            "organization_limit": (
                limit_dict(self.organization_limit)
                if self.organization_limit
                else None
            ),
            "project_limits": [limit_dict(item) for item in self.project_limits],
            "prepaid_credit_balance": (
                self.prepaid_credit_balance.to_dict()
                if self.prepaid_credit_balance
                else None
            ),
            "approved_usage_limit": (
                self.approved_usage_limit.to_dict()
                if self.approved_usage_limit
                else None
            ),
            "evidence": self.evidence.value,
            "attribution": self.attribution.value,
            "attribution_reason": self.attribution_reason,
            "source_request_ids": self.source_request_ids,
            "limitations": self.limitations,
            "fetched_at": self.fetched_at.astimezone(UTC).isoformat(),
        }

    def canonical_hash(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class LocalUsageSummary:
    request_count: int = 0
    tokens: TokenUsage = field(default_factory=TokenUsage)
    by_model: dict[str, TokenUsage] = field(default_factory=dict)
    evidence_counts: dict[str, int] = field(default_factory=dict)
    latest_rate_limits: RateLimitSnapshot | None = None


@dataclass
class UsageIdentities:
    organization_ids: set[str] = field(default_factory=set)
    project_ids: set[str] = field(default_factory=set)
    api_key_ids: set[str] = field(default_factory=set)


@dataclass
class ReconciliationReport:
    local: LocalUsageSummary
    official: AccountUsageSummary
    input_token_delta: int
    output_token_delta: int
    aggregate_verified: bool
    exact_request_linkage: bool
    explanation: str
