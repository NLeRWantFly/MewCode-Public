"""Incremental persistence must follow stable objects, not recycled wrapper ids."""
from __future__ import annotations

import gc
import json
import weakref

import pytest

from frontend import server
from frontend.chat_history_persistence import ChatHistoryPersistence
from mewcode.agent import CompactNotification, TurnComplete
from mewcode.context.manager import CompactBoundary
from mewcode.conversation import (
    ConversationManager, INTERRUPTED_TOOL_RESULT, Message, ThinkingBlock, ToolResultBlock, ToolUseBlock,
)
from mewcode.memory.session import SessionManager
from mewcode.runtime_scheduler import RuntimeScheduler
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from tests.test_agent import MockLLMClient
from tests.test_continuation_chat import setup


@pytest.mark.parametrize("already_saved", [False, True])
def test_ordinary_message_identity_stays_reserved_until_reset(already_saved):
    message = Message("assistant", "same text", tool_uses=[ToolUseBlock("old", "ReadFile", {})])
    tracker = ChatHistoryPersistence([message] if already_saved else [])
    if not already_saved:
        tracker.persist([message], lambda _message: None)
    identity, reference = id(message), weakref.ref(message)
    del message
    gc.collect()
    assert reference() is not None and id(reference()) == identity

    fresh = Message("assistant", "same text", tool_uses=[ToolUseBlock("new", "ReadFile", {})])
    saved = []
    tracker.persist([fresh], saved.append)
    assert saved == [fresh]
    tracker.reset([])
    gc.collect()
    assert reference() is None


def test_result_block_is_retained_while_rewrapped_message_is_not_saved_again():
    call = Message("assistant", "", tool_uses=[ToolUseBlock("old", "ReadFile", {})])
    result = ToolResultBlock("old", "saved output")
    wrapper = Message("user", "", tool_results=[result])
    wrapper_ref, result_ref = weakref.ref(wrapper), weakref.ref(result)
    tracker = ChatHistoryPersistence([call, wrapper])
    del wrapper, result
    gc.collect()
    assert wrapper_ref() is None and result_ref() is not None

    conversation = ConversationManager()
    conversation.history = [call, Message("user", "", tool_results=[result_ref()])]
    saved = []
    for _ in range(3):
        assert conversation.repair_incomplete_tool_turns() == 0
        tracker.persist(conversation.history, saved.append)
    assert not saved


def test_repair_persists_only_new_and_synthetic_results_in_mixed_batch():
    call = Message("assistant", "", tool_uses=[ToolUseBlock(name, "ReadFile", {}) for name in ("old", "new", "missing")])
    old = ToolResultBlock("old", "already saved")
    fresh = ToolResultBlock("new", "new output", content_blocks=[{"type": "text", "text": "full output"}],
                            host_observation={"evidence_source": "host", "command_started": True})
    conversation = ConversationManager()
    conversation.history = [call, Message("user", "", tool_results=[old])]
    tracker = ChatHistoryPersistence(conversation.history)
    conversation.history.append(Message("user", "", tool_results=[fresh]))
    assert conversation.repair_incomplete_tool_turns() == 1
    saved = []
    tracker.persist(conversation.history, saved.append)
    assert len(saved) == 1
    assert [result.tool_use_id for result in saved[0].tool_results] == ["new", "missing"]
    assert saved[0].tool_results[0] is fresh
    assert fresh.content_blocks == [{"type": "text", "text": "full output"}]
    assert fresh.host_observation == {"evidence_source": "host", "command_started": True}
    interrupted = saved[0].tool_results[1]
    assert interrupted.is_error and interrupted.content == INTERRUPTED_TOOL_RESULT
    assert conversation.repair_incomplete_tool_turns() == 0
    tracker.persist(conversation.history, saved.append)
    assert len(saved) == 1


def test_result_projection_preserves_all_wrapper_and_block_metadata():
    old = ToolResultBlock("old", "saved")
    fresh = ToolResultBlock("new", "new", is_error=True,
                            content_blocks=[{"type": "text", "text": "detail"}], host_observation={"source": "host"})
    tracker = ChatHistoryPersistence([Message("user", "", tool_results=[old])])
    wrapper = Message("user", "context", tool_results=[old, fresh],
                      thinking_blocks=[ThinkingBlock("thought", "signature")],
                      content_blocks=[{"type": "text", "text": "context detail"}],
                      context_kind="host-context", host_observation={"wrapper": "kept"})
    saved = []
    tracker.persist([wrapper], saved.append)
    projected = saved[0]
    assert projected is not wrapper and projected.tool_results == [fresh]
    assert projected.tool_results[0] is fresh and wrapper.tool_results == [old, fresh]
    assert projected.content == wrapper.content
    assert projected.content_blocks is wrapper.content_blocks
    assert projected.thinking_blocks is wrapper.thinking_blocks
    assert projected.host_observation is wrapper.host_observation
    assert projected.context_kind == wrapper.context_kind


def test_distinct_user_messages_with_same_text_are_both_appended():
    first, second = Message("user", "继续核对"), Message("user", "继续核对")
    tracker, saved = ChatHistoryPersistence([]), []
    tracker.persist([first, second], saved.append)
    tracker.persist([first, second], saved.append)
    assert len(saved) == 2 and saved[0] is first and saved[1] is second


