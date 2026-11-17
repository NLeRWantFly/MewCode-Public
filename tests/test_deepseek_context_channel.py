from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from mewcode.agent import Agent, LLMResponse
from mewcode.client import OpenAICompatClient, _deepseek_cache_usage
from mewcode.config import ProviderConfig
from mewcode.context.manager import (
    CompactEvent,
    apply_tool_result_budget,
    auto_compact,
    compute_compact_threshold,
    create_replacement_state,
)
from mewcode.conversation import (
    ConversationManager,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    estimate_tokens,
)
from mewcode.model_capabilities import (
    canonical_model_name,
    resolve_model_capabilities,
)
from mewcode.memory.session import SessionRecord, records_to_messages
from mewcode.serialization import build_chat_completion_messages
from mewcode.tools import ToolRegistry
from mewcode.tools.base import (
    StreamEnd,
    ThinkingComplete,
    Tool,
    ToolCallComplete,
    ToolResult,
)
from mewcode.tools.impl.tool_search import ToolSearchParams, ToolSearchTool


@pytest.mark.parametrize(
    ("model", "canonical"),
    [
        ("deepseek-v4-flash", "deepseek-v4-flash"),
        ("deepseek-v4-pro", "deepseek-v4-pro"),
        # 官方 API 的短 id 归一化到规范 id。
        ("deepseek-flash", "deepseek-v4-flash"),
        ("deepseek/deepseek-v4-pro", "deepseek-v4-pro"),
        ("deepseek:deepseek-v4-flash", "deepseek-v4-flash"),
        ("openrouter/deepseek-v4-pro:free", "deepseek-v4-pro"),
        ("deepseek-v4-flash-latest", "deepseek-v4-flash"),
    ],
)
def test_model_names_select_deepseek_channel(model: str, canonical: str) -> None:
    assert canonical_model_name(model) == canonical
    capabilities = resolve_model_capabilities(model, "https://relay.example/v1")
    assert capabilities.family == "deepseek"
    assert capabilities.context_channel == "deepseek"
    assert capabilities.context_window == 1_000_000
    assert capabilities.max_output_tokens == 384_000
    assert capabilities.supports_thinking is True
    assert capabilities.requires_reasoning_content_for_tools is True
    assert capabilities.cache_usage_format == "deepseek"


def test_context_channel_overrides_detection() -> None:
    generic_override = resolve_model_capabilities(
        "deepseek-v4-pro", context_channel="generic"
    )
    assert generic_override.context_channel == "generic"
    assert generic_override.family == "deepseek"
    assert generic_override.context_window == 1_000_000
    assert resolve_model_capabilities(
        "vendor-reasoner", context_channel="deepseek"
    ).context_channel == "deepseek"
    assert resolve_model_capabilities("gpt-5").context_channel == "generic"
    assert resolve_model_capabilities("mydeepseek-v4-pro").context_channel == "generic"


@pytest.mark.parametrize("model", ["grok-4.7", "grok-4.7-build-fast"])
def test_grok_47_keeps_its_runtime_family(model: str) -> None:
    capabilities = resolve_model_capabilities(model)
    assert capabilities.family == "grok-4.7"
    assert capabilities.context_channel == "generic"
    assert capabilities.supports_thinking is True


class _AsyncChunks:
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def _iterate():
            for chunk in self._chunks:
                yield chunk

        return _iterate()


def _usage_chunk(**usage_values: int) -> SimpleNamespace:
    values = {"prompt_tokens": 0, "completion_tokens": 0, **usage_values}
    return SimpleNamespace(choices=[], usage=SimpleNamespace(**values))


