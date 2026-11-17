from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import uuid
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlparse

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from mewcode.cache_slots import resolve_cache_usage_provider, usage_counter
from mewcode.config import ProviderConfig
from mewcode.openrouter import merge_llm_headers
from mewcode.conversation import ConversationManager
from mewcode.model_capabilities import (
    estimate_request_cost_usd,
    resolve_model_capabilities,
)
from mewcode.model_catalog import DEFAULT_MODEL_CATALOG, ModelCatalogService
from mewcode.serialization import (
    build_anthropic_messages,
    build_chat_completion_messages,
    build_openai_input,
)
from mewcode.tools.base import (
    StreamEnd,
    StreamEvent,
    StreamStatus,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)
from mewcode.usage.models import (
    AttributionStatus,
    EvidenceLevel,
    RateLimitSnapshot,
    RequestUsage,
    TokenUsage,
    credential_fingerprint,
)
from mewcode.runtime_scheduler import (
    GLOBAL_RUNTIME_SCHEDULER,
    current_runtime_scheduler,
)
from mewcode.usage.service import current_usage_scope

log = logging.getLogger(__name__)


# OpenCode Go 会话头。网关要求每个请求携带 x-opencode-session（稳定会话 ID，
# 用于把同一对话路由到同一上游并保持 prompt cache），缺失时以
# MissingSessionID (HTTP 400) 拒绝。见 https://opencode.ai/docs/go。
_OPENCODE_GO_FALLBACK_SESSION: str | None = None


def _is_opencode_go_route(base_url: str, provider_type: str = "") -> bool:
    if provider_type == "opencode-go":
        return True
    parsed = urlparse(base_url or "")
    return parsed.hostname == "opencode.ai" and parsed.path.startswith("/zen/go")


def _opencode_go_session_headers(
    base_url: str, provider_type: str = ""
) -> dict[str, str]:
    """会话头只对 OpenCode Go 路由生效；会话内稳定，进程级兜底。

    优先取当前 usage scope 的会话 ID（随对话稳定）；客户端在会话作用域
    之外构造（元数据预取、辅助请求等）时退回进程级固定 UUID，保证头
    始终存在且进程内不变。
    """
    if not _is_opencode_go_route(base_url, provider_type):
        return {}
    global _OPENCODE_GO_FALLBACK_SESSION
    session = (current_usage_scope().session_id or "").strip()
    if not session:
        if _OPENCODE_GO_FALLBACK_SESSION is None:
            _OPENCODE_GO_FALLBACK_SESSION = str(uuid.uuid4())
        session = _OPENCODE_GO_FALLBACK_SESSION
    return {
        "x-opencode-session": session,
        "x-opencode-client": "MewCode/0.1",
    }


def _llm_default_headers(config: ProviderConfig, api_key: str) -> dict[str, str]:
    """Identify the actual application, while preserving caller overrides.

    Some compatible relays reject the SDK's generic Python User-Agent even
    for an otherwise authorized request. Use our own identity, not another
    client's identity; SDK telemetry and authentication remain unchanged.
    """
    headers = merge_llm_headers(config.base_url, api_key, config.default_headers) or {}
    configured_agent = next((value for key, value in headers.items() if key.lower() == "user-agent"), None)
    headers = {key: value for key, value in headers.items() if key.lower() != "user-agent"}
    headers["User-Agent"] = configured_agent if configured_agent is not None else "MewCode/0.1"
    # OpenCode Go：缺会话头会被 400 拒绝；用户显式配置的同名头优先。
    for key, value in _opencode_go_session_headers(
        config.base_url, provider_type=config.provider_type
    ).items():
        headers.setdefault(key, value)
    return headers


def _apply_opencode_go_session_headers(
    kwargs: dict[str, Any], base_url: str
) -> None:
    """请求级刷新会话头：stream 时会话作用域已确定，优先于构造期默认值。"""

    extra = _opencode_go_session_headers(base_url)
    if extra:
        kwargs["extra_headers"] = {**kwargs.get("extra_headers", {}), **extra}


class ModelStreamAdmission(StreamStatus):
    """Local scheduler activity; never a provider heartbeat or model content."""


class ModelRequestTimeout(TimeoutError):
    """The host's request deadline expired after scheduler admission."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(f"model request reached its {seconds:g}s total deadline")


async def scheduled_client_stream(
    client: Any,
    conversation: ConversationManager,
    system: str = "",
    tools: list[dict[str, Any]] | None = None,
    *,
    request_timeout: float | None = None,
    include_admission_events: bool = False,
    enforce_deadline: bool = True,
) -> AsyncIterator[StreamEvent]:
    """Schedule a stream for any client implementing the legacy protocol."""

    seconds = LLM_REQUEST_TIMEOUT if request_timeout is None else float(request_timeout)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("model request timeout must be finite and positive")
    enforce_deadline = enforce_deadline and not getattr(client, 'workflow_long_running', False)
    scope = current_usage_scope()
    if include_admission_events:
        yield ModelStreamAdmission("queued", "正在等待本地模型调度名额，尚未发送 Provider 请求。")
    async with current_runtime_scheduler(GLOBAL_RUNTIME_SCHEDULER).slot(
        kind="model",
        label=str(getattr(client, "_usage_model", "") or type(client).__name__),
        session_id=scope.session_id,
        agent_id=scope.agent_id,
    ):
        stream = (
            client.stream(conversation, system=system, tools=tools)
            if tools is not None
            else client.stream(conversation, system=system)
        )
        # httpx read timeouts reset on every received chunk. A provider sending
        # keep-alives forever must not monopolize a scheduler slot forever.
        # Start the request deadline after admission, not while queued.
        # Workflow conversations opt out and end through delivery or user controls.
        try:
            async with asyncio.timeout(seconds if enforce_deadline else None) as deadline:
                if include_admission_events:
                    yield ModelStreamAdmission("requesting", "已获得调度名额，正在发送模型请求。")
                async for event in stream:
                    yield event
        except TimeoutError as exc:
            # A provider's own TimeoutError is not our deadline.
            if deadline.expired():
                raise ModelRequestTimeout(seconds) from exc
            raise
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()


# 限制自动拉取模型元数据的超时时间，防止慢响应或挂起的
# /v1/models 端点拖延启动。超时后降级为 None（即"未知"），
# 由下一层 context window 解析逻辑接管。
ANTHROPIC_MODEL_FETCH_TIMEOUT = 3.0

# LLM 请求超时配置。SDK 默认 connect 超时仅 5s，对慢速中转站/
# 代理类 base_url 太苛刻——高峰期建连超过 5s 就会报 "Request timed out"。
# 建连阈值为 30s，读/写超时为 600s；普通请求默认另限总时长 600s，
# 长时工作流通过交付或用户控制结束，不套用该总时限。
LLM_CONNECT_TIMEOUT = 30.0
LLM_REQUEST_TIMEOUT = 600.0
LLM_TIMEOUT = httpx.Timeout(
    LLM_REQUEST_TIMEOUT,
    connect=LLM_CONNECT_TIMEOUT,
    read=LLM_REQUEST_TIMEOUT,
    write=LLM_REQUEST_TIMEOUT,
)


_EPHEMERAL = {"type": "ephemeral"}
_EXPLICIT_PROMPT_CACHE_BREAKPOINT = {"mode": "explicit"}


def _mark_last_user_tail_for_cache(messages: list[dict[str, Any]]) -> None:
    """给最后一条 user 消息的最后一个 block 附加 cache_control。

    会原地修改 `messages`。Anthropic 会缓存到（且包含）这个 block 为止的前缀；
    后续请求只要前缀逐字节相同，缓存命中的 token 只需支付 10% 的费用。
    仅适用于 Anthropic 协议的消息。
    """
    if not messages:
        return
    # 从后往前找到最后一条 user 角色消息；assistant 尾部不能锚定 cache。
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            # 把字符串 content 升级为 block 形式，以便附加 cache_control。
            msg["content"] = [{
                "type": "text",
                "text": content,
                "cache_control": _EPHEMERAL,
            }]
        elif isinstance(content, list) and content:
            last = content[-1]
            if isinstance(last, dict):
                last["cache_control"] = _EPHEMERAL
        return


def _mark_last_tool_for_cache(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回一个浅拷贝的 tools 列表，并在最后一个 tool 上标记 cache_control。

    tool schema 在多轮对话之间是稳定的，因此标记列表尾部即可缓存整个 tool block。
    我们不直接修改调用方传入的列表，因为这些 tool schema 往往是注册表里的
    模块级单例。
    """
    if not tools:
        return tools
    marked = list(tools)
    last = dict(marked[-1])
    last["cache_control"] = _EPHEMERAL
    marked[-1] = last
    return marked


def _uses_gpt_5_6_prompt_cache(model: str) -> bool:
    """Return whether *model* accepts the GPT-5.6 cache-control fields."""
    name = model.strip().lower().rsplit("/", 1)[-1]
    if name == "gpt-6-astra" or name.startswith("gpt-6-astra-"):
        return True
    if not name.startswith("gpt-"):
        return False
    version = name[4:].split("-", 1)[0].split(".")
    try:
        major = int(version[0])
        minor = int(version[1])
    except (IndexError, ValueError):
        return False
    return (major, minor) >= (5, 6)


