from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CacheSlot:
    """Declarative description of a provider cache/telemetry contract."""

    name: str
    provider: str
    cache_type: str
    supports_streaming: bool = True
    stable_history: bool = True
    single_result_char_limit: int | None = 50_000
    aggregate_result_char_limit: int | None = 200_000


@dataclass(frozen=True)
class CacheUsage:
    """Normalized cache counters for one model request.

    ``available`` is deliberately independent from the numeric values.  A
    provider reporting hit=0/miss=0 is known zero; an omitted cache section is
    unknown and must never be rendered or accumulated as a 0% hit rate.
    """

    hit_tokens: int = 0
    miss_tokens: int = 0
    creation_tokens: int = 0
    prompt_tokens: int = 0
    available: bool = False

    @property
    def hit_rate(self) -> float | None:
        if not self.available:
            return None
        total = max(
            self.prompt_tokens,
            self.hit_tokens + self.miss_tokens + self.creation_tokens,
        )
        return self.hit_tokens / total if total else 0.0


@dataclass
class CacheTotals:
    """Provider-neutral accumulation of already-normalized request counters.

    Extraction remains provider-specific in ``CacheUsageProvider``.  The
    agent only sees this normalized form, so OpenAI, Anthropic and DeepSeek
    counters can never be mixed or interpreted with another vendor's rules.
    Unknown cache telemetry is deliberately excluded from the denominator.
    """

    slot: CacheSlot
    hit_tokens: int = 0
    miss_tokens: int = 0
    creation_tokens: int = 0
    prompt_tokens: int = 0
    reported_requests: int = 0

    @property
    def available(self) -> bool:
        return self.reported_requests > 0

    @property
    def hit_rate(self) -> float | None:
        if not self.available:
            return None
        return self.hit_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    def record(self, usage: CacheUsage) -> None:
        if not usage.available:
            return
        self.hit_tokens += max(int(usage.hit_tokens), 0)
        self.miss_tokens += max(int(usage.miss_tokens), 0)
        self.creation_tokens += max(int(usage.creation_tokens), 0)
        component_total = (
            int(usage.hit_tokens)
            + int(usage.miss_tokens)
            + int(usage.creation_tokens)
        )
        self.prompt_tokens += max(int(usage.prompt_tokens), component_total, 0)
        self.reported_requests += 1


def normalized_cache_usage(
    *,
    input_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    available: bool,
) -> CacheUsage:
    """Build one canonical request sample from a normalized client response."""

    miss = max(int(input_tokens), 0)
    hit = max(int(cache_read_tokens), 0)
    creation = max(int(cache_creation_tokens), 0)
    return CacheUsage(
        hit_tokens=hit,
        miss_tokens=miss,
        creation_tokens=creation,
        prompt_tokens=hit + miss + creation,
        available=bool(available),
    )


class CacheUsageProvider(Protocol):
    slot: CacheSlot

    def extract_usage(self, usage: Any) -> CacheUsage: ...


def usage_counter(source: Any, *names: str) -> tuple[int, bool]:
    """Read a usage counter while distinguishing reported zero from missing."""
    if source is None:
        return 0, False
    if isinstance(source, dict):
        for name in names:
            if name in source:
                return int(source.get(name) or 0), True
        return 0, False

    extras = (
        getattr(source, "model_extra", None)
        or getattr(source, "__pydantic_extra__", None)
        or {}
    )
    if isinstance(extras, dict):
        for name in names:
            if name in extras:
                return int(extras.get(name) or 0), True

    fields_set = getattr(source, "model_fields_set", None)
    for name in names:
        if not hasattr(source, name):
            continue
        value = getattr(source, name)
        if value is not None:
            return int(value or 0), True
        if fields_set is not None and name in fields_set:
            return 0, True
    return 0, False


