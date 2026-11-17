"""Persistence constraints and verified pre-write snapshots at real dispatch."""
import copy

import pytest

from mewcode.conversation import ConversationManager, Message, ToolUseBlock, ToolResultBlock
from mewcode.permissions.review_recovery import ReviewRecovery
from tests.test_effect_recovery_integration import _make_agent, _invoke, _write, _deny, _allow


def remember(agent, call, result):
    agent._current_conversation.history.extend([
        Message(role="assistant", content="", tool_uses=[ToolUseBlock(tool_use_id=call.tool_id,
            tool_name=call.tool_name, arguments=call.arguments)]),
        Message(role="user", content="", tool_results=[ToolResultBlock(tool_use_id=call.tool_id,
            content=result.output, is_error=result.is_error,
            host_observation=copy.deepcopy(result.host_observation))]),
    ])
    agent._stamp_review_checkpoint()


@pytest.mark.asyncio
async def test_abandoned_denial_survives_checkpoint_and_still_requires_review(tmp_path, monkeypatch):
    agent, checker, _ = _make_agent(tmp_path, monkeypatch, [_deny()])
    checker.review_workspace_writes = True
    call = _write(tmp_path / "target.txt")
    results, _ = await _invoke(agent, call)
    item = next(iter(agent._review_recovery.requests.values()))
    assert agent._review_recovery.abandon([item.reference], "skip this operation")
    remember(agent, call, results[0])
    restored, _, _ = _make_agent(tmp_path, monkeypatch)
    restored._current_conversation = copy.deepcopy(agent._current_conversation)
    restored._restore_safety_review_state(restored._current_conversation)
    assert not restored._safety_review_unavailable
    assert restored._review_recovery.requests[item.reference].abandoned
    assert restored._review_recovery.needs_review(restored, call.tool_name, call.arguments)
    assert restored._review_recovery.total_denials == 1
    assert not restored._review_recovery.needs_review(restored, "WriteFile", {
        "file_path": str(tmp_path / "independent.txt"), "content": "safe"})


@pytest.mark.asyncio
@pytest.mark.parametrize('interactive', [True, False])
async def test_restored_abandonment_does_not_append_a_footer_to_later_replies(tmp_path, monkeypatch, interactive):
    from mewcode.agent import StreamText
    from mewcode.tools.base import TextDelta, StreamEnd
    from tests.test_permissions import MockLLMClient

    agent, checker, _ = _make_agent(tmp_path, monkeypatch, [_deny()])
    checker.review_workspace_writes = True
    call = _write(tmp_path / 'not-written.txt')
    results, _ = await _invoke(agent, call)
    item = next(iter(agent._review_recovery.requests.values()))
    assert agent._review_recovery.abandon([item.reference], 'historical abandoned operation')
    remember(agent, call, results[0])
    conversation = copy.deepcopy(agent._current_conversation)
    for _ in range(2):
        restored, _, reviewer = _make_agent(tmp_path, monkeypatch)
        restored.client = MockLLMClient([[TextDelta('当前结果。'), StreamEnd('end_turn')]])
        if interactive:
            conversation.add_user_message('说明当前结果')
            events = [event async for event in restored.run(conversation)]
            answer = ''.join(event.text for event in events if isinstance(event, StreamText))
        else:
            answer = await restored.run_to_completion('说明当前结果', conversation)
        assert answer == '当前结果。'
        assert conversation.history[-1].content == '当前结果。'
        assert restored._review_recovery.requests[item.reference].abandoned
        assert restored._review_recovery.needs_review(restored, call.tool_name, call.arguments)
        reviewer._ask_model.assert_not_awaited()
        assert not (tmp_path / 'not-written.txt').exists()


@pytest.mark.asyncio
async def test_checkpoint_never_restores_one_time_consent(tmp_path, monkeypatch):
    agent, checker, _ = _make_agent(tmp_path, monkeypatch, [_deny("missing_authorization")])
    checker.review_workspace_writes = True
    call = _write(tmp_path / "target.txt")
    results, _ = await _invoke(agent, call)
    item = next(iter(agent._review_recovery.requests.values()))
    item.user_response = "approved_once"
    remember(agent, call, results[0])
    restored, _, _ = _make_agent(tmp_path, monkeypatch)
    restored._current_conversation = copy.deepcopy(agent._current_conversation)
    restored._restore_safety_review_state(restored._current_conversation)
    assert restored._review_recovery.requests[item.reference].user_response == "expired"
    assert restored._review_recovery.consume_consent(restored, call.tool_name, call.arguments) is None


@pytest.mark.asyncio
async def test_completed_operation_cannot_be_reported_as_abandoned(tmp_path, monkeypatch):
    agent, checker, _ = _make_agent(tmp_path, monkeypatch, [_deny(), _allow()])
    checker.review_workspace_writes = True
    call = _write(tmp_path / "target.txt")
    await _invoke(agent, call)
    item = next(iter(agent._review_recovery.requests.values()))
    results, _ = await _invoke(agent, call)
    assert not results[0].is_error
    assert not agent._review_recovery.abandon([item.reference], "not performed")
    from mewcode.tools.base import ToolResult
    for index in range(65):
        row = agent._review_recovery.begin_effect(agent, _write(tmp_path / f"other{index}.txt"))
        agent._review_recovery.finish_effect(row, ToolResult("mock result"))
    assert not agent._review_recovery.abandon([item.reference], "not performed")


