from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"


def httpx_timeout(seconds: float = 60.0) -> httpx.Timeout:
    return httpx.Timeout(seconds)


@asynccontextmanager
async def http_client(*, timeout: float = 60.0) -> AsyncIterator[httpx.AsyncClient]:
    """Outbound HTTP with env proxies (HTTP_PROXY / HTTPS_PROXY)."""
    from mewcode.permissions.network_policy import active_policy
    from mewcode.tools.network_transport import PolicyTransport
    scoped = active_policy() is not None
    async with httpx.AsyncClient(
        timeout=httpx_timeout(timeout),
        follow_redirects=True,
        trust_env=not scoped,
        transport=PolicyTransport() if scoped else None,
    ) as client:
        yield client


def is_http_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def responses_endpoint(base_url: str) -> str:
    base = (base_url or DEEPSEEK_DEFAULT_BASE).rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return f"{base}/responses"


def resolve_deepseek_search_auth(work_dir: str | None = None, providers=None) -> tuple[str, str, str]:
    """Return (api_key, base_url, model) for DeepSeek Responses search.

    Prefers dedicated env vars, then a DeepSeek-looking provider in config.
    """
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    env_base = (os.environ.get("DEEPSEEK_BASE_URL") or "").strip()
    env_model = (os.environ.get("DEEPSEEK_SEARCH_MODEL") or "").strip()
    if env_key:
        return (
            env_key,
            env_base or DEEPSEEK_DEFAULT_BASE,
            env_model or DEEPSEEK_DEFAULT_MODEL,
        )

    if providers is not None:
        candidates = [r for r in providers.records() if r.brand == "deepseek"]
        if len(candidates) == 1:
            config = candidates[0].provider
            key = config.resolve_api_key()
            if key:
                return key, env_base or config.base_url, env_model or config.model or DEEPSEEK_DEFAULT_MODEL
        # A live registry is authoritative: do not resurrect a deleted desktop
        # credential from a stale project YAML fallback.
        return "", DEEPSEEK_DEFAULT_BASE, DEEPSEEK_DEFAULT_MODEL
    for path in _config_candidates(work_dir):
        key, base, model = _provider_from_yaml(path)
        if key:
            return key, env_base or base or DEEPSEEK_DEFAULT_BASE, env_model or model or DEEPSEEK_DEFAULT_MODEL
    return "", DEEPSEEK_DEFAULT_BASE, DEEPSEEK_DEFAULT_MODEL


def resolve_tavily_auth(work_dir: str | None = None) -> str:
    """Dedicated search credentials; never reuse a chat provider's API key."""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if key:
        return key
    for path in _tavily_config_candidates(work_dir):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            search = data.get("search") or {}
            config = search.get("tavily") or {}
            if config.get("enabled", True) is False:
                return ""
            key = str(config.get("api_key") or "").strip()
            if key.startswith("${") and key.endswith("}"):
                key = os.environ.get(key[2:-1], "")
            if key:
                return key
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            continue
    return ""


def _tavily_config_candidates(work_dir: str | None) -> list[Path]:
    # Keep user search secrets independent of the main config, whose schema
    # requires chat providers. Read on every call so key rotation is immediate.
    paths: list[Path] = []
    if work_dir:
        paths.extend(Path(work_dir) / ".mewcode" / name for name in ("config.local.yaml", "config.yaml"))
    paths.extend(Path.home() / ".mewcode" / name for name in ("search.yaml", "config.yaml"))
    return paths


def _config_candidates(work_dir: str | None) -> list[Path]:
    paths: list[Path] = []
    if work_dir:
        paths.append(Path(work_dir) / ".mewcode" / "config.yaml")
    paths.append(Path.home() / ".mewcode" / "config.yaml")
    return paths


def _provider_from_yaml(path: Path) -> tuple[str, str, str]:
    if not path.is_file():
        return "", "", ""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return "", "", ""
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, list):
        return "", "", ""
    for item in providers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        base = str(item.get("base_url") or "")
        model = str(item.get("model") or "")
        blob = f"{name} {base} {model}".lower()
        if "deepseek" not in blob:
            continue
        key = str(item.get("api_key") or "").strip()
        if key.startswith("${") and key.endswith("}"):
            key = os.environ.get(key[2:-1], "")
        from mewcode.config import resolve_env_vars

        if key:
            key = resolve_env_vars(key)
        if not key:
            key = os.environ.get("DEEPSEEK_API_KEY", "")
        if key:
            return key, base, model
    return "", "", ""


def format_search_output(payload: Any) -> str:
    """Flatten a DeepSeek/OpenAI Responses body into titles, URLs, and text."""
    if not isinstance(payload, dict):
        return str(payload)
    lines: list[str] = []
    citations: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            query = action.get("query") or item.get("query")
            if query:
                lines.append(f"Query: {query}")
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            text = str(part.get("text") or "").strip()
            if text:
                lines.append(text)
            for note in part.get("annotations") or []:
                if not isinstance(note, dict):
                    continue
                url = str(note.get("url") or "").strip()
                title = str(note.get("title") or url).strip()
                if url:
                    citations.append(f"- {title}: {url}")
    summary = str(payload.get("output_text") or "").strip()
    if summary and summary not in "\n".join(lines):
        lines.insert(0, summary)
    if citations:
        lines.append("")
        lines.append("Sources:")
        lines.extend(dict.fromkeys(citations))
    return "\n".join(lines).strip() or "No search results returned."
