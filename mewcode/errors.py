"""跨层共享的错误分类与格式化工具（仅依赖标准库）。

被 agent（LLM 请求级重试）、pipeline executor（节点级重试）共用，
保证「什么算瞬时错误」全项目只有一份定义。
"""

from __future__ import annotations


class NetworkCircuitOpenError(Exception):
    """连续网络失败达到熔断阈值。

    语义：网络处于持续不可用状态，继续重试只会白烧时间。
    捕获方应将运行标记为可恢复的中断（interrupted）而非失败，
    等网络恢复后断点续跑。
    """

    def __init__(self, streak: int, threshold: int, last_error: str) -> None:
        self.streak = streak
        self.threshold = threshold
        self.last_error = last_error
        super().__init__(
            f"Network circuit open: {streak} consecutive network failures "
            f"(threshold {threshold}). Last error: {last_error}"
        )


def format_error(e: BaseException) -> str:
    """把异常格式化为可诊断的字符串。

    关键点：str(e) 可能为空（如 asyncio.TimeoutError()），
    必须带上异常类型名，否则日志里只剩一个空串。
    """
    msg = str(e).strip()
    name = type(e).__name__
    if not msg:
        return name
    if msg.lower().startswith(name.lower()):
        return msg
    return f"{name}: {msg}"


def is_transient_error(e: BaseException) -> bool:
    """判断是否为可重试的瞬时错误（超时/网络/服务端/限流/流中断）。

    策略：
    - 4xx 客户端错误（400/401/404 等，除 429）不重试；
    - 429 / 5xx、连接/超时、流中断的基础 APIError 均重试。
    """
    if getattr(e, "retryable", None) is False:
        return False
    # 提取 HTTP 状态码
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(e, "http_status", None)
    if status is None:
        resp = getattr(e, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)

    # 4xx（除 429）是客户端错误，不可重试
    if isinstance(status, int) and 400 <= status < 500 and status != 429:
        return False

    # 认证错误永不重试（无论叫什么名字）
    type_names = [cls.__name__.lower() for cls in type(e).__mro__]
    if any("authentication" in name for name in type_names):
        return False

    # 消息关键字
    msg = str(e).lower()
    # Relays may wrap an upstream 401/403 in HTTP 502. A permission or
    # authentication rejection is not a transient network failure; in
    # particular the word "upstream" must not match our broad "stream" rule.
    if any(reason in msg for reason in (
        "upstream access forbidden",
        "upstream authentication failed",
        "upstream_error",
    )):
        return False
    keywords = (
        "timeout", "timed out", "network", "connection", "temporarily",
        "503", "429", "502", "504", "500", "rate limit", "overloaded",
        "reset", "refused", "unavailable", "server error", "internal server",
        "stream", "aborted", "interrupted", "incomplete", "eof",
        "broken pipe", "read error", "ssl",
    )
    if any(k in msg for k in keywords):
        return True

    # 异常类型名（OpenAI / Anthropic / httpx SDK / asyncio）
    transient_type_keywords = (
        "timeout", "connection", "ratelimit", "rate_limit",
        "internalservererror", "serviceunavailable", "servererror",
        "networkerror", "readtimeout", "connecttimeout", "remoteprotocol",
        "incompleteread", "chunkedencoding",
    )
    if any(any(t in name for t in transient_type_keywords) for name in type_names):
        return True

    # 429 或 5xx
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True

    # 基础 APIError（无状态码，通常是流式响应中断）→ 可重试
    if "apierror" in type_names and status is None:
        return True

    return False
