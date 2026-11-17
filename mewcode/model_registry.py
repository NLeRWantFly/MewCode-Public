"""Unified provider and model registry for composite-agent execution."""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
import weakref
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from mewcode.client import (
    LLMClient,
    create_client,
    model_effort_levels,
    model_supports_vision,
    scheduled_client_stream,
)
from mewcode.config import ProviderConfig, UsageConfig
from mewcode.conversation import ConversationManager
from mewcode.errors import format_error
from mewcode.model_catalog import (
    DEFAULT_MODEL_CATALOG,
    ModelCatalogService,
    ModelDescriptor,
    canonicalize_selectable_model_id,
    gemini_advertised_efforts,
    is_official_gemini_endpoint,
    select_gemini_wire_model_id,
    split_gemini_baked_effort_sku,
)
from mewcode.opencode_go import (
    OPENCODE_GO_DEFAULT_MODEL,
    OPENCODE_GO_PROVIDER_TYPE,
    OPENCODE_GO_ROOT,
    is_opencode_go_provider,
    opencode_go_route,
    ordered_go_model_ids,
    resolve_effective_provider_config,
)
from mewcode.openrouter import (
    OPENROUTER_CATALOG_REQUIRED,
    OPENROUTER_DEFAULT_NOT_IN_CATALOG,
    fetch_openrouter_user_models,
    is_official_openrouter,
)
from mewcode.provider_sources import ProviderSlot, validate_slot
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete


PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "typesafe": {
        "label": "desktop:example_provider_7（决策 / 评审）",
        "protocol": "typesafe",
        "base_url": "https://api.typesafe.ai/v1",
        "default_model": "jev-latest",
    },
    "openai": {
        "label": "OpenAI",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
    },
    "xai": {
        "label": "desktop:example_provider_2",
        "protocol": "openai",
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-4.6",
    },
    "opencode-go": {
        "label": "OpenCode Go",
        "protocol": "openai-compat",
        "provider_type": OPENCODE_GO_PROVIDER_TYPE,
        "base_url": OPENCODE_GO_ROOT,
        "default_model": OPENCODE_GO_DEFAULT_MODEL,
    },
    "anthropic": {
        "label": "Anthropic",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com",
    },
    "kimi": {
        "label": "Moonshot / Kimi",
        "protocol": "openai-compat",
        "base_url": "https://api.moonshot.cn/v1",
    },
    "deepseek": {
        "label": "DeepSeek",
        "protocol": "openai-compat",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
    "openrouter": {
        "label": "OpenRouter",
        "protocol": "openai-compat",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "custom": {
        "label": "OpenAI Compatible",
        "protocol": "openai-compat",
        "base_url": "",
    },
}
ROUTE_ONLY_MODEL_IDS = frozenset({"codex-auto-review"})


def infer_brand(name: str, model: str = "", base_url: str = "") -> str:
    def _matches(value: str) -> str:
        if "magpie" in value:
            return "magpie"
        if "typesafe" in value or value.startswith("jev-"):
            return "typesafe"
        if "opencode" in value or "/zen/go" in value:
            return "opencode-go"
        if any(marker in value for marker in ("grok", "api.x.ai", "xai")):
            return "xai"
        if "openrouter" in value:
            return "openrouter"
        if any(marker in value for marker in ("moonshot", "kimi")):
            return "kimi"
        if "deepseek" in value:
            return "deepseek"
        if any(marker in value for marker in ("anthropic", "claude")):
            return "anthropic"
        if any(marker in value for marker in ("openai", "gpt-", "codex")):
            return "openai"
        return ""

    # Pass 1: identify the provider by its own name and base URL only.
    # A relay (e.g. desktop:example_provider_4) may host third-party models; the model name
    # must not override the provider's identity.
    brand = _matches(" ".join((name, base_url)).lower())
    if brand:
        return brand
    # Pass 2: use the model name as a fallback, but only when the provider
    # has no explicit name (auto-discovered slots).  A user-named provider
    # like "desktop:example_provider_4" should stay "custom" even if it serves a kimi model.
    if not name.strip():
        return _matches(model.lower()) or "custom"
    return "custom"


@dataclass(frozen=True)
class ModelRef:
    provider_key: str
    model_id: str
    effort: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "ModelRef":
        if isinstance(value, cls):
            provider_key = value.provider_key
            model_id = value.model_id
            effort = value.effort
        elif not isinstance(value, dict):
            raise ValueError("model reference must be an object")
        else:
            provider_key = str(value.get("provider_key") or "").strip()
            model_id = str(value.get("model_id") or value.get("model") or "").strip()
            effort = str(value.get("effort") or "").strip()
        if effort == "auto":
            effort = ""
        split = split_gemini_baked_effort_sku(model_id)
        if split is not None:
            model_id, baked = split
            if not effort:
                effort = baked
        if model_id in ROUTE_ONLY_MODEL_IDS:
            raise ValueError(
                f'"{model_id}" 是宿主审批路由标签，不是可调用的 Provider 模型'
            )
        if not provider_key or not model_id:
            raise ValueError("provider_key and model_id are required")
        return cls(provider_key=provider_key, model_id=model_id, effort=effort)

    def as_dict(self) -> dict[str, str]:
        return {
            "provider_key": self.provider_key,
            "model_id": self.model_id,
            "effort": self.effort,
        }


@dataclass(frozen=True)
class ProviderProfile:
    provider_key: str
    name: str
    brand: str
    protocol: str
    base_url: str
    default_model: str
    api_key: str
    provider_type: str = "standard"
    models: list[str] = field(default_factory=list)
    default_headers: dict[str, str] = field(default_factory=dict)
    context_channel: str = "auto"

    @classmethod
    def from_payload(cls, payload: Any) -> "ProviderProfile":
        if not isinstance(payload, dict):
            raise ValueError("provider profile must be an object")
        brand = str(payload.get("brand") or "custom").strip().lower()
        preset = PROVIDER_PRESETS.get(brand, PROVIDER_PRESETS["custom"])
        raw_key = str(
            payload.get("provider_key")
            or payload.get("id")
            or uuid.uuid4().hex
        ).strip()
        provider_key = raw_key if raw_key.startswith("desktop:") else f"desktop:{raw_key}"
        name = str(payload.get("name") or preset["label"]).strip()
        protocol = str(payload.get("protocol") or preset["protocol"]).strip()
        provider_type = str(
            payload.get("provider_type") or preset.get("provider_type") or "standard"
        ).strip()
        base_url = str(payload.get("base_url") or preset["base_url"]).strip().rstrip("/")
        # Official DeepSeek OpenAI-compat routes live under /v1; older desktop
        # profiles may still store the bare host.
        if "api.deepseek.com" in base_url.lower() and not base_url.lower().endswith("/v1"):
            base_url = f"{base_url}/v1"
        default_model = str(
            payload.get("default_model")
            or payload.get("model")
            or preset.get("default_model")
            or ""
        ).strip()
        api_key = str(payload.get("api_key") or "")
        models = [
            str(item).strip()
            for item in payload.get("models", [])
            if str(item).strip() and str(item).strip() not in ROUTE_ONLY_MODEL_IDS
        ]
        if default_model in ROUTE_ONLY_MODEL_IDS:
            raise ValueError(
                f'"{default_model}" is an approval route label, not a provider model'
            )
        official_openrouter = is_official_openrouter(brand=brand, base_url=base_url)
        if official_openrouter:
            if models:
                if default_model not in models:
                    raise ValueError(OPENROUTER_DEFAULT_NOT_IN_CATALOG)
            elif default_model:
                models.insert(0, default_model)
            else:
                raise ValueError(OPENROUTER_CATALOG_REQUIRED)
        elif default_model and default_model not in models:
            models.insert(0, default_model)
        raw_headers = payload.get("default_headers") or {}
        headers = (
            {str(key): str(value) for key, value in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        if protocol not in {"anthropic", "openai", "openai-compat", "typesafe"}:
            raise ValueError(f"unsupported provider protocol: {protocol}")
        if provider_type not in {"standard", OPENCODE_GO_PROVIDER_TYPE}:
            raise ValueError(f"unsupported provider type: {provider_type}")
        context_channel = str(payload.get("context_channel") or "auto").strip()
        if context_channel not in {"auto", "deepseek", "generic"}:
            raise ValueError("context_channel must be auto, deepseek, or generic")
        if not name or not base_url or not default_model or not api_key:
            raise ValueError("name, base_url, default_model and api_key are required")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("base_url must use http or https")
        return cls(
            provider_key=provider_key,
            name=name,
            brand=brand,
            protocol=protocol,
            base_url=base_url,
            default_model=default_model,
            api_key=api_key,
            provider_type=provider_type,
            models=models,
            default_headers=headers,
            context_channel=context_channel,
        )

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "name": self.name,
            "brand": self.brand,
            "protocol": self.protocol,
            "provider_type": self.provider_type,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "models": list(self.models),
            "has_secret": bool(self.api_key),
            "context_channel": self.context_channel,
            "source": "desktop",
        }


@dataclass
class ProviderRecord:
    key: str
    provider: ProviderConfig
    source: str
    label: str
    brand: str
    models: list[str] = field(default_factory=list)
    advertised_models: list[str] = field(default_factory=list)
    error: str = ""
    effort_levels: list[str] = field(default_factory=list)
    default_effort: str = ""
    model_efforts: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ResolvedModel:
    ref: ModelRef
    provider: ProviderRecord
    config: ProviderConfig
    client: LLMClient
    protocol: str
    context_window: int
    effort: str
    capabilities: tuple[str, ...]
    display_name: str


class ProviderRegistry:
    """Merge discovered and desktop providers and resolve per-agent clients."""

    def __init__(
        self,
        *,
        catalog_ttl: float = 300.0,
        list_timeout: float = 4.0,
        client_factory: Callable[[ProviderConfig], LLMClient] | None = None,
        catalog_service: ModelCatalogService | None = None,
        desktop_source: ProviderRegistry | None = None,
    ) -> None:
        self.catalog_ttl = catalog_ttl
        self.list_timeout = list_timeout
        self.catalog_service = catalog_service or DEFAULT_MODEL_CATALOG
        self._discovered: dict[str, ProviderRecord] = {}
        self._desktop: dict[str, ProviderRecord] = {}
        self._desktop_profiles: dict[str, ProviderProfile] = {}
        # Project scopes own discovery and client factories, but desktop
        # credentials have one live source. Never snapshot them per session.
        self._desktop_source = desktop_source
        self._service_tiers: dict[tuple[str, str], str] = {}
        self._speed_clients = weakref.WeakKeyDictionary()
        self._desktop_revision = 0
        self._catalog_desktop_revision = -1
        self._catalog_cache: dict[str, Any] | None = None
        self._catalog_at = 0.0
        self._catalog_lock = asyncio.Lock()
        # Keep the legacy module-level seam patchable while allowing Hosts to
        # inject a lifecycle-owned factory explicitly.
        self._client_factory = (
            client_factory
            if client_factory is not None
            else lambda config: create_client(config)
        )

    def _describe_model(self, model: str) -> ModelDescriptor | None:
        return self.catalog_service.describe(model)

    @staticmethod
    def _is_claude_family(record: ProviderRecord) -> bool:
        blob = " ".join(
            (
                record.key,
                record.label,
                record.brand,
                record.source,
                record.provider.name,
                record.provider.protocol,
                record.provider.model,
            )
        ).lower()
        return (
            record.provider.protocol == "anthropic"
            or record.brand == "anthropic"
            or record.source == "claude-code"
            or "claude" in blob
            or "anthropic" in blob
        )

    def _claude_seed_models(self) -> list[str]:
        """Concrete Claude ids from the capability catalog for picker seeding.

        Broad prefix stubs like bare ``claude`` are matching aids only and are
        not useful selectable rows in the desktop picker.
        """
        skip = {"claude", "claude-3"}
        seeded: list[str] = []
        for row in self.catalog_service.catalog():
            if str(row.get("provider_brand") or "").lower() != "anthropic":
                continue
            model_id = str(row.get("model_id") or "").strip()
            if not model_id or model_id in skip:
                continue
            seeded.append(model_id)
        return seeded

    def _effort_levels(
        self,
        protocol: str,
        model: str,
        record: ProviderRecord,
    ) -> list[str]:
        """Resolve explicit provider declarations before catalog fallbacks."""

        levels = list(record.model_efforts.get(model) or record.effort_levels)
        if not levels:
            descriptor = self.catalog_service.describe(model)
            levels = (list(descriptor.efforts) if descriptor is not None else model_effort_levels(
                protocol, model, catalog_service=self.catalog_service))
        offered = (None if is_official_gemini_endpoint(record.provider.base_url) else
                   gemini_advertised_efforts(model, record.advertised_models or record.models))
        return [level for level in offered if not levels or level in levels] if offered is not None else levels

    @staticmethod
    def _context_window(
        config: ProviderConfig,
        descriptor: ModelDescriptor | None,
    ) -> int:
        """Use catalog context only when provider/config has no value."""

        if (
            descriptor is not None
            and descriptor.context_window > 0
            and config.context_window <= 0
            and config._route_context_window <= 0
            and config._fetched_context_window <= 0
        ):
            return descriptor.context_window
        return config.get_context_window()

    @staticmethod
    def _max_output_tokens(
        config: ProviderConfig,
        descriptor: ModelDescriptor | None,
    ) -> int:
        if config.max_output_tokens > 0:
            return config.max_output_tokens
        if descriptor is not None and descriptor.max_output_tokens > 0:
            return descriptor.max_output_tokens
        return config.get_max_output_tokens()

    @staticmethod
    def _from_slot(slot: ProviderSlot) -> ProviderRecord:
        provider = slot.provider
        return ProviderRecord(
            key=slot.key,
            provider=provider,
            source=slot.source,
            label=slot.label,
            brand=infer_brand(provider.name, provider.model, provider.base_url),
            models=list(dict.fromkeys([
                *([provider.model] if provider.model else []),
                *slot.models,
            ])),
            error=validate_slot(slot) or "",
            effort_levels=list(slot.effort_levels),
            default_effort=slot.default_effort,
            model_efforts={
                model: list(efforts)
                for model, efforts in slot.model_efforts.items()
            },
        )

    def refresh_discovered(self, slots: Iterable[ProviderSlot]) -> None:
        self._discovered = {slot.key: self._from_slot(slot) for slot in slots}
        self.invalidate_catalog()

    def replace_desktop_profiles(self, payloads: Iterable[Any]) -> list[dict[str, Any]]:
        if self._desktop_source is not None:
            return self._desktop_source.replace_desktop_profiles(payloads)
        profiles = [ProviderProfile.from_payload(payload) for payload in payloads]
        records: dict[str, ProviderRecord] = {}
        for profile in profiles:
            model_efforts: dict[str, list[str]] = {}
            for model in profile.models:
                descriptor = self._describe_model(model)
                if descriptor is not None and descriptor.efforts:
                    model_efforts[model] = list(descriptor.efforts)
            default_descriptor = self._describe_model(profile.default_model)
            config = ProviderConfig(
                name=profile.name,
                protocol=profile.protocol,
                base_url=profile.base_url,
                model=profile.default_model,
                provider_type=profile.provider_type,
                api_key=profile.api_key,
                default_headers=dict(profile.default_headers),
                context_channel=profile.context_channel,
                usage=UsageConfig(),
            )
            records[profile.provider_key] = ProviderRecord(
                key=profile.provider_key,
                provider=config,
                source="desktop",
                label=f"{profile.name}  [{profile.default_model}]  (桌面客户端)",
                brand=profile.brand,
                models=list(profile.models),
                default_effort=(
                    default_descriptor.default_effort
                    if default_descriptor is not None
                    else ""
                ),
                model_efforts=model_efforts,
            )
        self._desktop = records
        self._desktop_profiles = {profile.provider_key: profile for profile in profiles}
        self._desktop_revision += 1
        self.invalidate_catalog()
        return [profile.as_public_dict() for profile in profiles]

    def invalidate_catalog(self) -> None:
        self._catalog_cache = None
        self._catalog_at = 0.0

    def _speed_source(self):
        return self._desktop_source._speed_source() if self._desktop_source is not None else self

    def service_tier_for(self, provider_key, model_id, default=''):
        return self._speed_source()._service_tiers.get((provider_key, model_id), default)

    def apply_service_tier(self, provider_key, config, *, model_id=None):
        tier = self.service_tier_for(provider_key, model_id or config.model, config.service_tier)
        return replace(config, service_tier=tier) if tier != config.service_tier else config

    def track_service_tier(self, provider_key, model_id, client):
        if not hasattr(client, 'service_tier'):
            return
        source = self._speed_source()
        client.service_tier = self.service_tier_for(provider_key, model_id, client.service_tier)
        try:
            source._speed_clients[client] = (provider_key, model_id)
        except TypeError:
            pass  # Some embedded/test clients cannot be weakly referenced.

    def configure_service_tiers(self, values):
        source = self._speed_source()
        source._service_tiers = dict(values)
        for client, route in list(source._speed_clients.items()):
            if route in values:
                client.service_tier = values[route]

    def records(self) -> list[ProviderRecord]:
        merged = {key: record for key, record in self._discovered.items()
                  if self._desktop_source is None or not key.startswith("desktop:")}
        merged.update(self._desktop_records())
        return list(merged.values())

    def _desktop_records(self) -> dict[str, ProviderRecord]:
        return self._desktop_source._desktop_records() if self._desktop_source is not None else self._desktop

    def desktop_revision(self) -> int:
        return self._desktop_source.desktop_revision() if self._desktop_source is not None else self._desktop_revision

    def get(self, provider_key: str) -> ProviderRecord | None:
        if self._desktop_source is not None and provider_key.startswith("desktop:"):
            # Removal must not resurrect a stale credential from discovery slots.
            return self._desktop_records().get(provider_key)
        return self._desktop.get(provider_key) or self._discovered.get(provider_key)

    def public_desktop_profiles(self) -> list[dict[str, Any]]:
        if self._desktop_source is not None:
            return self._desktop_source.public_desktop_profiles()
        return [profile.as_public_dict() for profile in self._desktop_profiles.values()]

    @staticmethod
    def _desktop_declared_models(record: ProviderRecord) -> list[str]:
        if record.source != "desktop":
            return []
        return [
            str(model).strip()
            for model in (record.advertised_models or record.models)
            if str(model).strip()
        ]

    def offers_model(self, record: ProviderRecord, model_id: str) -> bool:
        """Whether resolve() would accept this model on the record's provider."""
        if record.error:
            return False
        declared = self._desktop_declared_models(record)
        if not declared:
            return True
        requested = canonicalize_selectable_model_id(model_id)
        return requested in {
            canonicalize_selectable_model_id(item) for item in declared
        }

    def _explicitly_lists_model(self, record: ProviderRecord, model_id: str) -> bool:
        declared = [
            str(model).strip()
            for model in (record.advertised_models or record.models)
            if str(model).strip()
        ]
        if not declared:
            return False
        requested = canonicalize_selectable_model_id(model_id)
        return requested in {
            canonicalize_selectable_model_id(item) for item in declared
        }

    def _relocate_score(self, record: ProviderRecord, model_id: str) -> int:
        requested = canonicalize_selectable_model_id(model_id).lower()
        blob = " ".join(
            (
                record.brand,
                record.label,
                record.provider.name,
                record.provider.base_url,
            )
        ).lower()
        score = 0
        if self._explicitly_lists_model(record, model_id):
            score += 100
        if record.source == "desktop":
            score += 5
        if requested.startswith("grok") or "grok" in requested:
            if record.brand == "xai" or "xai" in blob or "grok" in blob:
                score += 40
        inferred = infer_brand(record.provider.name, model_id, record.provider.base_url)
        if inferred and inferred == (record.brand or "").lower():
            score += 10
        return score

    def find_provider_key_for_model(
        self,
        model_id: str,
        *,
        prefer_key: str = "",
    ) -> str:
        """Pick a live provider that can actually serve this model id."""
        requested = canonicalize_selectable_model_id(str(model_id or "").strip())
        if not requested:
            return ""
        if prefer_key:
            record = self.get(prefer_key)
            if record is not None and self.offers_model(record, requested):
                return prefer_key
        best: ProviderRecord | None = None
        best_score = -1
        for record in self.records():
            # An unadvertised model may be accepted by an explicitly selected
            # custom endpoint, but that is not evidence for an inferred route.
            if not self.offers_model(record, requested) or not self._explicitly_lists_model(record, requested):
                continue
            score = self._relocate_score(record, requested)
            if score > best_score:
                best = record
                best_score = score
        return best.key if best is not None else ""

    def find_subagent_provider_key(self, model_id: str) -> str:
        """Resolve a model-only child request without inheriting an unrelated route.

        Codex's configured provider is authoritative for GPT-6 submodels when
        present.  In particular, an OpenCode Go catalog entry is not evidence
        that its endpoint can serve the Codex Luna SKU.
        """
        requested = canonicalize_selectable_model_id(str(model_id or "").strip())
        if requested.lower() == "gpt-6.1-sol" or requested.lower().startswith(("gpt-6-", "gpt-6.1-sol-")):
            codex = [
                record for record in self.records()
                if record.source == "codex" and not record.error
                and record.provider.name == "codex-custom"
                and self.offers_model(record, requested)
            ]
            if len(codex) == 1:
                return codex[0].key
        if requested.lower() == "gpt-6-luna":
            candidates = [
                record for record in self.records()
                if record.provider.provider_type != OPENCODE_GO_PROVIDER_TYPE
                and not record.error and self.offers_model(record, requested)
                and self._explicitly_lists_model(record, requested)
            ]
            return candidates[0].key if len(candidates) == 1 else ""
        return self.find_provider_key_for_model(requested)

    def relocate_model_ref(self, ref: ModelRef | dict[str, Any]) -> ModelRef:
        """Move a stale provider/model pair onto a provider that still serves it.

        resolve() stays strict: this only rewrites the binding when the original
        route would fail and another configured provider lists the same model.
        """
        model_ref = ModelRef.from_value(ref)
        record = self.get(model_ref.provider_key)
        if record is not None and self.offers_model(record, model_ref.model_id):
            return model_ref
        candidates = [
            candidate.key for candidate in self.records()
            if self.offers_model(candidate, model_ref.model_id)
            and self._explicitly_lists_model(candidate, model_ref.model_id)
        ]
        # Ambiguous routes must be chosen by the user, not by catalog order or
        # the currently selected chat provider (which may have different costs).
        target_key = candidates[0] if len(candidates) == 1 else ""
        if not target_key or target_key == model_ref.provider_key:
            return model_ref
        return ModelRef(target_key, model_ref.model_id, model_ref.effort)

    def resolve(
        self,
        ref: ModelRef | dict[str, Any],
        *,
        usage_recorder: Callable[[Any], None] | None = None,
    ) -> ResolvedModel:
        model_ref = ModelRef.from_value(ref)
        record = self.get(model_ref.provider_key)
        if record is None:
            raise ValueError(f"provider is unavailable: {model_ref.provider_key}")
        if record.error:
            raise ValueError(record.error)
        declared_models = self._desktop_declared_models(record)
        if declared_models and not self.offers_model(record, model_ref.model_id):
            available = ", ".join(
                dict.fromkeys(
                    canonicalize_selectable_model_id(str(model).strip())
                    for model in declared_models
                    if str(model).strip()
                )
            )
            if len(available) > 240:
                available = available[:237] + "…"
            raise ValueError(
                f'模型 "{model_ref.model_id}" 不属于 Provider '
                f'"{record.provider.name}" 的可用模型列表'
                + (f"（可用：{available}）" if available else "")
            )
        wire_id = select_gemini_wire_model_id(
            model_ref.model_id,
            model_ref.effort,
            record.advertised_models or record.models,
            official_gemini_endpoint=is_official_gemini_endpoint(
                record.provider.base_url
            ),
        )
        config = resolve_effective_provider_config(record.provider, wire_id)
        config = self.apply_service_tier(model_ref.provider_key, config, model_id=model_ref.model_id)
        client = self._client_factory(config)
        self.track_service_tier(model_ref.provider_key, model_ref.model_id, client)
        effort = model_ref.effort
        if effort:
            levels = self._effort_levels(config.protocol, model_ref.model_id, record)
            if effort not in levels:
                from mewcode.pipeline.auto_policy import converge_effort
                converged = converge_effort(
                    effort,
                    model_ref.model_id,
                    [{"id": model_ref.model_id, "efforts": levels}],
                )
                effort = str(converged or "")
            baked = split_gemini_baked_effort_sku(config.model)
            if effort and not (baked is not None and baked[1] == effort):
                client.set_effort(effort)
            if effort != model_ref.effort:
                model_ref = ModelRef(
                    provider_key=model_ref.provider_key,
                    model_id=model_ref.model_id,
                    effort=effort,
                )
        if usage_recorder is not None:
            client.set_usage_recorder(usage_recorder)
        descriptor = (
            self._describe_model(model_ref.model_id)
            or self._describe_model(config.model)
        )
        return ResolvedModel(
            ref=model_ref,
            provider=record,
            config=config,
            client=client,
            protocol=config.protocol,
            context_window=self._context_window(config, descriptor),
            effort=model_ref.effort,
            capabilities=("structured-review", "structured-decision") if config.protocol == "typesafe" else (
                "chat",
                "tools",
                *(("vision",) if model_supports_vision(
                    config.protocol,
                    model_ref.model_id,
                    catalog_service=self.catalog_service,
                ) else ()),
            ),
            display_name=f"{record.provider.name} · {model_ref.model_id}",
        )

    async def _catalog_provider(
        self, record: ProviderRecord, *, local_only: bool = False
    ) -> dict[str, Any]:
        models = list(record.advertised_models or record.models) if local_only else list(record.models)
        error = record.error
        catalog_error = record.error
        models_authoritative = False
        catalog_stale = False
        go_provider = is_opencode_go_provider(record.provider)
        claude_family = self._is_claude_family(record)
        frozen_openrouter = (
            record.source == "desktop"
            and is_official_openrouter(
                brand=record.brand, base_url=record.provider.base_url
            )
        )
        # Codex keeps a local model catalog — trust it when non-trivial.
        # cc-switch / Claude Code often only persist role mappings (opus/sonnet/
        # haiku/env model ≤4 ids). The full dropdown (same as cc-switch's
        # 「获取模型列表」) comes from the provider /models endpoint.
        if record.brand == "magpie":
            skip_live_list = False
        elif record.source == "codex":
            skip_live_list = len(models) > 1
        elif record.source in {"cc-switch", "claude-code"}:
            skip_live_list = len(models) > 4
        else:
            skip_live_list = False
        if frozen_openrouter:
            models_authoritative = bool(record.models)
            if not record.models and not error:
                error = OPENROUTER_CATALOG_REQUIRED
                catalog_error = error
        elif local_only or skip_live_list:
            models_authoritative = False
        elif not error:
            try:
                client = self._client_factory(
                    resolve_effective_provider_config(record.provider)
                )
                # Relays often need a cold-start handshake longer than the
                # default 4s probe budget used elsewhere.
                list_budget = self.list_timeout
                if record.source in {"cc-switch", "claude-code", "codex"}:
                    list_budget = max(list_budget, 15.0)
                fetched = await asyncio.wait_for(
                    client.list_models(), timeout=list_budget
                )
                if record.brand == "magpie" and fetched is None:
                    raise RuntimeError("Magpie model catalog is unavailable")
                if go_provider and fetched is None:
                    raise RuntimeError("OpenCode Go model catalog is unavailable")
                models_authoritative = fetched is not None
                if record.brand == "magpie" and fetched is not None:
                    # Magpie ids include the selected upstream route. A Codex
                    # cache entry or a bare id is not an advertised route here.
                    models = [str(model).strip() for model in fetched if str(model).strip()]
                elif go_provider and fetched is not None:
                    models = ordered_go_model_ids(fetched)
                elif fetched:
                    fetched_ids = [
                        str(model).strip()
                        for model in fetched
                        if str(model).strip()
                    ]
                    if record.source in {"cc-switch", "claude-code"}:
                        known = set(fetched_ids)
                        extras = [item for item in models if item not in known]
                        models = fetched_ids + extras
                    else:
                        models.extend(fetched_ids)
                elif claude_family and not models:
                    # Live endpoint empty/unsupported: seed capability ids only
                    # when discovery gave us nothing selectable.
                    models = list(self._claude_seed_models())
                    catalog_stale = True
            except Exception as exc:
                # Standard providers keep explicit models; Go falls back to
                # its maintained route table and exposes staleness explicitly.
                catalog_error = str(exc)
                if record.brand == "magpie":
                    catalog_stale = True
                elif go_provider:
                    models = ordered_go_model_ids()
                    catalog_stale = True
                elif claude_family and models:
                    catalog_stale = True
                elif claude_family and not models:
                    models = list(self._claude_seed_models())
                    catalog_stale = True
                elif not models:
                    error = catalog_error
        elif go_provider:
            models = ordered_go_model_ids()
            catalog_stale = True
        elif claude_family and not models:
            models = self._claude_seed_models()
            catalog_stale = True
        unique_models = list(dict.fromkeys(models))
        record.advertised_models = [
            str(model).strip()
            for model in unique_models
            if str(model).strip()
        ]
        # Prefer base Gemini ids; hide relay SKUs that bake thinking into the name.
        selectable: list[str] = []
        seen_selectable: set[str] = set()
        for model in unique_models:
            base = canonicalize_selectable_model_id(str(model).strip())
            if not base or base in ROUTE_ONLY_MODEL_IDS or base in seen_selectable:
                continue
            seen_selectable.add(base)
            selectable.append(base)
        unique_models = selectable
        model_payload = []
        for model in unique_models:
            route = opencode_go_route(model) if go_provider else None
            unavailable_reason = ""
            available = not bool(error)
            protocol = record.provider.protocol
            effective = copy.deepcopy(record.provider)
            effective.model = model
            if go_provider:
                if route is None:
                    available = False
                    protocol = "unknown"
                    unavailable_reason = "Missing OpenCode Go routing metadata"
                else:
                    protocol = route.protocol
                    effective = resolve_effective_provider_config(record.provider, model)
            else:
                effective = resolve_effective_provider_config(record.provider, model)
                protocol = effective.protocol
            descriptor = self._describe_model(model)
            efforts = self._effort_levels(protocol, model, record)
            default_effort = descriptor.default_effort if descriptor is not None else ''
            if default_effort and default_effort not in efforts:
                default_effort = efforts[-1] if efforts else ''
            model_payload.append({
                "provider_key": record.key,
                "provider_name": record.provider.name,
                "brand": record.brand,
                "source": record.source,
                "model_id": model,
                "protocol": protocol,
                "efforts": efforts,
                "default_effort": default_effort,
                "vision": model_supports_vision(
                    protocol,
                    model,
                    catalog_service=self.catalog_service,
                ) if route or not go_provider else False,
                "tools": protocol != "typesafe",
                "review_only": protocol == "typesafe",
                "available": available,
                "unavailable_reason": unavailable_reason or error,
                "context_window": (
                    route.context_window
                    if go_provider and route is not None and route.context_window > 0
                    else self._context_window(effective, descriptor) if available else 0
                ),
                "max_output_tokens": (
                    route.max_output_tokens
                    if go_provider and route is not None and route.max_output_tokens > 0
                    else self._max_output_tokens(effective, descriptor)
                    if available else 0
                ),
                # Chat Completions/Anthropic clients send this configured cap.
                # Responses may omit a wire cap, so keep its model maximum as
                # the conservative reservation rather than claiming a cap.
                "request_output_tokens": (
                    effective.get_max_output_tokens()
                    if available and protocol in {"openai-compat", "anthropic"} else None
                ),
                "family": descriptor.family if descriptor is not None else "",
                "provider_brand": (
                    descriptor.provider_brand if descriptor is not None else ""
                ),
                "capabilities": (
                    ["structured-review", "structured-decision"] if protocol == "typesafe" else list(descriptor.capabilities) if descriptor is not None
                    else ["chat", "tools"]
                ),
                "source_url": descriptor.source_url if descriptor is not None else "",
                "source_label": descriptor.source_label if descriptor is not None else "",
                "verified_at": descriptor.verified_at if descriptor is not None else "",
            })
        return {
            "provider": {
                "key": record.key,
                "name": record.provider.name,
                "label": record.label,
                "brand": record.brand,
                "source": record.source,
                "protocol": record.provider.protocol,
                "provider_type": record.provider.provider_type,
                "protocol_mode": "per-model" if go_provider else "fixed",
                "base_url": record.provider.base_url,
                "default_model": record.provider.model,
                "available": not bool(error),
                "error": error,
                "catalog_error": catalog_error,
                "models_authoritative": models_authoritative,
                "catalog_stale": catalog_stale,
                "managed": record.source == "desktop",
            },
            "models": model_payload,
        }

    async def catalog(self, *, force: bool = False, local_only: bool = False) -> dict[str, Any]:
        revision = self.desktop_revision()
        if self._catalog_desktop_revision != revision:
            self.invalidate_catalog()
            self._catalog_desktop_revision = revision
        if local_only:
            # Opening a picker must not depend on remote /models or ranking feeds.
            # Use explicit configured routes (and previously advertised models),
            # never capability-family guesses, without replacing the live cache.
            entries = await asyncio.gather(
                *(self._catalog_provider(record, local_only=True) for record in self.records())
            )
            return {
                "generated_at": time.time(), "catalog_mode": "configured",
                "providers": [entry["provider"] for entry in entries],
                "models": [model for entry in entries for model in entry["models"]],
                "presets": copy.deepcopy(PROVIDER_PRESETS),
            }
        now = time.time()
        if (
            not force
            and self._catalog_cache is not None
            and now - self._catalog_at < self.catalog_ttl
        ):
            return copy.deepcopy(self._catalog_cache)
        async with self._catalog_lock:
            now = time.time()
            revision = self.desktop_revision()
            if self._catalog_desktop_revision != revision:
                self.invalidate_catalog()
                self._catalog_desktop_revision = revision
            if (
                not force
                and self._catalog_cache is not None
                and now - self._catalog_at < self.catalog_ttl
            ):
                return copy.deepcopy(self._catalog_cache)
            entries = await asyncio.gather(
                *(self._catalog_provider(record) for record in self.records())
            )
            payload = {
                "generated_at": now,
                "providers": [entry["provider"] for entry in entries],
                "models": [model for entry in entries for model in entry["models"]],
                "presets": copy.deepcopy(PROVIDER_PRESETS),
            }
            if self.desktop_revision() == revision:
                self._catalog_cache = payload
                self._catalog_at = now
            else:
                self.invalidate_catalog()  # A sync during fetch cannot publish a stale cache.
            return copy.deepcopy(payload)

    async def _test_openrouter_catalog(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        api_key = str(payload.get("api_key") or "")
        base_url = str(
            payload.get("base_url") or PROVIDER_PRESETS["openrouter"]["base_url"]
        )
        try:
            models = await asyncio.wait_for(
                fetch_openrouter_user_models(api_key=api_key, base_url=base_url),
                timeout=max(self.list_timeout, 8.0),
            )
        except Exception as exc:
            return {
                "ok": False,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "models": [],
                "error": str(exc),
            }
        return {
            "ok": True,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "models": models,
            "checks": {
                "authentication": True,
                "streaming": False,
                "tool_calling": False,
                "model_catalog": True,
            },
            "catalog_error": "",
            "note": "已验证 Key 并拉取模型目录。",
        }

    async def test_profile(self, payload: Any) -> dict[str, Any]:
        started = time.monotonic()
        detected_models: list[str] = []
        phase = "configuration"
        checks = dict(authentication=False, streaming=False, tool_calling=False, model_catalog=False)
        phase_labels = {"configuration": "配置校验", "model_catalog": "模型目录",
                        "streaming": "实际流式推理", "tool_calling": "工具调用"}
        profile = None
        client = None
        try:
            if isinstance(payload, dict) and is_official_openrouter(
                brand=str(payload.get("brand") or ""),
                base_url=str(
                    payload.get("base_url")
                    or PROVIDER_PRESETS["openrouter"]["base_url"]
                ),
            ):
                return await self._test_openrouter_catalog(payload)
            profile = ProviderProfile.from_payload(payload)
            config = ProviderConfig(
                name=profile.name,
                protocol=profile.protocol,
                base_url=profile.base_url,
                model=profile.default_model,
                provider_type=profile.provider_type,
                api_key=profile.api_key,
                default_headers=dict(profile.default_headers),
                context_channel=profile.context_channel,
            )
            config = resolve_effective_provider_config(config)
            client = self._client_factory(config)
            models = None
            catalog_error = ""
            phase = "model_catalog"
            try:
                models = await asyncio.wait_for(
                    client.list_models(), timeout=max(self.list_timeout, 8.0)
                )
                if models:
                    detected_models = [str(item) for item in models if str(item).strip()]
                    checks["model_catalog"] = True
            except Exception as exc:
                # A model-list endpoint is optional. The paid probes below
                # verify the actual authentication and inference path.
                catalog_error = format_error(exc).replace(profile.api_key, "[REDACTED]")

            if (
                profile.provider_type == OPENCODE_GO_PROVIDER_TYPE
                and models is None
            ):
                raise RuntimeError(
                    "OpenCode Go model catalog check failed"
                    + (f": {catalog_error}" if catalog_error else "")
                )

            response_probe = ConversationManager()
            if profile.protocol == "typesafe":
                phase = "evaluation"
                phase_labels[phase] = "结构化评审"
                async with asyncio.timeout(30):
                    result = await client.evaluate({'probe': 'MEWCODE_OK'}, {'probe': {
                        'type': 'choice', 'instructions': 'Select the exact value of state.probe.',
                        'criteria': {'MEWCODE_OK': 'Exact match', 'other': 'Different value'}}})
                if result['answers']['probe']['choice'] != 'MEWCODE_OK':
                    raise ValueError('Jev 未返回期望的探测判定')
                checks.update(authentication=True, structured_evaluation=True)
                return {'ok': True, 'models': detected_models or profile.models, 'checks': checks,
                    'phase': 'completed', 'catalog_error': catalog_error,
                    'latency_ms': round((time.monotonic() - started) * 1000),
                    'note': '鉴权与结构化判定已验证；Jev 不提供对话流、工具调用或视觉能力。'}
            response_probe.add_user_message(
                "Reply with exactly MEWCODE_OK. Do not call a tool."
            )
            response_text: list[str] = []
            response_ended = False
            phase = "streaming"
            async with asyncio.timeout(30):
                async for event in scheduled_client_stream(client, response_probe):
                    if isinstance(event, TextDelta):
                        response_text.append(event.text)
                    elif isinstance(event, StreamEnd):
                        response_ended = True
            if not response_ended or not "".join(response_text).strip():
                raise RuntimeError("streaming response probe returned no text")
            checks.update(authentication=True, streaming=True)

            base_schema = {
                "name": "mewcode_connection_probe",
                "description": "Return a probe value to verify tool calling.",
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
            probe_schema: dict[str, Any] = base_schema
            if config.protocol in {"openai", "openai-compat"}:
                probe_schema = {
                    "type": "function",
                    "name": base_schema["name"],
                    "description": base_schema["description"],
                    "parameters": base_schema["input_schema"],
                }
            tool_probe = ConversationManager()
            tool_probe.add_user_message(
                "Call mewcode_connection_probe once with value MEWCODE_OK. "
                "Do not answer with plain text."
            )
            tool_called = False
            phase = "tool_calling"
            async with asyncio.timeout(30):
                async for event in scheduled_client_stream(
                    client, tool_probe, tools=[probe_schema]
                ):
                    if (
                        isinstance(event, ToolCallComplete)
                        and event.tool_name == "mewcode_connection_probe"
                        and event.arguments.get("value") == "MEWCODE_OK"
                    ):
                        tool_called = True

            if not tool_called:
                raise RuntimeError("model did not complete the tool-call probe")
            checks["tool_calling"] = True
            return {
                "ok": True,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "models": list(detected_models or profile.models),
                "checks": checks,
                "phase": "completed",
                "catalog_error": catalog_error,
                "note": (
                    "鉴权、流式响应和工具调用均已验证。"
                    + (
                        f" 已探测到 {len(detected_models)} 个模型。"
                        if detected_models
                        else ""
                    )
                ),
            }
        except Exception as exc:
            detail = format_error(exc)
            if profile is not None:
                detail = detail.replace(profile.api_key, "[REDACTED]")
            if isinstance(exc, TimeoutError):
                detail = "本阶段超过测试时限（包含并发排队时间）；本次测试已停止，请稍后重试"
            return {
                "ok": False,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "models": detected_models,
                "error": f"{phase_labels[phase]}失败：{detail}",
                "phase": phase,
                "checks": checks,
            }
        finally:
            # Probes create short-lived SDK clients; do not leave their HTTP
            # pools behind on failure/timeout. Cleanup must also be bounded.
            sdk = getattr(client, "_client", None)
            if sdk is not None and callable(getattr(sdk, "close", None)):
                try:
                    await asyncio.wait_for(sdk.close(), timeout=2)
                except Exception:
                    pass
