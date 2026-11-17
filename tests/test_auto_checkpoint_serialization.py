from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mewcode.conversation import ConversationManager, Message, ToolResultBlock, ToolUseBlock
from mewcode.expert_runs import deserialize_messages, serialize_messages
from mewcode.memory.session import (
    RecordType,
    SessionRecord,
    make_compact_boundary,
    parse_compact_boundary,
    records_to_messages,
)
from mewcode.serialization import build_messages
from mewcode.tools.base import ToolCallComplete


@pytest.fixture
def checkpoint_observation():
    return {
        "auto_review_checkpoint": {
            "schema": "mewcode.auto-review-checkpoint.v1",
            "review_call_ref": "checkpoint-ref-1",
            "tool_name": "WriteFile",
            "status": "denied_not_executed",
            "effect": {"kind": "write", "targets": ["path:/workspace/result.txt"]},
        },
        "command_execution": {"status": "denied", "command_started": False},
    }


def _history(checkpoint_observation):
    return [
        Message(
            role="assistant",
            content="",
            tool_uses=[ToolUseBlock("write-id", "WriteFile", {
                "file_path": "/workspace/result.txt", "content": "candidate",
            })],
        ),
        Message(
            role="user",
            content="",
            tool_results=[ToolResultBlock(
                "write-id", "AUTO_DENIED: write not performed", is_error=True,
                host_observation=checkpoint_observation,
            )],
        ),
    ]


def _restored_observation(messages):
    return messages[-1].tool_results[0].host_observation


def test_expert_node_message_serializer_preserves_host_checkpoint_roundtrip(checkpoint_observation):
    original = _history(checkpoint_observation)

    encoded = serialize_messages(original)
    persisted = json.loads(json.dumps(encoded, ensure_ascii=False))
    restored = deserialize_messages(persisted)

    assert _restored_observation(restored) == checkpoint_observation
    assert _restored_observation(restored) is not checkpoint_observation
    assert restored[0].tool_uses[0].tool_name == "WriteFile"


def test_session_records_and_compact_boundary_preserve_host_checkpoint(checkpoint_observation):
    original = _history(checkpoint_observation)
    records = [record for message in original for record in SessionRecord.from_message(message)]
    stored = [SessionRecord.from_jsonl(record.to_jsonl()) for record in records]
    restored = records_to_messages([record for record in stored if record is not None])

    assert _restored_observation(restored) == checkpoint_observation
    assert any(record.type == RecordType.TOOL_RESULT and record.host_observation
               for record in stored if record is not None)

    boundary = make_compact_boundary("older context summarized", original)
    summary, compacted_history = parse_compact_boundary(boundary)
    assert summary == "older context summarized"
    assert _restored_observation(compacted_history) == checkpoint_observation


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_plaintext_carrier_message_checkpoint_roundtrips_and_stays_host_only(
    role, checkpoint_observation,
):
    carrier = Message(
        role=role,
        content="A host-only checkpoint is attached to this ordinary message.",
        host_observation=checkpoint_observation,
    )

    expert_roundtrip = deserialize_messages(json.loads(json.dumps(serialize_messages([carrier]))))
    assert expert_roundtrip[0].content == carrier.content
    assert expert_roundtrip[0].host_observation == checkpoint_observation

    record = SessionRecord.from_message(carrier)[0]
    stored = SessionRecord.from_jsonl(record.to_jsonl())
    assert stored is not None
    session_roundtrip = records_to_messages([stored])
    assert session_roundtrip[0].content == carrier.content
    assert session_roundtrip[0].host_observation == checkpoint_observation

    boundary = make_compact_boundary("summary", [carrier])
    _, compacted = parse_compact_boundary(boundary)
    assert compacted[0].content == carrier.content
    assert compacted[0].host_observation == checkpoint_observation

    for protocol in ("anthropic", "openai", "openai-compat"):
        provider_input = json.dumps(build_messages([carrier], protocol), ensure_ascii=False)
        assert "auto_review_checkpoint" not in provider_input
        assert "checkpoint-ref-1" not in provider_input


