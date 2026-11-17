from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


CONTEXT_CHANNELS = {"auto", "deepseek", "generic"}
_DEEPSEEK_MODEL_RE = re.compile(
    r"(?:^|[/:\s])(?P<model>deepseek-[a-z0-9][a-z0-9._-]*)",
    re.IGNORECASE,
)
_MODEL_SUFFIXES = ("-latest",)

# 官方 API 对部分模型使用短 id（api.deepseek.com 上 flash 档即 "deepseek-flash"），
# 归一化时映射到内部规范 id，使能力与定价都按规范行解析。
_MODEL_ALIASES = {
    "deepseek-flash": "deepseek-v4-flash",
    "deepseek-v4.1-flash": "deepseek-v4-flash",
}


@dataclass(frozen=True)
class ModelCapabilities:
    family: str = "generic"
    context_channel: str = "generic"
    context_window: int = 0
    max_output_tokens: int = 0
    supports_thinking: bool = False
    requires_reasoning_content_for_tools: bool = False
    cache_usage_format: str = "generic"
    canonical_model: str = ""
    official_endpoint: bool = False
    cache_hit_usd_per_million: float = 0.0
    cache_miss_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0


def canonical_model_name(model: str) -> str:
    value = model.strip().lower().lstrip("~")
    match = _DEEPSEEK_MODEL_RE.search(value)
    if match:
        value = match.group("model")
    else:
        value = value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    for suffix in _MODEL_SUFFIXES:
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return _MODEL_ALIASES.get(value, value)


def is_official_deepseek_endpoint(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host == "api.deepseek.com" or host.endswith(".api.deepseek.com")


def resolve_model_capabilities(
    model: str,
    base_url: str = "",
    context_channel: str = "auto",
) -> ModelCapabilities:
    if context_channel not in CONTEXT_CHANNELS:
        raise ValueError(f"unsupported context channel: {context_channel}")

    canonical = canonical_model_name(model)
    detected_deepseek = canonical.startswith("deepseek-")
    channel = (
        "deepseek"
        if context_channel == "deepseek"
        or (context_channel == "auto" and detected_deepseek)
        else "generic"
    )
    deepseek_family = detected_deepseek or context_channel == "deepseek"
    if not deepseek_family:
        if canonical.startswith("grok-4"):
            family_match = re.match(r"^(grok-4(?:\.\d+)?)", canonical)
            return ModelCapabilities(
                family=family_match.group(1) if family_match else "grok-4",
                context_channel="generic",
                context_window=500_000,
                supports_thinking=True,
                canonical_model=canonical,
            )
        return ModelCapabilities(canonical_model=canonical)

    is_v4 = canonical.startswith("deepseek-v4-")
    # DeepSeek rates per 1M tokens, also used by the current OpenCode Go plan.
    # Relays may change pricing; callers must present this as a local estimate.
    if canonical.startswith("deepseek-v4-pro"):
        cache_hit_price, cache_miss_price, output_price = 0.003625, 0.435, 0.87
    elif canonical.startswith("deepseek-v4-flash"):
        cache_hit_price, cache_miss_price, output_price = 0.0028, 0.14, 0.28
    else:
        cache_hit_price = cache_miss_price = output_price = 0.0
    return ModelCapabilities(
        family="deepseek",
        context_channel=channel,
        context_window=1_000_000 if is_v4 else 0,
        max_output_tokens=384_000 if is_v4 else 0,
        supports_thinking=True,
        requires_reasoning_content_for_tools=True,
        cache_usage_format="deepseek",
        canonical_model=canonical,
        official_endpoint=is_official_deepseek_endpoint(base_url),
        cache_hit_usd_per_million=cache_hit_price,
        cache_miss_usd_per_million=cache_miss_price,
        output_usd_per_million=output_price,
    )


def estimate_request_cost_usd(
    capabilities: ModelCapabilities,
    *,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate against the official model price; relays may bill differently."""
    return (
        cache_hit_tokens * capabilities.cache_hit_usd_per_million
        + cache_miss_tokens * capabilities.cache_miss_usd_per_million
        + output_tokens * capabilities.output_usd_per_million
    ) / 1_000_000
