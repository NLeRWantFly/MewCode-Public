"""A resumed outage exposes its exact operation without granting or replaying it."""
import copy
import json
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import ErrorEvent
from mewcode.conversation import ConversationManager, Message
from mewcode.permissions.reviewer import SafetyReviewResult
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete, ToolResult
from tests.test_auto_effect_checkpoint import remember
from tests.test_effect_recovery_integration import _make_agent, _invoke, _allow
from tests.test_permissions import MockLLMClient


MARKER = "HOST_PENDING_AUTO_REVIEWS"


def pending_recovery(tmp_path, monkeypatch, compacted=False):
    agent, _, reviewer = _make_agent(tmp_path, monkeypatch)
    conversation = agent._current_conversation
    conversation.add_user_message("检查并恢复本机运行环境")
    call = ToolCallComplete("old-recover", "RuntimeDoctor", {"action": "recover"})
    result = agent._unavailable_review_result(call, SafetyReviewResult(
        decision="deny", risk="unknown", unavailable=True, reason="历史 TimeoutError",
        alternative="服务恢复后复审原操作", review_route={"provider_key": "codex:codex-custom",
            "configured_model": "gpt-5.6-luna", "status": "unavailable"}))
    reference = agent._review_recovery.pending_reviews()[0].reference
    remember(agent, call, result)
    if compacted:
        checkpoint = copy.deepcopy(conversation.history[-1].tool_results[0].host_observation["auto_review_checkpoint"])
        conversation.history = [Message("user", "继续核对运行环境", host_observation={"auto_review_checkpoint": checkpoint})]
    agent.registry.get("RuntimeDoctor").run = AsyncMock(return_value=ToolResult(
        '{"ready":true,"workspace_ready":false,"workspace_blocker":"Unsettled native transaction"}'))
    return agent, reviewer, conversation, reference


class InspectingClient(MockLLMClient):
    def __init__(self, responses, reference):
        super().__init__(responses)
        self.reference = reference
        self.contexts = []

    async def stream(self, conversation, system="", tools=None):
        contexts = [m.content for m in conversation.history if m.context_kind == MARKER]
        self.contexts.append(contexts)
        async for event in super().stream(conversation, system=system, tools=tools):
            yield event


@pytest.mark.parametrize("mode", ["stream", "completion"])
@pytest.mark.parametrize("compacted", [False, True])
@pytest.mark.asyncio
async def test_resume_exposes_recover_and_inspect_does_not_resolve_it(tmp_path, monkeypatch, mode, compacted):
    agent, reviewer, conversation, ref = pending_recovery(tmp_path, monkeypatch, compacted)
    client = InspectingClient([
        [ToolCallComplete("new-inspect", "RuntimeDoctor", {"action": "inspect"}), StreamEnd("tool_use")],
        [TextDelta("存在独立事务阻塞"), StreamEnd("end_turn")],
    ], ref)
    agent.client = client
    if mode == "stream":
        events = [event async for event in agent.run(conversation)]
        error = next(event for event in events if isinstance(event, ErrorEvent))
        text = error.message
    else:
        text = await agent.run_to_completion("继续", conversation)
    assert "RuntimeDoctor(action=recover)" in text and ref in text
    assert "保留的审查失败记录" in text and "不是本轮连接检测结果" in text
    for contexts in client.contexts:
        assert len(contexts) == 1
        assert ref in contexts[0] and '"action": "recover"' in contexts[0]
        assert '"configured_model": "gpt-5.6-luna"' in contexts[0]
        assert "当前审核配置" in contexts[0] and "ResolveAutoReview" in contexts[0]
    doctor = agent.registry.get("RuntimeDoctor")
    doctor.run.assert_awaited_once()
    assert doctor.run.call_args.args[0].action == "inspect"
    reviewer._ask_model.assert_not_awaited()
    assert len(agent._review_recovery.pending_reviews()) == 1
    assert not agent._review_recovery.pending_reviews()[0].abandoned
    assert not conversation.safety_review_state  # no new session-wide evidence gate


@pytest.mark.asyncio
async def test_explicit_abandonment_clears_context_but_keeps_failure_history(tmp_path, monkeypatch):
    agent, reviewer, conversation, ref = pending_recovery(tmp_path, monkeypatch)
    agent._refresh_pending_review_context(conversation)
    original = next(m for m in conversation.history if m.context_kind == MARKER)
    agent._refresh_pending_review_context(conversation)
    assert next(m for m in conversation.history if m.context_kind == MARKER) is original
    conversation.history = [m for m in conversation.history if not m.transient]
    agent._compact_restorer("test environment")(conversation)
    assert sum(m.context_kind == MARKER for m in conversation.history) == 1
    result = agent._resolve_auto_review(ToolCallComplete("abandon", "ResolveAutoReview", {
        "action": "abandon", "review_call_refs": [ref],
        "reason": "新检查表明运行环境已启用；未结事务仍须另行处理，旧恢复操作未执行。"}))
    assert not result.is_error
    agent._refresh_pending_review_context(conversation)
    assert not any(m.context_kind == MARKER for m in conversation.history)
    item = agent._review_recovery.requests[ref]
    assert item.abandoned and item.reason == "历史 TimeoutError"
    assert item.review_route["configured_model"] == "gpt-5.6-luna"
    reviewer._ask_model.assert_not_awaited()
    agent.registry.get("RuntimeDoctor").run.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_retry_still_requires_new_review_before_context_disappears(tmp_path, monkeypatch):
    agent, reviewer, conversation, ref = pending_recovery(tmp_path, monkeypatch)
    reviewer._ask_model = AsyncMock(return_value=_allow())
    call = ToolCallComplete("retry-original", "RuntimeDoctor", {"action": "recover"})
    results, _ = await _invoke(agent, call)
    assert not results[0].is_error
    reviewer._ask_model.assert_awaited_once()
    assert reviewer._ask_model.call_args.kwargs["arguments"] == {"action": "recover"}
    agent.registry.get("RuntimeDoctor").run.assert_awaited_once()
    agent._refresh_pending_review_context(conversation)
    assert not any(m.context_kind == MARKER for m in conversation.history)
    assert not agent._review_recovery.pending_reviews()
    checkpoint = agent._review_recovery.checkpoint(conversation)
    row = next(row for row in checkpoint["requests"] if row["reference"] == ref)
    assert row["reason"] == "历史 TimeoutError"
    assert json.loads(json.dumps(row))["pending_arguments"] is None