@pytest.mark.asyncio
async def test_denied_abandoned_review_checkpoint_restores_after_expert_message_roundtrip(
    tmp_path, monkeypatch,
):
    from tests.test_effect_recovery_integration import (
        _deny,
        _make_agent,
        _only_result,
        _review_ref,
        _write,
        _invoke,
    )

    from mewcode.permissions.review_recovery import ReviewRecovery

    agent, checker, _ = _make_agent(tmp_path, monkeypatch, [_deny()])
    checker.review_workspace_writes = True
    conversation = agent._current_conversation
    conversation.add_user_message("Create the requested file only if review allows it.")

    def record_tool_turn(conv, call, result):
        conv.add_assistant_message("", [ToolUseBlock(call.tool_id, call.tool_name, call.arguments)])
        conv.add_tool_results_message([ToolResultBlock(
            call.tool_id,
            result.output,
            is_error=result.is_error,
            content_blocks=list(result.content_blocks),
            host_observation=result.host_observation,
        )])

    target = tmp_path / "reviewed.txt"
    denied_call = _write(target, "must remain absent")
    denied_results, _ = await _invoke(agent, denied_call)
    denied = _only_result(denied_results)
    assert "AUTO_DENIED:" in denied.output
    denied_ref = _review_ref(denied.output)
    assert not target.exists()
    record_tool_turn(conversation, denied_call, denied)

    abandon_call = ToolCallComplete(
        "abandon-id", "ResolveAutoReview", {
            "action": "abandon",
            "review_call_refs": [denied_ref],
            "reason": "Leave the denied write undone and report the limit.",
        },
    )
    abandoned_results, _ = await _invoke(agent, abandon_call)
    abandoned = _only_result(abandoned_results)
    assert "AUTO_OPERATION_ABANDONED:" in abandoned.output
    assert agent._review_recovery.requests[denied_ref].abandoned
    record_tool_turn(conversation, abandon_call, abandoned)

    checkpoint = conversation.history[-1].tool_results[0].host_observation["auto_review_checkpoint"]
    effect_key = agent._review_recovery.requests[denied_ref].effect.key
    assert isinstance(checkpoint["stalls"][effect_key], list)

    persisted = json.loads(json.dumps(serialize_messages(conversation.history), ensure_ascii=False))
    restored_conversation = ConversationManager(history=deserialize_messages(persisted))
    restored_agent, restored_checker, _ = _make_agent(tmp_path, monkeypatch, [], work_dir=tmp_path)
    restored_checker.review_workspace_writes = True
    restored_agent._current_conversation = restored_conversation
    restored_agent._review_recovery = ReviewRecovery()
    restored_agent._restore_safety_review_state(restored_conversation)

    restored = restored_agent._review_recovery.requests[denied_ref]
    assert restored.abandoned
    assert restored.user_response == ""
    assert restored.effect.as_dict() == agent._review_recovery.requests[denied_ref].effect.as_dict()
    assert restored_agent._review_recovery.strict == agent._review_recovery.strict
    assert restored_agent._review_recovery.total_denials == 1
    assert list(restored_agent._review_recovery.recent) == [True]
    assert restored_agent._review_recovery.stalls[effect_key] == agent._review_recovery.stalls[effect_key]
    assert restored_agent._review_recovery.needs_review(
        restored_agent, "WriteFile", {"file_path": str(target), "content": "another attempt"}
    )
    assert not target.exists()


@pytest.mark.parametrize("mode", ["fix", "update"])
def test_fix_and_update_checkpoint_histories_keep_host_observations(mode, checkpoint_observation):
    # Both maintenance modes use this one durable history serializer before
    # checkpointing their state; rehydrate through the same saved-history path.
    from mewcode.orchestration.maintenance_agent import agent_history

    conversation = SimpleNamespace(history=_history(checkpoint_observation))
    encoded = agent_history(SimpleNamespace(_current_conversation=None), conversation)
    persisted = json.loads(json.dumps(encoded, ensure_ascii=False))
    restored = deserialize_messages(persisted)

    assert _restored_observation(restored) == checkpoint_observation


def test_old_expert_message_without_host_observation_still_deserializes():
    restored = deserialize_messages([{
        "role": "user",
        "content": "",
        "tool_results": [{"id": "old-result", "content": "old transcript", "is_error": False}],
    }])

    assert restored[0].tool_results[0].host_observation is None
