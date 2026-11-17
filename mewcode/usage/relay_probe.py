"""Quota probes for non-official (relay) OpenAI-compatible gateways.

Mirrors the common cc-switch templates without executing arbitrary scripts:
- Gateway usage: GET {base}/v1/usage  (Bearer API key; remaining/balance)
- NewAPI token: GET {base}/api/usage/token/
- NewAPI account: GET {base}/api/user/self
- Generic balance: GET {base}/user/balance
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

NEWAPI_QUOTA_PER_USD = 500_000.0


@dataclass
class RelayQuota:
    ok: bool
    remaining: float | None = None
    used: float | None = None
    total: float | None = None
    unit: str = "USD"
    plan_name: str = ""
    unlimited: bool = False
    source: str = ""
    message: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "remaining": self.remaining,
            "used": self.used,
            "total": self.total,
            "unit": self.unit,
            "plan_name": self.plan_name,
            "unlimited": self.unlimited,
            "source": self.source,
            "message": self.message,
            "extras": dict(self.extras),
        }


def normalize_api_root(base_url: str) -> str:
    """Strip trailing /v1 (and query/fragment) so /api/... paths resolve correctly."""
    parsed = urlsplit((base_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return (base_url or "").rstrip("/")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quota_to_usd(points: float | None) -> float | None:
    if points is None:
        return None
    return points / NEWAPI_QUOTA_PER_USD


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
) -> tuple[int, Any]:
    response = await client.get(url, headers=headers)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


async def probe_newapi_token(
    base_url: str,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 12.0,
) -> RelayQuota:
    root = normalize_api_root(base_url)
    key = (api_key or "").strip()
    if not root or not key:
        return RelayQuota(ok=False, message="缺少 base_url 或 API Key", source="newapi_token")
    url = f"{root}/api/usage/token/"
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "mewcode-usage/1.0",
    }
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as owned:
                status, payload = await _get_json(owned, url, headers)
        else:
            status, payload = await _get_json(client, url, headers)
    except httpx.HTTPError as exc:
        return RelayQuota(ok=False, message=f"网络失败：{exc.__class__.__name__}", source="newapi_token")
    if status in {401, 403}:
        return RelayQuota(ok=False, message="鉴权失败", source="newapi_token")
    if status >= 400 or not isinstance(payload, dict):
        return RelayQuota(ok=False, message=f"HTTP {status}", source="newapi_token")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return RelayQuota(ok=False, message="响应无法解析", source="newapi_token")
    if data.get("unlimited_quota") is True:
        return RelayQuota(
            ok=True,
            unlimited=True,
            remaining=None,
            unit="USD",
            source="newapi_token",
            message="无限额度",
        )
    available = _as_float(data.get("total_available"))
    granted = _as_float(data.get("total_granted"))
    used = _as_float(data.get("total_used"))
    if available is None and granted is None:
        return RelayQuota(ok=False, message="未返回额度字段", source="newapi_token")
    return RelayQuota(
        ok=True,
        remaining=_quota_to_usd(available),
        used=_quota_to_usd(used),
        total=_quota_to_usd(granted if granted is not None else (
            (available or 0) + (used or 0) if available is not None or used is not None else None
        )),
        unit="USD",
        source="newapi_token",
    )


async def probe_newapi_user_self(
    base_url: str,
    access_token: str,
    user_id: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 12.0,
) -> RelayQuota:
    root = normalize_api_root(base_url)
    token = (access_token or "").strip()
    uid = str(user_id or "").strip()
    if not root or not token or not uid:
        return RelayQuota(ok=False, message="缺少 NewAPI accessToken / userId", source="newapi_self")
    url = f"{root}/api/user/self"
    headers = {
        "Authorization": f"Bearer {token}",
        "New-Api-User": uid,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "mewcode-usage/1.0",
    }
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as owned:
                status, payload = await _get_json(owned, url, headers)
        else:
            status, payload = await _get_json(client, url, headers)
    except httpx.HTTPError as exc:
        return RelayQuota(ok=False, message=f"网络失败：{exc.__class__.__name__}", source="newapi_self")
    if status in {401, 403}:
        return RelayQuota(ok=False, message="鉴权失败", source="newapi_self")
    if not isinstance(payload, dict):
        return RelayQuota(ok=False, message=f"HTTP {status}", source="newapi_self")
    if payload.get("success") is False:
        return RelayQuota(
            ok=False,
            message=str(payload.get("message") or "查询失败")[:200],
            source="newapi_self",
        )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else None
    if data is None:
        return RelayQuota(ok=False, message="响应缺少 data", source="newapi_self")
    quota = _as_float(data.get("quota"))
    used_quota = _as_float(data.get("used_quota"))
    return RelayQuota(
        ok=True,
        remaining=_quota_to_usd(quota),
        used=_quota_to_usd(used_quota),
        total=_quota_to_usd(
            (quota or 0) + (used_quota or 0)
            if quota is not None or used_quota is not None
            else None
        ),
        unit="USD",
        plan_name=str(data.get("group") or "")[:80],
        source="newapi_self",
    )


async def probe_v1_usage(
    base_url: str,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 12.0,
) -> RelayQuota:
    """Probe ``GET {root}/v1/usage`` used by many cc-switch relay templates."""
    root = normalize_api_root(base_url)
    key = (api_key or "").strip()
    if not root or not key:
        return RelayQuota(ok=False, message="缺少 base_url 或 API Key", source="v1_usage")
    url = f"{root}/v1/usage"
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "mewcode-usage/1.0",
    }
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as owned:
                status, payload = await _get_json(owned, url, headers)
        else:
            status, payload = await _get_json(client, url, headers)
    except httpx.HTTPError as exc:
        return RelayQuota(ok=False, message=f"网络失败：{exc.__class__.__name__}", source="v1_usage")
    if status in {401, 403}:
        return RelayQuota(ok=False, message="鉴权失败", source="v1_usage")
    if status >= 400 or not isinstance(payload, dict):
        return RelayQuota(ok=False, message=f"HTTP {status}", source="v1_usage")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return RelayQuota(ok=False, message="响应无法解析", source="v1_usage")
    remaining = _as_float(
        data.get("remaining")
        if data.get("remaining") is not None
        else data.get("balance")
    )
    used = None
    usage = data.get("usage")
    if isinstance(usage, dict):
        today = usage.get("today") if isinstance(usage.get("today"), dict) else {}
        used = _as_float(
            today.get("actual_cost")
            if today.get("actual_cost") is not None
            else usage.get("total_cost")
        )
    total = _as_float(data.get("total") or data.get("quota") or data.get("limit"))
    unit = str(data.get("unit") or "USD").strip().upper() or "USD"
    plan_name = str(
        data.get("planName") or data.get("plan_name") or data.get("plan") or ""
    ).strip()[:80]
    if remaining is None and used is None and total is None:
        return RelayQuota(ok=False, message="未返回额度字段", source="v1_usage")
    unlimited = bool(
        data.get("unlimited")
        or str(data.get("mode") or "").lower() in {"unrestricted", "unlimited"}
        and remaining is None
    )
    # mode=unrestricted with a numeric remaining still means metered balance.
    if remaining is None and unlimited:
        return RelayQuota(
            ok=True,
            unlimited=True,
            unit=unit,
            plan_name=plan_name,
            source="v1_usage",
            message="无限额度",
        )
    return RelayQuota(
        ok=True,
        remaining=remaining,
        used=used,
        total=total,
        unit=unit,
        plan_name=plan_name,
        source="v1_usage",
    )


async def probe_generic_balance(
    base_url: str,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 12.0,
) -> RelayQuota:
    root = normalize_api_root(base_url)
    key = (api_key or "").strip()
    if not root or not key:
        return RelayQuota(ok=False, message="缺少 base_url 或 API Key", source="generic_balance")
    url = f"{root}/user/balance"
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "mewcode-usage/1.0",
    }
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=timeout) as owned:
                status, payload = await _get_json(owned, url, headers)
        else:
            status, payload = await _get_json(client, url, headers)
    except httpx.HTTPError as exc:
        return RelayQuota(ok=False, message=f"网络失败：{exc.__class__.__name__}", source="generic_balance")
    if status in {401, 403}:
        return RelayQuota(ok=False, message="鉴权失败", source="generic_balance")
    if status >= 400 or not isinstance(payload, dict):
        return RelayQuota(ok=False, message=f"HTTP {status}", source="generic_balance")
    balance = _as_float(payload.get("balance"))
    if balance is None and isinstance(payload.get("data"), dict):
        balance = _as_float(payload["data"].get("balance"))
    if balance is None:
        infos = payload.get("balance_infos")
        if isinstance(infos, list) and infos and isinstance(infos[0], dict):
            balance = _as_float(infos[0].get("total_balance"))
            unit = str(infos[0].get("currency") or "USD").upper()
            return RelayQuota(ok=True, remaining=balance, unit=unit, source="generic_balance")
        return RelayQuota(ok=False, message="未返回 balance", source="generic_balance")
    return RelayQuota(ok=True, remaining=balance, unit="USD", source="generic_balance")


async def probe_relay_quota(
    base_url: str,
    api_key: str = "",
    *,
    access_token: str = "",
    user_id: str = "",
    client: httpx.AsyncClient | None = None,
    timeout: float = 12.0,
) -> RelayQuota:
    """Try cc-switch-compatible probes in preference order; return first success."""
    errors: list[str] = []
    if access_token and user_id:
        result = await probe_newapi_user_self(
            base_url, access_token, user_id, client=client, timeout=timeout
        )
        if result.ok:
            return result
        errors.append(result.message or result.source)
    if api_key:
        for probe in (probe_v1_usage, probe_newapi_token, probe_generic_balance):
            result = await probe(base_url, api_key, client=client, timeout=timeout)
            if result.ok:
                return result
            errors.append(result.message or result.source)
    return RelayQuota(
        ok=False,
        message="；".join(errors)[:240] or "未检测到可用额度接口",
        source="relay",
    )
