"""Read refreshed credentials from the existing Codex login owner.

MewCode neither rotates refresh tokens nor changes the selected account.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from urllib.parse import urlsplit


class CodexLoginRequired(ValueError):
    pass


def is_codex_login_endpoint(base_url: str) -> bool:
    parsed = urlsplit(base_url)
    return (parsed.scheme == "https" and parsed.hostname == "chatgpt.com"
            and parsed.port in {None, 443} and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and parsed.path.rstrip("/") in {"/backend-api/codex", "/backend-api/codex/responses"})


def current_codex_headers(path: str, *, base_url: str, account_id: str) -> dict[str, str]:
    if not is_codex_login_endpoint(base_url) or not account_id:
        raise CodexLoginRequired("Codex/ChatGPT 登录来源与当前请求不匹配，请重新选择已登录的 Provider。")
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("oversize")
        auth = json.loads(raw)
        tokens = auth.get("tokens") or {}
        token = tokens.get("access_token")
        current_account = tokens.get("account_id")
        if not isinstance(token, str) or not token:
            raise ValueError("missing token")
    except (OSError, ValueError, TypeError, AttributeError):
        raise CodexLoginRequired("Codex/ChatGPT 登录不可用，请在 Codex 中重新登录后重试。") from None
    if current_account != account_id:
        raise CodexLoginRequired("Codex/ChatGPT 登录账号已变化，请重新选择 Provider，当前请求不会自动切换账号。")
    # Expiration is an early usability check, not token verification. The
    # official endpoint remains responsible for validating the credential.
    try:
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        expires = claims.get("exp")
    except (ValueError, IndexError, TypeError, AttributeError):
        expires = None
    if type(expires) in {int, float} and expires <= time.time():
        raise CodexLoginRequired("Codex/ChatGPT 登录令牌已过期，请在 Codex 中重新登录后重试；无需更换 API key。")
    return {"Authorization": f"Bearer {token}", "ChatGPT-Account-Id": account_id}
