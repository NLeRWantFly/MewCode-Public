"""Read-only snapshot of the local cc-switch provider database."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
)
_TOML_MODEL_RE = re.compile(r'(?im)^\s*model\s*=\s*["\']([^"\']+)["\']')


def cc_switch_db_paths() -> list[Path]:
    paths: list[Path] = []
    if os.environ.get("CC_SWITCH_HOME"):
        paths.append(Path(os.environ["CC_SWITCH_HOME"]) / "cc-switch.db")
    paths.append(Path.home() / ".cc-switch" / "cc-switch.db")
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        paths.append(Path(appdata) / "cc-switch" / "cc-switch.db")
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in {"", "."} or key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _models_from_settings(settings: dict[str, Any]) -> list[str]:
    found: list[str] = []
    model = settings.get("model")
    if isinstance(model, str) and model.strip():
        found.append(model.strip())
    for key in ("models", "model_list", "available_models", "modelList"):
        value = settings.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    found.append(item.strip())
                elif isinstance(item, dict):
                    mid = item.get("id") or item.get("model") or item.get("model_id")
                    if isinstance(mid, str) and mid.strip():
                        found.append(mid.strip())
    env = settings.get("env")
    if isinstance(env, dict):
        for key in _MODEL_ENV_KEYS:
            value = env.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
        for key, value in env.items():
            if "MODEL" in str(key).upper() and isinstance(value, str) and value.strip():
                found.append(value.strip())
    config = settings.get("config")
    if isinstance(config, str) and config.strip():
        for match in _TOML_MODEL_RE.finditer(config):
            found.append(match.group(1).strip())
    # de-dupe preserve order
    out: list[str] = []
    seen: set[str] = set()
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _usage_script(meta: dict[str, Any]) -> dict[str, Any] | None:
    script = meta.get("usage_script") if isinstance(meta, dict) else None
    if not isinstance(script, dict):
        return None
    return {
        "enabled": bool(script.get("enabled")),
        "language": str(script.get("language") or ""),
        "template_type": str(script.get("templateType") or script.get("template_type") or ""),
        "has_code": bool(str(script.get("code") or "").strip()),
    }


def _hosts_from_settings(settings: dict[str, Any]) -> list[str]:
    hosts: list[str] = []
    candidates: list[str] = []
    for key in ("baseUrl", "base_url", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    env = settings.get("env")
    if isinstance(env, dict):
        for key, value in env.items():
            if not isinstance(value, str) or not value.strip():
                continue
            upper = str(key).upper()
            if "BASE_URL" in upper or upper.endswith("_URL"):
                candidates.append(value.strip())
    for raw in candidates:
        try:
            host = (urlsplit(raw).hostname or "").lower()
        except Exception:
            host = ""
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _newapi_creds(settings: dict[str, Any], meta: dict[str, Any]) -> dict[str, str]:
    """Extract NewAPI probe credentials when present (for server-side use only)."""
    access_token = ""
    user_id = ""
    script = meta.get("usage_script") if isinstance(meta, dict) else None
    bags: list[dict[str, Any]] = []
    if isinstance(script, dict):
        for key in ("variables", "vars", "env"):
            bag = script.get(key)
            if isinstance(bag, dict):
                bags.append(bag)
    bags.append(settings)
    env = settings.get("env")
    if isinstance(env, dict):
        bags.append(env)
    for bag in bags:
        if not access_token:
            for key in ("accessToken", "access_token", "NEW_API_TOKEN", "newapi_token"):
                value = bag.get(key)
                if isinstance(value, str) and value.strip():
                    access_token = value.strip()
                    break
        if not user_id:
            for key in ("userId", "user_id", "NEW_API_USER", "newapi_user"):
                value = bag.get(key)
                if isinstance(value, str) and str(value).strip():
                    user_id = str(value).strip()
                    break
    return {"access_token": access_token, "user_id": user_id}


def read_cc_switch_snapshot() -> dict[str, Any]:
    """Return current providers/models from cc-switch without writing."""
    db_path = next((path for path in cc_switch_db_paths() if path.is_file()), None)
    if db_path is None:
        return {
            "available": False,
            "db_path": "",
            "current": [],
            "providers": [],
            "error": "cc-switch database not found",
        }
    try:
        connection = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT id, app_type, name, settings_config, meta, is_current, provider_type "
                "FROM providers ORDER BY sort_index, id"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        return {
            "available": False,
            "db_path": str(db_path),
            "current": [],
            "providers": [],
            "error": str(exc),
        }
    providers: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for row in rows:
        provider_id, app_type, name, settings_config, meta_raw, is_current, provider_type = row
        try:
            settings = json.loads(settings_config or "{}")
        except (TypeError, ValueError):
            settings = {}
        try:
            meta = json.loads(meta_raw or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(settings, dict):
            settings = {}
        if not isinstance(meta, dict):
            meta = {}
        models = _models_from_settings(settings)
        entry = {
            "id": provider_id,
            "app_type": str(app_type or ""),
            "name": str(name or ""),
            "provider_type": str(provider_type or ""),
            "is_current": bool(is_current),
            "models": models,
            "selected_model": models[0] if models else "",
            "usage_script": _usage_script(meta),
            "hosts": _hosts_from_settings(settings),
        }
        providers.append(entry)
        if entry["is_current"]:
            current.append(entry)
    return {
        "available": True,
        "db_path": str(db_path),
        "current": current,
        "providers": providers,
        "error": None,
    }


def read_cc_switch_probe_index() -> dict[str, dict[str, str]]:
    """Host → NewAPI probe credentials for server-side relay meters only."""
    db_path = next((path for path in cc_switch_db_paths() if path.is_file()), None)
    if db_path is None:
        return {}
    try:
        connection = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT name, settings_config, meta FROM providers"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return {}
    index: dict[str, dict[str, str]] = {}
    for name, settings_config, meta_raw in rows:
        try:
            settings = json.loads(settings_config or "{}")
        except (TypeError, ValueError):
            settings = {}
        try:
            meta = json.loads(meta_raw or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(settings, dict):
            settings = {}
        if not isinstance(meta, dict):
            meta = {}
        creds = _newapi_creds(settings, meta)
        if not creds["access_token"] and not creds["user_id"]:
            # Still index by host so name matching works for display hints.
            pass
        for host in _hosts_from_settings(settings):
            index[host] = {
                "name": str(name or ""),
                "access_token": creds["access_token"],
                "user_id": creds["user_id"],
            }
    return index


def _toml_assign(config: str, key: str) -> str:
    if not config:
        return ""
    pattern = re.compile(
        rf'(?im)^\s*{re.escape(key)}\s*=\s*["\']([^"\']+)["\']'
    )
    match = pattern.search(config)
    return match.group(1).strip() if match else ""


def _binding_models(settings: dict[str, Any], selected: str = "") -> list[str]:
    """Preserve cc-switch configured model list order, with selected first."""
    models = _models_from_settings(settings)
    selected = (selected or "").strip()
    if selected and selected not in models:
        models = [selected, *models]
    elif selected and selected in models:
        models = [selected, *[item for item in models if item != selected]]
    return models


def _runtime_from_settings(
    *,
    app_type: str,
    name: str,
    settings: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract a usable in-memory provider binding from one cc-switch row."""
    app = (app_type or "").strip().lower()
    label = str(name or app or "cc-switch").strip()
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    auth = settings.get("auth") if isinstance(settings.get("auth"), dict) else {}
    config_blob = settings.get("config") if isinstance(settings.get("config"), str) else ""

    if app in {"claude", "claude-desktop", "claude-code"}:
        api_key = str(
            env.get("ANTHROPIC_AUTH_TOKEN")
            or env.get("ANTHROPIC_API_KEY")
            or settings.get("ANTHROPIC_AUTH_TOKEN")
            or settings.get("ANTHROPIC_API_KEY")
            or ""
        ).strip()
        base_url = str(
            env.get("ANTHROPIC_BASE_URL")
            or settings.get("ANTHROPIC_BASE_URL")
            or ""
        ).strip()
        model = str(
            settings.get("model")
            or env.get("ANTHROPIC_MODEL")
            or ""
        ).strip()
        if not api_key:
            return None
        models = _binding_models(settings, model)
        if not models:
            return None
        selected = model or models[0]
        return {
            "app_type": app,
            "name": label,
            "protocol": "anthropic",
            "base_url": base_url or "https://api.anthropic.com",
            "model": selected,
            "models": models or [selected],
            "api_key": api_key,
            "slot_name": f"cc-switch-{app}",
        }

    if app in {"codex", "openai"}:
        api_key = str(
            auth.get("OPENAI_API_KEY")
            or env.get("OPENAI_API_KEY")
            or settings.get("OPENAI_API_KEY")
            or ""
        ).strip()
        # OAuth tokens in auth.tokens are ChatGPT login — skip for custom relays.
        tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
        if not api_key and isinstance(tokens.get("access_token"), str):
            # Only treat as API key when auth_mode is not chatgpt subscription.
            if str(auth.get("auth_mode") or "").lower() not in {"chatgpt", "oauth"}:
                api_key = str(tokens.get("access_token") or "").strip()
        base_url = _toml_assign(config_blob, "base_url") or str(
            env.get("OPENAI_BASE_URL") or ""
        ).strip()
        model = (
            _toml_assign(config_blob, "model")
            or str(settings.get("model") or "").strip()
        )
        wire = (_toml_assign(config_blob, "wire_api") or "chat").lower()
        protocol = "openai" if wire == "responses" else "openai-compat"
        if not api_key:
            return None
        models = _binding_models(settings, model)
        selected = model or (models[0] if models else "gpt-4o")
        return {
            "app_type": app,
            "name": label,
            "protocol": protocol,
            "base_url": base_url or "https://api.openai.com/v1",
            "model": selected,
            "models": models or [selected],
            "api_key": api_key,
            "slot_name": f"cc-switch-{app}",
        }

    if app in {"grokbuild", "grok", "xai"}:
        api_key = str(
            env.get("XAI_API_KEY")
            or env.get("OPENAI_API_KEY")
            or auth.get("OPENAI_API_KEY")
            or ""
        ).strip()
        base_url = str(
            env.get("XAI_BASE_URL")
            or env.get("OPENAI_BASE_URL")
            or _toml_assign(config_blob, "base_url")
            or "https://api.x.ai/v1"
        ).strip()
        model = str(
            settings.get("model")
            or _toml_assign(config_blob, "model")
            or "grok-4.6"
        ).strip()
        if not api_key:
            return None
        models = _binding_models(settings, model)
        selected = model or (models[0] if models else "grok-4.6")
        return {
            "app_type": app,
            "name": label,
            "protocol": "openai-compat",
            "base_url": base_url,
            "model": selected,
            "models": models or [selected],
            "api_key": api_key,
            "slot_name": f"cc-switch-{app}",
        }
    return None


