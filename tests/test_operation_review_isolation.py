"""One failed review must not become a session-wide execution boundary."""
import copy
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import ErrorEvent, LoopComplete, ToolResultEvent
from mewcode.client import RateLimitError
from mewcode.conversation import ConversationManager, Message, ToolResultBlock, ToolUseBlock
from mewcode.expert_runs import deserialize_messages, serialize_messages
from mewcode.permissions.reviewer import SafetyReviewResult
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete, ToolResult
from tests.legacy_review_policy import LegacyReviewChecker
from tests.test_auto_effect_checkpoint import remember
from tests.test_effect_recovery_integration import _make_agent, _invoke, _write, _deny, _allow
from tests.test_permissions import MockLLMClient
from tests.test_review_text_capacity_recovery import stopped_history


COMMAND = {"command": 'python -c "print(1)"'}


@pytest.fixture(autouse=True)
def mocked_command_runtime(monkeypatch):
    # Every command in this module is mocked. Readiness selects the existing
    # review protocol, without launching a process or touching host settings.
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    monkeypatch.setattr("mewcode.sandbox.windows.native_fastpath_ready", lambda: True)


def review_agent(tmp_path, monkeypatch):
    agent, checker, reviewer = _make_agent(tmp_path, monkeypatch)
    # Production retains its own OS boundary gate; exercise the legacy review
    # route only for commands whose execution is mocked in this test.
    agent.permission_checker = LegacyReviewChecker(
        checker.detector, checker.sandbox, checker.rule_engine, checker.mode)
    agent.registry.get("Bash").run = AsyncMock(return_value=ToolResult("reviewed command executed"))
    reviewer._ask_model = AsyncMock(side_effect=TimeoutError())
    return agent, agent.permission_checker, reviewer


async def invoke(agent, call, mode):
    if mode == "completion":
        return await agent._execute_tool_noninteractive(call)
    results, _ = await _invoke(agent, call)
    assert len(results) == 1
    return results[0]


@pytest.mark.parametrize("mode", ["stream", "completion"])
@pytest.mark.asyncio
async def test_opaque_command_outage_keeps_default_allowed_writes_running(tmp_path, monkeypatch, mode):
    agent, checker, reviewer = review_agent(tmp_path, monkeypatch)
    independent = tmp_path / "independent.txt"
    agent.client = MockLLMClient([
        [ToolCallComplete("failed", "Bash", COMMAND),
         _write(independent, "same batch finished"), StreamEnd("tool_use")],
        [_write(tmp_path / "next.txt", "next turn finished"), StreamEnd("tool_use")],
        [TextDelta("the command is still pending"), StreamEnd("end_turn")],
    ])
    conversation = agent._current_conversation
    conversation.add_user_message("Run the check and update the independent notes")
    if mode == "stream":
        events = [event async for event in agent.run(conversation)]
        assert not any(isinstance(event, LoopComplete) for event in events)
        assert any(isinstance(event, ErrorEvent) and event.kind == "safety_review_unavailable" for event in events)
        writes = [event for event in events if isinstance(event, ToolResultEvent) and event.tool_name == "WriteFile"]
        assert len(writes) == 2 and all(not event.is_error for event in writes)
    else:
        assert "AUTO_REVIEW_UNAVAILABLE" in await agent.run_to_completion("Continue this task", conversation)
    assert not checker.review_workspace_writes
    assert independent.read_text(encoding="utf-8") == "same batch finished"
    assert (tmp_path / "next.txt").read_text(encoding="utf-8") == "next turn finished"
    assert agent.client._call_index == 3
    assert reviewer._ask_model.await_count == 2
    agent.registry.get("Bash").run.assert_not_awaited()
    assert not agent._safety_review_unavailable
    assert not conversation.safety_review_state  # not an evidence/inconclusive gate
    assert len(agent._review_recovery.pending_reviews()) == 1
    assert agent._review_recovery.total_denials == 0


