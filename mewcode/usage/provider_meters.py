"""Aggregate per-Provider usage cards for the desktop usage panel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

from mewcode.cc_switch import read_cc_switch_probe_index
from mewcode.usage.anthropic import AnthropicUsageAdapter, is_official_anthropic_url
from mewcode.usage.codex import CodexOAuthUsageAdapter, discover_codex_usage_account
from mewcode.usage.deepseek import DeepSeekUsageAdapter, is_official_deepseek_url
from mewcode.usage.models import utc_now
from mewcode.usage.openai import is_official_openai_url
from mewcode.usage.relay_probe import probe_relay_quota
from mewcode.usage.runtime import current_month_period
from mewcode.usage.service import UsageService


OFFICIAL_OPENAI_HOSTS = {"api.openai.com", "chatgpt.com"}
OFFICIAL_ANTHROPIC_HOSTS = {"api.anthropic.com"}
OFFICIAL_DEEPSEEK_HOSTS = {"api.deepseek.com"}
OFFICIAL_XAI_HOSTS = {"api.x.ai"}


@dataclass
class ProviderMeterCard:
    provider_key: str
    name: str
    kind: str  # official | relay | unknown
    vendor: str  # openai | anthropic | deepseek | xai | codex | relay | ...
    base_url: str
    model: str = ""
    status: str = "ok"  # ok | error | missing_auth | unsupported
    headline: str = ""
    lines: list[dict[str, str]] = field(default_factory=list)
    message: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_key": self.provider_key,
            "name": self.name,
            "kind": self.kind,
            "vendor": self.vendor,
            "base_url": self.base_url,
            "model": self.model,
            "status": self.status,
            "headline": self.headline,
            "lines": list(self.lines),
            "message": self.message,
            "source": self.source,
        }


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _public_base(url: str) -> str:
    parsed = urlsplit(url or "")
    if not parsed.scheme or not parsed.netloc:
        return (url or "")[:200]
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{netloc}{path}"


def classify_provider(name: str, base_url: str, protocol: str = "") -> tuple[str, str]:
    """Return (kind, vendor). kind is official|relay."""
    host = _host(base_url)
    blob = f"{name} {base_url} {protocol}".lower()
    if host in OFFICIAL_OPENAI_HOSTS or "chatgpt.com" in host:
        if "chatgpt.com" in host or "codex" in blob:
            return "official", "codex" if "chatgpt.com" in host else "openai"
        return "official", "openai"
    if host in OFFICIAL_ANTHROPIC_HOSTS or is_official_anthropic_url(base_url):
        return "official", "anthropic"
    if host in OFFICIAL_DEEPSEEK_HOSTS or is_official_deepseek_url(base_url):
        return "official", "deepseek"
    if host in OFFICIAL_XAI_HOSTS or "x.ai" in host or "grok" in blob and "api.x.ai" in base_url.lower():
        return "official", "xai"
    if is_official_openai_url(base_url):
        return "official", "openai"
    return "relay", "relay"


def _fmt_money(value: float | None, unit: str = "USD") -> str:
    if value is None:
        return "—"
    if abs(value) >= 100:
        text = f"{value:,.2f}"
    else:
        text = f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{text} {unit}".strip()


def _cc_index() -> dict[str, dict[str, str]]:
    return read_cc_switch_probe_index()


async def _meter_official(
    *,
    vendor: str,
    provider_key: str,
    name: str,
    base_url: str,
    model: str,
    api_key: str,
    usage_service: UsageService | None,
) -> ProviderMeterCard:
    card = ProviderMeterCard(
        provider_key=provider_key,
        name=name,
        kind="official",
        vendor=vendor,
        base_url=_public_base(base_url),
        model=model,
        source="official_api",
    )
    period = current_month_period()
    try:
        if vendor == "codex":
            account = discover_codex_usage_account(linked_provider_keys=[provider_key])
            if account is None:
                card.status = "missing_auth"
                card.headline = "需要 ChatGPT / Codex 登录"
                card.message = "未发现本机 Codex ChatGPT OAuth"
                return card
            snap = await CodexOAuthUsageAdapter().fetch_snapshot(account, period)
            windows = snap.quota_windows or []
            if not windows:
                card.status = "error"
                card.headline = "无窗口数据"
                card.message = "订阅额度接口未返回窗口"
                return card
            card.headline = " · ".join(
                f"{w.name} {float(w.used_percent or 0):.0f}% 已用" for w in windows[:2]
            )
            for window in windows:
                reset = window.resets_at.isoformat(timespec="minutes") if window.resets_at else ""
                card.lines.append({
                    "label": window.name,
                    "value": f"{float(window.used_percent or 0):.1f}% 已用",
                    "hint": f"重置 {reset}" if reset else "",
                })
            return card

        if vendor == "deepseek":
            from mewcode.usage.models import UsageAccount

            if not api_key:
                card.status = "missing_auth"
                card.headline = "缺少 API Key"
                return card
            account = UsageAccount(
                id=f"meter:{provider_key}",
                name=name,
                adapter_id="deepseek",
                config={"base_url": base_url or "https://api.deepseek.com"},
                has_secret=True,
                secret_values={"api_key": api_key},
            )
            snap = await DeepSeekUsageAdapter().fetch_snapshot(account, period)
            for money in snap.financials or []:
                bal = float(money.balance.value) if money.balance else None
                card.lines.append({
                    "label": f"余额 ({money.currency})",
                    "value": _fmt_money(bal, money.currency),
                    "hint": "官方余额",
                })
            if card.lines:
                card.headline = card.lines[0]["value"]
            else:
                card.status = "error"
                card.headline = "无余额"
                card.message = "官方未返回余额"
            return card

        if vendor == "anthropic":
            from mewcode.usage.models import UsageAccount

            if not api_key:
                card.status = "missing_auth"
                card.headline = "缺少 API Key"
                return card
            account = UsageAccount(
                id=f"meter:{provider_key}",
                name=name,
                adapter_id="anthropic",
                config={"base_url": base_url or "https://api.anthropic.com"},
                has_secret=True,
                secret_values={"admin_api_key": api_key, "api_key": api_key},
            )
            snap = await AnthropicUsageAdapter().fetch_snapshot(account, period)
            for money in snap.financials or []:
                used = float(money.used.value) if money.used else None
                card.lines.append({
                    "label": f"本期 ({money.currency})",
                    "value": _fmt_money(used, money.currency),
                    "hint": "官方用量",
                })
            if card.lines:
                card.headline = "已用 " + card.lines[0]["value"]
            else:
                card.status = "error"
                card.headline = "无用量数据"
                card.message = (snap.warnings[0] if snap.warnings else "") or "官方未返回费用"
            return card

        if vendor == "openai":
            from mewcode.usage.adapters import OpenAIAccountAdapter
            from mewcode.usage.models import UsageAccount

            if not api_key:
                card.status = "missing_auth"
                card.headline = "缺少 Admin / API Key"
                return card
            account = UsageAccount(
                id=f"meter:{provider_key}",
                name=name,
                adapter_id="openai",
                config={"base_url": base_url or "https://api.openai.com"},
                has_secret=True,
                secret_values={"admin_api_key": api_key, "api_key": api_key},
            )
            snap = await OpenAIAccountAdapter().fetch_snapshot(account, period)
            for money in snap.financials or []:
                used = float(money.used.value) if money.used else None
                card.lines.append({
                    "label": f"本期费用 ({money.currency})",
                    "value": _fmt_money(used, money.currency),
                    "hint": "官方 Costs",
                })
            if card.lines:
                card.headline = card.lines[0]["value"]
            else:
                card.status = "error"
                card.headline = "无费用数据"
                card.message = "需要具备 Usage/Costs 权限的 Admin Key"
            return card

        if vendor == "xai":
            # xAI does not expose a stable public prepaid balance API comparable to DeepSeek.
            # Surface configuration honestly; spend is tracked via local ledger when present.
            card.status = "unsupported"
            card.headline = "暂无公开余额接口"
            card.message = "desktop:example_provider_2 请以控制台账单为准；本地 Token 统计仍可用"
            if usage_service is not None:
                local = usage_service.local_summary(provider_name=name)
                total_tokens = int(getattr(local.tokens, "total", 0) or 0)
                if total_tokens:
                    card.lines.append({
                        "label": "本地 Token",
                        "value": f"{total_tokens:,}",
                        "hint": "本机账本",
                    })
                    card.headline = f"本地 {total_tokens:,} tokens"
                    card.status = "ok"
            return card

        card.status = "unsupported"
        card.headline = "未支持的官方厂商"
        return card
    except Exception as exc:  # noqa: BLE001 — card must never crash the panel
        card.status = "error"
        card.headline = "查询失败"
        card.message = str(exc)[:240]
        return card


async def _meter_relay(
    *,
    provider_key: str,
    name: str,
    base_url: str,
    model: str,
    api_key: str,
    cc_entry: dict[str, Any] | None,
) -> ProviderMeterCard:
    card = ProviderMeterCard(
        provider_key=provider_key,
        name=name,
        kind="relay",
        vendor="relay",
        base_url=_public_base(base_url),
        model=model,
        source="relay_probe",
    )
    access_token = ""
    user_id = ""
    if cc_entry:
        access_token = str(cc_entry.get("access_token") or "")
        user_id = str(cc_entry.get("user_id") or "")
        if cc_entry.get("name"):
            card.source = "relay_probe+cc-switch"
    if not api_key and not (access_token and user_id):
        card.status = "missing_auth"
        card.headline = "缺少探测凭据"
        card.message = "需要 Provider API Key，或 cc-switch NewAPI accessToken + userId"
        return card
    try:
        quota = await probe_relay_quota(
            base_url,
            api_key=api_key,
            access_token=access_token,
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        card.status = "error"
        card.headline = "查询失败"
        card.message = str(exc)[:240]
        return card
    if not quota.ok:
        card.status = "error"
        card.headline = "查询失败"
        card.message = quota.message or "未检测到额度接口"
        return card
    card.source = quota.source or card.source
    if quota.unlimited:
        card.headline = "无限额度"
        card.lines.append({"label": "额度", "value": "无限", "hint": quota.source})
        return card
    card.headline = f"剩余 {_fmt_money(quota.remaining, quota.unit)}"
    if quota.plan_name:
        card.lines.append({"label": "套餐", "value": quota.plan_name, "hint": ""})
    card.lines.append({
        "label": "剩余",
        "value": _fmt_money(quota.remaining, quota.unit),
        "hint": quota.source,
    })
    if quota.used is not None:
        card.lines.append({
            "label": "已用",
            "value": _fmt_money(quota.used, quota.unit),
            "hint": "",
        })
    if quota.total is not None:
        card.lines.append({
            "label": "总量",
            "value": _fmt_money(quota.total, quota.unit),
            "hint": "",
        })
    return card


async def collect_provider_meters(
    records: list[Any],
    *,
    usage_service: UsageService | None = None,
    resolve_key: Callable[[Any], str] | None = None,
) -> dict[str, Any]:
    """Build usage cards for registry provider records."""
    cc_by_host = _cc_index()
    tasks = []
    meta: list[tuple[str, str, str]] = []

    for record in records:
        provider = getattr(record, "provider", None)
        if provider is None:
            continue
        key = str(getattr(record, "key", "") or "")
        name = str(getattr(provider, "name", "") or key)
        base_url = str(getattr(provider, "base_url", "") or "")
        model = str(getattr(provider, "model", "") or "")
        protocol = str(getattr(provider, "protocol", "") or "")
        kind, vendor = classify_provider(name, base_url, protocol)
        if resolve_key is not None:
            api_key = resolve_key(record) or ""
        else:
            try:
                api_key = str(provider.resolve_api_key() or "")
            except Exception:
                api_key = ""
        host = _host(base_url)
        cc_entry = cc_by_host.get(host)
        meta.append((key, kind, vendor))
        if kind == "official":
            # Codex oauth providers often have chatgpt host even without api key
            if vendor == "openai" and not api_key:
                # Prefer codex window if chatgpt login exists
                tasks.append(_meter_official(
                    vendor="codex",
                    provider_key=key,
                    name=name,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    usage_service=usage_service,
                ))
            else:
                tasks.append(_meter_official(
                    vendor=vendor,
                    provider_key=key,
                    name=name,
                    base_url=base_url,
                    model=model,
                    api_key=api_key,
                    usage_service=usage_service,
                ))
        else:
            tasks.append(_meter_relay(
                provider_key=key,
                name=name,
                base_url=base_url,
                model=model,
                api_key=api_key,
                cc_entry=cc_entry,
            ))

    cards = await asyncio.gather(*tasks) if tasks else []
    return {
        "generated_at": utc_now().isoformat(),
        "providers": [card.to_dict() for card in cards],
        "count": len(cards),
    }