def read_cc_switch_runtime_bindings() -> list[dict[str, Any]]:
    """Current cc-switch providers that carry usable API credentials (server-only).

    Never include these secrets in public JSON snapshots. Used to silently
    materialize MewCode provider slots so Codex/Claude-style auto switch works.
    """
    db_path = next((path for path in cc_switch_db_paths() if path.is_file()), None)
    if db_path is None:
        return []
    try:
        connection = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT app_type, name, settings_config, is_current "
                "FROM providers WHERE is_current = 1 ORDER BY sort_index, id"
            ).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return []
    bindings: list[dict[str, Any]] = []
    for app_type, name, settings_config, _is_current in rows:
        try:
            settings = json.loads(settings_config or "{}")
        except (TypeError, ValueError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        binding = _runtime_from_settings(
            app_type=str(app_type or ""),
            name=str(name or ""),
            settings=settings,
        )
        if binding is None:
            continue
        fingerprint = "|".join([
            binding["app_type"],
            binding["name"],
            binding["protocol"],
            binding["base_url"],
            binding["model"],
            str(len(binding["api_key"])),
            binding["api_key"][:4] + binding["api_key"][-4:],
        ])
        binding["fingerprint"] = fingerprint
        bindings.append(binding)
    return bindings


def preferred_cc_switch_binding(
    bindings: list[dict[str, str]] | None = None,
) -> dict[str, str] | None:
    """Pick the binding MewCode should silently follow."""
    items = bindings if bindings is not None else read_cc_switch_runtime_bindings()
    if not items:
        return None
    priority = (
        "claude",
        "claude-desktop",
        "codex",
        "grokbuild",
        "grok",
        "xai",
        "openai",
    )
    by_app = {item["app_type"]: item for item in items}
    for app in priority:
        if app in by_app:
            return by_app[app]
    return items[0]


def cc_switch_family(app_type: str) -> str:
    app = (app_type or "").strip().lower()
    if app in {"claude", "claude-desktop", "claude-code"}:
        return "claude"
    if app in {"codex", "openai"}:
        return "codex"
    if app in {"grok", "grokbuild", "xai"}:
        return "grok"
    return app


def cc_switch_bindings_fingerprint(
    bindings: list[dict[str, str]] | None = None,
) -> str:
    items = bindings if bindings is not None else read_cc_switch_runtime_bindings()
    return "||".join(sorted(item.get("fingerprint") or "" for item in items))


def select_cc_switch_mirror_target(
    bindings: list[dict[str, str]],
    *,
    current_family: str,
    current_valid: bool,
    previous_fp: str,
    combined_fp: str,
) -> dict[str, str] | None:
    """Decide which cc-switch current binding MewCode should follow.

    Follow Claude/Codex family slots the way cc-switch auto-switches those
    apps. Do not steal an unrelated, still-valid MewCode provider (for
    example a desktop DeepSeek profile) when cc-switch changes.
    """
    if not bindings:
        return None
    by_family: dict[str, dict[str, str]] = {}
    for item in bindings:
        family = cc_switch_family(item.get("app_type") or "")
        if family and family not in by_family:
            by_family[family] = item
    family_binding = by_family.get(current_family) if current_family else None

    if combined_fp == previous_fp:
        if current_valid:
            return None
        return family_binding or preferred_cc_switch_binding(bindings)

    if family_binding is not None:
        return family_binding
    if not previous_fp:
        if current_valid:
            return None
        return preferred_cc_switch_binding(bindings)
    if not current_valid:
        return preferred_cc_switch_binding(bindings)
    return None