def _stop_chunk(
    *,
    finish_reason: str = "stop",
    reasoning_content: str | None = None,
    tool_calls: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(
        content=None,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=None,
    )


def _install_fake_openai(monkeypatch, chunks_per_call, captured):
    import mewcode.client as client_module

    class FakeCompletions:
        def __init__(self) -> None:
            self.call = 0

        async def create(self, **kwargs):
            captured.append(kwargs)
            chunks = chunks_per_call[min(self.call, len(chunks_per_call) - 1)]
            self.call += 1
            return _AsyncChunks(chunks)

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(client_module, "AsyncOpenAI", FakeOpenAI)


def _client_config(
    *,
    base_url: str,
    model: str = "deepseek-v4-flash",
    context_channel: str = "auto",
) -> ProviderConfig:
    return ProviderConfig(
        name="deepseek",
        protocol="openai-compat",
        base_url=base_url,
        model=model,
        api_key="sk-test",
        context_channel=context_channel,
    )


@pytest.mark.asyncio
async def test_official_endpoint_sends_thinking_and_collects_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict] = []
    tool_delta = SimpleNamespace(
        index=0,
        id="call-1",
        function=SimpleNamespace(name="ReadFile", arguments='{"file_path":"a.py"}'),
    )
    chunks = [
        _stop_chunk(finish_reason="", reasoning_content="inspect first"),
        _stop_chunk(finish_reason="tool_calls", tool_calls=[tool_delta]),
        _usage_chunk(
            prompt_tokens=120,
            completion_tokens=10,
            prompt_cache_hit_tokens=100,
            prompt_cache_miss_tokens=20,
        ),
    ]
    _install_fake_openai(monkeypatch, [chunks], captured)
    client = OpenAICompatClient(
        _client_config(base_url="https://api.deepseek.com")
    )
    conversation = ConversationManager()
    conversation.add_user_message("inspect")

    events = [event async for event in client.stream(conversation)]

    assert captured[0]["reasoning_effort"] == "high"
    assert captured[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    thinking = next(event for event in events if isinstance(event, ThinkingComplete))
    assert thinking.thinking == "inspect first"
    assert thinking.kind == "deepseek"
    call = next(event for event in events if isinstance(event, ToolCallComplete))
    assert call.arguments == {"file_path": "a.py"}
    usage = next(event for event in events if isinstance(event, StreamEnd))
    assert usage.input_tokens == 20
    assert usage.cache_read == 100
    assert usage.cache_creation == 0
    assert usage.cache_usage_available is True


@pytest.mark.asyncio
async def test_relay_uses_safe_subset_unless_explicitly_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict] = []
    chunks = [[_stop_chunk(), _usage_chunk(prompt_tokens=12, completion_tokens=2)]]
    _install_fake_openai(monkeypatch, chunks, captured)
    auto_client = OpenAICompatClient(
        _client_config(base_url="https://relay.example/v1")
    )
    conversation = ConversationManager()
    conversation.add_user_message("hello")
    auto_events = [event async for event in auto_client.stream(conversation)]
    assert "reasoning_effort" not in captured[0]
    assert "extra_body" not in captured[0]
    auto_usage = next(event for event in auto_events if isinstance(event, StreamEnd))
    assert auto_usage.cache_usage_available is False
    assert auto_usage.input_tokens == 12

    captured.clear()
    _install_fake_openai(monkeypatch, chunks, captured)
    explicit_client = OpenAICompatClient(
        _client_config(
            base_url="https://relay.example/v1", context_channel="deepseek"
        )
    )
    _ = [event async for event in explicit_client.stream(conversation)]
    assert captured[0]["reasoning_effort"] == "high"
    assert captured[0]["extra_body"] == {"thinking": {"type": "enabled"}}


def test_cache_usage_distinguishes_missing_from_explicit_zero() -> None:
    assert _deepseek_cache_usage(SimpleNamespace()) == (0, 0, False)
    assert _deepseek_cache_usage(
        SimpleNamespace(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=0)
    ) == (0, 0, True)


