"""OpenCode Go model routing and effective provider resolution.

OpenCode Go is one logical provider backed by three wire protocols.  Keep the
logical gateway URL in persisted configuration and resolve the protocol/base
URL only when a concrete model is selected.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Iterable

from .config import ProviderConfig


STANDARD_PROVIDER_TYPE = "standard"
OPENCODE_GO_PROVIDER_TYPE = "opencode-go"
OPENCODE_GO_ROOT = "https://opencode.ai/zen/go"
OPENCODE_GO_DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class OpenCodeGoModelRoute:
    protocol: str
    context_window: int = 0
    max_output_tokens: int = 0
    priority: int = 100


def _route(
    protocol: str,
    *,
    context_window: int = 0,
    max_output_tokens: int = 0,
    priority: int = 100,
) -> OpenCodeGoModelRoute:
    return OpenCodeGoModelRoute(
        protocol=protocol,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        priority=priority,
    )


# This is intentionally an explicit allow-list.  A new ID returned by the Go
# catalog is visible to the user, but is not selectable until its wire protocol
# is known.  Insertion order is the fallback catalog order.
#
# context_window values are the vendors' published context windows (checked
# 2026-09-10 against each vendor's own docs).  Notes on the non-obvious ones:
#   deepseek-v4-*        DeepSeek API docs: 1M context, 384K max output.
#   gpt-5.6-luna         OpenAI model page: all three GPT-5.6 tiers share a
#                        1,050,000 window and 128K output.  The 272K figure is
#                        the long-prompt pricing threshold (>272K input is
#                        billed at 2x), not the window.
#   glm-5.3 / glm-5.2    Zhipu: 1M (Volcengine writes it as "1024k" with 896K
#                        max input + 128K max output).
#   glm-5.1 / glm-5      Zhipu: 200K.
#   kimi-k3              1M; kimi-k2.x line is 256K (k2.5 was retired
#                        2026-08-31 but stays routable for pinned sessions).
#   qwen3.[5-8]-*        Alibaba Cloud Model Studio: 1M on the plus/max tiers.
#                        983,616 max input / 131,072 max output on 3.8-max.
#   mimo-v2.5(-pro)      1M; mimo-v2-omni and mimo-v2-pro are the older
#                        256K / 1M entries.
#   hy3 / hy3-preview    Tencent Hunyuan: 256K.
#   ox-alpha-free        OpenRouter stealth listing: 1,048,576 context with
#                        131,072 max output.
#   muse-spark-1.2-*     1,048,576 context, 131,072 max output.
#   grok-4.5             xAI model page: 500K.
#   minimax-m3           1,000,000; minimax-m2.7 / m2.5 are 204,800.
OPENCODE_GO_MODEL_ROUTES: dict[str, OpenCodeGoModelRoute] = {
    "deepseek-v4-flash": _route(
        "openai-compat", context_window=1_000_000, max_output_tokens=384_000, priority=0
    ),
    "deepseek-v4-pro": _route(
        "openai-compat", context_window=1_000_000, max_output_tokens=384_000, priority=1
    ),
    "deepseek-v4-flash-vision-exp": _route(
        "openai-compat", context_window=1_000_000, max_output_tokens=384_000, priority=2
    ),
    # https://opencode.ai/docs/go/ and DeepSeek's model details (2026-10-14).
    "deepseek-v4.1-flash": _route(
        "openai-compat", context_window=1_000_000, max_output_tokens=384_000, priority=3
    ),
    "gpt-5.6-luna": _route("openai", context_window=1_050_000, priority=10),
    "muse-spark-1.2-contributor": _route("openai", context_window=1_048_576, priority=11),
    "kimi-k3": _route("openai-compat", context_window=1_000_000, priority=20),
    "kimi-k2.7-code": _route("openai-compat", context_window=256_000, priority=21),
    "kimi-k2.6": _route("openai-compat", context_window=256_000, priority=22),
    "kimi-k2.5": _route("openai-compat", context_window=256_000, priority=23),
    "glm-5.3": _route("openai-compat", context_window=1_000_000, priority=29),
    "glm-5.2": _route("openai-compat", context_window=1_000_000, priority=30),
    "glm-5.1": _route("openai-compat", context_window=200_000, priority=31),
    "glm-5": _route("openai-compat", context_window=200_000, priority=32),
    "mimo-v2.5-pro": _route("openai-compat", context_window=1_000_000, priority=40),
    "mimo-v2.5": _route("openai-compat", context_window=1_000_000, priority=41),
    "mimo-v2-pro": _route("openai-compat", context_window=1_000_000, priority=42),
    "mimo-v2-omni": _route("openai-compat", context_window=256_000, priority=43),
    "hy3": _route("openai-compat", context_window=256_000, priority=50),
    "hy3-preview": _route("openai-compat", context_window=256_000, priority=51),
    "ox-alpha-free": _route("openai-compat", context_window=1_048_576, priority=52),
    "grok-4.5": _route("openai", context_window=500_000, priority=60),
    "minimax-m3": _route("anthropic", context_window=1_000_000, priority=70),
    "minimax-m2.7": _route("anthropic", context_window=204_800, priority=71),
    "minimax-m2.5": _route("anthropic", context_window=204_800, priority=72),
    "qwen3.8-max": _route("anthropic", context_window=1_000_000, priority=80),
    "qwen3.7-max": _route("anthropic", context_window=1_000_000, priority=81),
    "qwen3.7-plus": _route("anthropic", context_window=1_000_000, priority=82),
    "qwen3.6-plus": _route("anthropic", context_window=1_000_000, priority=83),
    "qwen3.5-plus": _route("anthropic", context_window=1_000_000, priority=84),
}


def is_opencode_go_provider(provider: ProviderConfig) -> bool:
    return provider.provider_type == OPENCODE_GO_PROVIDER_TYPE


def opencode_go_root(base_url: str) -> str:
    """Normalize a persisted logical URL or a previously resolved SDK URL."""

    value = (base_url or OPENCODE_GO_ROOT).strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/v1/responses", "/v1/messages", "/v1"):
        if value.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
            break
    return value or OPENCODE_GO_ROOT


def opencode_go_route(model: str) -> OpenCodeGoModelRoute | None:
    return OPENCODE_GO_MODEL_ROUTES.get((model or "").strip())


def resolve_effective_provider_config(
    provider: ProviderConfig,
    model: str | None = None,
) -> ProviderConfig:
    """Return the concrete protocol configuration for one model selection."""

    config = copy.deepcopy(provider)
    if model is not None:
        config.model = str(model).strip()
        config._fetched_context_window = 0
    if not is_opencode_go_provider(config):
        from mewcode.model_catalog import DEFAULT_MODEL_CATALOG
        descriptor = DEFAULT_MODEL_CATALOG.describe(config.model)
        if config.protocol == "openai-compat" and descriptor is not None and descriptor.family in {"gpt-6-astra", "gpt-6.1-sol"}:
            # Tool schemas, cache observations and the wire client must agree.
            # GPT-6 Astra and GPT-6.1 Sol tool use require Responses.
            config.protocol = "openai"
        return config

    route = opencode_go_route(config.model)
    if route is None:
        raise ValueError(
            f"OpenCode Go model '{config.model}' is unavailable: missing routing metadata"
        )
    root = opencode_go_root(config.base_url)
    config.protocol = route.protocol
    config.base_url = f"{root}/v1" if route.protocol in {"openai", "openai-compat"} else root
    config._route_context_window = route.context_window
    if config.model.lower().startswith("deepseek-"):
        config.context_channel = "deepseek"
    elif config.context_channel == "deepseek":
        config.context_channel = "auto"
    return config


def ordered_go_model_ids(remote_ids: Iterable[str] | None = None) -> list[str]:
    """Put DeepSeek first while preserving remote order for all other IDs."""

    source = (
        list(OPENCODE_GO_MODEL_ROUTES)
        if remote_ids is None
        else list(dict.fromkeys(str(item).strip() for item in remote_ids if str(item).strip()))
    )
    indexed = list(enumerate(source))
    indexed.sort(
        key=lambda item: (
            0 if item[1].startswith("deepseek-") else 1,
            OPENCODE_GO_MODEL_ROUTES.get(item[1], _route("", priority=10_000)).priority
            if item[1].startswith("deepseek-")
            else item[0],
        )
    )
    return [model for _, model in indexed]
