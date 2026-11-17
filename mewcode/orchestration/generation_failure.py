"""Safe, durable model-request diagnostics for graph construction."""
from __future__ import annotations

import re

from mewcode.client import LLMError
from mewcode.errors import is_transient_error
from mewcode.permissions.decision_envelope import redact_value


def generation_failure(exc, context=None):
    context = context or {}
    provider = context.get("provider")
    route = {key: str(context.get(key) or "") for key in ("provider_key", "provider_name", "model", "requested_effort")}
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "http_status", None)
    detail = str(exc).strip()
    # Redact actual configured secrets too: a relay may echo an opaque key
    # without naming its field. Never expose unrelated unexpected exceptions.
    secrets = [getattr(provider, "api_key", "")]
    for key, value in (getattr(provider, "default_headers", {}) or {}).items():
        if re.search(r"(?i)authorization|token|secret|api.?key|cookie", str(key)):
            secrets.append(value)
    for secret in secrets:
        if isinstance(secret, str) and secret:
            detail = detail.replace(secret, "[REDACTED]")
    detail = str(redact_value(detail))
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", detail)
    if not isinstance(exc, (LLMError, ValueError)):
        detail = ""
    if status in {401, 403}:
        category, summary = "authentication", "模型服务拒绝认证或访问权限"
    elif status == 404:
        category, summary = "route", "当前模型或服务接口不存在"
    elif status in {400, 422}:
        category, summary = "request", "模型服务拒绝构图请求参数"
    elif status == 413:
        category, summary = "capacity", "模型服务拒绝当前请求长度"
    elif status == 429:
        category, summary = "rate_limit", "模型服务报告限流或额度不足"
    elif isinstance(status, int) and status >= 500:
        category, summary = "upstream", "模型服务或上游返回错误"
    elif isinstance(exc, LLMError):
        category, summary = "provider", "构图模型请求失败"
    else:
        category, summary = "generation", "图结构整理失败"
    retryable = is_transient_error(exc)
    suffix = "；" + detail if detail else ""
    if isinstance(status, int):
        summary += f"（HTTP {status}）"
    else:
        summary += "（" + type(exc).__name__ + "）"
    return {"category": category, "exception_type": type(exc).__name__, "http_status": status,
        "retryable": retryable, "route": route, "detail": detail[:2000], "summary": summary + suffix[:1200]}
