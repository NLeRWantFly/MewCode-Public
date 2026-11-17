"""插槽式 Provider 配置源发现。

除 .mewcode/config.yaml 中显式声明的 providers 外，还从以下外部配置源
发现可切换的 LLM Provider"插槽"：

  1. cc-switch 当前（静默注入密钥，并回填 Claude Code / Codex 插槽）
  2. 本地 codex 配置（~/.codex/config.toml + ~/.codex/auth.json）
  3. Claude Code 配置（~/.claude/settings.json 及项目 .claude/settings*.json）
  4. 项目目录环境配置（<work_dir>/.env）

每个插槽（ProviderSlot）携带一个标准化后的 ProviderConfig 以及来源标识，
由 /provider 命令列出并在校验通过后热切换。发现过程完全尽力而为：
任何一个配置源解析失败都只会跳过该源，绝不抛出异常。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mewcode.config import ProviderConfig, resolve_env_vars
from mewcode.codex_credentials import is_codex_login_endpoint
from mewcode.validator import VALID_PROTOCOLS, VALID_PROVIDER_TYPES

# 各配置源缺省值。Anthropic 模型必须由来源明确声明，不能从密钥/端点推定。
_DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
_DEFAULT_OPENAI_MODEL = "gpt-4o"
_DEFAULT_XAI_MODEL = "grok-4.6"
_GROK_46_EFFORTS = ["low", "medium", "high", "xhigh"]
_CODEX_OAUTH_BASE_URL = "https://chatgpt.com/backend-api/codex"

# 来源标识 -> 展示名
SOURCE_LABELS = {
    "config": "mewcode 配置",
    "codex": "codex 配置",
    "claude-code": "Claude Code 配置",
    "project-env": "项目 .env",
    "cc-switch": "cc-switch 当前",
}


@dataclass
class ProviderSlot:
    provider: ProviderConfig
    source: str  # config / codex / claude-code / project-env
    origin: str = ""  # 来源文件路径（展示用）
    # 来源配置显式声明的 reasoning effort 档位与默认档（目前仅 codex
    # config.toml 提供：desktop.enabled-reasoning-efforts 与
    # model_reasoning_effort）。空表示未声明，回退到模型能力识别。
    effort_levels: list[str] = field(default_factory=list)
    default_effort: str = ""
    # A discovered provider may expose more than its configured default model.
    # Codex keeps this information (including per-model effort levels) in its
    # local models cache, even when the remote /models endpoint only returns the
    # currently selected model.
    models: list[str] = field(default_factory=list)
    model_efforts: dict[str, list[str]] = field(default_factory=dict)
    # Desktop providers are registered under an opaque, stable key.  Keep it
    # instead of rebuilding a key from the editable display name.
    registry_key: str = ""
    # A provider-specific credential must not be replaced by cc-switch's
    # current Codex binding merely because both services share a host.
    credentials_explicit: bool = False

    @property
    def key(self) -> str:
        """唯一标识，用于 /provider <key> 直接切换。"""
        return self.registry_key or f"{self.source}:{self.provider.name}"

    @property
    def label(self) -> str:
        source_name = SOURCE_LABELS.get(self.source, self.source)
        return f"{self.provider.name}  [{self.provider.model}]  ({source_name})"


def validate_slot(slot: ProviderSlot) -> str | None:
    """静态校验插槽配置的有效性，返回错误信息；有效则返回 None。

    在应用切换之前调用——协议、端点、模型名与可解析的 API key 缺一不可。
    真正的连通性校验由 create_client 及首次请求兜底。
    """
    p = slot.provider
    if p.provider_type not in VALID_PROVIDER_TYPES:
        return (
            f"Invalid provider type: {p.provider_type} "
            f"(choose from {', '.join(sorted(VALID_PROVIDER_TYPES))})"
        )
    if p.protocol not in VALID_PROTOCOLS:
        return f"无效协议: {p.protocol}（可选: {', '.join(sorted(VALID_PROTOCOLS))}）"
    if not p.base_url or not p.base_url.startswith(("http://", "https://")):
        return f"无效 base_url: {p.base_url!r}"
    if not p.model:
        return "缺少模型名称（model）"
    if not p.resolve_api_key():
        return (
            f"无法解析 API key：{slot.origin or slot.source} 未提供密钥，"
            "且对应环境变量未设置"
        )
    return None


# ---------------------------------------------------------------------------
# 配置源 1：本地 codex 配置
# ---------------------------------------------------------------------------

def _load_codex_model_catalog(
    home: Path,
) -> tuple[list[str], dict[str, list[str]]]:
    """Read API-capable models and effort levels from Codex's local cache."""
    path = home / ".codex" / "models_cache.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return [], {}
    if not isinstance(payload, dict):
        return [], {}

    entries: list[tuple[int, str, list[str]]] = []
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("slug") or "").strip()
        if (
            not model_id
            or item.get("visibility") != "list"
            or item.get("supported_in_api") is not True
        ):
            continue
        efforts = [
            str(level.get("effort")).strip()
            for level in item.get("supported_reasoning_levels", [])
            if isinstance(level, dict) and str(level.get("effort") or "").strip()
        ]
        try:
            priority = int(item.get("priority") or 999)
        except (TypeError, ValueError):
            priority = 999
        entries.append((priority, model_id, list(dict.fromkeys(efforts))))

    entries.sort(key=lambda entry: (entry[0], entry[1]))
    models = list(dict.fromkeys(entry[1] for entry in entries))
    model_efforts = {model_id: efforts for _, model_id, efforts in entries}
    return models, model_efforts

