from unittest.mock import AsyncMock

import asyncio
import pytest

from mewcode.agent import Agent, ErrorEvent, PermissionRequest, ToolResultEvent
from mewcode.conversation import ConversationManager
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from tests.test_permissions import MockLLMClient
from tests.legacy_review_policy import LegacyReviewChecker
from mewcode.client import RateLimitError
from mewcode.permissions.review_retry import action_key


REQUEST = dict(tool_name="Bash", arguments={"command": 'python -c "print(1)"'}, risk={}, conversation=None)


@pytest.mark.asyncio
async def test_transient_review_failure_retries_review_only(tmp_path):
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(side_effect=[TimeoutError(), '{"outcome":"allow","rationale":"exact action is safe"}'])
    result = await reviewer.review(**REQUEST)
    assert result.allowed and not result.unavailable
    assert reviewer._ask_model.await_count == 2


@pytest.mark.asyncio
async def test_rate_limit_honors_retry_after_before_retrying_review(tmp_path, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("mewcode.permissions.reviewer.asyncio.sleep", sleep)
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(side_effect=[RateLimitError("slow down", retry_after=5), '{"outcome":"allow","rationale":"exact action is safe"}'])
    result = await reviewer.review(**REQUEST)
    assert result.allowed
    sleep.assert_awaited_once_with(5)
    assert reviewer._ask_model.await_count == 2


@pytest.mark.asyncio
async def test_repeated_rate_limits_preserve_transport_metadata_not_approval(tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.permissions.reviewer.asyncio.sleep", AsyncMock())
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(side_effect=RateLimitError("slow down", retry_after=5))
    result = await reviewer.review(**REQUEST)
    assert result.unavailable and not result.allowed and not result.escalatable
    assert result.review_route["failure_kind"] == "rate_limit"
    assert result.review_route["retry_after_seconds"] == 5
    assert result.review_route["action_key"] == action_key(REQUEST["tool_name"], REQUEST["arguments"])
    assert not reviewer._denials


@pytest.mark.asyncio
async def test_long_cooldown_is_not_shortened_to_retry_early(tmp_path, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("mewcode.permissions.reviewer.asyncio.sleep", sleep)
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(side_effect=RateLimitError("slow down", retry_after=600))
    result = await reviewer.review(**REQUEST)
    assert result.unavailable and result.review_route["retry_after_seconds"] == 600
    assert reviewer._ask_model.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_must_fit_remaining_total_budget(tmp_path, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("mewcode.permissions.reviewer.asyncio.sleep", sleep)
    reviewer = SafetyReviewer(object(), str(tmp_path), timeout_seconds=0.1)
    reviewer._ask_model = AsyncMock(side_effect=RateLimitError("cooldown", retry_after=1))
    result = await reviewer.review(**REQUEST)
    assert result.unavailable and not result.allowed
    assert reviewer._ask_model.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_review_attempt_receives_only_remaining_budget(tmp_path):
    import time
    reviewer = SafetyReviewer(object(), str(tmp_path), timeout_seconds=0.15)
    cancelled = asyncio.Event()
    attempts = 0
    async def request(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await asyncio.sleep(0.1)
            raise TimeoutError("transient provider timeout")
        try:
            await asyncio.sleep(0.1)
            return '{"outcome":"allow"}'
        except asyncio.CancelledError:
            cancelled.set()
            raise
    reviewer._ask_model = AsyncMock(side_effect=request)
    started = time.monotonic()
    result = await reviewer.review(**REQUEST)
    assert result.unavailable and not result.allowed
    assert attempts == 2 and cancelled.is_set()
    assert time.monotonic() - started < 0.25


@pytest.mark.asyncio
async def test_cancel_during_cooldown_does_not_issue_another_review(tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.permissions.reviewer.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(side_effect=RateLimitError("slow down", retry_after=5))
    with pytest.raises(asyncio.CancelledError):
        await reviewer.review(**REQUEST)
    assert reviewer._ask_model.await_count == 1


@pytest.mark.asyncio
async def test_outage_does_not_poison_same_command_after_recovery(tmp_path):
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(side_effect=[TimeoutError(), TimeoutError(), '{"outcome":"allow","rationale":"exact action is safe"}'])
    failed = await reviewer.review(**REQUEST)
    assert failed.unavailable and not failed.allowed and not failed.escalatable
    recovered = await reviewer.review(**REQUEST)
    assert recovered.allowed and not recovered.cache_hit
    assert reviewer._ask_model.await_count == 3


@pytest.mark.asyncio
async def test_shell_deny_is_not_cached_and_cancellation_not_retried(tmp_path):
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(return_value='{"outcome":"deny","rationale":"out of scope"}')
    first = await reviewer.review(**REQUEST)
    second = await reviewer.review(**REQUEST)
    assert not first.escalatable and not first.unavailable
    assert not second.cache_hit and second.denial_count == 1
    assert reviewer._ask_model.await_count == 2
    reviewer.clear_denial(tool_name=REQUEST["tool_name"], arguments=REQUEST["arguments"], conversation=None)
    reviewer._ask_model = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await reviewer.review(**REQUEST)
    assert reviewer._ask_model.await_count == 1


@pytest.mark.asyncio
async def test_insufficient_evidence_gets_one_static_script_recheck(tmp_path):
    script = tmp_path / "inspect.py"
    script.write_text("import pathlib\npathlib.Path('x').exists()\n", encoding="utf-8")
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(side_effect=[
        '{"decision":"deny","risk":"medium","reason":"need script facts",'
        '"blocker_kind":"insufficient_evidence"}',
        '{"decision":"allow","risk":"low","reason":"static facts clarify action"}',
    ])
    result = await reviewer.review(tool_name="Bash", arguments={"command": "python inspect.py"},
                                   risk={}, conversation=None)
    assert result.allowed
    assert reviewer._ask_model.await_count == 2
    second = reviewer._ask_model.await_args_list[1].kwargs["review_evidence"]
    assert "pathlib" in second[-1]["text"] and "host_readonly_script_facts" in second[-1]["text"]


@pytest.mark.parametrize("mode", ["stream", "completion"])
@pytest.mark.parametrize("failure", ["timeout", "response_format"])
@pytest.mark.asyncio
async def test_agent_review_outage_only_blocks_unreviewed_operations(tmp_path, monkeypatch, mode, failure):
    # All commands are mocked; runtime readiness only selects the real review
    # path and never launches a command or consults the production sandbox.
    monkeypatch.setattr("mewcode.sandbox.srt_runtime.runtime_status", lambda: {"ready": True})
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    monkeypatch.setattr("mewcode.sandbox.windows.native_fastpath_ready", lambda: True)
    source = tmp_path / "evidence.txt"
    source.write_text("independent evidence", encoding="utf-8")
    marker = tmp_path / "must-not-exist.txt"
    client = MockLLMClient([
        [ToolCallComplete("t1", "Bash", REQUEST["arguments"]),
         ToolCallComplete("t2", "ReadFile", {"file_path": str(source)}),
         ToolCallComplete("t3", "WriteFile", {"file_path": str(marker), "content": "not approved"}),
         ToolCallComplete("t4", "Bash", {"command": "rm -rf /"}),
         StreamEnd("end_turn", input_tokens=10, output_tokens=10)],
        [ToolCallComplete("t5", "ReadFile", {"file_path": str(source)}), StreamEnd("tool_use")],
        [TextDelta("The failed operation is still pending"), StreamEnd("end_turn")],
    ])
    registry = create_default_registry()
    registry.get("Bash").run = AsyncMock(side_effect=AssertionError("Command must not execute"))
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = (AsyncMock(side_effect=TimeoutError()) if failure == "timeout" else
                           AsyncMock(return_value='{"outcome":"allow","blocker_kind":"invalid"}'))
    checker = LegacyReviewChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    checker.set_task_authorization(TaskAuthorization.from_user_scope({
        "read_directories": [str(tmp_path)], "allowed_behaviors": ["isolated_command"],
    }, task_id="test-task"), task_id="test-task")
    checker.review_workspace_writes = True  # this write independently requires review
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path), permission_checker=checker, safety_reviewer=reviewer)
    conversation = ConversationManager()
    conversation.add_user_message("Check the figure")
    if mode == "stream":
        events = [event async for event in agent.run(conversation)]
        expected_kind = "safety_review_format" if failure == "response_format" else "safety_review_unavailable"
        assert any(isinstance(event, ErrorEvent) and event.kind == expected_kind for event in events)
        assert not any(isinstance(event, PermissionRequest) for event in events)
        reads = [event for event in events if isinstance(event, ToolResultEvent) and event.tool_name == "ReadFile"]
        assert len(reads) == 2 and all(not event.is_error and "independent evidence" in event.output for event in reads)
    else:
        result = await agent.run_to_completion("Check the figure", conversation=conversation)
        assert "AUTO_REVIEW_UNAVAILABLE" in result
    assert not agent._safety_review_unavailable
    assert not marker.exists()
    registry.get("Bash").run.assert_not_awaited()
    assert reviewer._ask_model.await_count == 4  # two bounded reviews, separate operations
    if failure == "response_format":
        assert all(item.kind == "unavailable" and item.review_route["failure_kind"] == "response_format"
                   for item in agent._review_recovery.pending_reviews())
    assert client._call_index == 3
    results = {r.tool_use_id: r for m in conversation.history for r in m.tool_results}
    assert all(not results[name].is_error and "independent evidence" in results[name].content for name in ("t2", "t5"))
    assert "AUTO_REVIEW_UNAVAILABLE" not in results["t4"].content
    assert "Permission denied" in results["t4"].content
    observations = [m.host_observation for m in conversation.history] + [r.host_observation for r in results.values()]
    checkpoints = [value["auto_review_checkpoint"] for value in observations
                   if isinstance(value, dict) and "auto_review_checkpoint" in value]
    assert len(checkpoints) == 1 and "blocked_reason" not in checkpoints[0]


@pytest.mark.asyncio
async def test_unavailable_reason_keeps_underlying_error_detail(tmp_path):
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(
        side_effect=RuntimeError("API error (404): model not supported by key group")
    )
    result = await reviewer.review(**REQUEST)
    assert result.unavailable and not result.allowed
    assert "RuntimeError" in result.reason
    assert "API error (404)" in result.reason


@pytest.mark.asyncio
async def test_unavailable_reason_redacts_secrets_in_error_detail(tmp_path):
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(
        side_effect=RuntimeError("request failed api_key=sk-live-secret-value")
    )
    result = await reviewer.review(**REQUEST)
    assert "sk-live-secret-value" not in result.reason
    assert "REDACTED" in result.reason


def test_review_timeout_is_configurable_and_bounded(tmp_path, monkeypatch):
    monkeypatch.delenv("MEWCODE_SAFETY_REVIEW_TIMEOUT_SECONDS", raising=False)
    assert SafetyReviewer(object(), str(tmp_path)).timeout_seconds == 30
    monkeypatch.setenv("MEWCODE_SAFETY_REVIEW_TIMEOUT_SECONDS", "60")
    assert SafetyReviewer(object(), str(tmp_path)).timeout_seconds == 60
    monkeypatch.setenv("MEWCODE_SAFETY_REVIEW_TIMEOUT_SECONDS", "nan")
    assert SafetyReviewer(object(), str(tmp_path)).timeout_seconds == 30


@pytest.mark.asyncio
async def test_real_timeout_cancels_review_without_refilling_its_budget(tmp_path):
    reviewer = SafetyReviewer(object(), str(tmp_path), timeout_seconds=0.01)
    attempts = []

    async def never_finishes(**_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            attempts.append("cancelled")

    reviewer._ask_model = never_finishes
    result = await reviewer.review(**REQUEST)
    assert result.unavailable and not result.allowed
    assert attempts == ["cancelled"]


@pytest.mark.parametrize("action", ["retry", "route", "fallback"])
@pytest.mark.asyncio
async def test_blocked_node_does_not_restart_via_failure_policy(tmp_path, action):
    from mewcode.expert_group import ExpertGroupOrchestrator, ExpertProfile, ExpertRoleResult
    from mewcode.experts.flow import ExpertFlowSpec
    from mewcode.model_registry import ModelRef

    calls = []

    async def runner(node, *_args, **_kwargs):
        calls.append(node.node_id)
        return ExpertRoleResult(text="安全审查服务不可用", status="blocked")

    flow = ExpertFlowSpec.from_value({
        "flow_id": "review-outage", "version": 1, "label": "review outage",
        "nodes": [
            {"node_id": "start", "role": "executor", "label": "start",
             "failure_policy": {"action": action, "max_attempts": 3,
                                "target_node_id": "repair" if action != "retry" else ""}},
            {"node_id": "repair", "role": "executor", "label": "repair"},
            {"node_id": "done", "role": "manager", "label": "done"},
        ],
        "edges": [{"from": "start", "to": "done"}, {"from": "repair", "to": "done"}],
        "finalizer_node_id": "done",
    })
    model = ModelRef("test", "test", "")
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner,
        archive_root=tmp_path).run("check", ExpertProfile(manager=model, executor=model, auditor=model),
        {"providers": []}, flow=flow, strategy="stable")
    assert result.status == "blocked"
    assert calls == ["start"]


@pytest.mark.parametrize("action", ["route", "fallback"])
@pytest.mark.parametrize("repair_status", ["blocked", "budget_exhausted", "failed"])
@pytest.mark.asyncio
async def test_failed_recovery_does_not_advance_or_replay_path(tmp_path, action, repair_status):
    from mewcode.expert_group import ExpertGroupOrchestrator, ExpertProfile, ExpertRoleResult
    from mewcode.experts.flow import ExpertFlowSpec
    from mewcode.model_registry import ModelRef

    calls = []

    async def runner(node, *_args, **_kwargs):
        calls.append(node.node_id)
        if node.node_id == "start":
            return ExpertRoleResult(text="initial failure", status="failed")
        assert node.node_id == "repair", "Stopped repair must not reach the next recovery stage"
        return ExpertRoleResult(text="repair service unavailable", status=repair_status)

    flow = ExpertFlowSpec.from_value({
        "flow_id": "stopped-repair", "version": 1, "label": "stopped repair",
        "nodes": [
            {"node_id": "repair", "role": "executor", "label": "repair"},
            {"node_id": "middle", "role": "executor", "label": "middle"},
            {"node_id": "start", "role": "executor", "label": "start",
             "failure_policy": {"action": action, "max_attempts": 3, "target_node_id": "repair"}},
            {"node_id": "done", "role": "manager", "label": "done"}],
        "edges": [{"from": "repair", "to": "middle"}, {"from": "middle", "to": "start"},
                  {"from": "start", "to": "done"}], "finalizer_node_id": "done"})
    model = ModelRef("test", "test", "")
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner,
        archive_root=tmp_path).run("check", ExpertProfile(manager=model, executor=model, auditor=model),
        {"providers": []}, flow=flow, strategy="stable", checkpoint={"node_states": {
            "repair": {"status": "completed", "text": "earlier work"},
            "middle": {"status": "completed", "text": "earlier work"}}})
    assert result.status == "blocked"
    assert calls == ["start", "repair"]
    if repair_status == "blocked" or action == "fallback":
        assert "repair service unavailable" in result.final_summary