@pytest.mark.asyncio
async def test_deepseek_collects_usage_attached_to_final_choice_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict] = []
    final = _stop_chunk()
    final.usage = SimpleNamespace(
        prompt_tokens=1_000,
        completion_tokens=25,
        model_extra={
            "prompt_cache_hit_tokens": 900,
            "prompt_cache_miss_tokens": 100,
        },
    )
    _install_fake_openai(monkeypatch, [[final]], captured)
    client = OpenAICompatClient(
        _client_config(base_url="https://api.deepseek.com")
    )
    conversation = ConversationManager()
    conversation.add_user_message("inspect")

    events = [event async for event in client.stream(conversation)]
    stream_ends = [event for event in events if isinstance(event, StreamEnd)]

    assert len(stream_ends) == 1
    assert stream_ends[0].cache_usage_available is True
    assert stream_ends[0].cache_read == 900
    assert stream_ends[0].input_tokens == 100


def test_deepseek_tool_turn_replays_reasoning_only_for_tool_calls() -> None:
    messages = [
        Message(role="user", content="inspect"),
        Message(
            role="assistant",
            content="",
            thinking_blocks=[
                ThinkingBlock(thinking="full chain", signature="", kind="deepseek")
            ],
            tool_uses=[ToolUseBlock("call-1", "ReadFile", {"file_path": "a.py"})],
        ),
        Message(
            role="user",
            content="",
            tool_results=[ToolResultBlock("call-1", "data")],
        ),
        Message(
            role="assistant",
            content="done",
            thinking_blocks=[
                ThinkingBlock(thinking="ordinary cot", signature="", kind="deepseek")
            ],
        ),
    ]
    payload = build_chat_completion_messages(messages, deepseek=True)
    tool_assistant = payload[1]
    assert tool_assistant["content"].strip() == ""
    assert tool_assistant["content"] != ""
    assert tool_assistant["reasoning_content"] == "full chain"
    assert "reasoning_content" not in payload[-1]


def test_session_roundtrip_preserves_deepseek_thinking_source() -> None:
    original = Message(
        role="assistant",
        content="",
        thinking_blocks=[
            ThinkingBlock(thinking="full chain", signature="", kind="deepseek")
        ],
        tool_uses=[ToolUseBlock("call-1", "ReadFile", {"file_path": "a.py"})],
    )
    restored = records_to_messages(SessionRecord.from_message(original))[0]
    assert restored.thinking_blocks == original.thinking_blocks
    replay = build_chat_completion_messages([restored], deepseek=True)[0]
    assert replay["reasoning_content"] == "full chain"


class DeferredParams(BaseModel):
    value: int


class DeferredEcho(Tool):
    name = "DeferredEcho"
    description = "Echo a validated integer"
    params_model = DeferredParams
    should_defer = True
    category = "read"

    async def execute(self, params: BaseModel) -> ToolResult:
        assert isinstance(params, DeferredParams)
        return ToolResult(output=f"echo:{params.value}")