def _load_codex_slots(home: Path) -> list[ProviderSlot]:
    """解析 ~/.codex/config.toml 中的 model_providers。

    wire_api=responses 映射到 Responses API；requires_openai_auth=true 时
    直接使用 codex login 写入 auth.json 的 ChatGPT OAuth access token。
    凭据只在运行时读入内存，不写回 MewCode 配置。
    """
    config_path = home / ".codex" / "config.toml"
    if not config_path.exists():
        return []

    try:
        import tomllib
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    # auth.json 同时兼容 API key 登录与 ChatGPT OAuth 登录。
    auth_key = ""
    oauth_token = ""
    account_id = ""
    auth_path = home / ".codex" / "auth.json"
    if auth_path.exists():
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
            auth_key = auth.get("OPENAI_API_KEY") or ""
            tokens = auth.get("tokens") or {}
            if isinstance(tokens, dict):
                oauth_token = tokens.get("access_token") or ""
                account_id = tokens.get("account_id") or ""
        except Exception:
            auth_key = ""
            oauth_token = ""
            account_id = ""

    default_model = raw.get("model") or _DEFAULT_OPENAI_MODEL
    service_tier = raw.get("service_tier") or ""
    if not isinstance(service_tier, str):
        service_tier = ""
    model_providers = raw.get("model_providers") or {}
    codex_models, codex_model_efforts = _load_codex_model_catalog(home)

    # effort 相关字段：顶层 model_reasoning_effort 为默认档，
    # [desktop] enabled-reasoning-efforts 为可选档位列表。
    default_effort = raw.get("model_reasoning_effort") or ""
    if not isinstance(default_effort, str):
        default_effort = ""
    desktop = raw.get("desktop") or {}
    enabled_efforts = desktop.get("enabled-reasoning-efforts") if isinstance(desktop, dict) else None
    effort_levels = (
        [str(x) for x in enabled_efforts if isinstance(x, str) and x]
        if isinstance(enabled_efforts, list)
        else []
    )

    slots: list[ProviderSlot] = []
    if isinstance(model_providers, dict):
        for pid, entry in model_providers.items():
            if not isinstance(entry, dict):
                continue
            requires_oauth = bool(entry.get("requires_openai_auth", False))
            wire_api = str(entry.get("wire_api") or "chat").lower()
            protocol = "openai" if wire_api == "responses" else "openai-compat"
            base_url = entry.get("base_url") or (
                _CODEX_OAUTH_BASE_URL
                if requires_oauth
                else _DEFAULT_OPENAI_BASE_URL
            )
            env_key = entry.get("env_key") or ""
            api_key = os.environ.get(env_key, "") if env_key else ""
            bearer = entry.get("experimental_bearer_token")
            if not api_key and isinstance(bearer, str):
                api_key = bearer.strip()
            credentials_explicit = bool(api_key)
            if not api_key:
                api_key = oauth_token if requires_oauth else auth_key
            headers = {}
            if requires_oauth and account_id and not credentials_explicit:
                headers["ChatGPT-Account-Id"] = account_id
            configured_model = entry.get("model") or default_model
            magpie = str(pid).casefold() == "magpie" or str(entry.get("name") or "").casefold() == "magpie"
            if magpie and not entry.get("model") and "/" not in configured_model:
                configured_model = "codex/" + configured_model
            entry_efforts = entry.get("enabled-reasoning-efforts")
            provider_efforts = (
                [str(x) for x in entry_efforts if isinstance(x, str) and x]
                if isinstance(entry_efforts, list)
                else list(effort_levels)
            )
            discovered_models = list(dict.fromkeys([configured_model, *([] if magpie else codex_models)]))
            discovered_efforts = {
                ("codex/" + model if magpie and "/" not in model else model): list(levels)
                for model, levels in codex_model_efforts.items()
            }
            if provider_efforts:
                discovered_efforts[configured_model] = list(provider_efforts)
            slots.append(ProviderSlot(
                provider=ProviderConfig(
                    name=f"codex-{pid}",
                    protocol=protocol,
                    base_url=base_url,
                    model=configured_model,
                    api_key=api_key,
                    service_tier=service_tier,
                    default_headers=headers,
                    _codex_auth_path=(str(auth_path) if requires_oauth and not credentials_explicit and api_key == oauth_token
                                      and account_id and is_codex_login_endpoint(base_url) else ""),
                ),
                source="codex",
                origin=str(config_path),
                effort_levels=provider_efforts,
                default_effort=default_effort,
                models=discovered_models,
                model_efforts=discovered_efforts,
                credentials_explicit=credentials_explicit,
            ))

    # 没有显式 model_providers 但存在凭据时，暴露一个默认 OpenAI 插槽
    if not slots and (
        oauth_token or auth_key or os.environ.get("OPENAI_API_KEY")
    ):
        api_key = oauth_token or auth_key
        headers = (
            {"ChatGPT-Account-Id": account_id}
            if oauth_token and account_id
            else {}
        )
        slots.append(ProviderSlot(
            provider=ProviderConfig(
                name="codex-openai",
                protocol="openai",
                base_url=(
                    _CODEX_OAUTH_BASE_URL
                    if oauth_token
                    else _DEFAULT_OPENAI_BASE_URL
                ),
                model=default_model,
                api_key=api_key,
                service_tier=service_tier,
                default_headers=headers,
                _codex_auth_path=str(auth_path) if oauth_token and account_id else "",
            ),
            source="codex",
            origin=str(config_path),
            effort_levels=effort_levels,
            default_effort=default_effort,
            models=list(dict.fromkeys([default_model, *codex_models])),
            model_efforts={
                model: list(levels) for model, levels in codex_model_efforts.items()
            },
        ))
    return slots