def test_compact_reset_marks_current_history_and_releases_removed_objects():
    discarded = Message("assistant", "summarized old response")
    discarded_result = ToolResultBlock("discarded", "summarized output")
    message_ref, result_ref = weakref.ref(discarded), weakref.ref(discarded_result)
    tracker = ChatHistoryPersistence([discarded, Message("user", "", tool_results=[discarded_result])])
    del discarded, discarded_result
    current = Message("user", "kept by compact boundary")
    tracker.reset([current])
    gc.collect()
    assert message_ref() is None and result_ref() is None
    saved = []
    tracker.persist([current], saved.append)
    assert not saved
    followup = Message("user", "kept by compact boundary")
    tracker.persist([current, followup], saved.append)
    assert saved == [followup]


def _raw_records(runtime):
    path = runtime.session._sessions_dir / (runtime.conversation_id + ".jsonl")
    raw = path.read_bytes()
    return raw, [json.loads(line) for line in raw.decode("utf-8").splitlines()]


@pytest.mark.asyncio
async def test_real_chat_persists_old_results_once_and_restores_new_tool_call(tmp_path, monkeypatch):
    _, runtime, socket = setup(tmp_path, [])
    source = tmp_path / "offline-source.txt"
    source.write_text("offline evidence", encoding="utf-8")
    seed = [Message("user", "old question"),
            Message("assistant", "", tool_uses=[ToolUseBlock("old-read", "ReadFile", {"file_path": str(source)})]),
            Message("user", "", tool_results=[ToolResultBlock("old-read", "old output", host_observation={"source": "host"})])]
    runtime.conversation.history = seed
    for message in seed:
        runtime.session.append(message)
    prefix, _ = _raw_records(runtime)
    client = MockLLMClient([
        [ToolCallComplete("fresh-read", "ReadFile", {"file_path": str(source)}), StreamEnd("tool_use")],
        [TextDelta("checked offline"), StreamEnd("end_turn")],
    ])
    runtime.client = runtime.agent.client = client
    runtime.agent.runtime_scheduler = RuntimeScheduler(limit=1)
    monkeypatch.setattr(server.SESSION, "_mcp_init_task", None, raising=False)
    try:
        outcome = await server.run_chat(socket, "解释这段离线文本", runtime=runtime)
        assert not outcome["interrupted"] and outcome["error"] is None
        assert client._call_index == 2
        raw, records = _raw_records(runtime)
        assert raw.startswith(prefix)
        call_ids = [block["id"] for record in records if record["type"] == "assistant" and isinstance(record["content"], list)
                    for block in record["content"] if block.get("type") == "tool_use"]
        result_ids = [record["tool_use_id"] for record in records if record["type"] == "tool_result"]
        assert call_ids == result_ids == ["old-read", "fresh-read"]
    finally:
        runtime.session.close()
    restored = SessionManager(str(tmp_path)).resume(runtime.conversation_id)
    assert restored is not None
    try:
        calls = [call.tool_use_id for message in restored.messages for call in message.tool_uses]
        results = [result for message in restored.messages for result in message.tool_results]
        assert calls == [result.tool_use_id for result in results] == ["old-read", "fresh-read"]
        assert results[0].host_observation == {"source": "host"}
        assert not results[1].is_error and "offline evidence" in results[1].content
    finally:
        restored.session.close()


@pytest.mark.asyncio
async def test_chat_compact_boundary_marks_kept_tail_without_reappending_it(tmp_path, monkeypatch):
    _, runtime, socket = setup(tmp_path, [])
    kept = Message("user", "kept original text")
    runtime.conversation.history = [Message("user", "summarized prefix"), kept]
    for message in runtime.conversation.history:
        runtime.session.append(message)
    prefix, _ = _raw_records(runtime)

    async def compacted_events(conversation):
        conversation.history = [Message("user", "compact summary"), kept]
        yield CompactNotification(100, "compacted offline", CompactBoundary(summary="compact summary", keep=[kept]))
        conversation.history.append(Message("user", "new original tail"))
        yield TurnComplete(0)

    monkeypatch.setattr(runtime.agent, "run", compacted_events)
    monkeypatch.setattr(server.SESSION, "_mcp_init_task", None, raising=False)
    try:
        outcome = await server.run_chat(socket, "", record_user=False, runtime=runtime)
        assert not outcome["interrupted"] and outcome["error"] is None
        raw, records = _raw_records(runtime)
        assert raw.startswith(prefix)
        tail = [json.loads(line) for line in raw[len(prefix):].decode("utf-8").splitlines()]
        assert [record["type"] for record in tail] == ["compact_boundary", "user"]
        assert tail[1]["content"] == "new original tail"
        assert sum(record["type"] == "user" and record["content"] == kept.content for record in records) == 1
    finally:
        runtime.session.close()
    restored = SessionManager(str(tmp_path)).resume(runtime.conversation_id)
    assert restored is not None
    try:
        assert [message.content for message in restored.messages][-2:] == ["kept original text", "new original tail"]
    finally:
        restored.session.close()
