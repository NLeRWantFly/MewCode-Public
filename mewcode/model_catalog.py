"""Data-driven model capability catalog.

The provider model-list endpoint remains the authority for availability.  This
module intentionally contains only stable model metadata: effort levels,
context/output limits, capabilities, and the source used to verify them.

The catalog is deliberately independent from :mod:`mewcode.client` so it can
be used by clients, provider registries, and plugins without creating an
import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from mewcode.cordis import PluginSpec


MODEL_CATALOG_SERVICE = "model_catalog"
MODEL_METADATA_SLOT = "core.models.metadata"
MODEL_CATALOG_PLUGIN_NAME = "model_catalog"
VERIFIED_AT = "2026-09-17"


@dataclass(frozen=True, slots=True)
class ModelDescriptor:
    """Immutable description of one model family or model id.

    ``model_id`` is the canonical public id.  A descriptor also matches ids
    with the canonical id as a prefix (for example ``-latest`` or a dated
    snapshot), and ``aliases``/``match_prefixes`` provide explicit provider
    spellings without duplicating public catalog rows.
    """

    model_id: str
    family: str = "generic"
    provider_brand: str = ""
    efforts: tuple[str, ...] = ()
    default_effort: str = ""
    context_window: int = 0
    max_output_tokens: int = 0
    vision: bool = False
    source_url: str = ""
    source_label: str = ""
    verified_at: str = VERIFIED_AT
    aliases: tuple[str, ...] = ()
    match_prefixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        model_id = str(self.model_id).strip()
        if not model_id:
            raise ValueError("model_id is required")
        efforts = _clean_strings(self.efforts)
        aliases = _clean_strings(self.aliases)
        prefixes = _clean_strings(self.match_prefixes)
        default = str(self.default_effort or "").strip()
        if default and efforts and default not in efforts:
            raise ValueError("default_effort must be one of efforts")
        if not default and efforts:
            default = efforts[-1]
        context_window = int(self.context_window or 0)
        max_output_tokens = int(self.max_output_tokens or 0)
        if context_window < 0 or max_output_tokens < 0:
            raise ValueError("context_window and max_output_tokens cannot be negative")
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "family", str(self.family or "generic").strip())
        object.__setattr__(self, "provider_brand", str(self.provider_brand or "").strip())
        object.__setattr__(self, "efforts", efforts)
        object.__setattr__(self, "default_effort", default)
        object.__setattr__(self, "context_window", context_window)
        object.__setattr__(self, "max_output_tokens", max_output_tokens)
        object.__setattr__(self, "vision", bool(self.vision))
        object.__setattr__(self, "source_url", str(self.source_url or "").strip())
        object.__setattr__(self, "source_label", str(self.source_label or "").strip())
        object.__setattr__(self, "verified_at", str(self.verified_at or "").strip())
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "match_prefixes", prefixes)

    @property
    def supports_vision(self) -> bool:
        """Compatibility spelling used by a few client integrations."""

        return self.vision

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Stable capability names suitable for a model picker."""

        return ("chat", "tools", *(("vision",) if self.vision else ()))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable public descriptor."""

        return {
            "model_id": self.model_id,
            "family": self.family,
            "provider_brand": self.provider_brand,
            "efforts": list(self.efforts),
            "default_effort": self.default_effort,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "vision": self.vision,
            "capabilities": list(self.capabilities),
            "source_url": self.source_url,
            "source_label": self.source_label,
            "verified_at": self.verified_at,
        }

    to_dict = as_dict

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ModelDescriptor":
        """Build a descriptor from plugin-friendly mapping data."""

        payload = dict(value)
        if "supports_vision" in payload and "vision" not in payload:
            payload["vision"] = payload.pop("supports_vision")
        if "model" in payload and "model_id" not in payload:
            payload["model_id"] = payload.pop("model")
        capabilities = payload.pop("capabilities", None)
        if capabilities is not None and "vision" not in payload:
            payload["vision"] = "vision" in _clean_strings(capabilities)
        source = payload.pop("source", None)
        if isinstance(source, Mapping):
            payload.setdefault("source_url", source.get("url", source.get("source_url", "")))
            payload.setdefault("source_label", source.get("label", source.get("source_label", "")))
            payload.setdefault("verified_at", source.get("verified_at", VERIFIED_AT))
        return cls(**payload)


def _clean_strings(values: Iterable[Any] | Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    try:
        iterator = iter(values)
    except TypeError:
        iterator = iter((values,))
    return tuple(dict.fromkeys(str(item).strip() for item in iterator if str(item).strip()))


def _model_variants(model: str) -> tuple[str, ...]:
    """Return ids used by official APIs and common relay spellings."""

    value = str(model or "").strip().lower()
    if not value:
        return ()
    value = value.lstrip("~")
    variants: list[str] = [value]
    seen: set[str] = set()
    # OpenRouter/relay model names commonly carry an org prefix and/or a
    # routing suffix.  Keep the complete form first for exact custom entries,
    # then repeatedly peel those decorations so ``openrouter/foo:free`` also
    # resolves to the ``foo`` descriptor.
    index = 0
    while index < len(variants):
        candidate = variants[index]
        index += 1
        if candidate in seen:
            continue
        seen.add(candidate)
        if "/" in candidate:
            variants.append(candidate.rsplit("/", 1)[-1])
        if ":" in candidate:
            variants.append(candidate.split(":", 1)[0])
        if candidate.endswith("-latest"):
            variants.append(candidate[: -len("-latest")])
    return tuple(dict.fromkeys(variants))


def _descriptor_keys(descriptor: ModelDescriptor) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            _normalise_key(key)
            for key in (
                descriptor.model_id,
                *descriptor.aliases,
                *descriptor.match_prefixes,
            )
            if str(key).strip()
        )
    )


def _normalise_key(value: str) -> str:
    return str(value).strip().lower().lstrip("~")


def _prefix_matches(model: str, key: str) -> bool:
    if model == key:
        return True
    if not model.startswith(key):
        return False
    # Avoid treating an unrelated model such as ``gpt-5.60`` as a snapshot of
    # ``gpt-5.6`` while allowing the normal ``-latest``/dated suffix forms.
    return len(model) > len(key) and model[len(key)] in "-_.:/"


def _coerce_descriptor(value: Any) -> ModelDescriptor:
    if isinstance(value, ModelDescriptor):
        return value
    if isinstance(value, Mapping):
        return ModelDescriptor.from_mapping(value)
    raise TypeError("model catalog contributions must contain ModelDescriptor values")


def _is_descriptor_mapping(value: Mapping[str, Any]) -> bool:
    return "model_id" in value or "model" in value


def _coerce_many(value: Any) -> tuple[ModelDescriptor, ...]:
    if isinstance(value, ModelDescriptor):
        return (value,)
    if isinstance(value, Mapping):
        if _is_descriptor_mapping(value):
            return (_coerce_descriptor(value),)
        return tuple(_coerce_descriptor(item) for item in value.values())
    if isinstance(value, (str, bytes)):
        raise TypeError("model catalog contributions must contain descriptors")
    return tuple(_coerce_descriptor(item) for item in value)


class ModelCatalogService:
    """Lookup service for built-in model metadata and plugin contributions."""

    def __init__(self, descriptors: Iterable[ModelDescriptor | Mapping[str, Any]] | None = None) -> None:
        builtins = tuple(
            _coerce_descriptor(item)
            for item in (BUILTIN_MODEL_DESCRIPTORS if descriptors is None else descriptors)
        )
        self._builtins = builtins
        self._contributions: dict[str, tuple[ModelDescriptor, ...]] = {}
        self._contribution_order: list[str] = []

    @property
    def builtins(self) -> tuple[ModelDescriptor, ...]:
        return self._builtins

    def register_contribution(
        self,
        contribution: ModelDescriptor | Mapping[str, Any] | Iterable[ModelDescriptor | Mapping[str, Any]],
        *,
        contribution_id: str | None = None,
        replace: bool = False,
    ) -> str:
        """Register a contribution and return its unload token.

        A contribution is grouped under one token so a plugin can atomically
        unload several descriptors.  ``register`` is provided as a concise
        alias for integrations that use registry terminology.
        """

        values = _coerce_many(contribution)
        if not values:
            raise ValueError("model catalog contribution cannot be empty")
        token = str(contribution_id or uuid4().hex)
        if token in self._contributions and not replace:
            raise ValueError(f"model catalog contribution {token!r} already exists")
        if token not in self._contributions:
            self._contribution_order.append(token)
        self._contributions[token] = values
        return token

    register = register_contribution
    register_descriptors = register_contribution

    def unregister_contribution(self, contribution_id: str | ModelDescriptor) -> bool:
        token = str(contribution_id)
        if token not in self._contributions and isinstance(contribution_id, ModelDescriptor):
            matches = [
                key
                for key, descriptors in self._contributions.items()
                if contribution_id in descriptors
            ]
            if not matches:
                return False
            token = matches[-1]
        if token not in self._contributions:
            # Accepting a model id here makes plugin teardown and small
            # integrations ergonomic without making built-ins mutable.
            matches = [
                key
                for key, descriptors in self._contributions.items()
                if any(item.model_id == token for item in descriptors)
            ]
            if not matches:
                return False
            token = matches[-1]
        self._contributions.pop(token, None)
        self._contribution_order = [item for item in self._contribution_order if item != token]
        return True

    unregister = unregister_contribution
    unregister_descriptors = unregister_contribution

    def _iter_descriptors(self) -> Iterator[ModelDescriptor]:
        yield from self._builtins
        # Later contributions win ties, allowing a provider plugin to correct
        # or extend a built-in family without mutating the immutable built-ins.
        for token in self._contribution_order:
            yield from self._contributions.get(token, ())

    def describe(
        self,
        model: str,
        *,
        provider_brand: str | None = None,
    ) -> ModelDescriptor | None:
        """Resolve an exact or longest-prefix descriptor for ``model``."""

        candidates = _model_variants(model)
        if not candidates:
            return None
        brand = _normalise_key(provider_brand or "")
        best: tuple[int, int, ModelDescriptor] | None = None
        for order, descriptor in enumerate(self._iter_descriptors()):
            descriptor_brand = _normalise_key(descriptor.provider_brand)
            # An explicit brand is a hint, not an authority: model ids from
            # relays (e.g. OpenRouter) often carry a different provider brand.
            brand_bonus = 1 if brand and descriptor_brand == brand else 0
            for candidate in candidates:
                for key in _descriptor_keys(descriptor):
                    if not _prefix_matches(candidate, key):
                        continue
                    score = (100_000 if candidate == key else 0) + len(key) * 10
                    score += brand_bonus
                    ranking = (score, order, descriptor)
                    if best is None or ranking[:2] >= best[:2]:
                        best = ranking
        return best[2] if best is not None else None

    match = describe
    lookup = describe

    def effort_levels(
        self,
        model: str,
        *,
        provider_brand: str | None = None,
    ) -> list[str]:
        descriptor = self.describe(model, provider_brand=provider_brand)
        return list(descriptor.efforts) if descriptor is not None else []

    def catalog(self) -> list[dict[str, Any]]:
        """Return the complete public, JSON-serializable model snapshot."""

        return [descriptor.as_dict() for descriptor in self._iter_descriptors()]

    public_catalog = catalog

    def snapshot(self) -> dict[str, Any]:
        """Return an API-style envelope while retaining ``catalog()`` as a list."""

        return {"models": self.catalog(), "verified_at": VERIFIED_AT}


# Official source references.  These are URLs, not pricing data; providers
# still decide whether a configured credential can actually use a model.
_OPENAI_URL = "https://developers.openai.com/api/docs/models"
_OPENAI_LABEL = "OpenAI models overview"
_ANTHROPIC_EFFORT_URL = "https://platform.claude.com/docs/en/build-with-claude/effort"
_ANTHROPIC_CONTEXT_URL = "https://platform.claude.com/docs/en/build-with-claude/context-windows"
_ANTHROPIC_LABEL = "Anthropic Claude effort and context documentation"
_GEMINI_THINKING_URL = "https://ai.google.dev/gemini-api/docs/thinking"
_GEMINI_MODEL_URL = "https://ai.google.dev/gemini-api/docs/models"
_GEMINI_LABEL = "Google Gemini model documentation"
_GEMINI_38_URL = (
    "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash"
)
_DEEPSEEK_URL = "https://api-docs.deepseek.com/quick_start/pricing"
_DEEPSEEK_LABEL = "DeepSeek models and pricing"
_XAI_GROK_46_URL = "https://docs.x.ai/developers/models/grok-4.6"
_XAI_GROK_45_URL = "https://docs.x.ai/developers/models/grok-4.5"
_XAI_GROK_LABEL = "xAI Grok model documentation"
_KIMI_URL = "https://platform.kimi.com/docs/api/models-overview"
_KIMI_LABEL = "Kimi models overview"
_GLM_URL = "https://docs.bigmodel.cn/cn/guide/start/model-overview"
_GLM_LABEL = "Zhipu GLM model overview"
_QWEN_URL = "https://help.aliyun.com/zh/model-studio/qwen3-8-max"
_QWEN_LABEL = "Alibaba Cloud Model Studio Qwen documentation"
_MINIMAX_URL = "https://platform.minimax.io/docs/guides/models-intro"
_MINIMAX_LABEL = "MiniMax models introduction"
_MIMO_URL = "https://mimo.mi.com/docs/zh-CN/quick-start/summary/model"
_MIMO_LABEL = "Xiaomi MiMo model summary"
_HY3_URL = "https://cloud.tencent.cn/document/product/1823/132252"
_HY3_LABEL = "Tencent Hunyuan TokenHub model guide"


def _make(
    model_id: str,
    *,
    family: str,
    brand: str,
    efforts: tuple[str, ...],
    default: str,
    context: int,
    output: int,
    vision: bool,
    source_url: str,
    source_label: str,
    aliases: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        family=family,
        provider_brand=brand,
        efforts=efforts,
        default_effort=default,
        context_window=context,
        max_output_tokens=output,
        vision=vision,
        source_url=source_url,
        source_label=source_label,
        verified_at=VERIFIED_AT,
        aliases=aliases,
        match_prefixes=prefixes,
    )


_GPT_56_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_CLAUDE_MODERN_EFFORTS = ("low", "medium", "high", "max")
_CLAUDE_XHIGH_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CLAUDE_OPUS_46_EFFORTS = ("low", "medium", "high", "max")
_CLAUDE_OLD_EFFORTS = ("off", "low", "medium", "high")
_GEMINI_3_EFFORTS = ("minimal", "low", "medium", "high")
_GEMINI_25_EFFORTS = ("low", "medium", "high")
_GROK_46_EFFORTS = ("low", "medium", "high", "xhigh")


def _openai_descriptors() -> tuple[ModelDescriptor, ...]:
    gpt_6 = tuple(
        ModelDescriptor(
            model_id=model, family=model, provider_brand="openai",
            efforts=efforts, default_effort="medium",
            context_window=1_050_000, max_output_tokens=128_000, vision=True,
            source_url=f"https://developers.openai.com/api/docs/models/{model}",
            source_label=f"OpenAI {model} documentation",
            verified_at="2026-09-30" if model == "gpt-6.1-sol" else "2026-09-23", match_prefixes=(f"{model}-",),
        )
        for model, efforts in (
            ("gpt-6.1-sol", ("low", "medium", "high", "xhigh", "max")),
            ("gpt-6-astra", ("low", "medium", "high", "xhigh", "max")),
            ("gpt-6-sol", _GPT_56_EFFORTS),
            ("gpt-6-luna", _GPT_56_EFFORTS),
        )
    )
    return gpt_6 + tuple(
        _make(
            model_id=model,
            family="gpt-5.6",
            brand="openai",
            efforts=_GPT_56_EFFORTS,
            default="high",
            context=1_050_000,
            output=128_000,
            vision=True,
            source_url=_OPENAI_URL,
            source_label=_OPENAI_LABEL,
            aliases=(model.replace(".", "-") if "." in model else "",),
            prefixes=(f"{model}-",),
        )
        for model in ("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
    )


def _claude_descriptors() -> tuple[ModelDescriptor, ...]:
    rows: list[ModelDescriptor] = []
    # Keep the broad legacy entries so dated Claude 3/4 snapshots retain the
    # existing off/low/medium/high UI, while more specific modern rows below
    # override them through longest-prefix matching.
    for model, family in (
        ("claude", "claude"),
        ("claude-sonnet-4", "claude-sonnet-4"),
        ("claude-opus-4", "claude-opus-4"),
        ("claude-haiku-4", "claude-haiku-4"),
        ("claude-3", "claude-3"),
    ):
        rows.append(
            _make(
                model_id=model,
                family=family,
                brand="anthropic",
                efforts=_CLAUDE_OLD_EFFORTS,
                default="high",
                context=200_000,
                output=64_000,
                vision=True,
                source_url=_ANTHROPIC_CONTEXT_URL,
                source_label=_ANTHROPIC_LABEL,
            )
        )
    modern = (
        ("claude-sonnet-4-6", "claude-sonnet-4.6", _CLAUDE_MODERN_EFFORTS),
        ("claude-sonnet-5", "claude-sonnet-5", _CLAUDE_XHIGH_EFFORTS),
        ("claude-opus-4-6", "claude-opus-4.6", _CLAUDE_OPUS_46_EFFORTS),
        ("claude-opus-4-7", "claude-opus-4.7", _CLAUDE_XHIGH_EFFORTS),
        ("claude-opus-4-8", "claude-opus-4.8", _CLAUDE_XHIGH_EFFORTS),
        ("claude-opus-5", "claude-opus-5", _CLAUDE_XHIGH_EFFORTS),
        ("claude-fable-5", "claude-fable-5", _CLAUDE_XHIGH_EFFORTS),
        ("claude-mythos-5", "claude-mythos-5", _CLAUDE_XHIGH_EFFORTS),
    )
    for model, dotted, efforts in modern:
        rows.append(
            _make(
                model_id=model,
                family=model,
                brand="anthropic",
                efforts=efforts,
                default="high",
                context=1_000_000,
                output=128_000,
                vision=True,
                source_url=_ANTHROPIC_EFFORT_URL,
                source_label=_ANTHROPIC_LABEL,
                aliases=(dotted,),
                prefixes=(f"{model}-",),
            )
        )
    return tuple(rows)


# Relay catalogs sometimes expose thinking level as a model-id suffix
# (gemini-3.8-flash-high).  Official Gemini uses the base id + thinking_level.
_GEMINI_BAKED_EFFORT_SUFFIXES = ("xhigh", "minimal", "medium", "high", "low")


def split_gemini_baked_effort_sku(model_id: str) -> tuple[str, str] | None:
    """Split ``gemini-*-{effort}`` into ``(base_model_id, effort)`` when applicable."""

    raw = str(model_id or "").strip()
    if not raw:
        return None
    if "/" in raw:
        org, bare = raw.rsplit("/", 1)
        prefix = f"{org}/"
    else:
        bare = raw
        prefix = ""
    lower = bare.casefold()
    if not lower.startswith("gemini-"):
        return None
    for effort in _GEMINI_BAKED_EFFORT_SUFFIXES:
        token = f"-{effort}"
        if not lower.endswith(token):
            continue
        base_bare = bare[: -len(token)]
        base_lower = base_bare.casefold()
        if not (
            base_lower.endswith("-flash")
            or base_lower.endswith("-pro")
            or base_lower.endswith("-flash-lite")
        ):
            continue
        return prefix + base_bare, effort
    return None


def canonicalize_selectable_model_id(model_id: str) -> str:
    """Return the picker-facing model id (Gemini baked effort SKUs → base id)."""

    split = split_gemini_baked_effort_sku(model_id)
    if split is not None:
        return split[0]
    return str(model_id or "").strip()


def is_official_gemini_endpoint(base_url: str) -> bool:
    """True for Google Gemini / Vertex hosts that expect a base model id."""

    host = urlsplit(str(base_url or "").strip()).hostname or ""
    host = host.casefold()
    return host in {
        "generativelanguage.googleapis.com",
        "aiplatform.googleapis.com",
    } or host.endswith((
        ".generativelanguage.googleapis.com",
        ".aiplatform.googleapis.com",
    ))


def gemini_advertised_efforts(model_id: str, advertised: Iterable[str] | None) -> list[str] | None:
    """Return the offered suffix levels when this route has no base model SKU."""
    model_id = str(model_id or '').strip()
    ids = {str(item).strip() for item in advertised or [] if str(item).strip()}
    if model_id in ids:
        return None  # The base SKU can receive a separate effort parameter.
    offered = {split[1] for item in ids if (split := split_gemini_baked_effort_sku(item))
               and split[0] == model_id}
    return [level for level in ('minimal', 'low', 'medium', 'high', 'xhigh') if level in offered] if offered else None


def select_gemini_wire_model_id(
    model_id: str,
    effort: str = "",
    advertised: Iterable[str] | None = None,
    *,
    official_gemini_endpoint: bool = False,
) -> str:
    """Choose the HTTP ``model`` id for a selectable Gemini id + effort.

    Pickers keep the base id. Only an advertised suffix SKU may replace it.
    A base-model route keeps thinking as a separate parameter. A route that
    offers suffix SKUs alone must explicitly support the requested level.
    """

    model_id = str(model_id or "").strip()
    effort = str(effort or "").strip()
    advertised_ids = [
        str(item).strip()
        for item in (advertised or [])
        if str(item).strip()
    ]
    advertised_set = set(advertised_ids)
    # Custom relays sometimes advertise exactly one baked effort SKU for a
    # Gemini base while the picker sends the bare id with no effort. Send that
    # single advertised SKU instead of a bare id the relay cannot serve. This
    # leaves official Google and multi-effort relays unchanged.
    if (
        not official_gemini_endpoint
        and not effort
        and model_id
        and "/" not in model_id
        and split_gemini_baked_effort_sku(model_id) is None
    ):
        only_sku = [a for a in advertised_ids if a.startswith(model_id + "-")]
        if (
            len(only_sku) == 1
            and split_gemini_baked_effort_sku(only_sku[0]) is not None
        ):
            return only_sku[0]
    if not model_id or official_gemini_endpoint:
        return model_id
    offered = gemini_advertised_efforts(model_id, advertised_ids)
    if offered is not None and effort not in offered:
        raise ValueError(f'当前 Provider 未提供 {model_id} 的 {effort or "未指定"} 推理档位；可用：{", ".join(offered)}')
    if not effort:
        return model_id
    baked = f"{model_id}-{effort}"
    if split_gemini_baked_effort_sku(baked) != (model_id, effort):
        return model_id
    if baked in advertised_set:
        return baked
    return model_id


def _gemini_descriptors() -> tuple[ModelDescriptor, ...]:
    rows: list[ModelDescriptor] = []
    for model, efforts, default in (
        ("gemini-3.8-flash", ("low", "medium", "high"), "medium"),
        ("gemini-3.7-flash", ("low", "medium", "high"), "medium"),
        ("gemini-3.6-flash", _GEMINI_3_EFFORTS, "medium"),
        ("gemini-3.5-flash", _GEMINI_3_EFFORTS, "medium"),
        ("gemini-3.1-pro", ("low", "medium", "high"), "high"),
        ("gemini-3.1-flash-lite", ("minimal", "high"), "minimal"),
    ):
        family = model
        rows.append(
            _make(
                model_id=model,
                family=family,
                brand="google",
                efforts=efforts,
                default=default,
                context=1_048_576,
                output=65_536,
                vision=True,
                source_url=_GEMINI_38_URL if model == "gemini-3.8-flash" else _GEMINI_THINKING_URL,
                source_label=_GEMINI_LABEL,
                prefixes=(f"{model}-",),
            )
        )
    rows.append(
        _make(
            model_id="gemini-3-flash",
            family="gemini-3-flash",
            brand="google",
            efforts=_GEMINI_3_EFFORTS,
            default="medium",
            context=1_048_576,
            output=65_536,
            vision=True,
            source_url="https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-flash",
            source_label=_GEMINI_LABEL,
            aliases=("gemini-3-flash-preview",),
            prefixes=("gemini-3-flash-",),
        )
    )
    rows.append(
        _make(
            model_id="gemini-3.1-flash-image",
            family="gemini-3.1-flash-image",
            brand="google",
            efforts=_GEMINI_25_EFFORTS,
            default="medium",
            context=131_072,
            output=32_768,
            vision=True,
            source_url="https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-image",
            source_label=_GEMINI_LABEL,
            prefixes=("gemini-3.1-flash-image-",),
        )
    )
    for model in (
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ):
        family = model
        rows.append(
            _make(
                model_id=model,
                family=family,
                brand="google",
                efforts=_GEMINI_25_EFFORTS,
                default="medium",
                context=1_048_576,
                output=65_536,
                vision=True,
                source_url=_GEMINI_MODEL_URL,
                source_label=_GEMINI_LABEL,
                prefixes=(f"{model}-",),
            )
        )
    return tuple(rows)


def _deepseek_descriptors() -> tuple[ModelDescriptor, ...]:
    common = {
        "brand": "deepseek",
        "efforts": ("off", "low", "high", "max"),
        "default": "high",
        "context": 1_000_000,
        "output": 384_000,
        "source_url": _DEEPSEEK_URL,
        "source_label": _DEEPSEEK_LABEL,
    }
    return (
        # Official API id is deepseek-flash (V4.1-Flash, native vision).
        # Legacy deepseek-v4-flash / vision-exp currently route to the same model.
        _make(
            model_id="deepseek-v4-flash",
            family="deepseek-v4-flash",
            vision=True,
            aliases=("deepseek-flash", "deepseek-v4.1-flash"),
            prefixes=("deepseek-v4-flash-", "deepseek-flash-"),
            **common,
        ),
        # Keep the retired experiment as its own row so pickers that still
        # advertise the id resolve to the same window and vision flag.
        _make(
            model_id="deepseek-v4-flash-vision-exp",
            family="deepseek-v4-flash-vision-exp",
            vision=True,
            **common,
        ),
        _make(
            model_id="deepseek-v4-pro",
            family="deepseek-v4-pro",
            vision=False,
            prefixes=("deepseek-v4-pro-",),
            **common,
        ),
    )


def _xai_descriptors() -> tuple[ModelDescriptor, ...]:
    grok_common = {
        "brand": "xai",
        "efforts": _GROK_46_EFFORTS,
        "default": "high",
        "context": 500_000,
        # xAI documents the context limit but does not publish a separate
        # maximum-output value.  Keep that field unknown instead of presenting
        # the generic client fallback as a model cap.
        "output": 0,
        "vision": True,
        "source_label": _XAI_GROK_LABEL,
    }
    return (
        _make(
            model_id="grok-4.6",
            family="grok-4.6",
            source_url=_XAI_GROK_46_URL,
            aliases=("grok-4.6-latest",),
            prefixes=("grok-4.6-",),
            **grok_common,
        ),
        _make(
            model_id="grok-4.5",
            family="grok-4.5",
            source_url=_XAI_GROK_45_URL,
            aliases=("grok-4.5-latest", "grok-build-latest"),
            prefixes=("grok-4.5-",),
            **grok_common,
        ),
    )


# Relay model families commonly served by OpenAI-compatible relays such as
# desktop:example_provider_4.  Effort pickers still use a shared low/medium/high set; context
# and vision come from each vendor's published docs (verified 2026-09-17).

_RELAY_EFFORTS = ("low", "medium", "high")


def _kimi_descriptors() -> tuple[ModelDescriptor, ...]:
    common = {
        "brand": "kimi",
        "efforts": _RELAY_EFFORTS,
        "default": "medium",
        "output": 131_072,
        "source_url": _KIMI_URL,
        "source_label": _KIMI_LABEL,
        "vision": True,
    }
    return (
        # Local/desktop:example_provider_4 SKU: the gateway advertises 300K, not Kimi's 1M.
        _make(
            model_id="kimi-k3-1",
            family="kimi-k3-1",
            context=300_000,
            prefixes=("kimi-k3-1-",),
            **common,
        ),
        _make(
            model_id="kimi-k3",
            family="kimi-k3",
            context=1_000_000,
            prefixes=("kimi-k3-",),
            **common,
        ),
        _make(
            model_id="kimi-k2.7-code",
            family="kimi-k2.7-code",
            context=256_000,
            aliases=("kimi-k2.7",),
            prefixes=("kimi-k2.7-code-", "kimi-k2.7-"),
            **common,
        ),
        _make(
            model_id="kimi-k2.6",
            family="kimi-k2.6",
            context=256_000,
            prefixes=("kimi-k2.6-",),
            **common,
        ),
        _make(
            model_id="kimi-k2.5",
            family="kimi-k2.5",
            context=256_000,
            prefixes=("kimi-k2.5-",),
            **common,
        ),
    )


def _glm_descriptors() -> tuple[ModelDescriptor, ...]:
    common = {
        "brand": "zhipu",
        "efforts": _RELAY_EFFORTS,
        "default": "medium",
        "output": 131_072,
        "source_url": _GLM_URL,
        "source_label": _GLM_LABEL,
    }
    rows: list[tuple[str, int, bool, tuple[str, ...]]] = [
        ("glm-5.3-flash", 1_000_000, True, ("glm-5.3-flash-",)),
        ("glm-5v-turbo", 200_000, True, ("glm-5v-turbo-",)),
        ("glm-5.3", 1_000_000, False, ("glm-5.3-",)),
        ("glm-5.2", 1_000_000, False, ("glm-5.2-",)),
        ("glm-5.1", 200_000, False, ("glm-5.1-",)),
        ("glm-5", 200_000, False, ("glm-5-",)),
        ("glm-4.6v", 128_000, True, ("glm-4.6v-",)),
    ]
    return tuple(
        _make(
            model_id=model,
            family=model,
            context=context,
            vision=vision,
            prefixes=prefixes,
            **common,
        )
        for model, context, vision, prefixes in rows
    )


def _mimo_descriptors() -> tuple[ModelDescriptor, ...]:
    common = {
        "brand": "mimo",
        "efforts": _RELAY_EFFORTS,
        "default": "medium",
        "output": 128_000,
        "source_url": _MIMO_URL,
        "source_label": _MIMO_LABEL,
    }
    rows: list[tuple[str, int, bool]] = [
        ("mimo-v2.5-pro", 1_000_000, False),
        ("mimo-v2.5", 1_000_000, True),
        ("mimo-v2-pro", 1_000_000, False),
        ("mimo-v2-omni", 256_000, True),
    ]
    return tuple(
        _make(
            model_id=model,
            family=model,
            context=context,
            vision=vision,
            prefixes=(f"{model}-",),
            **common,
        )
        for model, context, vision in rows
    )


def _hy3_descriptors() -> tuple[ModelDescriptor, ...]:
    common = {
        "brand": "hy3",
        "efforts": _RELAY_EFFORTS,
        "default": "medium",
        "context": 256_000,
        "output": 128_000,
        "source_url": _HY3_URL,
        "source_label": _HY3_LABEL,
        "vision": False,
    }
    return tuple(
        _make(
            model_id=model,
            family=model,
            prefixes=(f"{model}-",),
            **common,
        )
        for model in ("hy3", "hy3-preview")
    )


def _qwen3_descriptors() -> tuple[ModelDescriptor, ...]:
    common = {
        "brand": "alibaba",
        "efforts": _RELAY_EFFORTS,
        "default": "medium",
        "context": 1_000_000,
        "output": 131_072,
        "source_url": _QWEN_URL,
        "source_label": _QWEN_LABEL,
        "vision": True,
    }
    return tuple(
        _make(
            model_id=model,
            family=model,
            prefixes=(f"{model}-",),
            **common,
        )
        for model in (
            "qwen3.8-max",
            "qwen3.8-flash",
            "qwen3.7-max",
            "qwen3.7-plus",
            "qwen3.6-plus",
            "qwen3.5-plus",
        )
    )


def _minimax_descriptors() -> tuple[ModelDescriptor, ...]:
    common = {
        "brand": "minimax",
        "efforts": _RELAY_EFFORTS,
        "default": "medium",
        "output": 0,
        "source_url": _MINIMAX_URL,
        "source_label": _MINIMAX_LABEL,
    }
    rows: list[tuple[str, int, bool]] = [
        ("minimax-m3", 1_000_000, True),
        ("minimax-m2.7", 204_800, False),
        ("minimax-m2.5", 204_800, False),
    ]
    return tuple(
        _make(
            model_id=model,
            family=model,
            context=context,
            vision=vision,
            prefixes=(f"{model}-",),
            **common,
        )
        for model, context, vision in rows
    )


BUILTIN_MODEL_DESCRIPTORS: tuple[ModelDescriptor, ...] = (
    *_openai_descriptors(),
    *_claude_descriptors(),
    *_gemini_descriptors(),
    *_deepseek_descriptors(),
    *_xai_descriptors(),
    *_kimi_descriptors(),
    *_glm_descriptors(),
    *_mimo_descriptors(),
    *_hy3_descriptors(),
    *_qwen3_descriptors(),
    *_minimax_descriptors(),
)


DEFAULT_MODEL_CATALOG = ModelCatalogService()


def model_catalog_plugin(
    catalog_service: ModelCatalogService | None = None,
    *,
    name: str = MODEL_CATALOG_PLUGIN_NAME,
) -> PluginSpec:
    """Return a plugin exposing the catalog service and metadata extension slot."""

    # Use the process-wide service by default so clients, registries, and the
    # plugin observe the same contribution set.  Tests/hosts can inject an
    # isolated service explicitly.
    service = catalog_service or DEFAULT_MODEL_CATALOG

    def apply(ctx: Any) -> Any:
        ctx.slots.declare(
            MODEL_METADATA_SLOT,
            kind="list",
            owner=MODEL_CATALOG_SERVICE,
        )
        ctx.provide(MODEL_CATALOG_SERVICE, service, replace=True)
        slot_tokens: dict[int, str] = {}

        def flatten(value: Any) -> list[ModelDescriptor]:
            if value is None:
                return []
            if isinstance(value, ModelDescriptor):
                return [value]
            if isinstance(value, Mapping):
                try:
                    return list(_coerce_many(value))
                except (TypeError, ValueError):
                    return []
            if isinstance(value, (str, bytes)):
                return []
            try:
                return [_coerce_descriptor(item) for item in value]
            except (TypeError, ValueError):
                return []

        def sync(values: Any) -> None:
            if values is None:
                contributions: list[Any] = []
            elif isinstance(values, ModelDescriptor):
                contributions = [values]
            elif isinstance(values, Mapping):
                contributions = [values] if _is_descriptor_mapping(values) else list(values.values())
            else:
                try:
                    contributions = list(values)
                except TypeError:
                    contributions = [values]
            live_ids: set[int] = set()
            for contribution in contributions:
                identity = id(contribution)
                live_ids.add(identity)
                if identity in slot_tokens:
                    continue
                descriptors = flatten(contribution)
                if descriptors:
                    slot_tokens[identity] = service.register_contribution(descriptors)
            for identity in list(slot_tokens):
                if identity not in live_ids:
                    service.unregister_contribution(slot_tokens.pop(identity))

        ctx.slots.inject(MODEL_METADATA_SLOT, sync)

        def cleanup() -> None:
            for identity, token in list(slot_tokens.items()):
                service.unregister_contribution(token)
                slot_tokens.pop(identity, None)

        return cleanup

    return PluginSpec(
        name=name,
        apply_fn=apply,
        version="1",
        purpose="Built-in model capability catalog and metadata extension slot",
        source="builtin",
    )


__all__ = [
    "BUILTIN_MODEL_DESCRIPTORS",
    "DEFAULT_MODEL_CATALOG",
    "MODEL_CATALOG_PLUGIN_NAME",
    "MODEL_CATALOG_SERVICE",
    "MODEL_METADATA_SLOT",
    "ModelCatalogService",
    "ModelDescriptor",
    "VERIFIED_AT",
    "canonicalize_selectable_model_id",
    "gemini_advertised_efforts",
    "is_official_gemini_endpoint",
    "model_catalog_plugin",
    "select_gemini_wire_model_id",
    "split_gemini_baked_effort_sku",
]