# ---------------------------------------------------------------------------
# 配置源 2：Claude Code 配置
# ---------------------------------------------------------------------------

def _load_claude_code_slots(home: Path, work_dir: Path) -> list[ProviderSlot]:
    """解析 Claude Code 的 settings.json（全局 + 项目级）。

    读取 env 块中的 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN /
    ANTHROPIC_API_KEY / ANTHROPIC_MODEL，映射为 anthropic 协议 provider。
    项目级配置覆盖全局配置（后加载的候选覆盖同名字段）。
    """
    candidates = [
        home / ".claude" / "settings.json",
        work_dir / ".claude" / "settings.json",
        work_dir / ".claude" / "settings.local.json",
    ]

    merged_env: dict[str, str] = {}
    merged_model = ""
    origin = ""
    for path in candidates:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        env = raw.get("env")
        if isinstance(env, dict):
            merged_env.update({k: str(v) for k, v in env.items()})
        if raw.get("model"):
            merged_model = str(raw["model"])
        origin = str(path)

    if not origin:
        return []

    api_key = (
        merged_env.get("ANTHROPIC_API_KEY")
        or merged_env.get("ANTHROPIC_AUTH_TOKEN")
        or ""
    )
    model = (
        merged_env.get("ANTHROPIC_MODEL")
        or merged_model
        or ""
    )
    model = model.strip()
    if not model:
        return []
    return [ProviderSlot(
        provider=ProviderConfig(
            name="claude-code",
            protocol="anthropic",
            base_url=merged_env.get("ANTHROPIC_BASE_URL") or _DEFAULT_ANTHROPIC_BASE_URL,
            model=model,
            # 留空则由 resolve_api_key() 回退到 ANTHROPIC_API_KEY 环境变量
            api_key=resolve_env_vars(api_key) if api_key else "",
        ),
        source="claude-code",
        origin=origin,
    )]


# ---------------------------------------------------------------------------
# 配置源 3：项目目录环境配置（.env）
# ---------------------------------------------------------------------------