@pytest.mark.asyncio
async def test_tool_search_keeps_provider_schemas_stable_and_gateway_executes(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    registry.register(DeferredEcho())
    search = ToolSearchTool(registry, protocol="openai-compat")
    registry.register(search)
    client = OpenAICompatClient(
        _client_config(base_url="https://relay.example/v1")
    )
    agent = Agent(
        client=client,
        registry=registry,
        protocol="openai-compat",
        work_dir=str(tmp_path),
        context_window=1_000_000,
    )
    before = json.dumps(agent._context_tools(), sort_keys=True)
    for _ in range(3):
        found = await search.execute(ToolSearchParams(query="select:DeferredEcho"))
        assert "DeferredEcho" in found.output
        after = json.dumps(agent._context_tools(), sort_keys=True)
        assert before == after
    assert "DeferredEcho" not in before
    assert "InvokeDeferredTool" in before

    result = await agent._execute_tool_noninteractive(
        ToolCallComplete(
            tool_id="gateway-1",
            tool_name="InvokeDeferredTool",
            arguments={"name": "DeferredEcho", "arguments": {"value": 7}},
        )
    )
    assert result.output == "echo:7"


def test_cache_stable_tool_budget_never_rewrites_old_results(tmp_path) -> None:
    conversation = ConversationManager()
    for index in range(12):
        conversation.history.extend(
            [
                Message(
                    role="assistant",
                    content="",
                    tool_uses=[ToolUseBlock(f"id-{index}", "ReadFile", {})],
                ),
                Message(
                    role="user",
                    content="",
                    tool_results=[
                        ToolResultBlock(f"id-{index}", f"RESULT-{index}-" + "x" * 3000)
                    ],
                ),
            ]
        )
    state = create_replacement_state()
    first, _ = apply_tool_result_budget(
        conversation, tmp_path, state, cache_stable=True
    )
    first_bytes = json.dumps(
        [asdict(message) for message in first.history],
        ensure_ascii=False,
        sort_keys=True,
    )
    conversation.history.extend(
        [
                Message(
                    role="assistant",
                    content="",
                    tool_uses=[ToolUseBlock("id-screenshot", "Screenshot", {})],
                ),
                Message(
                    role="user",
                    content="",
                    tool_results=[
                    ToolResultBlock(
                        "id-screenshot",
                        "new screenshot",
                        content_blocks=[
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "aGVsbG8=",
                                },
                            }
                        ],
                    )
                ],
            ),
        ]
    )
    second, _ = apply_tool_result_budget(
        conversation, tmp_path, state, cache_stable=True
    )
    second_prefix_bytes = json.dumps(
        [asdict(message) for message in second.history[: len(first.history)]],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert second_prefix_bytes == first_bytes
    assert "<snipped>" not in second_prefix_bytes
    assert second.history[-1].tool_results[0].content_blocks[0]["type"] == "image"


def test_request_shape_tracks_prefix_and_rewrite_reason(tmp_path) -> None:
    registry = ToolRegistry()
    client = OpenAICompatClient(
        _client_config(base_url="https://relay.example/v1")
    )
    agent = Agent(
        client=client,
        registry=registry,
        protocol="openai-compat",
        work_dir=str(tmp_path),
        context_window=1_000_000,
    )
    conversation = ConversationManager()
    conversation.add_user_message("first")
    system = agent._context_system()
    tools = agent._context_tools()
    first = agent._begin_cache_observation(conversation, system, tools)
    assert first is not None
    conversation.add_assistant_message("answer")
    conversation.add_user_message("second")
    second = agent._begin_cache_observation(conversation, system, tools)
    assert second is not None
    assert second.longest_common_prefix_messages == len(
        first.message_segment_hashes
    )
    assert second.invalidation_reason == ""

    conversation.history[0].content = "rewritten"
    third = agent._begin_cache_observation(conversation, system, tools)
    assert third is not None
    assert third.invalidation_reason == "history_rewritten"


def test_server_miss_cost_and_model_switch_start_new_epoch(tmp_path) -> None:
    registry = ToolRegistry()
    deepseek_client = OpenAICompatClient(
        _client_config(base_url="https://api.deepseek.com", model="deepseek-v4-pro")
    )
    agent = Agent(
        client=deepseek_client,
        registry=registry,
        protocol="openai-compat",
        work_dir=str(tmp_path),
        context_window=1_000_000,
    )
    conversation = ConversationManager()
    conversation.add_user_message("first")
    agent._current_conversation = conversation
    system = agent._context_system()
    tools = agent._context_tools()
    first = agent._begin_cache_observation(conversation, system, tools)
    agent._finish_cache_observation(
        first,
        LLMResponse(
            input_tokens=100,
            output_tokens=20,
            cache_read=900,
            cache_usage_available=True,
        ),
    )
    conversation.add_assistant_message("answer")
    conversation.add_user_message("next")
    second = agent._begin_cache_observation(conversation, system, tools)
    assert second is not None
    agent._finish_cache_observation(
        second,
        LLMResponse(
            input_tokens=1_100,
            output_tokens=10,
            cache_read=0,
            cache_usage_available=True,
        ),
    )
    assert second.invalidation_reason == "same_shape_server_miss"
    assert second.request_cost_usd > first.request_cost_usd
    assert agent.total_deepseek_cache_hit_tokens == 900
    assert agent.total_deepseek_cache_miss_tokens == 1_200

    old_epoch = agent.cache_epoch
    conversation.record_usage_anchor(input_tokens=50_000)
    generic_client = OpenAICompatClient(
        _client_config(
            base_url="https://relay.example/v1",
            model="gpt-5",
            context_channel="generic",
        )
    )
    agent.configure_model(
        generic_client,
        protocol="openai-compat",
        context_window=128_000,
    )
    assert agent.cache_epoch == old_epoch + 1
    assert agent.context_channel == "generic"
    assert conversation.baseline_tokens == 0
    assert agent._last_request_shape is None


def test_unreported_deepseek_usage_is_not_accumulated_as_a_cache_miss(
    tmp_path,
) -> None:
    agent = Agent(
        client=OpenAICompatClient(
            _client_config(base_url="https://api.deepseek.com")
        ),
        registry=ToolRegistry(),
        protocol="openai-compat",
        work_dir=str(tmp_path),
        context_window=1_000_000,
    )
    conversation = ConversationManager()
    conversation.add_user_message("first")
    observation = agent._begin_cache_observation(
        conversation, agent._context_system(), agent._context_tools()
    )

    agent._finish_cache_observation(
        observation,
        LLMResponse(
            input_tokens=2_000,
            output_tokens=10,
            cache_usage_available=False,
        ),
    )

    assert observation is not None
    assert observation.cache_usage_available is False
    assert observation.cache_hit_tokens == 0
    assert observation.cache_miss_tokens == 0
    assert agent.total_deepseek_cache_hit_tokens == 0
    assert agent.total_deepseek_cache_miss_tokens == 0


class SummaryClient:
    def __init__(self):
        self.calls = 0

    async def stream(self, conversation, system="", tools=None):
        from mewcode.tools.base import TextDelta

        self.calls += 1
        # An oversized draft is regenerated, never silently cut mid-constraint.
        size = 80_000 if self.calls == 1 else 8_000
        yield TextDelta(text="<summary>" + "s" * size + "</summary>")
        yield StreamEnd(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_deepseek_compacts_at_85_percent_then_below_half(tmp_path) -> None:
    assert compute_compact_threshold(
        1_000_000, trigger_ratio=0.85
    ) == 850_000
    conversation = ConversationManager()
    conversation.add_user_message("original goal " + "g" * 50_000)
    for index in range(19):
        conversation.history.append(
            Message(role="assistant", content=f"turn-{index}-" + "x" * 35_000)
        )
    conversation.record_usage_anchor(input_tokens=900_000)

    client = SummaryClient()
    result = await auto_compact(
        conversation,
        client,
        context_window=1_000_000,
        session_dir=tmp_path,
        protocol="openai-compat",
        context_channel="deepseek",
        fixed_prefix_tokens=40_000,
        output_reserve_tokens=20_000,
    )
    assert isinstance(result, CompactEvent)
    assert client.calls == 2
    assert result.boundary is not None
    assert estimate_tokens(
        [Message(role="user", content=result.boundary.summary)]
    ) <= 16_000
    assert conversation.current_tokens() + 40_000 + 20_000 < 500_000
    assert "[首个用户目标]" in conversation.history[0].content
    assert estimate_tokens([conversation.history[0]]) < 120_000

    second = await auto_compact(
        conversation,
        SummaryClient(),
        context_window=1_000_000,
        session_dir=tmp_path,
        protocol="openai-compat",
        context_channel="deepseek",
    )
    assert second is None