def _is_codex_backend_route(base_url: str) -> bool:
    """Codex's ChatGPT backend is not the public Responses API contract."""
    parsed = urlparse(base_url)
    return parsed.hostname == "chatgpt.com" and parsed.path.rstrip("/") in {
        "/backend-api/codex", "/backend-api/codex/responses",
    }


def _prompt_cache_route_capabilities(
    base_url: str, model: str
) -> tuple[bool, bool]:
    """Return (cache key, explicit breakpoint) support for verified routes."""
    if not _uses_gpt_5_6_prompt_cache(model):
        return False, False
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    if host == "api.openai.com":
        return True, True
    if host == "chatgpt.com" and path.endswith("/backend-api/codex"):
        # The subscription-backed Codex endpoint accepts prompt_cache_key but
        # rejects the public API's prompt_cache_options field.
        return True, False
    return False, False


def _input_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "input_text"
    )


def _mark_openai_input_item(
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a copied Responses message with a breakpoint on its last text."""

    marked_item = dict(item)
    content = marked_item.get("content")
    if isinstance(content, str):
        marked_item["content"] = [{
            "type": "input_text",
            "text": content,
            "prompt_cache_breakpoint": _EXPLICIT_PROMPT_CACHE_BREAKPOINT,
        }]
        return marked_item
    if not isinstance(content, list):
        return None
    blocks = [
        dict(block) if isinstance(block, dict) else block
        for block in content
    ]
    text_index = next(
        (
            index
            for index in range(len(blocks) - 1, -1, -1)
            if isinstance(blocks[index], dict)
            and blocks[index].get("type") == "input_text"
        ),
        -1,
    )
    if text_index < 0:
        return None
    blocks[text_index]["prompt_cache_breakpoint"] = (
        _EXPLICIT_PROMPT_CACHE_BREAKPOINT
    )
    marked_item["content"] = blocks
    return marked_item


def _count_prompt_cache_breakpoints(value: Any) -> int:
    if isinstance(value, dict):
        return int("prompt_cache_breakpoint" in value) + sum(
            _count_prompt_cache_breakpoints(item) for item in value.values()
        )
    if isinstance(value, list):
        return sum(_count_prompt_cache_breakpoints(item) for item in value)
    return 0


def _mark_stable_openai_prefix(
    input_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Mark MewCode's stable environment prefix for GPT-5.6 caching.

    The first breakpoint protects the stable environment/memory prefix. A
    second rolling breakpoint is placed on the newest real user turn. During
    the following tool loop that exact prefix is reused, while volatile runtime
    reminders remain after the stable breakpoint and before the user turn.
    """
    if not input_items:
        return None

    anchor = -1
    first_text = _input_text(input_items[0])
    if (
        input_items[0].get("role") == "user"
        and first_text.startswith("Current working directory:")
    ):
        anchor = 0

    if anchor == 0 and len(input_items) > 1:
        second_text = _input_text(input_items[1])
        if (
            input_items[1].get("role") == "user"
            and second_text.startswith(
                "<system-reminder>\nAs you answer the user's questions"
            )
        ):
            anchor = 1

    if anchor < 0:
        return None

    marked = list(input_items)
    stable_item = _mark_openai_input_item(marked[anchor])
    if stable_item is None:
        return None
    marked[anchor] = stable_item

    for index in range(len(marked) - 1, anchor, -1):
        item = marked[index]
        if item.get("role") != "user":
            continue
        text = _input_text(item).strip()
        if not text or text.startswith("<system-reminder>"):
            continue
        rolling_item = _mark_openai_input_item(item)
        if rolling_item is not None:
            marked[index] = rolling_item
            break
    return marked, marked[: anchor + 1]


def _openai_prompt_cache_key(
    model: str,
    system: str,
    tools: list[dict[str, Any]] | None,
    stable_prefix: list[dict[str, Any]],
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "instructions": system,
            "tools": tools or [],
            "prefix": stable_prefix,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"mewcode:{digest}"


def _usage_counter(source: Any, *names: str) -> tuple[int, bool]:
    return usage_counter(source, *names)


def _openai_cache_usage(
    usage: Any, details_name: str
) -> tuple[int, int, bool]:
    details = (
        usage.get(details_name)
        if isinstance(usage, dict)
        else getattr(usage, details_name, None)
    ) if usage is not None else None
    cache_read, read_reported = _usage_counter(
        details,
        "cached_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
    )
    cache_creation, creation_reported = _usage_counter(
        details,
        "cache_write_tokens",
        "cache_write_input_tokens",
        "cache_creation_input_tokens",
    )
    if not read_reported:
        cache_read, read_reported = _usage_counter(
            usage,
            "cached_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
        )
    if not creation_reported:
        cache_creation, creation_reported = _usage_counter(
            usage,
            "cache_write_tokens",
            "cache_write_input_tokens",
            "cache_creation_input_tokens",
        )
    return cache_read, cache_creation, read_reported or creation_reported


def _deepseek_cache_usage(
    usage: Any, prompt_tokens: int | None = None
) -> tuple[int, int, bool]:
    """Return DeepSeek hit/miss counters without conflating missing with zero.

    Official DeepSeek responses expose both ``prompt_cache_*`` fields.  A few
    OpenAI-compatible relays expose only one side (or use the shorter aliases),
    so reconcile the missing side from ``prompt_tokens`` when possible.
    """
    metrics = resolve_cache_usage_provider("deepseek", "openai-compat").extract_usage(
        usage
    )
    if prompt_tokens is not None and not metrics.prompt_tokens:
        cache_read, read_reported = _usage_counter(
            usage, "prompt_cache_hit_tokens", "cache_hit_tokens"
        )
        cache_miss, miss_reported = _usage_counter(
            usage, "prompt_cache_miss_tokens", "cache_miss_tokens"
        )
        if read_reported and not miss_reported:
            cache_miss = max(prompt_tokens - cache_read, 0)
        elif miss_reported and not read_reported:
            cache_read = max(prompt_tokens - cache_miss, 0)
        return cache_read, cache_miss, read_reported or miss_reported
    return metrics.hit_tokens, metrics.miss_tokens, metrics.available


def _stream_response_headers(stream: Any) -> dict[str, str]:
    """Best-effort extraction of the raw HTTP headers from an SDK stream."""
    response = getattr(stream, "response", None) or getattr(stream, "_response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return {}
    return {str(key): str(value) for key, value in headers.items()}

def _safe_usage_base_url(url: str) -> str:
    """Remove userinfo and query secrets before persisting an endpoint URL."""
    parsed = urlparse(url)
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return parsed._replace(netloc=netloc, params="", query="", fragment="").geturl()



class LLMError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(LLMError):
    pass


class RateLimitError(LLMError):


    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        from mewcode.provider_cooldown import MAX_INLINE_RETRY, retry_seconds
        self.retry_after = retry_seconds(retry_after) or None
        if self.retry_after is not None and self.retry_after > MAX_INLINE_RETRY:
            self.retryable = False


class NetworkError(LLMError):
    pass


class LLMClient(ABC):
    _usage_recorder: Callable[[RequestUsage], None] | None = None
    _usage_enabled: bool = False
    _usage_provider_name: str = ""
    _usage_protocol: str = ""
    _usage_base_url: str = ""
    _usage_endpoint: str = ""
    _usage_model: str = ""
    _usage_key_fingerprint: str = ""
    _usage_organization_id: str = ""
    _usage_expected_organization_id: str = ""
    _usage_project_id: str = ""
    _usage_api_key_id: str = ""

    def _api_status_error(self, error: Any, hint: str = "") -> LLMError:
        """Keep the actual selected route and gateway request id on failures."""
        response = getattr(error, "response", None)
        request = getattr(response, "request", None)
        url = _safe_usage_base_url(str(getattr(request, "url", "") or self._usage_base_url))
        headers = getattr(response, "headers", {}) or {}
        request_id = headers.get("x-request-id") or headers.get("request-id") or ""
        route = f"Provider={self._usage_provider_name or self._usage_protocol}; model={getattr(self, 'model', self._usage_model)}; URL={url}"
        if request_id:
            route += f"; request_id={request_id}"
        message = f"API error ({error.status_code}): {error.message}{hint}\n请求路由：{route}"
        return LLMError(message, status_code=error.status_code)

    @abstractmethod
    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield TextDelta("")

    def configure_usage_tracking(
        self, config: ProviderConfig, *, endpoint: str
    ) -> None:
        self._usage_enabled = config.usage.enabled
        self._usage_provider_name = (
            "OpenCode Go" if config.provider_type == "opencode-go" else config.name
        )
        self._usage_protocol = config.protocol
        self._usage_base_url = _safe_usage_base_url(config.base_url)
        self._usage_endpoint = endpoint
        self._usage_model = config.model
        self._usage_key_fingerprint = credential_fingerprint(config.resolve_api_key())
        headers = {k.lower(): v for k, v in config.default_headers.items()}
        self._usage_expected_organization_id = (
            headers.get("openai-organization")
            or config.usage.upstream_organization_id
        )
        self._usage_organization_id = (
            headers.get("openai-organization")
            or headers.get("chatgpt-account-id")
            or config.usage.upstream_organization_id
        )
        self._usage_project_id = (
            headers.get("openai-project")
            or (config.usage.project_ids[0] if len(config.usage.project_ids) == 1 else "")
        )
        self._usage_api_key_id = (
            config.usage.api_key_ids[0]
            if len(config.usage.api_key_ids) == 1
            else ""
        )

    def set_usage_recorder(
        self, recorder: Callable[[RequestUsage], None] | None
    ) -> None:
        self._usage_recorder = recorder

    def get_usage_recorder(self) -> Callable[[RequestUsage], None] | None:
        return self._usage_recorder

    async def scheduled_stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run one provider stream through the shared leaf-work scheduler."""

        async for event in scheduled_client_stream(
            self, conversation, system=system, tools=tools
        ):
            yield event

    def _record_stream_end(
        self,
        event: StreamEnd,
        response_headers: dict[str, str] | None = None,
        request_metadata: dict | None = None,
    ) -> StreamEnd:
        if not self._usage_enabled or self._usage_recorder is None:
            return event
        headers = {str(k).lower(): str(v) for k, v in (response_headers or {}).items()}
        host = (urlparse(self._usage_base_url).hostname or "").lower()
        official = (
            self._usage_protocol == "anthropic" and host == "api.anthropic.com"
        ) or (
            self._usage_protocol in {"openai", "openai-compat"}
            and host in {"api.openai.com", "chatgpt.com"}
        )
        response_organization_id = headers.get("openai-organization", "")
        response_project_id = headers.get("openai-project", "")
        organization_id = (
            response_organization_id
            or headers.get("x-mewcode-upstream-organization-id")
            or self._usage_organization_id
        )
        project_id = (
            response_project_id
            or headers.get("x-mewcode-upstream-project-id")
            or self._usage_project_id
        )
        api_key_id = (
            headers.get("x-mewcode-upstream-api-key-id") or self._usage_api_key_id
        )
        receipt = headers.get("x-mewcode-usage-receipt", "")
        signature = headers.get("x-mewcode-usage-signature", "")
        request_id = event.request_id or headers.get("x-request-id", "")
        upstream_request_id = headers.get("x-mewcode-upstream-request-id", "") or (
            request_id if official else ""
        )
        evidence = EvidenceLevel.OFFICIAL_RESPONSE if official else EvidenceLevel.RELAY_REPORTED
        if (
            official
            and response_organization_id
            and self._usage_expected_organization_id
            and response_organization_id != self._usage_expected_organization_id
        ):
            attribution = AttributionStatus.MISMATCH
        elif official and (response_organization_id or response_project_id):
            attribution = AttributionStatus.REQUEST_VERIFIED
        elif organization_id or project_id or api_key_id or official:
            attribution = AttributionStatus.CONFIGURED
        else:
            attribution = AttributionStatus.UNVERIFIABLE
        cache_slot = getattr(
            getattr(self, "cache_usage_provider", None), "slot", None
        )
        record = RequestUsage(
            provider_name=self._usage_provider_name,
            protocol=self._usage_protocol,
            base_url=self._usage_base_url,
            endpoint=self._usage_endpoint,
            configured_model=self._usage_model,
            response_model=event.model,
            response_id=event.response_id,
            request_id=request_id,
            upstream_request_id=upstream_request_id,
            service_tier=event.service_tier,
            organization_id=organization_id,
            project_id=project_id,
            api_key_id=api_key_id,
            credential_fingerprint=self._usage_key_fingerprint,
            tokens=TokenUsage(
                input_tokens=event.input_tokens,
                cached_input_tokens=event.cache_read,
                cache_write_input_tokens=event.cache_creation,
                output_tokens=event.output_tokens,
                reasoning_tokens=event.reasoning_tokens,
            ),
            cache_usage_available=event.cache_usage_available,
            rate_limits=RateLimitSnapshot.from_headers(headers),
            evidence=evidence,
            attribution=attribution,
            receipt_sha256=(
                hashlib.sha256(receipt.encode("utf-8")).hexdigest()
                if receipt
                else ""
            ),
            receipt_signature_present=bool(signature),
            metadata={
                "model_call": {"request": request_metadata,
                    "response": {"model": event.model or None, "effort": event.provider_effort or None,
                        "finish_reason": event.provider_stop_reason or None,
                        "normalized_stop_reason": event.stop_reason,
                        "effort_execution_verified": False}},
                "system_fingerprint": event.system_fingerprint,
                "cache_slot": str(getattr(cache_slot, "name", "") or ""),
                "cache_provider": str(
                    getattr(cache_slot, "provider", "") or ""
                ),
                "cache_type": str(
                    getattr(cache_slot, "cache_type", "") or ""
                ),
                "cache_key": event.cache_key,
                "cache_mode": event.cache_mode,
                "cache_breakpoints": event.cache_breakpoints,
                "estimated_cost_usd": (
                    estimate_request_cost_usd(
                        resolve_model_capabilities(
                            self._usage_model,
                            self._usage_base_url,
                        ),
                        cache_hit_tokens=event.cache_read,
                        cache_miss_tokens=event.input_tokens,
                        output_tokens=event.output_tokens,
                    )
                    if self._usage_provider_name == "OpenCode Go"
                    else None
                ),
                "cost_basis": (
                    "local_estimate"
                    if self._usage_provider_name == "OpenCode Go"
                    else ""
                ),
            },
        )
        try:
            from mewcode.usage.service import apply_scope
            self._usage_recorder(apply_scope(record))
        except Exception as exc:
            log.debug("failed to persist usage record: %s", exc)
        return event

    async def _record_stream_end_async(self, event, response_headers=None, request_metadata=None):
        if not self._usage_enabled or self._usage_recorder is None:
            return event
        # Ledger durability is required for this request. Its SQLite fsync must
        # not stop every other model/tool sharing the event loop, and to_thread
        # preserves the request's usage attribution ContextVars.
        return await asyncio.to_thread(self._record_stream_end, event, response_headers, request_metadata)

    def set_max_output_tokens(self, tokens: int) -> None:
        pass

    def set_effort(self, level: str) -> None:
        """调整模型的推理强度（/effort 命令）。不支持的客户端静默忽略。"""

    async def list_models(self) -> list[str] | None:
        """从 provider 拉取可用模型列表（/model 命令）。

        返回 None 表示该 provider 不支持列表端点或拉取失败。"""
        return None


# /effort 命令的可选级别，按协议区分。anthropic 有 "off"（关闭思考），
# OpenAI 系列没有真正意义上的关闭项，从 low 起。
EFFORT_LEVELS: dict[str, list[str]] = {
    "anthropic": ["off", "low", "medium", "high"],
    "openai": ["low", "medium", "high"],
    "openai-compat": ["low", "medium", "high"],
}

_GPT_56_EFFORT_LEVELS = ["none", "low", "medium", "high", "xhigh", "max"]
_GROK_47_EFFORT_LEVELS = ["low", "medium", "high", "xhigh"]

# OpenAI 系推理模型族（支持 reasoning_effort 参数）。
# 中转站 /v1/models 不返回能力字段（实测 sharesai 只有 id/name），
# 只能按模型名识别；不匹配时 effort 不适用（选项为空）。
_REASONING_MODEL_MARKERS = (
    "gpt-5", "codex",
    "kimi", "glm-", "mimo-", "hy3", "qwen3", "minimax-",
)
_REASONING_MODEL_PREFIXES = ("o1", "o3", "o4")


def _catalog_for_effort(
    catalog_service: ModelCatalogService | None = None,
) -> ModelCatalogService:
    """Return the shared metadata service without importing client from it."""

    return catalog_service or DEFAULT_MODEL_CATALOG


def model_effort_levels(
    protocol: str,
    model: str,
    *,
    catalog_service: ModelCatalogService | None = None,
) -> list[str]:
    """按协议 + 模型能力返回可用的 effort 档位。

    - 已知现代模型优先使用目录中的精确档位；旧 Anthropic 模型保留
      off/low/medium/high thinking budget fallback。
    - 未知 OpenAI 系模型仍按旧推理模型族规则回退；其他模型返回空列表。
    """
    catalog = _catalog_for_effort(catalog_service)
    # Catalog metadata is the precise source for known current families.  An
    # unknown model still receives the conservative protocol fallback below.
    # A known row with empty efforts means the model does not take a separate
    # effort parameter: do not fall through to protocol defaults.
    descriptor = catalog.describe(model)
    if descriptor is not None:
        catalog_levels = list(descriptor.efforts)
        if not catalog_levels:
            return []
        if protocol == "anthropic":
            if descriptor.provider_brand == "anthropic":
                return catalog_levels
        elif protocol in {"openai", "openai-compat"}:
            return catalog_levels

    if protocol == "anthropic":
        return list(EFFORT_LEVELS["anthropic"])
    levels = EFFORT_LEVELS.get(protocol)
    if levels is None:
        return []
    capabilities = resolve_model_capabilities(model)
    if capabilities.context_channel == "deepseek":
        return ["off", "low", "high", "max"]
    # Grok 4.7 is exposed dynamically by Grok Build before it appears in the
    # stable public model catalog.  Keep its picker levels aligned with the
    # reasoning values accepted by the Responses endpoint.
    if capabilities.family == "grok-4.7":
        return list(_GROK_47_EFFORT_LEVELS)
    m = model.lower()
    if "gpt-5.6" in m:
        return list(_GPT_56_EFFORT_LEVELS)
    if any(marker in m for marker in _REASONING_MODEL_MARKERS) or m.startswith(
        _REASONING_MODEL_PREFIXES
    ):
        return list(levels)
    return []

def model_supports_vision(
    protocol: str,
    model: str,
    *,
    catalog_service: ModelCatalogService | None = None,
) -> bool:
    """Return whether *model* accepts image input.

    The builtin catalog is the source of truth for known ids.  Unknown ids
    only inherit vision from well-known multimodal families; text-only
    families such as GLM-5 and MiniMax M2 must not be advertised as Vision.
    """
    if protocol == "typesafe":
        return False
    m = model.strip().lower()
    if not m:
        return False
    descriptor = _catalog_for_effort(catalog_service).describe(model)
    if descriptor is not None:
        return bool(descriptor.vision)
    if any(
        token in m
        for token in ("-vision", "vision-", "-vl", "vl-", "-omni", "omni-")
    ):
        return True
    # Dated snapshots and older SKUs that are not catalogued yet, but whose
    # families officially accept images.
    if m.startswith(("gpt-", "claude", "gemini-", "grok-", "kimi-", "qwen")):
        return True
    return False


# anthropic 协议下各 effort 级别对应的 thinking budget。
_ANTHROPIC_EFFORT_BUDGET = {"low": 2048, "medium": 8192}


def _extract_context_window(info: Any) -> int | None:
    """从 /v1/models/{model} 的响应中提取 context window。

    各家 provider/中转站字段名不一，逐个尝试常见字段（含 pydantic
    model_extra 里的扩展字段）。取不到返回 None。"""
    fields = (
        "max_input_tokens",
        "context_length",
        "context_window",
        "max_context_length",
        "max_tokens",
    )
    for f in fields:
        v = getattr(info, f, None)
        if isinstance(v, int) and v > 0:
            return v
    extra = getattr(info, "model_extra", None)
    if isinstance(extra, dict):
        for f in fields:
            v = extra.get(f)
            if isinstance(v, int) and v > 0:
                return v
    return None


def _supports_adaptive_thinking(model: str) -> bool:
    for family in ("claude-opus-4-", "claude-sonnet-4-"):
        if model.startswith(family):
            rest = model[len(family):]
            if rest and rest[0].isdigit() and int(rest[0]) >= 6:
                return True
    return False


def _supports_catalog_effort_wire(model: str) -> bool:
    """Whether Anthropic's modern ``output_config.effort`` wire is allowed."""

    descriptor = DEFAULT_MODEL_CATALOG.describe(model, provider_brand="anthropic")
    return bool(
        descriptor is not None
        and descriptor.provider_brand == "anthropic"
        and descriptor.efforts
        and "off" not in descriptor.efforts
    )


class AnthropicClient(LLMClient):
    def __init__(self, config: ProviderConfig) -> None:
        self.model = config.model
        self.base_url = config.base_url
        self.capabilities = resolve_model_capabilities(
            config.model, config.base_url, config.context_channel
        )
        self.cache_usage_provider = resolve_cache_usage_provider(
            self.capabilities.cache_usage_format, "anthropic"
        )
        self.thinking = (
            config.thinking or self.capabilities.context_channel == "deepseek"
        )
        self.max_output_tokens = config.get_max_output_tokens()
        self._catalog_effort_levels = model_effort_levels("anthropic", self.model)
        self._uses_catalog_effort_wire = _supports_catalog_effort_wire(self.model)
        # thinking 配置映射为初始 effort：开=high，关=off。
        self.effort = "high" if self.thinking else "off"
        api_key = config.resolve_api_key()
        if not api_key:
            if config.provider_type == "opencode-go":
                raise AuthenticationError(
                    "OpenCode Go API key not found. Set api_key in "
                    ".mewcode/config.yaml or set OPENCODE_API_KEY."
                )
            raise AuthenticationError(
                "Anthropic API key not found. "
                "Set it in .mewcode/config.yaml or via ANTHROPIC_API_KEY env var."
            )
        self._client = AsyncAnthropic(
            api_key=api_key, base_url=config.base_url, timeout=LLM_TIMEOUT,
            max_retries=0,  # The agent owns the visible, bounded retry budget.
            default_headers=_llm_default_headers(config, api_key),
        )

    def set_max_output_tokens(self, tokens: int) -> None:
        self.max_output_tokens = tokens

    def set_effort(self, level: str) -> None:
        levels = self._catalog_effort_levels or EFFORT_LEVELS["anthropic"]
        if level in levels:
            self.effort = level
            self.thinking = level != "off"

    async def list_models(self) -> list[str] | None:
        try:
            page = await self._client.models.list()
            return [m.id for m in page.data]
        except Exception:
            return None

    async def fetch_model_context_window(self) -> int | None:
        """向 Anthropic 兼容的 /v1/models/{model} 端点查询模型的
        max_input_tokens（context window 解析的第 2 层）。

        采用尽力而为策略：遇到任何错误——非 anthropic 端点、网络故障、
        超时、字段缺失——都返回 ``None`` 而非抛出异常，以便调用方降级到
        下一层。它的阻塞时间不会超过 ANTHROPIC_MODEL_FETCH_TIMEOUT，也不会
        向外传播异常，因此在启动时调用是安全的。
        """
        try:
            info = await self._client.models.retrieve(
                self.model, timeout=ANTHROPIC_MODEL_FETCH_TIMEOUT
            )
            window = getattr(info, "max_input_tokens", None)
            if isinstance(window, int) and window > 0:
                return window
            return None
        except Exception:
            return None

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import anthropic as _anthropic

        messages = build_anthropic_messages(conversation.get_messages())

        # 在最长稳定前缀上标记 prompt cache 断点：system、tools
        # 以及最后一条 user 消息的尾部。Anthropic 会缓存到每个断点，
        # 并在下次请求时按字节比对——context.manager 中的
        # ContentReplacementState 保证断点之后的 tool_result 内容保持稳定。
        _mark_last_user_tail_for_cache(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        if tools:
            kwargs["tools"] = _mark_last_tool_for_cache(tools)

        if self.effort != "off":
            if self._uses_catalog_effort_wire:
                # Anthropic's modern effort control is request-level and must
                # not be sent to legacy dated Claude models, which continue to
                # use the thinking budget contract below.
                kwargs["output_config"] = {"effort": self.effort}
            elif _supports_adaptive_thinking(self.model):
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": 0}
            else:
                budget = _ANTHROPIC_EFFORT_BUDGET.get(self.effort)
                if budget is None:  # high：用足输出上限
                    budget = max(self.max_output_tokens - 1, 1024)
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": budget,
                }

        current_tool_name = ""
        current_tool_id = ""
        json_accum = ""
        current_tool_initial_input: dict[str, Any] = {}
        saw_tool_json_delta = False
        in_thinking = False
        thinking_accum = ""
        thinking_signature = ""

        _apply_opencode_go_session_headers(kwargs, self.base_url)

        from mewcode.experts.model_receipts import request_receipt
        request_metadata = request_receipt(kwargs, self) if self._usage_recorder else None
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                response_headers = _stream_response_headers(stream)
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "thinking":
                            in_thinking = True
                            thinking_accum = ""
                            thinking_signature = ""
                        elif block.type == "tool_use":
                            current_tool_name = block.name
                            current_tool_id = block.id
                            json_accum = ""
                            initial_input = getattr(block, "input", None)
                            current_tool_initial_input = (
                                dict(initial_input) if isinstance(initial_input, dict) else {}
                            )
                            saw_tool_json_delta = False
                            yield ToolCallStart(
                                tool_name=current_tool_name,
                                tool_id=current_tool_id,
                            )
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield TextDelta(text=delta.text)
                        elif delta.type == "thinking_delta":
                            thinking_accum += delta.thinking
                            yield ThinkingDelta(text=delta.thinking)
                        elif delta.type == "signature_delta":
                            thinking_signature = delta.signature
                        elif delta.type == "input_json_delta":
                            json_accum += delta.partial_json
                            saw_tool_json_delta = True
                            yield ToolCallDelta(text=delta.partial_json)
                    elif event.type == "content_block_stop":
                        if in_thinking:
                            yield ThinkingComplete(
                                thinking=thinking_accum,
                                signature=thinking_signature,
                                kind="anthropic",
                            )
                            in_thinking = False
                        if current_tool_name:
                            if saw_tool_json_delta:
                                try:
                                    args = json.loads(json_accum) if json_accum else {}
                                except json.JSONDecodeError:
                                    args = {}
                            else:
                                args = current_tool_initial_input
                            yield ToolCallComplete(
                                tool_id=current_tool_id,
                                tool_name=current_tool_name,
                                arguments=args,
                            )
                            current_tool_name = ""
                            current_tool_id = ""
                            json_accum = ""
                            current_tool_initial_input = {}
                            saw_tool_json_delta = False
                    elif event.type == "message_stop":
                        pass

                final = await stream.get_final_message()
                usage = final.usage
                cache_metrics = self.cache_usage_provider.extract_usage(usage)
                yield await self._record_stream_end_async(
                    StreamEnd(
                        stop_reason=final.stop_reason or "end_turn",
                        input_tokens=cache_metrics.miss_tokens,
                        output_tokens=usage.output_tokens,
                        cache_read=cache_metrics.hit_tokens,
                        cache_creation=cache_metrics.creation_tokens,
                        cache_usage_available=cache_metrics.available,
                        response_id=getattr(final, "id", "") or "",
                        request_id=getattr(final, "_request_id", "") or "",
                        model=getattr(final, "model", "") or "",
                        provider_stop_reason=final.stop_reason or "",
                    ),
                    response_headers,
                    request_metadata,
                )

        except _anthropic.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _anthropic.RateLimitError as e:
            retry = e.response.headers.get("retry-after") if e.response else None
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=retry,
            ) from e
        except _anthropic.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _anthropic.APIStatusError as e:
            raise self._api_status_error(e) from e


class OpenAIClient(LLMClient):
    def __init__(self, config: ProviderConfig) -> None:
        self._codex_auth_path = config._codex_auth_path
        self._codex_account_id = next((value for name, value in config.default_headers.items()
                                      if name.lower() == "chatgpt-account-id"), "")
        self.model = config.model
        self.base_url = config.base_url
        self.capabilities = resolve_model_capabilities(
            config.model, config.base_url, config.context_channel
        )
        self.cache_usage_provider = resolve_cache_usage_provider(
            self.capabilities.cache_usage_format, "openai"
        )
        self.max_output_tokens = config.get_max_output_tokens()
        # Explicit service settings also apply to user-configured relays.
        # Let an unsupported endpoint report its error instead of dropping it.
        self.service_tier = config.service_tier
        api_key = config.resolve_api_key()
        if not api_key:
            if config.provider_type == "opencode-go":
                raise AuthenticationError(
                    "OpenCode Go API key not found. Set api_key in "
                    ".mewcode/config.yaml or set OPENCODE_API_KEY."
                )
            raise AuthenticationError(
                "OpenAI API key not found. "
                "Set it in .mewcode/config.yaml or via OPENAI_API_KEY env var."
            )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=LLM_TIMEOUT,
            max_retries=0,
            default_headers=_llm_default_headers(config, api_key),
        )
        # 空串表示未显式设置（不向 API 发送 reasoning 参数）。
        self.effort = (
            "high" if self.capabilities.context_channel == "deepseek" else ""
        )

    def set_max_output_tokens(self, tokens: int) -> None:
        self.max_output_tokens = tokens

    def set_effort(self, level: str) -> None:
        # OpenAI 系无 off（关闭）概念；档位随平台扩展（low/medium/high/
        # xhigh/ultra/max…），可选集由上层（codex config.toml / 模型能力
        # 识别）校验，client 层透传非空档位。
        descriptor = DEFAULT_MODEL_CATALOG.describe(self.model)
        if descriptor is not None and descriptor.family in {"gpt-6-astra", "gpt-6.1-sol"}:
            if level in {"none", "minimal", "off"}:
                level = "low"
            if level == "ultra" and urlparse(self.base_url).hostname == "api.openai.com":
                level = "max"
        if level and level != "off":
            self.effort = level

    async def list_models(self) -> list[str] | None:
        try:
            auth = await self._current_auth_headers()
            page = await self._client.models.list(**({"extra_headers": auth} if auth else {}))
            return [m.id for m in page.data]
        except Exception:
            return None

    async def fetch_model_context_window(self) -> int | None:
        """向 OpenAI 兼容的 /v1/models/{model} 查询模型窗口大小
        （context window 解析的第 2 层）。尽力而为，任何失败都返回 None。"""
        try:
            auth = await self._current_auth_headers()
            info = await self._client.models.retrieve(
                self.model, timeout=ANTHROPIC_MODEL_FETCH_TIMEOUT,
                **({"extra_headers": auth} if auth else {}),
            )
            return _extract_context_window(info)
        except Exception:
            return None

    async def _current_auth_headers(self) -> dict[str, str]:
        if not self._codex_auth_path:
            return {}
        from mewcode.codex_credentials import CodexLoginRequired, current_codex_headers
        try:
            return await asyncio.to_thread(current_codex_headers, self._codex_auth_path,
                base_url=self.base_url, account_id=self._codex_account_id)
        except CodexLoginRequired as exc:
            raise AuthenticationError(str(exc), status_code=401) from None

    async def _create_response_stream(self, kwargs: dict[str, Any]):
        """Retry a rejected request once only if its login actually rotated."""
        import openai as _openai
        try:
            return await self._client.responses.create(**kwargs)
        except _openai.AuthenticationError:
            if not self._codex_auth_path:
                raise
            refreshed = await self._current_auth_headers()
            previous = kwargs.get("extra_headers") or {}
            if refreshed.get("Authorization") == previous.get("Authorization"):
                raise
            # The 401 happened before a response stream existed, so no model
            # output or tool call can be replayed by this bounded retry.
            kwargs["extra_headers"] = {**previous, **refreshed}
            return await self._client.responses.create(**kwargs)

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import openai as _openai

        # Read the login owner's latest token before serializing a long chat.
        # Per-request headers avoid mutating a shared SDK client's credentials.
        auth_headers = await self._current_auth_headers()
        input_messages = build_openai_input(conversation.get_messages())
        supports_cache_key, supports_explicit_cache = (
            _prompt_cache_route_capabilities(self.base_url, self.model)
        )
        cache_prefix = (
            _mark_stable_openai_prefix(input_messages)
            if supports_cache_key
            else None
        )
        if cache_prefix is not None:
            marked_input_messages, stable_prefix = cache_prefix
            if supports_explicit_cache:
                input_messages = marked_input_messages

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": input_messages,
            "stream": True,
            "store": False,
        }
        if auth_headers:
            kwargs["extra_headers"] = auth_headers
        # The ChatGPT Codex backend rejects max_output_tokens with HTTP 400.
        # Keep the local budget/usage state, but don't send this unsupported
        # public-API field. This route has no wire-level output cap guarantee.
        if (getattr(self, "response_token_limit", None) is not None
                and not _is_codex_backend_route(self.base_url)):
            kwargs["max_output_tokens"] = self.response_token_limit
        if self.effort:
            kwargs["reasoning"] = {"effort": self.effort}
        if system:
            kwargs["instructions"] = system
        if tools:
            kwargs["tools"] = tools
        if self.service_tier:
            kwargs["service_tier"] = self.service_tier
        cache_key = ""
        cache_mode = ""
        cache_breakpoints = 0
        if cache_prefix is not None and supports_cache_key:
            cache_key = _openai_prompt_cache_key(
                self.model, system, tools, stable_prefix
            )
            kwargs["prompt_cache_key"] = cache_key
            cache_mode = "implicit"
        if cache_prefix is not None and supports_explicit_cache:
            # openai-python 2.34 predates this public GPT-5.6 field.
            # Hybrid caching: explicit breakpoints preserve MewCode's stable
            # environment prefix while implicit mode can also cache the newest
            # tool-loop prefix. Explicit-only mode misses that growing tail.
            kwargs["extra_body"] = {
                "prompt_cache_options": {"mode": "implicit", "ttl": "30m"}
            }
            cache_mode = "implicit+explicit"
            cache_breakpoints = _count_prompt_cache_breakpoints(input_messages)

        response_tool_aliases: dict[tuple[str, Any], dict[str, Any]] = {}

        def resolve_response_tool_call(event: Any, item: Any = None) -> dict[str, Any]:
            aliases: list[tuple[str, Any]] = []
            for source in (event, item):
                if source is None:
                    continue
                item_id = getattr(source, "item_id", None)
                if item_id:
                    aliases.append(("item_id", str(item_id)))
                call_id = getattr(source, "call_id", None)
                if call_id:
                    aliases.append(("call_id", str(call_id)))
                output_index = getattr(source, "output_index", None)
                if output_index is not None:
                    if isinstance(output_index, bool):
                        raise LLMError("Responses function_call has an invalid output_index")
                    if isinstance(output_index, int):
                        aliases.append(("output_index", output_index))
                    elif isinstance(output_index, str) and output_index.isdecimal():
                        aliases.append(("output_index", int(output_index)))
                    else:
                        raise LLMError("Responses function_call has an invalid output_index")
                if source is item:
                    item_id = getattr(source, "id", None)
                    if item_id:
                        aliases.append(("item_id", str(item_id)))

            # Event and item payloads can repeat the same identity fields.
            aliases = list(dict.fromkeys(aliases))
            if not aliases:
                raise LLMError(
                    "Responses function_call event is missing item_id, call_id, and output_index"
                )

            matches = {
                id(response_tool_aliases[alias]): response_tool_aliases[alias]
                for alias in aliases
                if alias in response_tool_aliases
            }
            if len(matches) > 1:
                raise LLMError("Responses function_call event has conflicting identities")
            state = next(iter(matches.values()), None)
            if state is None:
                state = {
                    "item_id": None,
                    "call_id": None,
                    "output_index": None,
                    "name": "",
                    "tool_id": "",
                    "json_delta": "",
                    "pending_deltas": [],
                    "started": False,
                    "completed": False,
                }
            for alias in aliases:
                existing = response_tool_aliases.get(alias)
                if existing is not None and existing is not state:
                    raise LLMError("Responses function_call event has conflicting identities")
                response_tool_aliases[alias] = state
                identity_field, identity_value = alias
                existing_value = state[identity_field]
                if existing_value is not None and existing_value != identity_value:
                    raise LLMError("Responses function_call identity changed during the stream")
                state[identity_field] = identity_value
            return state

        def set_response_tool_name(state: dict[str, Any], value: Any) -> None:
            if value is None:
                return
            name = str(value)
            if not name:
                return
            if state["name"] and state["name"] != name:
                raise LLMError("Responses function_call name changed during the stream")
            state["name"] = name

        def response_tool_id(state: dict[str, Any]) -> str:
            if not state["tool_id"]:
                state["tool_id"] = (
                    state["call_id"]
                    or state["item_id"]
                    or (
                        f"responses-output-index-{state['output_index']}"
                        if state["output_index"] is not None
                        else ""
                    )
                )
            if not state["tool_id"]:
                raise LLMError("Responses function_call is missing a usable call identity")
            return state["tool_id"]

        def start_response_tool_call(state: dict[str, Any]) -> list[StreamEvent]:
            events: list[StreamEvent] = []
            if not state["started"] and state["name"]:
                state["started"] = True
                events.append(ToolCallStart(
                    tool_name=state["name"],
                    tool_id=response_tool_id(state),
                ))
            if state["started"] and state["pending_deltas"]:
                events.append(ToolCallDelta(text="".join(state["pending_deltas"])))
                state["pending_deltas"].clear()
            return events

        def parse_response_tool_arguments(raw: Any) -> dict[str, Any]:
            if isinstance(raw, dict):
                return raw
            if raw is None or raw == "":
                return {}
            try:
                parsed = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                # Preserve the existing malformed-argument fallback, but never
                # infer a missing function name from argument content.
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def complete_response_tool_call(
            state: dict[str, Any], raw_arguments: Any
        ) -> list[StreamEvent]:
            events = start_response_tool_call(state)
            if state["completed"]:
                return events
            if not state["name"]:
                raise LLMError("Responses function_call is missing its function name")
            tool_id = response_tool_id(state)
            raw = state["json_delta"] if raw_arguments is None else raw_arguments
            arguments = parse_response_tool_arguments(raw)
            state["completed"] = True
            events.append(ToolCallComplete(
                tool_id=tool_id,
                tool_name=state["name"],
                arguments=arguments,
            ))
            return events

        saw_output_delta = False
        saw_private_reasoning_delta = False
        saw_reasoning_summary_delta = False

        _apply_opencode_go_session_headers(kwargs, self.base_url)

        from mewcode.experts.model_receipts import request_receipt
        request_metadata = request_receipt(kwargs, self) if self._usage_recorder else None
        try:
            response_stream = await self._create_response_stream(kwargs)
            response_headers = _stream_response_headers(response_stream)
            async for event in response_stream:
                event_type = str(getattr(event, "type", "") or "")
                if event_type in {"response.created", "response.queued"}:
                    yield StreamStatus(
                        stage="accepted",
                        detail="Provider 已接收请求，正在排队或准备推理。",
                    )
                elif event_type == "response.in_progress":
                    yield StreamStatus(
                        stage="processing",
                        detail="Provider 正在处理请求。",
                    )
                elif event_type in {
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_summary.delta",
                    "response.reasoning.delta",
                }:
                    delta = str(getattr(event, "delta", "") or "")
                    if delta:
                        kind = (
                            "summary"
                            if "reasoning_summary" in event_type
                            else "private"
                        )
                        if kind == "summary":
                            saw_reasoning_summary_delta = True
                        else:
                            saw_private_reasoning_delta = True
                        yield ThinkingDelta(text=delta, kind=kind)
                    else:
                        yield StreamStatus(
                            stage="activity",
                            detail=f"Provider 流事件：{event_type}",
                        )
                elif event_type == "response.output_text.delta":
                    delta = str(getattr(event, "delta", "") or "")
                    if delta:
                        saw_output_delta = True
                        yield TextDelta(text=delta)
                    else:
                        yield StreamStatus(
                            stage="activity",
                            detail="Provider 正在继续输出响应。",
                        )
                elif event_type == "response.output_text.done":
                    text = str(getattr(event, "text", "") or "")
                    if text and not saw_output_delta:
                        saw_output_delta = True
                        yield TextDelta(text=text)
                    else:
                        yield StreamStatus(
                            stage="activity",
                            detail="Provider 正文输出块已完成。",
                        )
                elif event_type in {
                    "response.reasoning_text.done",
                    "response.reasoning_summary_text.done",
                    "response.reasoning_summary.done",
                    "response.reasoning.done",
                }:
                    text = str(getattr(event, "text", "") or "")
                    kind = (
                        "summary"
                        if "reasoning_summary" in event_type
                        else "private"
                    )
                    already_streamed = (
                        saw_reasoning_summary_delta
                        if kind == "summary"
                        else saw_private_reasoning_delta
                    )
                    if text and not already_streamed:
                        if kind == "summary":
                            saw_reasoning_summary_delta = True
                        else:
                            saw_private_reasoning_delta = True
                        yield ThinkingDelta(text=text, kind=kind)
                    else:
                        yield StreamStatus(
                            stage="activity",
                            detail="Provider 推理输出块已完成。",
                        )
                elif event_type == "response.function_call_arguments.delta":
                    state = resolve_response_tool_call(event)
                    set_response_tool_name(state, getattr(event, "name", None))
                    for call_event in start_response_tool_call(state):
                        yield call_event
                    delta = getattr(event, "delta", None)
                    if delta is not None:
                        delta = str(delta)
                        state["json_delta"] += delta
                        if state["started"]:
                            yield ToolCallDelta(text=delta)
                        else:
                            state["pending_deltas"].append(delta)
                elif event_type == "response.function_call_arguments.done":
                    state = resolve_response_tool_call(event)
                    set_response_tool_name(state, getattr(event, "name", None))
                    raw_arguments = getattr(event, "arguments", None)
                    for call_event in complete_response_tool_call(state, raw_arguments):
                        yield call_event
                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        state = resolve_response_tool_call(event, item)
                        set_response_tool_name(state, getattr(item, "name", None))
                        for call_event in start_response_tool_call(state):
                            yield call_event
                    elif item and getattr(item, "type", "") == "reasoning":
                        yield StreamStatus(
                            stage="thinking",
                            detail="Provider 已进入推理阶段。",
                        )
                    else:
                        yield StreamStatus(
                            stage="activity",
                            detail="Provider 新增了一个响应输出项。",
                        )
                elif event_type == "response.output_item.done":
                    # Some Responses-compatible providers omit text delta/done
                    # events and only include completed content on the item.
                    # Recover that content without duplicating normal streams.
                    item = getattr(event, "item", None)
                    item_type = str(getattr(item, "type", "") or "")
                    if item_type == "function_call":
                        state = resolve_response_tool_call(event, item)
                        set_response_tool_name(state, getattr(item, "name", None))
                        raw_arguments = getattr(item, "arguments", None)
                        for call_event in complete_response_tool_call(state, raw_arguments):
                            yield call_event
                    elif item_type == "message" and not saw_output_delta:
                        content = getattr(item, "content", None) or []
                        text = "".join(
                            str(getattr(part, "text", "") or "")
                            for part in content
                            if str(getattr(part, "type", "") or "")
                            in {"output_text", "text"}
                        )
                        if text:
                            saw_output_delta = True
                            yield TextDelta(text=text)
                        else:
                            yield StreamStatus(
                                stage="activity",
                                detail="Provider 响应输出项已完成。",
                            )
                    elif (
                        item_type == "reasoning"
                        and not saw_reasoning_summary_delta
                    ):
                        summary = getattr(item, "summary", None) or []
                        text = "".join(
                            str(getattr(part, "text", "") or "")
                            for part in summary
                            if str(getattr(part, "type", "") or "")
                            in {"summary_text", "text"}
                        )
                        if text:
                            saw_reasoning_summary_delta = True
                            yield ThinkingDelta(text=text, kind="summary")
                        else:
                            yield StreamStatus(
                                stage="activity",
                                detail="Provider 推理输出项已完成。",
                            )
                    else:
                        yield StreamStatus(
                            stage="activity",
                            detail="Provider 响应输出项已完成。",
                        )
                elif event_type in {"response.failed", "response.incomplete"}:
                    resp = getattr(event, "response", None)
                    error = getattr(resp, "error", None) if resp else None
                    incomplete = (
                        getattr(resp, "incomplete_details", None) if resp else None
                    )
                    message = str(getattr(error, "message", "") or "").strip()
                    reason = str(getattr(incomplete, "reason", "") or "").strip()
                    detail = message or reason or event_type
                    raise LLMError(f"Provider 响应未完成：{detail}")
                elif event_type == "error":
                    message = str(getattr(event, "message", "") or "").strip()
                    code = str(getattr(event, "code", "") or "").strip()
                    detail = message or code or "未知流式错误"
                    raise LLMError(f"Provider 流式错误：{detail}")
                elif event_type == "response.completed":
                    resp = getattr(event, "response", None)
                    usage = getattr(resp, "usage", None) if resp else None
                    # Responses API 的 input_tokens 包含缓存读取和写入 token。
                    # 分离三者后，input + cache_read + cache_creation 仍等于
                    # 完整 prompt 大小，并保留“未上报”与“明确为 0”的区别。
                    cache_metrics = self.cache_usage_provider.extract_usage(usage)
                    output_details = getattr(usage, "output_tokens_details", None)
                    reasoning_tokens, _ = _usage_counter(
                        output_details, "reasoning_tokens"
                    )
                    response_reasoning = getattr(resp, "reasoning", None)
                    response_effort = (response_reasoning.get("effort") if isinstance(response_reasoning, dict)
                                       else getattr(response_reasoning, "effort", None))
                    yield await self._record_stream_end_async(
                        StreamEnd(
                            stop_reason="end_turn",
                            input_tokens=cache_metrics.miss_tokens,
                            output_tokens=getattr(usage, "output_tokens", 0) or 0,
                            cache_read=cache_metrics.hit_tokens,
                            cache_creation=cache_metrics.creation_tokens,
                            cache_usage_available=cache_metrics.available,
                            reasoning_tokens=reasoning_tokens,
                            response_id=getattr(resp, "id", "") or "",
                            request_id=getattr(resp, "_request_id", "") or "",
                            model=getattr(resp, "model", "") or "",
                            provider_stop_reason=getattr(resp, "status", "") or "completed",
                            provider_effort=response_effort or "",
                            service_tier=getattr(resp, "service_tier", "") or "",
                            cache_key=cache_key,
                            cache_mode=cache_mode,
                            cache_breakpoints=cache_breakpoints,
                        ),
                        response_headers,
                        request_metadata,
                    )
                elif event_type:
                    # Preserve liveness for provider-specific or newly added
                    # Responses events. Consumers may not need to render the
                    # event, but an active SSE stream must reset idle watchdogs.
                    yield StreamStatus(
                        stage="activity",
                        detail=f"Provider 流事件：{event_type}",
                    )

        except _openai.AuthenticationError as e:
            if self._codex_auth_path:
                raise AuthenticationError(
                    "Codex/ChatGPT 登录认证失败（HTTP 401）。已读取最新本地令牌；"
                    "请在 Codex 中重新登录后重试，当前请求不会重复发送。",
                    status_code=401,
                ) from e
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _openai.RateLimitError as e:
            retry = None
            if hasattr(e, "response") and e.response is not None:
                retry = e.response.headers.get("retry-after")
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=retry,
            ) from e
        except _openai.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _openai.APIStatusError as e:
            raise self._api_status_error(e) from e