def _parse_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE 行，支持 export 前缀、引号与 # 注释。"""
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def _load_project_env_slots(work_dir: Path) -> list[ProviderSlot]:
    """从项目 .env 构建插槽：按存在的密钥各生成一个 provider。"""
    env_path = work_dir / ".env"
    if not env_path.exists():
        return []
    env = _parse_dotenv(env_path)
    origin = str(env_path)

    slots: list[ProviderSlot] = []
    anthropic_model = str(env.get("ANTHROPIC_MODEL") or "").strip()
    if env.get("ANTHROPIC_API_KEY") and anthropic_model:
        slots.append(ProviderSlot(
            provider=ProviderConfig(
                name="env-anthropic",
                protocol="anthropic",
                base_url=env.get("ANTHROPIC_BASE_URL") or _DEFAULT_ANTHROPIC_BASE_URL,
                model=anthropic_model,
                api_key=env["ANTHROPIC_API_KEY"],
            ),
            source="project-env",
            origin=origin,
        ))
    if env.get("OPENAI_API_KEY"):
        base_url = env.get("OPENAI_BASE_URL") or _DEFAULT_OPENAI_BASE_URL
        slots.append(ProviderSlot(
            provider=ProviderConfig(
                name="env-openai",
                protocol="openai-compat",
                base_url=base_url,
                model=env.get("OPENAI_MODEL") or _DEFAULT_OPENAI_MODEL,
                api_key=env["OPENAI_API_KEY"],
            ),
            source="project-env",
            origin=origin,
        ))
    if env.get("XAI_API_KEY"):
        model = env.get("XAI_MODEL") or _DEFAULT_XAI_MODEL
        slots.append(ProviderSlot(
            provider=ProviderConfig(
                name="env-grok",
                protocol="openai",
                base_url=env.get("XAI_BASE_URL") or _DEFAULT_XAI_BASE_URL,
                model=model,
                api_key=env["XAI_API_KEY"],
            ),
            source="project-env",
            origin=origin,
            effort_levels=list(_GROK_46_EFFORTS),
            default_effort="high",
            models=[model],
            model_efforts={model: list(_GROK_46_EFFORTS)},
        ))
    return slots


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _host_of(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except Exception:
        return ""


def _codex_slot_is_oauth(slot: ProviderSlot) -> bool:
    url = (slot.provider.base_url or "").lower()
    if "chatgpt.com" in url:
        return True
    headers = slot.provider.default_headers or {}
    return any("chatgpt" in str(key).lower() for key in headers)


def _slot_uses_codex_catalog(slot: ProviderSlot) -> bool:
    if slot.provider.name.casefold() in {"magpie", "codex-magpie"}:
        return False
    if slot.source == "codex":
        return True
    origin = slot.origin or ""
    return slot.source == "cc-switch" and origin.startswith("cc-switch:codex:")


def _apply_codex_model_catalog(
    slot: ProviderSlot,
    catalog_models: list[str],
    catalog_efforts: dict[str, list[str]],
) -> None:
    configured = str(slot.provider.model or "").strip()
    slot.models = list(dict.fromkeys([
        *([configured] if configured else []),
        *slot.models,
        *catalog_models,
    ]))
    merged = {model: list(levels) for model, levels in catalog_efforts.items()}
    for model, levels in slot.model_efforts.items():
        if levels:
            merged[model] = list(levels)
    if configured and slot.effort_levels:
        merged[configured] = list(slot.effort_levels)
    slot.model_efforts = merged


def _load_cc_switch_bindings(
    explicit: list[dict[str, str]] | None,
    *,
    enabled: bool,
) -> list[dict[str, str]]:
    if explicit is not None:
        return explicit
    if not enabled:
        return []
    try:
        from mewcode.cc_switch import read_cc_switch_runtime_bindings
        return read_cc_switch_runtime_bindings()
    except Exception:
        return []


def _models_from_binding(binding: dict[str, Any]) -> list[str]:
    raw = binding.get("models")
    models: list[str] = []
    if isinstance(raw, list):
        models = [str(item).strip() for item in raw if str(item).strip()]
    selected = str(binding.get("model") or "").strip()
    if selected and selected not in models:
        models = [selected, *models]
    elif selected and selected in models:
        models = [selected, *[item for item in models if item != selected]]
    return models


def _cc_switch_slots(bindings: list[dict[str, Any]]) -> list[ProviderSlot]:
    """Materialize silent slots from cc-switch current providers that have keys."""
    slots: list[ProviderSlot] = []
    for binding in bindings:
        try:
            models = _models_from_binding(binding)
            app_type = str(binding.get("app_type") or "").strip().lower()
            grok_efforts = {
                model: list(_GROK_46_EFFORTS)
                for model in models
                if model.lower().startswith("grok-4.6")
            }
            selected = str(binding.get("model") or (models[0] if models else ""))
            slots.append(ProviderSlot(
                provider=ProviderConfig(
                    name=str(binding.get("slot_name") or binding.get("name") or "cc-switch"),
                    protocol=str(binding.get("protocol") or "openai-compat"),
                    base_url=str(binding.get("base_url") or ""),
                    model=selected,
                    api_key=str(binding.get("api_key") or ""),
                ),
                source="cc-switch",
                origin=f"cc-switch:{binding.get('app_type')}:{binding.get('name')}",
                effort_levels=(
                    list(_GROK_46_EFFORTS)
                    if app_type in {"grok", "grokbuild", "xai"}
                    and selected.lower().startswith("grok-4.6")
                    else []
                ),
                default_effort=(
                    "high"
                    if app_type in {"grok", "grokbuild", "xai"}
                    and selected.lower().startswith("grok-4.6")
                    else ""
                ),
                models=models,
                model_efforts=grok_efforts,
            ))
        except Exception:
            continue
    return slots


def _apply_binding(slot: ProviderSlot, binding: dict[str, Any]) -> None:
    slot.provider.api_key = binding["api_key"]
    if binding.get("base_url"):
        slot.provider.base_url = binding["base_url"]
    if binding.get("model"):
        slot.provider.model = binding["model"]
    if binding.get("protocol"):
        slot.provider.protocol = binding["protocol"]
    models = _models_from_binding(binding)
    if models:
        slot.models = models
    label = binding.get("name") or binding.get("app_type") or "cc-switch"
    if slot.origin and "cc-switch" not in slot.origin:
        slot.origin = f"{slot.origin} + cc-switch:{label}"


def _enrich_slots_from_cc_switch(
    slots: list[ProviderSlot],
    bindings: list[dict[str, Any]],
) -> None:
    """Fill Claude Code / Codex discovered slots from matching cc-switch current."""
    if not bindings:
        return
    by_app = {item["app_type"]: item for item in bindings}
    claude_binding = by_app.get("claude") or by_app.get("claude-desktop")
    codex_binding = by_app.get("codex")
    for slot in slots:
        if slot.source == "claude-code" and claude_binding:
            _apply_binding(slot, claude_binding)
    if not codex_binding:
        return
    bind_host = _host_of(codex_binding.get("base_url") or "")
    custom = [
        slot for slot in slots
        if slot.source == "codex" and not _codex_slot_is_oauth(slot)
        and not slot.credentials_explicit
    ]
    matched = [
        slot for slot in custom
        if bind_host and _host_of(slot.provider.base_url) == bind_host
    ]
    targets = matched or custom
    for slot in targets:
        _apply_binding(slot, codex_binding)


def discover_provider_slots(
    config_providers: list[ProviderConfig],
    work_dir: str | Path = ".",
    home: str | Path | None = None,
    cc_switch_bindings: list[dict[str, Any]] | None = None,
) -> list[ProviderSlot]:
    """汇总所有配置源的 provider 插槽。

    顺序：cc-switch 当前（静默）-> mewcode 配置 -> codex -> Claude Code -> 项目 .env。
    key（source:name）冲突时保留先出现的插槽。
    测试传入 home= 时默认不读本机 cc-switch；显式传入 cc_switch_bindings 可注入。
    """
    work = Path(work_dir)
    home_dir = Path(home) if home is not None else Path.home()
    bindings = _load_cc_switch_bindings(
        cc_switch_bindings,
        enabled=home is None or cc_switch_bindings is not None,
    )

    slots: list[ProviderSlot] = []
    slots.extend(_cc_switch_slots(bindings))
    slots.extend(
        ProviderSlot(provider=p, source="config", origin=".mewcode/config.yaml")
        for p in config_providers
    )
    slots.extend(_load_codex_slots(home_dir))
    slots.extend(_load_claude_code_slots(home_dir, work))
    slots.extend(_load_project_env_slots(work))
    _enrich_slots_from_cc_switch(slots, bindings)
    catalog_models, catalog_efforts = _load_codex_model_catalog(home_dir)
    for slot in slots:
        if _slot_uses_codex_catalog(slot):
            _apply_codex_model_catalog(slot, catalog_models, catalog_efforts)

    seen: set[str] = set()
    unique: list[ProviderSlot] = []
    for slot in slots:
        if slot.key in seen:
            continue
        seen.add(slot.key)
        unique.append(slot)
    return unique
