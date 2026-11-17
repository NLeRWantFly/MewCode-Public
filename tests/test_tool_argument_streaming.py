from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import mewcode.client as client_module
from mewcode.client import AnthropicClient, LLMError, OpenAIClient
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.tools.base import ToolCallComplete, ToolCallStart
from mewcode.tools.load_skill import LoadSkillParams


def _conversation() -> ConversationManager:
    conversation = ConversationManager()
    conversation.add_user_message("Load the relevant tool")
    return conversation


async def _async_events(events):
    for event in events:
        yield event


def _response_completed():
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            id="resp-test",
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                input_tokens_details=None,
                output_tokens_details=None,
            ),
        ),
    )


async def _openai_events(monkeypatch, events):
    class FakeResponses:
        async def create(self, **_kwargs):
            return _async_events(events)

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.responses = FakeResponses()

    monkeypatch.setattr(client_module, "AsyncOpenAI", FakeOpenAI)
    client = OpenAIClient(ProviderConfig(
        name="offline-test",
        protocol="openai",
        base_url="http://127.0.0.1/v1",
        model="gpt-5.6",
        api_key="test-key",
    ))
    return [event async for event in client.stream(_conversation())]


async def _anthropic_events(monkeypatch, events):
    final_message = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=1,
            output_tokens=1,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        stop_reason="end_turn",
        id="msg-test",
        model="claude-sonnet-4-6",
        _request_id="req-test",
    )

    class FakeMessageStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def __aiter__(self):
            return _async_events(events)

        async def get_final_message(self):
            return final_message

    class FakeMessages:
        def stream(self, **_kwargs):
            return FakeMessageStream()

    class FakeAnthropic:
        def __init__(self, **_kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(client_module, "AsyncAnthropic", FakeAnthropic)
    client = AnthropicClient(ProviderConfig(
        name="offline-test",
        protocol="anthropic",
        base_url="http://127.0.0.1",
        model="claude-sonnet-4-6",
        api_key="test-key",
    ))
    return [event async for event in client.stream(_conversation())]


@pytest.mark.asyncio
async def test_responses_done_only_tool_call_uses_full_arguments_and_real_name(monkeypatch):
    events = await _openai_events(monkeypatch, [
        SimpleNamespace(
            type="response.function_call_arguments.done",
            item_id="fc-load-skill",
            call_id="call-load-skill",
            output_index=0,
            name="LoadSkill",
            arguments="{}",
        ),
        _response_completed(),
    ])

    starts = [event for event in events if isinstance(event, ToolCallStart)]
    calls = [event for event in events if isinstance(event, ToolCallComplete)]
    assert [(event.tool_name, event.tool_id) for event in starts] == [
        ("LoadSkill", "call-load-skill")
    ]
    assert [(event.tool_name, event.tool_id, event.arguments) for event in calls] == [
        ("LoadSkill", "call-load-skill", {})
    ]
    with pytest.raises(ValidationError):
        LoadSkillParams.model_validate(calls[0].arguments)


@pytest.mark.asyncio
async def test_responses_output_item_done_alone_completes_a_tool_call(monkeypatch):
    events = await _openai_events(monkeypatch, [
        SimpleNamespace(
            type="response.output_item.done",
            output_index=0,
            item=SimpleNamespace(
                type="function_call",
                id="fc-read-file",
                call_id="call-read-file",
                name="ReadFile",
                arguments='{"file_path":"README.md"}',
            ),
        ),
        _response_completed(),
    ])

    starts = [event for event in events if isinstance(event, ToolCallStart)]
    calls = [event for event in events if isinstance(event, ToolCallComplete)]
    assert [(event.tool_name, event.tool_id) for event in starts] == [
        ("ReadFile", "call-read-file")
    ]
    assert [(event.tool_name, event.tool_id, event.arguments) for event in calls] == [
        ("ReadFile", "call-read-file", {"file_path": "README.md"})
    ]


@pytest.mark.asyncio
async def test_responses_delta_done_and_item_done_complete_once_using_done_arguments(monkeypatch):
    events = await _openai_events(monkeypatch, [
        SimpleNamespace(
            type="response.output_item.added",
            output_index=0,
            item=SimpleNamespace(
                type="function_call", id="fc-1", call_id="call-1",
                name="ReadFile", arguments="",
            ),
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            output_index=0,
            item_id="fc-1",
            call_id="call-1",
            delta='{"file_path":"partial"}',
        ),
        SimpleNamespace(
            type="response.function_call_arguments.done",
            output_index=0,
            item_id="fc-1",
            call_id="call-1",
            arguments='{"file_path":"complete"}',
        ),
        SimpleNamespace(
            type="response.output_item.done",
            output_index=0,
            item=SimpleNamespace(
                type="function_call", id="fc-1", call_id="call-1",
                name="ReadFile", arguments='{"file_path":"complete"}',
            ),
        ),
        _response_completed(),
    ])

    starts = [event for event in events if isinstance(event, ToolCallStart)]
    calls = [event for event in events if isinstance(event, ToolCallComplete)]
    assert len(starts) == 1
    assert starts[0].tool_id == "call-1"
    assert len(calls) == 1
    assert calls[0].tool_id == starts[0].tool_id
    assert calls[0].arguments == {"file_path": "complete"}


@pytest.mark.asyncio
async def test_responses_interleaved_calls_keep_identity_and_arguments_separate(monkeypatch):
    events = await _openai_events(monkeypatch, [
        SimpleNamespace(type="response.output_item.added", output_index=0,
            item=SimpleNamespace(type="function_call", id="fc-A", call_id="call-A",
                name="ReadFile", arguments="")),
        SimpleNamespace(type="response.output_item.added", output_index=1,
            item=SimpleNamespace(type="function_call", id="fc-B", call_id="call-B",
                name="WriteFile", arguments="")),
        SimpleNamespace(type="response.function_call_arguments.delta", output_index=0,
            delta='{"file_path":"A"}'),
        SimpleNamespace(type="response.function_call_arguments.delta", item_id="fc-B",
            delta='{"file_path":"B-delta"}'),
        SimpleNamespace(type="response.function_call_arguments.done", call_id="call-B",
            output_index=1, arguments='{"file_path":"B-final"}'),
        SimpleNamespace(type="response.output_item.done", output_index=0,
            item=SimpleNamespace(type="function_call", id="fc-A", name="ReadFile",
                arguments='{"file_path":"A-final"}')),
        _response_completed(),
    ])

    starts = [event for event in events if isinstance(event, ToolCallStart)]
    calls = [event for event in events if isinstance(event, ToolCallComplete)]
    assert [(event.tool_id, event.tool_name) for event in starts] == [
        ("call-A", "ReadFile"), ("call-B", "WriteFile")
    ]
    assert [(event.tool_id, event.arguments) for event in calls] == [
        ("call-B", {"file_path": "B-final"}),
        ("call-A", {"file_path": "A-final"}),
    ]


@pytest.mark.asyncio
async def test_responses_missing_identity_or_name_fails_without_guessing(monkeypatch):
    with pytest.raises(LLMError, match="missing item_id, call_id, and output_index"):
        await _openai_events(monkeypatch, [SimpleNamespace(
            type="response.function_call_arguments.done",
            name="LoadSkill",
            arguments='{"name":"skill-creator"}',
        )])

    with pytest.raises(LLMError, match="missing its function name"):
        await _openai_events(monkeypatch, [SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id="call-without-name",
            arguments='{"name":"skill-creator"',
        )])