class OpenAICompatClient(LLMClient):
    """面向 OpenAI 兼容 provider 的客户端，使用 Chat Completions API。

    与面向较新的 Responses API（``/responses``）的 ``OpenAIClient`` 不同，
    本客户端使用受广泛支持的 Chat Completions 端点（``/chat/completions``），
    因此能兼容任何暴露 OpenAI 兼容接口的 provider（例如 vLLM、Ollama、
    Together、Azure OpenAI 等）。
    """

    def __init__(self, config: ProviderConfig) -> None:
        self.model = config.model
        self.base_url = config.base_url
        self.capabilities = resolve_model_capabilities(
            config.model, config.base_url, config.context_channel
        )
        self.cache_usage_provider = resolve_cache_usage_provider(
            self.capabilities.cache_usage_format, "openai-compat"
        )
        self.explicit_deepseek_compat = config.context_channel == "deepseek"
        self.max_output_tokens = config.get_max_output_tokens()
        self.service_tier = config.service_tier
        api_key = config.resolve_api_key()
        if not api_key:
            if config.provider_type == "opencode-go":
                raise AuthenticationError(
                    "OpenCode Go API key not found. Set api_key in "
                    ".mewcode/config.yaml or set OPENCODE_API_KEY."
                )
            raise AuthenticationError(
                "OpenAI-compatible API key not found. "
                "Set it in .mewcode/config.yaml or via OPENAI_API_KEY env var."
            )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=config.base_url,
            timeout=LLM_TIMEOUT,
            max_retries=0,
            default_headers=_llm_default_headers(config, api_key),
        )
        # 空串表示未显式设置（不向 API 发送 reasoning_effort 参数）。
        self.effort = (
            "high" if self.capabilities.context_channel == "deepseek" else ""
        )

    def set_max_output_tokens(self, tokens: int) -> None:
        self.max_output_tokens = tokens

    def set_effort(self, level: str) -> None:
        # 同 OpenAIClient：透传非空且非 off 的档位（扩展档由上层校验）。
        if self.capabilities.context_channel == "deepseek":
            if level in {"off", "low", "high", "max"}:
                self.effort = level
            return
        if level and level != "off":
            self.effort = level

    async def list_models(self) -> list[str] | None:
        try:
            page = await self._client.models.list()
            return [m.id for m in page.data]
        except Exception:
            return None

    async def fetch_model_context_window(self) -> int | None:
        """见 OpenAIClient.fetch_model_context_window。"""
        try:
            info = await self._client.models.retrieve(
                self.model, timeout=ANTHROPIC_MODEL_FETCH_TIMEOUT
            )
            return _extract_context_window(info)
        except Exception:
            return None

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 tool schema 转换成 Chat Completions 格式。

        tool 注册表为 ``openai`` 系列输出的是 Responses API 风格的 dict::

            {"type": "function", "name": "...", "description": "...",
             "parameters": {...}}

        而 Chat Completions 要求把 name/description/parameters 嵌套在
        ``function`` 键下::

            {"type": "function", "function": {"name": "...",
             "description": "...", "parameters": {...}}}
        """
        converted: list[dict[str, Any]] = []
        for t in tools:
            converted.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", t.get("input_schema", {})),
                },
            })
        return converted

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        import openai as _openai

        is_deepseek = self.capabilities.context_channel == "deepseek"
        is_deepseek_model = self.capabilities.family == "deepseek"
        messages = build_chat_completion_messages(
            conversation.get_messages(), deepseek=is_deepseek
        )

        # 如果有 system 消息则插入到消息列表头部。
        if system:
            messages = [{"role": "system", "content": system}] + messages

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        deepseek_wire_extensions = (
            self.capabilities.official_endpoint or self.explicit_deepseek_compat
        )
        if (
            self.effort
            and self.effort != "off"
            and (not is_deepseek or deepseek_wire_extensions)
        ):
            kwargs["reasoning_effort"] = self.effort
        if is_deepseek and deepseek_wire_extensions:
            kwargs["extra_body"] = {
                "thinking": {
                    "type": "disabled" if self.effort == "off" else "enabled"
                }
            }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
        if self.service_tier:
            kwargs["service_tier"] = self.service_tier

        # 用于累积 streaming tool call 的状态。Chat Completions 流按
        # tool_calls 列表中的位置索引下发 delta，我们按索引跟踪每个进行中的调用。
        active_calls: dict[int, dict[str, str]] = {}  # 索引 -> {id, name, args}
        reasoning_accum = ""

        _apply_opencode_go_session_headers(kwargs, self.base_url)

        from mewcode.experts.model_receipts import request_receipt
        request_metadata = request_receipt(kwargs, self) if self._usage_recorder else None
        try:
            response = await self._client.chat.completions.create(**kwargs)
            response_headers = _stream_response_headers(response)
            usage: Any = None
            usage_chunk: Any = None
            last_chunk: Any = None
            stop_reason = "end_turn"
            provider_stop_reason = ""
            stream_end_emitted = False
            async for chunk in response:
                last_chunk = chunk
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage
                    usage_chunk = chunk
                if not chunk.choices:
                    # 最后一个 chunk，只包含 usage 数据。
                    if chunk.usage:
                        # 兼容 provider 可能上报缓存读取/写入，也可能完全省略。
                        cache_read = 0
                        cache_creation = 0
                        cache_miss = 0
                        cache_usage_available = False
                        deepseek_usage_available = False
                        metrics = self.cache_usage_provider.extract_usage(chunk.usage)
                        cache_read = metrics.hit_tokens
                        cache_creation = metrics.creation_tokens
                        cache_miss = metrics.miss_tokens
                        cache_usage_available = metrics.available
                        deepseek_usage_available = (
                            metrics.available if is_deepseek_model else False
                        )
                        if not cache_usage_available:
                            cache_read, cache_creation, cache_usage_available = (
                                _openai_cache_usage(
                                    chunk.usage, "prompt_tokens_details"
                                )
                            )
                            if is_deepseek_model:
                                cache_creation = 0
                        if (
                            is_deepseek_model
                            and self.capabilities.official_endpoint
                            and not cache_usage_available
                        ):
                            log.warning(
                                "[deepseek-cache] official response omitted cache "
                                "usage fields; telemetry is unavailable"
                            )
                        prompt_tokens = chunk.usage.prompt_tokens or 0
                        completion_details = getattr(
                            chunk.usage, "completion_tokens_details", None
                        )
                        reasoning_tokens, _ = _usage_counter(
                            completion_details, "reasoning_tokens"
                        )
                        stream_end_emitted = True
                        yield await self._record_stream_end_async(
                            StreamEnd(
                                stop_reason="end_turn",
                                input_tokens=(
                                    cache_miss
                                    if is_deepseek_model and deepseek_usage_available
                                    else max(
                                        prompt_tokens
                                        - cache_read
                                        - cache_creation,
                                        0,
                                    )
                                ),
                                output_tokens=chunk.usage.completion_tokens or 0,
                                cache_read=cache_read,
                                cache_creation=cache_creation,
                                cache_usage_available=cache_usage_available,
                                reasoning_tokens=reasoning_tokens,
                                response_id=getattr(chunk, "id", "") or "",
                                request_id=getattr(chunk, "_request_id", "") or "",
                                model=getattr(chunk, "model", "") or "",
                                provider_stop_reason=provider_stop_reason,
                                service_tier=getattr(chunk, "service_tier", "") or "",
                                system_fingerprint=getattr(
                                    chunk, "system_fingerprint", ""
                                ) or "",
                            ),
                            response_headers,
                            request_metadata,
                        )
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                # --- 文本内容 ---
                if delta and delta.content:
                    yield TextDelta(text=delta.content)

                reasoning_delta = (
                    getattr(delta, "reasoning_content", None)
                    if delta and is_deepseek_model
                    else None
                )
                if reasoning_delta:
                    reasoning_accum += reasoning_delta
                    yield ThinkingDelta(text=reasoning_delta)

                # --- tool call 增量 ---
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in active_calls:
                            active_calls[idx] = {"id": "", "name": "", "args": ""}
                        call = active_calls[idx]

                        if tc.id:
                            call["id"] = tc.id
                        if tc.function and tc.function.name:
                            call["name"] = tc.function.name
                            yield ToolCallStart(
                                tool_name=call["name"],
                                tool_id=call["id"],
                            )
                        if tc.function and tc.function.arguments:
                            call["args"] += tc.function.arguments
                            yield ToolCallDelta(text=tc.function.arguments)

                # --- 结束原因 ---
                if choice.finish_reason:
                    provider_stop_reason = choice.finish_reason
                if choice.finish_reason in ("tool_calls", "stop"):
                    stop_reason = (
                        "tool_calls"
                        if choice.finish_reason == "tool_calls"
                        else "end_turn"
                    )
                    if reasoning_accum:
                        yield ThinkingComplete(
                            thinking=reasoning_accum,
                            signature="",
                            kind="deepseek",
                        )
                        reasoning_accum = ""
                    if choice.finish_reason == "tool_calls":
                        for _idx, call in sorted(active_calls.items()):
                            try:
                                args = json.loads(call["args"]) if call["args"] else {}
                            except json.JSONDecodeError:
                                args = {}
                            yield ToolCallComplete(
                                tool_id=call["id"],
                                tool_name=call["name"],
                                arguments=args,
                            )
                        active_calls.clear()
                elif choice.finish_reason == "length":
                    stop_reason = "max_tokens"

            if reasoning_accum:
                yield ThinkingComplete(
                    thinking=reasoning_accum,
                    signature="",
                    kind="deepseek" if is_deepseek_model else "auto",
                )

            if not stream_end_emitted:
                cache_read = 0
                cache_creation = 0
                cache_miss = 0
                cache_usage_available = False
                prompt_tokens = 0
                output_tokens = 0
                reasoning_tokens = 0
                if usage is not None:
                    metrics = self.cache_usage_provider.extract_usage(usage)
                    cache_read = metrics.hit_tokens
                    cache_creation = metrics.creation_tokens
                    cache_miss = metrics.miss_tokens
                    cache_usage_available = metrics.available
                    prompt_tokens = metrics.prompt_tokens
                    if is_deepseek_model and not cache_usage_available:
                        fallback = resolve_cache_usage_provider(
                            "generic", "openai-compat"
                        ).extract_usage(usage)
                        if fallback.available:
                            cache_read = fallback.hit_tokens
                            cache_miss = fallback.miss_tokens
                            cache_usage_available = True
                    output_tokens, _ = _usage_counter(
                        usage, "completion_tokens"
                    )
                    completion_details = (
                        usage.get("completion_tokens_details")
                        if isinstance(usage, dict)
                        else getattr(usage, "completion_tokens_details", None)
                    )
                    reasoning_tokens, _ = _usage_counter(
                        completion_details, "reasoning_tokens"
                    )
                    if (
                        is_deepseek_model
                        and self.capabilities.official_endpoint
                        and not cache_usage_available
                    ):
                        log.warning(
                            "[deepseek-cache] official response omitted cache "
                            "usage fields; telemetry is unavailable"
                        )

                metadata_chunk = usage_chunk or last_chunk
                yield await self._record_stream_end_async(
                    StreamEnd(
                        stop_reason=stop_reason,
                        input_tokens=(
                            cache_miss if cache_usage_available else prompt_tokens
                        ),
                        output_tokens=output_tokens,
                        cache_read=cache_read,
                        cache_creation=cache_creation,
                        cache_usage_available=cache_usage_available,
                        reasoning_tokens=reasoning_tokens,
                        response_id=getattr(metadata_chunk, "id", "") or "",
                        request_id=getattr(metadata_chunk, "_request_id", "") or "",
                        model=getattr(metadata_chunk, "model", "") or "",
                        provider_stop_reason=provider_stop_reason,
                        service_tier=getattr(metadata_chunk, "service_tier", "") or "",
                        system_fingerprint=getattr(
                            metadata_chunk, "system_fingerprint", ""
                        ) or "",
                    ),
                    response_headers,
                    request_metadata,
                )

        except _openai.AuthenticationError as e:
            raise AuthenticationError(f"Invalid API key: {e}") from e
        except _openai.RateLimitError as e:
            retry = None
            if hasattr(e, "response") and e.response is not None:
                retry = e.response.headers.get("retry-after")
            raise RateLimitError(
                f"Rate limited. {f'Retry after {retry}s.' if retry else 'Please wait.'}",
                retry_after=retry,
            ) from e
        except _openai.APIConnectionError as e:
            raise NetworkError(f"Network error: {e}") from e
        except _openai.APIStatusError as e:
            hint = ""
            if is_deepseek and e.status_code == 400:
                if self.explicit_deepseek_compat and not self.capabilities.official_endpoint:
                    hint = (
                        " Relay compatibility hint: use context_channel: auto to omit "
                        "optional DeepSeek thinking parameters."
                    )
                else:
                    hint = (
                        " Relay compatibility hint: this endpoint rejected the "
                        "DeepSeek contract; use context_channel: generic only to "
                        "explicitly opt out of DeepSeek tool-reasoning replay."
                    )
            raise self._api_status_error(e, hint) from e


def create_client(config: ProviderConfig) -> LLMClient:
    from mewcode.opencode_go import resolve_effective_provider_config

    config = resolve_effective_provider_config(config)
    descriptor = DEFAULT_MODEL_CATALOG.describe(config.model)
    requires_responses = descriptor is not None and descriptor.family in {"gpt-6-astra", "gpt-6.1-sol"}
    if config.protocol == "typesafe":
        from mewcode.typesafe_client import TypeSafeClient
        client = TypeSafeClient(config)
        endpoint = "/v1/systemone"
    elif config.protocol == "anthropic":
        client: LLMClient = AnthropicClient(config)
        endpoint = "/v1/messages"
    elif config.protocol == "openai" or (config.protocol == "openai-compat" and requires_responses):
        client = OpenAIClient(config)
        endpoint = "/v1/responses"
    elif config.protocol == "openai-compat":
        client = OpenAICompatClient(config)
        endpoint = "/v1/chat/completions"
    else:
        raise ValueError(f"Unknown protocol: {config.protocol}")
    client.configure_usage_tracking(config, endpoint=endpoint)
    return client


async def resolve_context_window(config: ProviderConfig) -> None:
    """context window 解析的第 2 层：对于 anthropic 协议的 provider，
    从 {base_url}/v1/models/{model} 自动拉取一次模型的 max_input_tokens，
    并通过 set_fetched_context_window 缓存到 ``config`` 上，这样后续
    config.get_context_window() 调用就能直接使用、无需再次访问网络。

    完全尽力而为，绝不抛出异常：非 anthropic provider、客户端构造失败
    （例如缺少 API key）、拉取失败或超时，都会让缓存保持不变，从而让
    get_context_window() 降级到内置映射表 / 默认值。在启动时调用是安全的——
    阻塞时间不会超过拉取自身的超时，也不会导致崩溃。
    """
    # 配置中显式指定的 window 在 get_context_window() 中优先级最高，
    # 上次调用已缓存的值也不需要重新拉取——直接跳过网络请求。
    from mewcode.opencode_go import resolve_effective_provider_config

    effective = resolve_effective_provider_config(config)
    if config.context_window > 0 or config._fetched_context_window > 0:
        return
    if effective.protocol != "anthropic":
        return

    try:
        client = create_client(effective)
    except Exception:
        return
    fetch = getattr(client, "fetch_model_context_window", None)
    if fetch is None:
        return

    try:
        window = await fetch()
    except Exception:
        window = None
    if window:
        config.set_fetched_context_window(window)