@pytest.mark.parametrize("compacted", [False, True])
@pytest.mark.asyncio
async def test_outage_checkpoint_restores_only_original_and_resumes_after_recovery(tmp_path, monkeypatch, compacted):
    agent, _, reviewer = review_agent(tmp_path, monkeypatch)
    original = ToolCallComplete("original", "Bash", COMMAND)
    failed = await invoke(agent, original, "stream")
    assert failed.is_error and "AUTO_REVIEW_UNAVAILABLE" in failed.output
    remember(agent, original, failed)
    conversation = ConversationManager(history=deserialize_messages(serialize_messages(agent._current_conversation.history)))
    if compacted:
        checkpoint = copy.deepcopy(conversation.history[-1].tool_results[0].host_observation["auto_review_checkpoint"])
        conversation.history = [Message("user", "summary of earlier work", host_observation={"auto_review_checkpoint": checkpoint})]
    resumed, checker, recovered = review_agent(tmp_path, monkeypatch)
    resumed._current_conversation = conversation
    resumed._restore_safety_review_state(conversation)
    assert not resumed._safety_review_unavailable
    pending = resumed._review_recovery.pending_reviews()
    assert len(pending) == 1 and pending[0].kind == "unavailable" and pending[0].arguments == COMMAND
    independent = tmp_path / "independent.txt"
    assert not (await invoke(resumed, _write(independent), "stream")).is_error
    recovered._ask_model.assert_not_awaited()
    still_failed = await invoke(resumed, ToolCallComplete("retry", "Bash", COMMAND), "stream")
    assert still_failed.is_error and "AUTO_REVIEW_UNAVAILABLE" in still_failed.output
    # Retry is still reviewed and denied while the service is unavailable.
    resumed.registry.get("Bash").run.assert_not_awaited()
    recovered._ask_model = AsyncMock(return_value=_allow())
    result = await invoke(resumed, ToolCallComplete("recovered", "Bash", COMMAND), "stream")
    assert not result.is_error
    recovered._ask_model.assert_awaited_once()
    resumed.registry.get("Bash").run.assert_awaited_once()
    assert not resumed._review_recovery.pending_reviews()
    assert independent.exists()


@pytest.mark.parametrize("mode", ["stream", "completion"])
@pytest.mark.asyncio
async def test_repeated_outage_limits_only_original_operation(tmp_path, monkeypatch, mode):
    agent, _, reviewer = review_agent(tmp_path, monkeypatch)
    original = ToolCallComplete("original", "Bash", COMMAND)
    for _ in range(3):
        assert (await invoke(agent, original, mode)).is_error
    reviews = reviewer._ask_model.await_count
    held = await invoke(agent, ToolCallComplete("new-id", "Bash", COMMAND), mode)
    assert held.is_error and "AUTO_REVIEW_DENIAL_LIMIT" in held.output
    assert reviewer._ask_model.await_count == reviews == 6
    assert not (await invoke(agent, _write(tmp_path / "independent.txt"), mode)).is_error
    agent.registry.get("Bash").run.assert_not_awaited()
    assert not agent._safety_review_unavailable
    # A new real user instruction permits a fresh review, without restarting
    # or replaying any completed independent operation.
    agent._current_conversation.add_user_message("The service has recovered; retry the same check")
    reviewer._ask_model = AsyncMock(return_value=_allow())
    assert not (await invoke(agent, original, mode)).is_error
    agent.registry.get("Bash").run.assert_awaited_once()


@pytest.mark.parametrize("mode", ["stream", "completion"])
@pytest.mark.asyncio
async def test_operation_cooldown_survives_restore_and_does_not_consume_retry_budget(tmp_path, monkeypatch, mode):
    now = [100.0]
    monkeypatch.setattr("mewcode.permissions.review_recovery.time.time", lambda: now[0])
    agent, _, reviewer = review_agent(tmp_path, monkeypatch)
    reviewer._ask_model = AsyncMock(side_effect=RateLimitError("review busy", retry_after=600))
    original = ToolCallComplete("limited", "Bash", COMMAND)
    failed = await invoke(agent, original, mode)
    assert failed.is_error and reviewer._ask_model.await_count == 1
    remember(agent, original, failed)
    resumed, _, recovered = review_agent(tmp_path, monkeypatch)
    resumed._current_conversation = copy.deepcopy(agent._current_conversation)
    resumed._restore_safety_review_state(resumed._current_conversation)
    recovered._ask_model = AsyncMock(return_value=_allow())
    held = await invoke(resumed, original, mode)
    assert held.is_error and "冷却" in held.output
    recovered._ask_model.assert_not_awaited()
    pending = resumed._review_recovery.pending_reviews()[0]
    assert pending.attempts == 1 and resumed._review_recovery.stalls[pending.effect.key][1] == 1
    assert not (await invoke(resumed, _write(tmp_path / "independent.txt"), mode)).is_error
    now[0] = 701
    assert not (await invoke(resumed, original, mode)).is_error
    recovered._ask_model.assert_awaited_once()
    resumed.registry.get("Bash").run.assert_awaited_once()
    assert not resumed._review_recovery.pending_reviews()