@pytest.mark.asyncio
async def test_anthropic_tool_start_input_is_used_when_no_json_delta_arrives(monkeypatch):
    events = await _anthropic_events(monkeypatch, [
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(
            type="tool_use", id="call-load-skill", name="LoadSkill",
            input={"name": "skill-creator"},
        )),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="message_stop"),
    ])

    calls = [event for event in events if isinstance(event, ToolCallComplete)]
    assert [(event.tool_id, event.tool_name, event.arguments) for event in calls] == [
        ("call-load-skill", "LoadSkill", {"name": "skill-creator"})
    ]


@pytest.mark.asyncio
async def test_anthropic_json_delta_is_final_instead_of_merging_start_input(monkeypatch):
    events = await _anthropic_events(monkeypatch, [
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(
            type="tool_use", id="call-load-skill", name="LoadSkill",
            input={"name": "initial"},
        )),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(
            type="input_json_delta", partial_json='{"name":"delta"}',
        )),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="message_stop"),
    ])

    calls = [event for event in events if isinstance(event, ToolCallComplete)]
    assert len(calls) == 1
    assert calls[0].arguments == {"name": "delta"}


@pytest.mark.asyncio
async def test_anthropic_empty_start_input_stays_empty_and_required_name_still_fails(monkeypatch):
    events = await _anthropic_events(monkeypatch, [
        SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(
            type="tool_use", id="call-empty", name="LoadSkill", input={},
        )),
        SimpleNamespace(type="content_block_stop"),
        SimpleNamespace(type="message_stop"),
    ])

    calls = [event for event in events if isinstance(event, ToolCallComplete)]
    assert len(calls) == 1
    assert calls[0].arguments == {}
    with pytest.raises(ValidationError):
        LoadSkillParams.model_validate(calls[0].arguments)
