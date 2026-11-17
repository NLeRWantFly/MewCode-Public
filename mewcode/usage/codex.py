"""ChatGPT subscription quota through the existing Codex OAuth login."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from mewcode.usage.backend import (
    UsageAdapter,
    UsageAuthenticationError,
    UsageBackendError,
    UsageUnavailable,
)
from mewcode.usage.models import (
    EvidenceLevel,
    LocalUsageSummary,
    MetricProvenance,
    QuotaWindow,
    UnavailableMetric,
    UsageAccount,
    UsagePeriod,
    UsageSnapshot,
    UsageSnapshotStatus,
    utc_now,
)


UTC = timezone.utc
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


@dataclass(frozen=True)
class CodexOAuthCredentials:
    access_token: str
    account_id: str = ""


def read_codex_oauth_credentials(home: str | Path | None = None) -> CodexOAuthCredentials | None:
    """Read only a ChatGPT OAuth login; API-key mode is intentionally ignored."""

    auth_path = Path(home or Path.home()) / ".codex" / "auth.json"
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("auth_mode") != "chatgpt":
        return None
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access_token = str(tokens.get("access_token") or "").strip()
    if not access_token:
        return None
    return CodexOAuthCredentials(
        access_token=access_token,
        account_id=str(tokens.get("account_id") or "").strip(),
    )


def discover_codex_usage_account(
    home: str | Path | None = None,
    *,
    linked_provider_keys: list[str] | None = None,
) -> UsageAccount | None:
    credentials = read_codex_oauth_credentials(home)
    if credentials is None:
        return None
    return UsageAccount(
        id="managed:codex-chatgpt",
        name="ChatGPT 登录态订阅额度",
        adapter_id="codex",
        linked_provider_keys=list(linked_provider_keys or []),
        config={"account_id": credentials.account_id},
        has_secret=True,
        secret_values={"oauth_access_token": credentials.access_token},
        managed=True,
        source_kind="auto_discovered",
    )


def _window_name(seconds: int | None, fallback: str) -> str:
    if seconds == 18_000:
        return "5 小时"
    if seconds == 604_800:
        return "7 天"
    if seconds == 2_592_000:
        return "30 天"
    if seconds and seconds % 86_400 == 0:
        return f"{seconds // 86_400} 天"
    if seconds and seconds % 3_600 == 0:
        return f"{seconds // 3_600} 小时"
    return fallback


def _reset_time(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp, tz=UTC)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


class CodexOAuthUsageAdapter(UsageAdapter):
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._http_client = http_client
        self.timeout = timeout

    @property
    def adapter_id(self) -> str:
        return "codex"

    async def _query(self, token: str, account_id: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "codex-cli",
            "Accept": "application/json",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        try:
            if self._http_client is not None:
                response = await self._http_client.get(CODEX_USAGE_URL, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(CODEX_USAGE_URL, headers=headers)
        except httpx.HTTPError as exc:
            raise UsageBackendError("codex_network_failed: 网络连接失败") from exc
        if response.status_code in {401, 403}:
            raise UsageAuthenticationError(
                "codex_login_expired: Codex/ChatGPT 登录已过期，请重新执行 codex login"
            )
        if response.status_code >= 500:
            raise UsageUnavailable(
                f"codex_interface_unavailable: 订阅额度接口暂不可用 (HTTP {response.status_code})"
            )
        if response.is_error:
            raise UsageBackendError(
                f"codex_request_failed: 订阅额度接口返回 HTTP {response.status_code}"
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise UsageUnavailable("codex_interface_unavailable: 返回内容无法解析") from exc
        if not isinstance(value, dict):
            raise UsageUnavailable("codex_interface_unavailable: 返回内容不是 JSON 对象")
        return value

    async def fetch_snapshot(
        self,
        account: UsageAccount,
        period: UsagePeriod,
        *,
        local: LocalUsageSummary | None = None,
    ) -> UsageSnapshot:
        token = account.secret_values.get("oauth_access_token", "")
        if not token:
            raise UsageAuthenticationError("codex_login_missing: 未发现 Codex ChatGPT OAuth 登录")
        payload = await self._query(token, str(account.config.get("account_id") or ""))
        rate_limit = payload.get("rate_limit")
        if not isinstance(rate_limit, dict):
            rate_limit = {}
        windows: list[QuotaWindow] = []
        for key, fallback in (("primary_window", "主额度窗口"), ("secondary_window", "次额度窗口")):
            raw = rate_limit.get(key)
            if not isinstance(raw, dict) or raw.get("used_percent") is None:
                continue
            seconds_value = raw.get("limit_window_seconds")
            try:
                seconds = int(seconds_value) if seconds_value is not None else None
            except (TypeError, ValueError):
                seconds = None
            windows.append(QuotaWindow(
                name=_window_name(seconds, fallback),
                used_percent=raw["used_percent"],
                window_seconds=seconds,
                resets_at=_reset_time(raw.get("reset_at")),
                provenance=MetricProvenance.OFFICIAL_API,
            ))
        fetched = utc_now()
        unavailable = []
        if not windows:
            unavailable.append(UnavailableMetric(
                field="quota_windows",
                code="unknown_response",
                message="ChatGPT 订阅额度接口未返回可识别的主/次窗口。",
            ))
        return UsageSnapshot(
            account_id=account.id,
            status=UsageSnapshotStatus.OK if windows else UsageSnapshotStatus.PARTIAL,
            period=period,
            quota_windows=windows,
            tokens=local.tokens if local else None,
            request_count=local.request_count if local else None,
            unavailable=unavailable,
            evidence=EvidenceLevel.OFFICIAL_ACCOUNT,
            fetched_at=fetched,
            stale_at=fetched + timedelta(minutes=5),
            warnings=["此处显示 ChatGPT 登录态订阅额度，不是 OpenAI API 组织账单。"],
        )