@pytest.mark.parametrize("outcome", ["inconclusive", "deny"])
@pytest.mark.asyncio
async def test_new_review_replaces_outage_state(tmp_path, monkeypatch, outcome):
    agent, _, reviewer = review_agent(tmp_path, monkeypatch)
    original = ToolCallComplete("original", "Bash", COMMAND)
    await invoke(agent, original, "stream")
    unavailable = agent._review_recovery.pending_reviews()[0]
    review = SafetyReviewResult(decision="deny", risk="unknown", reason="needs concrete evidence",
        alternative="read source first", inconclusive=outcome == "inconclusive",
        denial_kind="semantic_risk" if outcome == "deny" else "",
        review_route={"status": "needs_evidence" if outcome == "inconclusive" else "denied"})
    reviewer.review = AsyncMock(return_value=review)
    result = await invoke(agent, original, "stream")
    assert result.is_error and unavailable.user_response == "superseded"
    if outcome == "inconclusive":
        pending = agent._review_recovery.pending_reviews()
        assert len(pending) == 1 and pending[0].kind == "inconclusive"
        assert "未增加有效补证" in agent._review_evidence_only(agent.registry.get("Bash"), original)
        assert agent._pending_review_error().kind == "safety_review_inconclusive"
    else:
        assert not agent._review_recovery.pending_reviews()
        assert "AUTO_DENIED" in result.output
        assert agent._review_recovery.total_denials == 1
    agent.registry.get("Bash").run.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_once_consent_is_not_hidden_by_later_record_of_same_effect(tmp_path, monkeypatch):
    agent, checker, reviewer = _make_agent(tmp_path, monkeypatch)
    checker.review_workspace_writes = True
    target = tmp_path / "target.txt"
    a, b = _write(target, "a"), _write(target, "b")
    review = SafetyReviewResult(decision="deny", risk="medium", reason="needs exact consent", denial_kind="missing_authorization")
    recovery = agent._review_recovery
    approved = recovery.deny(agent, a, review)
    recovery.deny(agent, a, review)
    recovery.deny(agent, b, review)
    approved.user_response = "approved_once"
    reviewer._ask_model = AsyncMock(return_value=_allow())
    assert recovery.stalled_action(agent, a.tool_name, a.arguments) is None
    result = await invoke(agent, a, "stream")
    assert not result.is_error and target.read_text(encoding="utf-8") == "a"
    reviewer._ask_model.assert_awaited_once()
    assert reviewer._ask_model.call_args.kwargs["risk"]["host_user_consent"]["review_call_ref"] == approved.reference


@pytest.mark.asyncio
async def test_verified_legacy_service_hold_is_migrated_to_one_pending_operation(tmp_path, monkeypatch):
    reason = "AUTO_REVIEW_UNAVAILABLE: reviewer timeout"
    _, _, conversation = await stopped_history(tmp_path, monkeypatch, reason)
    agent, _, reviewer = _make_agent(tmp_path, monkeypatch)
    agent._current_conversation = conversation
    agent._restore_safety_review_state(conversation)
    assert not agent._safety_review_unavailable
    pending = agent._review_recovery.pending_reviews()
    assert len(pending) == 1 and pending[0].tool_name == "ReadFile" and pending[0].kind == "unavailable"
    source = tmp_path / "other.txt"
    source.write_text("independent evidence", encoding="utf-8")
    result = await invoke(agent, ToolCallComplete("read", "ReadFile", {"file_path": str(source)}), "stream")
    assert not result.is_error and "independent evidence" in result.output
    reviewer._ask_model.assert_not_awaited()
    assert not (tmp_path / "target.txt").exists()  # earlier policy denial preserved


def test_legacy_outage_migration_uses_latest_episode_not_earlier_resolved_call():
    from mewcode.permissions.review_recovery import legacy_review_outage_call
    reason = "AUTO_REVIEW_UNAVAILABLE: reviewer timeout"
    a = ToolUseBlock("a", "Bash", {"command": "old check"})
    retry = ToolUseBlock("a-ok", "Bash", a.arguments)
    b = ToolUseBlock("b", "Bash", {"command": "new check"})
    sibling = ToolUseBlock("held", "ReadFile", {"file_path": "evidence.txt"})
    conversation = ConversationManager(history=[
        Message("assistant", "", tool_uses=[a]), Message("user", "", tool_results=[ToolResultBlock("a", reason, True)]),
        Message("assistant", "", tool_uses=[retry]), Message("user", "", tool_results=[ToolResultBlock("a-ok", "ok")]),
        Message("assistant", "", tool_uses=[b, sibling]), Message("user", "", tool_results=[
            ToolResultBlock("b", reason, True), ToolResultBlock("held", reason, True)]),
    ])
    assert legacy_review_outage_call({"blocked_reason": reason}, conversation) is b


