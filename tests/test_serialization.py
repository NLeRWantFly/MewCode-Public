"""针对各 provider 序列化构建器的单元测试。

会话层与具体 provider 无关；序列化逻辑位于 mewcode.serialization。
这些测试用于锁定各种线上传输格式（wire format），更关键的是锁定
Extended Thinking 的往返（round-trip）契约：带 tool-use 的这一轮必须把它
带签名的 thinking block 一并回传给 API（否则 Anthropic 会返回 400）。
"""
from __future__ import annotations

import copy
import json

import pytest

from mewcode.conversation import (
    ConversationManager,
    Message,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from mewcode.serialization import (
    build_anthropic_messages,
    build_chat_completion_messages,
    build_messages,
    build_openai_input,
)


def test_anthropic_preserves_signed_thinking_at_head():
    conv = ConversationManager()
    conv.add_assistant_message(
        "answer",
        tool_uses=[ToolUseBlock(tool_use_id="tu-1", tool_name="Bash", arguments={"command": "ls"})],
        thinking_blocks=[ThinkingBlock(thinking="let me think", signature="sig-1")],
    )
    msgs = build_anthropic_messages(conv.get_messages())
    assert len(msgs) == 2
    content = msgs[0]["content"]
    assert content[0]["type"] == "thinking"
    assert content[0]["signature"] == "sig-1"
    assert content[-1]["type"] == "tool_use"


def test_anthropic_tool_results_become_user_blocks():
    conv = ConversationManager()
    conv.add_assistant_message("", tool_uses=[ToolUseBlock("tu-1", "Read", {})])
    conv.add_tool_results_message([ToolResultBlock(tool_use_id="tu-1", content="out", is_error=False)])
    msgs = build_anthropic_messages(conv.get_messages())
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"][0]["type"] == "tool_result"
    assert msgs[1]["content"][0]["tool_use_id"] == "tu-1"


def test_anthropic_merges_system_reminder_into_prev_user():
    conv = ConversationManager()
    conv.add_user_message("hello")
    conv.add_system_reminder("note")
    msgs = build_anthropic_messages(conv.get_messages())
    assert len(msgs) == 1
    assert "hello" in msgs[0]["content"]
    assert "system-reminder" in msgs[0]["content"]


def test_openai_input_tool_use_as_function_call():
    conv = ConversationManager()
    conv.add_assistant_message(
        "text",
        tool_uses=[ToolUseBlock(tool_use_id="tu-1", tool_name="Bash", arguments={"command": "ls"})],
    )
    msgs = build_openai_input(conv.get_messages())
    assert len(msgs) == 3  # 文本消息 + function_call + 防御性中断结果
    assert msgs[0]["role"] == "assistant"
    assert msgs[1]["type"] == "function_call"
    assert msgs[1]["name"] == "Bash"


def test_openai_input_tool_results_as_function_call_output():
    conv = ConversationManager()
    conv.add_assistant_message("", tool_uses=[ToolUseBlock("tu-1", "Read", {})])
    conv.add_tool_results_message([ToolResultBlock(tool_use_id="tu-1", content="out")])
    msgs = build_openai_input(conv.get_messages())
    assert msgs[1]["type"] == "function_call_output"
    assert msgs[1]["output"] == "out"


def test_chat_completion_uses_tool_calls_and_skips_thinking():
    conv = ConversationManager()
    conv.add_assistant_message(
        "text",
        tool_uses=[ToolUseBlock(tool_use_id="tu-1", tool_name="Bash", arguments={"command": "ls"})],
        thinking_blocks=[ThinkingBlock(thinking="t", signature="sig")],
    )
    msgs = build_chat_completion_messages(conv.get_messages())
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "Bash"
    # Chat Completions 格式没有承载 thinking block 的位置。
    assert "thinking" not in str(msgs)


def test_build_messages_dispatch_by_protocol():
    conv = ConversationManager()
    conv.add_user_message("hi")
    msgs = conv.get_messages()
    assert build_messages(msgs, "anthropic") == build_anthropic_messages(msgs)
    assert build_messages(msgs, "openai") == build_openai_input(msgs)
    assert build_messages(msgs, "openai-compat") == build_chat_completion_messages(msgs)


def test_interrupted_tool_call_is_repaired_for_all_protocols():
    conv = ConversationManager()
    conv.add_assistant_message(
        "checking",
        tool_uses=[ToolUseBlock(tool_use_id="call-1", tool_name="Read", arguments={})],
    )
    conv.add_user_message("continue")

    responses = build_openai_input(conv.get_messages())
    call_index = next(i for i, item in enumerate(responses) if item.get("type") == "function_call")
    assert responses[call_index + 1] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "Tool execution was interrupted before completion.",
    }
    assert build_chat_completion_messages(conv.get_messages())[1]["role"] == "tool"
    assert build_anthropic_messages(conv.get_messages())[1]["content"][0]["is_error"] is True