@pytest.mark.asyncio
async def test_independent_policy_denials_do_not_enable_global_strict_mode(tmp_path, monkeypatch):
    agent, checker, _ = _make_agent(tmp_path, monkeypatch, [_deny("policy_violation")] * 3)
    checker.review_workspace_writes = True
    for name in ("one.txt", "two.txt", "three.txt"):
        await _invoke(agent, _write(tmp_path / name))
    assert not agent._review_recovery.strict
    assert agent._review_recovery.stalled_action(agent, "WriteFile", _write(tmp_path / "three.txt").arguments) is None


@pytest.mark.asyncio
async def test_auto_write_creates_real_backup_that_existing_rewind_restores(tmp_path, monkeypatch):
    agent, _, reviewer = _make_agent(tmp_path, monkeypatch)
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    from mewcode.tools.base import ToolCallComplete
    await _invoke(agent, ToolCallComplete("read", "ReadFile", {"file_path": str(target)}))
    call = _write(target, "after")
    results, _ = await _invoke(agent, call)
    assert not results[0].is_error
    assert results[0].host_observation["rollback_prewrite"]["kind"] == "verified_file_snapshot"
    reviewer._ask_model.assert_not_awaited()
    remember(agent, call, results[0])
    agent.file_history.make_snapshot(2, "edit")
    agent.file_history.rewind(0)
    assert target.read_text(encoding="utf-8") == "before"


def test_cumulative_read_signal_survives_truncation_and_restore(tmp_path, monkeypatch):
    agent, _, _ = _make_agent(tmp_path, monkeypatch)
    recovery = agent._review_recovery
    from mewcode.tools.base import ToolCallComplete, ToolResult
    row = recovery.begin_effect(agent, ToolCallComplete("read", "ReadFile", {"file_path": str(tmp_path / "input")}))
    recovery.finish_effect(row, ToolResult("untrusted document"))
    for index in range(70):
        row = recovery.begin_effect(agent, _write(tmp_path / f"{index}.txt"))
        recovery.finish_effect(row, ToolResult("written"))
    restored = ReviewRecovery()
    restored.restore(agent, agent._current_conversation, recovery.checkpoint(agent._current_conversation))
    effect = restored.signature(agent, "Bash", {"command": "python -c 'opaque'"})
    assert restored.injection_signals(effect)
    context = restored.review_effect_context(effect)
    assert len(context["rows"]) <= 16
    assert context["omitted_rows"] == 55
    assert context["counts"]["read"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["abandoned", "pending", "rejected"])
@pytest.mark.parametrize("transient_tail", [False, True])
async def test_compaction_preserves_constraints_without_original_tool_messages(tmp_path, monkeypatch, state, transient_tail):
    from tests.test_context import _SummaryClient, _make_long_conversation
    from mewcode.context.manager import auto_compact, CompactEvent
    from mewcode.memory.session import make_compact_boundary, records_to_messages
    agent, checker, _ = _make_agent(tmp_path, monkeypatch, [_deny("missing_authorization")])
    checker.review_workspace_writes = True
    call = _write(tmp_path / "target.txt")
    results, _ = await _invoke(agent, call)
    item = next(iter(agent._review_recovery.requests.values()))
    if state == "abandoned":
        assert agent._review_recovery.abandon([item.reference], "skip")
    if state == "rejected":
        item.user_response = "rejected"
    remember(agent, call, results[0])
    conversation = agent._current_conversation
    conversation.history.extend(_make_long_conversation().history)
    if transient_tail:
        conversation.history[-1].transient = True
    agent._stamp_review_checkpoint(conversation.history[1].tool_results[0])
    event = await auto_compact(conversation, _SummaryClient(), context_window=200_000,
                              session_dir=tmp_path, manual=True)
    assert isinstance(event, CompactEvent)
    assert not any(message.tool_uses for message in conversation.history)
    restored, _, _ = _make_agent(tmp_path, monkeypatch)
    restored._current_conversation = ConversationManager()
    boundary = make_compact_boundary(event.boundary.summary, event.boundary.keep)
    restored._current_conversation.history = records_to_messages([boundary])
    restored._restore_safety_review_state(restored._current_conversation)
    assert not restored._safety_review_unavailable
    assert restored._review_recovery.needs_review(restored, call.tool_name, call.arguments)
    saved = restored._review_recovery.requests[item.reference]
    assert saved.abandoned == (state == "abandoned")
    if state == "pending":
        assert saved.arguments == call.arguments
        assert restored._review_recovery.pending_authorizations() == [saved]
    if state == "rejected":
        assert saved.user_response == "rejected"
        from mewcode.permissions.checker import Decision
        assert restored._review_recovery_decision(restored.registry.get(call.tool_name), call,
                                                 Decision("allow", "test", "test")).effect == "deny"


@pytest.mark.asyncio
async def test_changed_backup_blocks_before_write(tmp_path, monkeypatch):
    agent, _, _ = _make_agent(tmp_path, monkeypatch)
    target = tmp_path / "new.txt"
    call = _write(target)
    history, receipt = agent._prepare_rollback_evidence(call.tool_name, call.arguments)
    from pathlib import Path
    backup = Path(history._prepared_by_id[receipt["backup_id"]]["backup_path"])
    backup.write_bytes(b"tampered")
    results, _ = await _invoke(agent, call)
    assert results[0].is_error
    assert "AUTO_ROLLBACK_CONTEXT_CHANGED" in results[0].output
    assert not target.exists()
    assert not agent._review_recovery.ledger