@pytest.mark.asyncio
async def test_strict_denial_context_and_total_denials_do_not_demote_independent_work(tmp_path, monkeypatch):
    agent, _, reviewer = _make_agent(tmp_path, monkeypatch)
    recovery = agent._review_recovery
    review = SafetyReviewResult(decision="deny", risk="high", reason="protected file", denial_kind="policy_violation",
                               review_route={"strict_review": True})
    for index in range(101):
        recovery.deny(agent, _write(tmp_path / f"denied-{index}.txt"), review)
    assert recovery.strict and recovery.total_denials == 101
    independent = _write(tmp_path / "independent.txt")
    assert not recovery.needs_review(agent, independent.tool_name, independent.arguments)
    assert not recovery.context(agent, independent.tool_name, independent.arguments)
    result = await invoke(agent, independent, "stream")
    assert not result.is_error and (tmp_path / "independent.txt").exists()
    reviewer._ask_model.assert_not_awaited()
    assert recovery.needs_review(agent, "WriteFile", _write(tmp_path / "denied-1.txt").arguments)


@pytest.mark.asyncio
async def test_service_failure_after_new_evidence_replaces_inconclusive_gate(tmp_path, monkeypatch):
    agent, _, reviewer = review_agent(tmp_path, monkeypatch)
    original = ToolCallComplete("original", "Bash", COMMAND)
    initial = SafetyReviewResult(decision="deny", risk="unknown", reason="need source evidence",
                                alternative="read the source", inconclusive=True)
    agent._inconclusive_review_message(initial, original)
    prior = agent._review_recovery.pending_reviews()[0]
    agent._current_conversation.add_tool_results_message([ToolResultBlock("read", "new concrete source facts")])
    result = await invoke(agent, original, "stream")
    assert result.is_error and "AUTO_REVIEW_UNAVAILABLE" in result.output
    assert prior.user_response == "superseded"
    assert [item.kind for item in agent._review_recovery.pending_reviews()] == ["unavailable"]
    assert not agent._current_conversation.safety_review_state
    assert agent._pending_review_error().kind == "safety_review_unavailable"
    agent.registry.get("Bash").run.assert_not_awaited()


@pytest.mark.parametrize("mode", ["stream", "completion"])
@pytest.mark.parametrize("failure", ["outage", "semantic_denial"])
@pytest.mark.asyncio
async def test_outer_stall_guard_does_not_abort_independent_work_after_repeated_blocks(tmp_path, monkeypatch, mode, failure):
    from mewcode.stall import StallPolicy
    agent, _, reviewer = review_agent(tmp_path, monkeypatch)
    agent.stall_policy = StallPolicy(repeated_calls=2, consecutive_errors=2,
        stale_rounds=2, repeated_failed_commands=2)
    agent.expert_role = "executor"
    if failure == "semantic_denial":
        reviewer._ask_model = AsyncMock(side_effect=lambda **kwargs:
            _deny() if kwargs["tool_name"] == "Bash" else _allow())
    independent = tmp_path / "independent.txt"
    agent.client = MockLLMClient([
        *[[ToolCallComplete(f"blocked-{index}", "Bash", COMMAND), StreamEnd("tool_use")] for index in range(12)],
        [_write(independent, "independent work completed"), StreamEnd("tool_use")],
        [TextDelta("independent work finished; original check remains blocked"), StreamEnd("end_turn")],
    ])
    conversation = agent._current_conversation
    conversation.add_user_message("Complete whatever independent work is possible")
    if mode == "stream":
        events = [event async for event in agent.run(conversation)]
        assert not any(isinstance(event, ErrorEvent) and event.kind == "stalled" for event in events)
        blocked = [event for event in events if isinstance(event, ToolResultEvent) and event.tool_name == "Bash"]
        assert len(blocked) == 12 and all(event.is_error and event.host_observation["operation_blocked"] for event in blocked)
    else:
        events = []
        await agent.run_to_completion("Continue independent work", conversation, event_callback=events.append)
        assert not any(event.get("kind") == "stalled" for event in events)
    assert independent.read_text(encoding="utf-8") == "independent work completed"
    assert agent.client._call_index == 14
    assert reviewer._ask_model.await_count <= 6
    agent.registry.get("Bash").run.assert_not_awaited()