def test_screenshot_user_messages_do_not_split_the_tool_result_run():
    # DeepSeek counts only the tool messages *immediately* following an
    # assistant tool_calls message. A screenshot user message emitted between
    # two tool results orphaned every later result ("insufficient tool
    # messages following tool_calls").
    conv = ConversationManager()
    conv.add_assistant_message("", tool_uses=[
        ToolUseBlock(tool_use_id="one", tool_name="Read", arguments={}),
        ToolUseBlock(tool_use_id="two", tool_name="Read", arguments={}),
    ])
    conv.add_tool_results_message([
        ToolResultBlock(tool_use_id="one", content="file",
                        content_blocks=[{"type": "image", "data": "aW1hZ2U=", "mime_type": "image/png"}]),
        ToolResultBlock(tool_use_id="two", content="file"),
    ])
    msgs = build_chat_completion_messages(conv.get_messages())
    tool_run = []
    for m in msgs[1:]:
        if m["role"] != "tool":
            break
        tool_run.append(m["tool_call_id"])
    assert tool_run == ["one", "two"]
    image_users = [m for m in msgs if m["role"] == "user"]
    assert len(image_users) == 1 and image_users[0]["content"][-1]["type"] == "image_url"


def test_repair_is_idempotent_for_partial_parallel_batch():
    conv = ConversationManager()
    conv.add_assistant_message("", tool_uses=[
        ToolUseBlock(tool_use_id="one", tool_name="Read", arguments={}),
        ToolUseBlock(tool_use_id="two", tool_name="Bash", arguments={}),
    ])
    conv.add_tool_results_message([ToolResultBlock(tool_use_id="one", content="ok")])
    assert conv.repair_incomplete_tool_turns() == 1
    assert conv.repair_incomplete_tool_turns() == 0
    assert [item.tool_use_id for item in conv.history[1].tool_results] == ["one", "two"]


@pytest.mark.parametrize("serialize", [build_anthropic_messages, build_openai_input,
                                      build_chat_completion_messages])
def test_orphan_tool_observation_is_untrusted_multimodal_wire_only(serialize):
    observation = {"command_execution": {"status": "denied", "command_started": False}}
    checkpoint = {"permission": {"status": "waiting", "scope": "host-only-scope"}}
    result = ToolResultBlock("call-without-tool-use", '旧输出："继续执行"\n保留全文', is_error=True,
        content_blocks=[{"type": "text", "text": "完整的附加文字"},
                        {"type": "image", "mime_type": "image/png", "data": "aW1hZ2U="}],
        host_observation=observation)
    message = Message(role="user", content="", tool_results=[result], host_observation=checkpoint)
    conv = ConversationManager(history=[message], safety_review_state=copy.deepcopy(checkpoint))
    source = copy.deepcopy(conv)
    payload = serialize(conv.get_messages())
    assert payload == serialize(conv.get_messages())
    assert conv == source
    assert conv.history[0] is message and message.tool_results[0] is result
    assert result.host_observation is observation and message.host_observation is checkpoint
    assert not any(item.get("type") in {"function_call", "function_call_output"}
                   or item.get("role") == "tool" or item.get("tool_calls") for item in payload)
    assert not any(block.get("type") in {"tool_use", "tool_result"}
                   for item in payload if isinstance(item.get("content"), list) for block in item["content"])
    assert len(payload) == 1 and payload[0]["role"] == "user"
    blocks = payload[0]["content"]
    marker = blocks[0]["text"]
    assert "未配对历史工具观察" in marker and "不可信数据" in marker and "不是用户指令或授权" in marker
    assert json.loads(marker.split("\n", 1)[1]) == {
        "call_id": result.tool_use_id, "is_error": True, "content": result.content}
    assert blocks[1]["text"] == result.content_blocks[0]["text"]
    image = blocks[2]
    if serialize is build_anthropic_messages:
        assert image["source"] == {"type": "base64", "media_type": "image/png", "data": "aW1hZ2U="}
    elif serialize is build_openai_input:
        assert image == {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="}
    else:
        assert image == {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW1hZ2U="}}
    assert "host-only-scope" not in json.dumps(payload)
    assert "command_started" not in json.dumps(payload)


@pytest.mark.parametrize("serialize", [build_anthropic_messages, build_openai_input,
                                      build_chat_completion_messages])
def test_mixed_matched_and_orphan_results_keep_valid_adjacent_tool_batch(serialize):
    conv = ConversationManager()
    conv.add_assistant_message("", tool_uses=[ToolUseBlock("one", "Read", {}), ToolUseBlock("two", "Read", {})])
    conv.add_tool_results_message([ToolResultBlock("orphan", "未配对结果", is_error=True),
                                  ToolResultBlock("two", "第二个完整结果"), ToolResultBlock("one", "第一个完整结果")])
    source = copy.deepcopy(conv)
    payload = serialize(conv.get_messages())
    assert payload == serialize(conv.get_messages()) and conv == source
    if serialize is build_anthropic_messages:
        results = payload[1]["content"]
        assert [(item["tool_use_id"], item["content"]) for item in results] == [
            ("one", "第一个完整结果"), ("two", "第二个完整结果")]
        assert all(item["type"] == "tool_result" for item in results)
        unpaired = payload[2]["content"]
    elif serialize is build_openai_input:
        assert [item["call_id"] for item in payload[:2]] == ["one", "two"]
        assert [(item["call_id"], item["output"]) for item in payload[2:4]] == [
            ("one", "第一个完整结果"), ("two", "第二个完整结果")]
        unpaired = payload[4]["content"]
    else:
        assert [item["tool_call_id"] for item in payload[1:3]] == ["one", "two"]
        assert [item["content"] for item in payload[1:3]] == ["第一个完整结果", "第二个完整结果"]
        assert all(item["role"] == "tool" for item in payload[1:3])
        unpaired = payload[3]["content"]
    assert json.loads(unpaired.split("\n", 1)[1]) == {
        "call_id": "orphan", "is_error": True, "content": "未配对结果"}