def usage_object(source: Any, *names: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        for name in names:
            if name in source:
                return source.get(name)
        return None
    extras = (
        getattr(source, "model_extra", None)
        or getattr(source, "__pydantic_extra__", None)
        or {}
    )
    for name in names:
        value = getattr(source, name, None)
        if value is not None:
            return value
        if isinstance(extras, dict) and name in extras:
            return extras.get(name)
    return None


class DeepSeekCacheUsageProvider:
    slot = CacheSlot(
        "deepseek_prefix",
        "deepseek",
        "prefix",
        single_result_char_limit=50_000,
        aggregate_result_char_limit=200_000,
    )

    def extract_usage(self, usage: Any) -> CacheUsage:
        prompt_tokens, _ = usage_counter(usage, "prompt_tokens", "input_tokens")
        hit, hit_reported = usage_counter(
            usage, "prompt_cache_hit_tokens", "cache_hit_tokens"
        )
        miss, miss_reported = usage_counter(
            usage, "prompt_cache_miss_tokens", "cache_miss_tokens"
        )
        available = hit_reported or miss_reported
        if not available:
            standard = OpenAICacheUsageProvider().extract_usage(usage)
            if standard.available:
                return standard
        if prompt_tokens:
            if hit_reported and not miss_reported:
                miss = max(prompt_tokens - hit, 0)
            elif miss_reported and not hit_reported:
                hit = max(prompt_tokens - miss, 0)
        return CacheUsage(
            hit_tokens=hit,
            miss_tokens=miss,
            prompt_tokens=prompt_tokens,
            available=available,
        )


class AnthropicCacheUsageProvider:
    slot = CacheSlot(
        "anthropic_ephemeral",
        "anthropic",
        "ephemeral",
        single_result_char_limit=30_000,
        aggregate_result_char_limit=120_000,
    )

    def extract_usage(self, usage: Any) -> CacheUsage:
        miss, _ = usage_counter(usage, "input_tokens")
        hit, hit_reported = usage_counter(usage, "cache_read_input_tokens")
        creation, creation_reported = usage_counter(
            usage, "cache_creation_input_tokens"
        )
        return CacheUsage(
            hit_tokens=hit,
            miss_tokens=miss,
            creation_tokens=creation,
            prompt_tokens=miss + hit + creation,
            available=hit_reported or creation_reported,
        )


class OpenAICacheUsageProvider:
    slot = CacheSlot(
        "openai_prompt",
        "openai",
        "automatic_or_explicit",
        single_result_char_limit=20_000,
        aggregate_result_char_limit=80_000,
    )

    def extract_usage(self, usage: Any) -> CacheUsage:
        details = usage_object(
            usage, "input_tokens_details", "prompt_tokens_details"
        )
        hit, hit_reported = usage_counter(
            details,
            "cached_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
        )
        creation, creation_reported = usage_counter(
            details,
            "cache_write_tokens",
            "cache_write_input_tokens",
            "cache_creation_input_tokens",
        )
        if not hit_reported:
            hit, hit_reported = usage_counter(
                usage,
                "cached_tokens",
                "cached_input_tokens",
                "cache_read_input_tokens",
            )
        if not creation_reported:
            creation, creation_reported = usage_counter(
                usage,
                "cache_write_tokens",
                "cache_write_input_tokens",
                "cache_creation_input_tokens",
            )
        prompt_tokens, _ = usage_counter(usage, "input_tokens", "prompt_tokens")
        return CacheUsage(
            hit_tokens=hit,
            miss_tokens=max(prompt_tokens - hit - creation, 0),
            creation_tokens=creation,
            prompt_tokens=prompt_tokens,
            available=hit_reported or creation_reported,
        )


class CacheSlotRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, CacheUsageProvider] = {}

    def register(self, key: str, provider: CacheUsageProvider) -> None:
        self._providers[key] = provider

    def unregister(self, key: str) -> None:
        self._providers.pop(key, None)

    def providers(self) -> dict[str, CacheUsageProvider]:
        return dict(self._providers)

    def resolve(self, cache_usage_format: str, protocol: str) -> CacheUsageProvider:
        if cache_usage_format == "deepseek":
            return self._providers["deepseek"]
        if protocol == "anthropic":
            return self._providers["anthropic"]
        return self._providers["openai"]


def create_cache_slot_registry() -> CacheSlotRegistry:
    registry = CacheSlotRegistry()
    registry.register("deepseek", DeepSeekCacheUsageProvider())
    registry.register("anthropic", AnthropicCacheUsageProvider())
    registry.register("openai", OpenAICacheUsageProvider())
    return registry


# Compatibility fallback for callers outside an ApplicationHost scope.
CACHE_SLOT_REGISTRY = create_cache_slot_registry()


def resolve_cache_usage_provider(
    cache_usage_format: str, protocol: str
) -> CacheUsageProvider:
    return CACHE_SLOT_REGISTRY.resolve(cache_usage_format, protocol)
