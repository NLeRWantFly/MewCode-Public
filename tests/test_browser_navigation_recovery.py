"""Exact denied navigations wait for changed evidence, without bypassing review."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from mewcode.tools.agent_status import AgentStatus, StatusParams
from mewcode.tools.base import Tool, ToolCallComplete, ToolResult
from tests.test_auto_effect_checkpoint import remember
from tests.test_effect_recovery_integration import _make_agent, _invoke, _deny, _allow, _write


class NavigationParams(BaseModel):
    url: str


class BrowserNavigation(Tool):
    name = "browser_navigate"
    description = "Mock browser navigation"
    params_model = NavigationParams
    interaction_kind = "browser"

    async def execute(self, params):
        return ToolResult("mock navigation completed")


def browser_agent(tmp_path, monkeypatch, reviews, *, pending=True):
    agent, _, reviewer = _make_agent(tmp_path, monkeypatch, reviews)
    tool = BrowserNavigation()
    tool.execute = AsyncMock(return_value=ToolResult("mock navigation completed"))
    agent.registry.register(tool)
    agent._current_conversation.add_user_message("Open the current MewCode UI through normal review")
    if pending:
        # Match the production incident: this navigation already awaits review.
        agent._review_recovery.unavailable(agent, navigation(), SimpleNamespace(
            reason="prior review response was invalid", alternative="retry the original review", review_route={}))
    return agent, reviewer, tool


async def invoke(agent, call, mode="stream"):
    if mode == "completion":
        return await agent._execute_tool_noninteractive(call)
    results, _ = await _invoke(agent, call)
    assert len(results) == 1
    return results[0]


def navigation(identity="navigation", url="http://127.0.0.1:8765"):
    return ToolCallComplete(identity, "browser_navigate", {"url": url})


@pytest.mark.parametrize("mode", ["stream", "completion"])
@pytest.mark.parametrize("kind", ["semantic_risk", "policy_violation"])
@pytest.mark.asyncio
async def test_exact_denial_stops_next_review_and_survives_restore(tmp_path, monkeypatch, mode, kind):
    agent, reviewer, tool = browser_agent(tmp_path, monkeypatch, [_deny(kind), _allow()])
    call = navigation()
    first = await invoke(agent, call, mode)
    assert first.is_error and "AUTO_DENIED" in first.output
    remember(agent, call, first)
    original = next(item for item in agent._review_recovery.requests.values() if item.kind == kind)
    agent._current_conversation.add_assistant_message("Retrying with a different call ID")
    retry = await invoke(agent, navigation("retry"), mode)
    assert retry.is_error and "AUTO_REVIEW_DENIAL_LIMIT" in retry.output
    assert reviewer._ask_model.await_count == 1
    tool.execute.assert_not_awaited()
    remember(agent, navigation("retry"), retry)

    restored, recovered_reviewer, recovered_tool = browser_agent(tmp_path, monkeypatch, [_allow()], pending=False)
    restored._current_conversation = copy.deepcopy(agent._current_conversation)
    restored._restore_safety_review_state(restored._current_conversation)
    assert restored._review_recovery.abandon([original.reference], "skip this navigation")
    held = await invoke(restored, navigation("after-restart"), mode)
    assert held.is_error and "AUTO_REVIEW_DENIAL_LIMIT" in held.output
    recovered_reviewer._ask_model.assert_not_awaited()
    recovered_tool.execute.assert_not_awaited()

    independent = tmp_path / "independent.txt"
    result = await invoke(agent, _write(independent, "independent work"), mode)
    assert not result.is_error and independent.read_text(encoding="utf-8") == "independent work"


@pytest.mark.parametrize("change", ["user", "evidence", "target"])
@pytest.mark.asyncio
async def test_changed_context_or_target_requires_a_fresh_review(tmp_path, monkeypatch, change):
    agent, reviewer, tool = browser_agent(tmp_path, monkeypatch, [_deny(), _allow()])
    call = navigation()
    first = await invoke(agent, call)
    remember(agent, call, first)
    original = next(item for item in agent._review_recovery.requests.values() if item.kind == "semantic_risk")
    if change == "user":
        agent._current_conversation.add_user_message("The listener has been restarted; reassess this address")
    elif change == "evidence":
        monkeypatch.setenv("MEWCODE_BACKEND_HOST", "127.0.0.1")
        monkeypatch.setenv("MEWCODE_BACKEND_PORT", "64219")
        status = await AgentStatus(agent).execute(StatusParams())
        remember(agent, ToolCallComplete("status", "AgentStatus", {}), status)
    retry = navigation("fresh", "http://127.0.0.1:64219/" if change == "target" else call.arguments["url"])
    result = await invoke(agent, retry)
    assert not result.is_error
    assert reviewer._ask_model.await_count == 2
    assert reviewer._ask_model.call_args.kwargs["arguments"] == retry.arguments
    tool.execute.assert_awaited_once()
    assert tool.execute.call_args.args[0].url == retry.arguments["url"]
    assert original.kind == "semantic_risk" and original.reason == "the reviewed effect is unsafe"
    assert original.arguments == call.arguments
    assert original in agent._review_recovery.related(agent, retry.tool_name, retry.arguments)


@pytest.mark.asyncio
async def test_identical_status_with_new_call_id_is_not_new_evidence(tmp_path, monkeypatch):
    agent, reviewer, tool = browser_agent(tmp_path, monkeypatch, [_deny()])
    status = await AgentStatus(agent).execute(StatusParams())
    remember(agent, ToolCallComplete("status-1", "AgentStatus", {}), status)
    call = navigation()
    remember(agent, call, await invoke(agent, call))
    approval_evidence = agent._safety_evidence_digest()
    progress = agent._safety_progress_digest()
    same = await AgentStatus(agent).execute(StatusParams())
    assert same.host_observation == {key: value for key, value in status.host_observation.items()
                                     if key != "auto_review_checkpoint"}
    remember(agent, ToolCallComplete("status-2", "AgentStatus", {}), same)
    assert agent._safety_evidence_digest() != approval_evidence
    assert agent._safety_progress_digest() == progress
    retry = await invoke(agent, navigation("retry"))
    assert retry.is_error and "AUTO_REVIEW_DENIAL_LIMIT" in retry.output
    assert reviewer._ask_model.await_count == 1
    tool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_evidence_then_service_outage_can_still_retry_review(tmp_path, monkeypatch):
    agent, reviewer, tool = browser_agent(tmp_path, monkeypatch, [_deny()])
    call = navigation()
    remember(agent, call, await invoke(agent, call))
    agent._current_conversation.add_user_message("The listener was restarted; review the same address again")
    reviewer._ask_model = AsyncMock(side_effect=TimeoutError())
    failed = await invoke(agent, navigation("service-unavailable"))
    assert failed.is_error and "AUTO_REVIEW_UNAVAILABLE" in failed.output
    remember(agent, navigation("service-unavailable"), failed)
    reviewer._ask_model = AsyncMock(return_value=_allow())
    retried = await invoke(agent, navigation("service-recovered"))
    assert not retried.is_error
    reviewer._ask_model.assert_awaited_once()
    tool.execute.assert_awaited_once()
