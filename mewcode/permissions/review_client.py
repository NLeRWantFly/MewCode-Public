"""Resolve the configured Auto safety-review model client."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from mewcode.model_registry import ModelRef, ProviderRegistry
from mewcode.runtime_paths import user_data_root

log = logging.getLogger(__name__)


def review_model_path() -> Path:
    return user_data_root() / "review-model.json"


def load_review_model_settings(path: Path | None = None) -> dict[str, str]:
    target = path or review_model_path()
    try:
        if not target.is_file():
            return {"model_id": "", "provider_key": "", "source": ""}
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"error": "审核模型配置格式错误"}
        return {
            "model_id": str(data.get("model_id") or "").strip(),
            "provider_key": str(data.get("provider_key") or "").strip(),
            "source": str(data.get("source") or "").strip(),
            "vision_model_id": str(data.get("vision_model_id") or "").strip(),
            "vision_provider_key": str(data.get("vision_provider_key") or "").strip(),
            "backup_model_id": str(data.get("backup_model_id") or "").strip(),
            "backup_provider_key": str(data.get("backup_provider_key") or "").strip(),
        }
    except (OSError, TypeError, ValueError):
        return {"model_id": "", "provider_key": "", "source": "", "error": "审核模型配置读取失败"}


def resolve_visual_evidence_client(registry, settings, *, usage_recorder=None, owner_binding=None):
    """Resolve an explicitly capable observer; never guess or silently pick a model."""
    from mewcode.permissions.review_evidence import ReviewEvidenceUnavailable

    model_id, provider_key = settings.get('vision_model_id'), settings.get('vision_provider_key')
    if model_id and provider_key:
        visual = registry.resolve(ModelRef(provider_key, model_id), usage_recorder=usage_recorder)
    elif owner_binding is not None:
        visual, ref = owner_binding
        if 'vision' not in visual.capabilities or getattr(visual.client, 'review_only', False) is True:
            raise ReviewEvidenceUnavailable()
        model_id, provider_key = ref.model_id, ref.provider_key
    else:
        raise ReviewEvidenceUnavailable()
    if 'vision' not in visual.capabilities or getattr(visual.client, 'review_only', False) is True:
        raise ReviewEvidenceUnavailable('看图能力模型必须支持图片，且不能使用 Jev 或 review_only 模型。')
    return visual.client, {'provider_key': provider_key, 'configured_model': model_id,
                           'model': model_id, 'supports_vision': True}


def resolve_safety_review_client(
    registry: ProviderRegistry | None,
    *,
    fallback_client: Any = None,
    usage_recorder: Callable[[Any], None] | None = None,
    settings_path: Path | None = None,
    settings: dict[str, str] | None = None,
    route_all: bool = False,
) -> Any:
    """Return the configured review-model client, or ``fallback_client``.

    Auto safety review must use the user-selected review model when available,
    not the primary/expert role model.
    """
    settings = load_review_model_settings(settings_path) if settings is None else settings
    if settings.get("error"):
        from mewcode.permissions.review_routing import ReviewRouter
        return ReviewRouter(None, error=settings["error"])
    model_id = settings.get("model_id") or ""
    provider_key = settings.get("provider_key") or ""
    if not model_id:
        return fallback_client
    if not provider_key or registry is None:
        from mewcode.permissions.review_routing import ReviewRouter
        return ReviewRouter(None, error="已选择评审模型但 Provider 不可用；请补齐配置，未使用执行模型替代。")
    try:
        resolved = registry.resolve(
            ModelRef(provider_key=provider_key, model_id=model_id),
            usage_recorder=usage_recorder,
        )
        log.info(
            "Safety review client bound to %s / %s",
            provider_key,
            model_id,
        )
        if route_all or getattr(resolved.client, "review_only", False) is True or settings.get("vision_model_id") or settings.get("backup_model_id"):
            from mewcode.permissions.review_routing import ReviewRouter
            vision_binding = None
            backup_binding = None
            def resolve_backup():
                nonlocal backup_binding
                if backup_binding is not None:
                    return backup_binding
                ref = ModelRef(settings.get("backup_provider_key", ""), settings.get("backup_model_id", ""))
                if (ref.provider_key, ref.model_id) == (provider_key, model_id):
                    raise ValueError("备用审查路由不能与主路由相同")
                other = registry.resolve(ref, usage_recorder=usage_recorder)
                backup_binding = other.client, {"provider_key": ref.provider_key,
                    "configured_model": ref.model_id, "model": ref.model_id,
                    'supports_vision': 'vision' in getattr(other, 'capabilities', ()) and getattr(other.client, 'review_only', False) is not True}
                return backup_binding
            def resolve_vision():
                nonlocal vision_binding
                if vision_binding is not None:
                    return vision_binding
                vision_binding = resolve_visual_evidence_client(registry, settings, usage_recorder=usage_recorder)
                return vision_binding
            visual_route = {"provider_key": settings.get("vision_provider_key", ""),
                            "configured_model": settings.get("vision_model_id", "")}
            visual_resolver = resolve_vision if settings.get("vision_model_id") and settings.get("vision_provider_key") else None
            # A selected general-purpose vision reviewer can review images itself.
            # Jev never falls back to a hard-coded model when no vision choice exists.
            return ReviewRouter(resolved.client,
                primary_route={"provider_key": provider_key, "configured_model": model_id},
                primary_supports_vision=('vision' in getattr(resolved, 'capabilities', ()) and getattr(resolved.client, 'review_only', False) is not True),
                vision_route=visual_route, vision_resolver=visual_resolver,
                fallback_resolver=(resolve_backup if settings.get("backup_model_id")
                                   and settings.get("backup_provider_key") else None))
        return resolved.client
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Unable to resolve review model %s/%s; review unavailable: %s",
            provider_key,
            model_id,
            exc,
        )
        from mewcode.permissions.review_routing import ReviewRouter
        return ReviewRouter(None, error=f"已配置的评审模型不可用：{provider_key}/{model_id}；请检查 Provider 配置，未切换到其他模型。")


def bind_safety_reviewer(agent: Any, registry: ProviderRegistry | None, *, settings_path: Path | None = None) -> None:
    """Resolve the latest user selection before EVERY review, including child agents.

    In-flight reviews retain their selected client; the next operation picks up
    changes. A denial is scoped to the selection, not shared across models.
    """
    tools = getattr(agent, 'registry', None)
    inspector = tools.get('InspectVisualEvidence') if callable(getattr(tools, 'get', None)) else None
    if inspector is not None:
        if not inspector.roots:
            inspector.roots = (str(Path(agent.work_dir).resolve()),)
        def resolve_inspector():
            settings = load_review_model_settings(settings_path)
            recorder = getattr(getattr(agent, 'client', None), 'get_usage_recorder', lambda: None)()
            return resolve_visual_evidence_client(registry, settings, usage_recorder=recorder)
        if not getattr(inspector, 'host_capability_binding', False):
            inspector.vision_resolver = resolve_inspector
    reviewer = getattr(agent, "safety_reviewer", None)
    if reviewer is None:
        return

    def resolve_current():
        from mewcode.permissions.review_routing import ReviewRouter
        settings = load_review_model_settings(settings_path)
        fallback = getattr(agent, "auxiliary_client", None) or getattr(agent, "client", None)
        recorder = getattr(getattr(agent, "client", None), "get_usage_recorder", lambda: None)()
        client = resolve_safety_review_client(registry, fallback_client=fallback,
            usage_recorder=recorder, settings_path=settings_path, settings=settings, route_all=True)
        route = {"provider_key": settings.get("provider_key", ""),
                 "configured_model": settings.get("model_id") or str(getattr(fallback, "model", ""))}
        if not isinstance(client, ReviewRouter):
            primary = client
            from mewcode.permissions.review_evidence import supports_vision
            vision_ok = supports_vision(primary)
            client = ReviewRouter(primary, primary_route=route, vision_route=route,
                                 primary_supports_vision=vision_ok)
        elif not client.primary_route:
            client.primary_route = route
        # No credentials in this key or in audit metadata.
        return client, json.dumps({**settings, "fallback_model": route["configured_model"]}, sort_keys=True)

    reviewer.client_resolver = resolve_current
