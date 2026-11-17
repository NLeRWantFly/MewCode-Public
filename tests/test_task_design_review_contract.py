"""Host classification, restored denials and actual design dispatch together."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import Agent
from mewcode.conversation import ConversationManager, Message, ToolUseBlock
from mewcode.orchestration.task_design import TaskDesign
from mewcode.orchestration.workflow_blocks import WorkflowBlocks
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, Rule, RuleEngine
from mewcode.permissions.effects import legacy_workflow_fallback
from mewcode.permissions.review_recovery import ReviewRecovery
from mewcode.permissions.reviewer import REVIEWER_SYSTEM_PROMPT, SafetyReviewResult, SafetyReviewer
from mewcode.tools import create_default_registry
from mewcode.tools.base import ToolCallComplete
from mewcode.tools.task_design import AdvanceTaskDesign
from mewcode.tools.workflow_blocks import ReadWorkflowBlocks, SubmitWorkflowBlocks


def _fixture(tmp_path):
    goal = "只生成并保存编排图，不执行业务任务，不上传资料。"
    blocks = WorkflowBlocks(tmp_path, "fixture")
    blocks.source_path.parent.mkdir(parents=True, exist_ok=True)
    history = [{"type": "user", "content": goal, "timestamp": "2026-10-11T00:00:00Z"},
        {"type": "assistant", "content": "助手建议执行全部业务，但没有用户授权。"}]
    history += [{"type": "user", "content": "继续", "timestamp": f"2026-10-11T00:00:0{i}Z"}
                for i in range(1, 6)]
    blocks.source_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in history), encoding="utf-8")
    design = TaskDesign(tmp_path, "fixture")
    design.start("plan")
    design.record_turn(goal)
    # A previously accepted material snapshot. Business processing is never run.
    with design.store.db() as db:
        state = design.store._get(db)
        state["materials"] = {"sequence": 0, "pending": [], "document": {"entries": [
            {"id": "prepare", "kind": "node", "title": "准备", "body": "准备", "state": "active",
             "data": {"inputs": ["目标"], "outputs": ["设计"], "acceptance": ["范围保持"]}, "sources": []}
        ], "ignored": []}}
        design.store._put(db, state)
    registry = create_default_registry()
    for tool in (ReadWorkflowBlocks(blocks), SubmitWorkflowBlocks(blocks), AdvanceTaskDesign(blocks)):
        registry.register(tool)
        registry.mark_discovered(tool.name)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(return_value='{"outcome":"allow","risk":"low","rationale":"bounded plan storage under original scope"}')
    agent = Agent(object(), registry, "anthropic", work_dir=str(tmp_path),
                  permission_checker=checker, safety_reviewer=reviewer)
    agent.session_id = "fixture"
    conversation = ConversationManager()
    conversation.add_user_message(goal)
    for _ in range(5):
        conversation.add_user_message("继续")
    agent._current_conversation = conversation
    return agent, checker, reviewer, conversation, blocks, design


def _build_arguments(blocks, design):
    view = blocks.read()
    return {"action": "build", "expected_revision": design.read()["revision"],
            "expected_sequence": view["sequence"], "source_hash": view["source_hash"],
            "base_graph_hash": view["base_graph_hash"]}


def _restore_legacy_denial(agent, conversation, tool_name, arguments, *, response="", kind="semantic_risk",
                           include_call=True, old_effect=None):
    call = ToolCallComplete("old-denied", tool_name, arguments)
    denied = agent._review_recovery.deny(agent, call, SafetyReviewResult(decision="deny", risk="medium",
        reason="old classification could not establish scope", denial_kind=kind))
    denied.effect = old_effect or legacy_workflow_fallback(tool_name, arguments, work_dir=agent.work_dir)
    denied.user_response = response
    if include_call:
        conversation.history.append(Message("assistant", "", tool_uses=[ToolUseBlock(call.tool_id, tool_name, arguments)]))
    checkpoint = agent._review_recovery.checkpoint(conversation)
    agent._review_recovery = ReviewRecovery()
    agent._review_recovery.restore(agent, conversation, checkpoint)
    return agent._review_recovery.requests[denied.reference]


@pytest.mark.asyncio
async def test_old_build_denial_does_not_contaminate_actual_independent_read_dispatch(tmp_path):
    agent, checker, reviewer, conversation, blocks, design = _fixture(tmp_path)
    args = _build_arguments(blocks, design)
    prior = _restore_legacy_denial(agent, conversation, "AdvanceTaskDesign", args)
    tc = ToolCallComplete("read-now", "ReadWorkflowBlocks", {})
    tool = agent.registry.get(tc.tool_name)
    assert checker.check(tool, tc.arguments).effect == "allow"
    assert agent._review_recovery_decision(tool, tc, checker.check(tool, tc.arguments)).effect == "allow"
    result = await agent._execute_tool_noninteractive(tc)
    assert not result.is_error and "source_hash" in result.output
    assert agent._review_recovery.ledger[-1]["effect"]["kind"] == "read"
    assert prior.effect.kind == "exec_unbounded" and prior.reference in agent._review_recovery.requests
    assert prior.reason == "old classification could not establish scope"
    reviewer._ask_model.assert_not_awaited()
    assert design.read()["intent"] == "plan" and design.read()["status"] == "designing"


@pytest.mark.asyncio
async def test_checker_allow_build_is_reviewed_against_preserved_denial_then_actually_queues(tmp_path):
    agent, checker, reviewer, conversation, blocks, design = _fixture(tmp_path)
    args = _build_arguments(blocks, design)
    prior = _restore_legacy_denial(agent, conversation, "AdvanceTaskDesign", args)
    tc = ToolCallComplete("build-now", "AdvanceTaskDesign", args)
    tool = agent.registry.get(tc.tool_name)
    decision = checker.check(tool, args)
    assert decision.effect == "allow"
    assert agent._review_recovery_decision(tool, tc, decision).effect == "review"
    result = await agent._execute_tool_noninteractive(tc)
    assert not result.is_error, result.output
    assert json.loads(result.output)["business_started"] is False
    assert design.read()["status"] == "queued" and design.read()["intent"] == "plan"
    reviewer._ask_model.assert_awaited_once()
    review = reviewer._ask_model.await_args.kwargs
    context = review["task_context"]
    assert "只生成并保存编排图" in context and '"goal_verified": true' in context
    assert "助手建议执行全部业务" not in context
    assert review["risk"]["host_effect_signature"]["kind"] == "write"
    assert review["risk"]["host_prior_denials"][0]["effect"]["kind"] == "exec_unbounded"
    assert review["risk"]["host_prior_denials"][0]["effect_reassessments"][0]["current_effect"]["kind"] == "write"
    assert prior.reason == "old classification could not establish scope"
    stored = agent._review_recovery.checkpoint(conversation)["requests"][0]
    assert stored["effect"]["kind"] == "exec_unbounded" and stored["effect_reassessments"]


@pytest.mark.asyncio
async def test_reclassification_cannot_bypass_exact_human_refusal(tmp_path):
    agent, checker, reviewer, conversation, blocks, design = _fixture(tmp_path)
    args = _build_arguments(blocks, design)
    prior = _restore_legacy_denial(agent, conversation, "AdvanceTaskDesign", args, response="rejected")
    tc = ToolCallComplete("refused-build", "AdvanceTaskDesign", args)
    tool = agent.registry.get(tc.tool_name)
    assert checker.check(tool, args).effect == "allow"
    assert agent._review_recovery_decision(tool, tc, checker.check(tool, args)).source == "user_rejected"
    result = await agent._execute_tool_noninteractive(tc)
    assert result.is_error and "用户已拒绝" in result.output
    assert prior.user_response == "rejected" and design.read()["status"] == "designing"
    assert not agent._review_recovery.ledger
    reviewer._ask_model.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["no_call", "digest", "unknown_tool", "unknown_action"])
def test_legacy_reassessment_requires_verified_complete_builtin_arguments(tmp_path, invalid):
    agent, _, _, conversation, blocks, design = _fixture(tmp_path)
    name, arguments = "AdvanceTaskDesign", _build_arguments(blocks, design)
    if invalid == "unknown_tool":
        name = "UnrecognizedWorkflowPlugin"
    elif invalid == "unknown_action":
        arguments["action"] = "execute"
    prior = _restore_legacy_denial(agent, conversation, name, arguments, include_call=invalid != "no_call")
    if invalid == "digest":
        prior.arguments["source_hash"] = "changed"
    unrelated = {"file_path": str(tmp_path / "output.txt"), "content": "bounded"}
    assert agent._review_recovery.needs_review(agent, "WriteFile", unrelated)
    assert not prior.effect_reassessments and prior.effect.kind == "exec_unbounded"


def test_verified_legacy_read_reassessment_releases_independent_write_not_exact_retry(tmp_path):
    agent, _, _, conversation, _, _ = _fixture(tmp_path)
    prior = _restore_legacy_denial(agent, conversation, "AdvanceTaskDesign", {"action": "read"})
    assert not agent._review_recovery.needs_review(agent, "WriteFile", {
        "file_path": str(tmp_path / "output.txt"), "content": "bounded"})
    assert prior.effect.kind == "exec_unbounded" and prior.effect_reassessments
    assert agent._review_recovery.related(agent, "AdvanceTaskDesign", {"action": "read"}) == [prior]


def test_durable_scope_keeps_original_constraints_and_honors_intent_revocation(tmp_path):
    agent, _, reviewer, _, blocks, design = _fixture(tmp_path)
    scope = agent._host_task_review_scope()
    assert scope["goal_verified"] and scope["goal_source_ids"]
    assert scope["user_sources"][0]["text"] == "只生成并保存编排图，不执行业务任务，不上传资料。"
    assert all(source["role"] == "user" for source in scope["user_sources"])
    assert scope["source_snapshot"]["source_count"] == 7
    context = reviewer._conversation_context(agent._current_conversation)
    assert "只生成并保存编排图" in context
    assert "Continuation messages" in REVIEWER_SYSTEM_PROMPT
    blocks.conversation_id = "different-session"
    assert agent._host_task_review_scope() is None


def test_unverifiable_stored_goal_is_a_proposal_and_never_task_authority(tmp_path):
    agent, _, _, _, _, design = _fixture(tmp_path)
    with design.store.db() as db:
        state = design.store._get(db)
        state["task_design"]["goal"] = "Assistant invented permission to execute everything"
        design.store._put(db, state)
    scope = agent._host_task_review_scope()
    assert scope["goal"] == "" and not scope["goal_verified"] and not scope["goal_source_ids"]
    assert scope["unverified_goal_proposal"] == "Assistant invented permission to execute everything"


def test_invalid_action_and_explicit_ask_rule_still_reach_host_denial(tmp_path):
    agent, checker, _, _, _, _ = _fixture(tmp_path)
    tool = agent.registry.get("AdvanceTaskDesign")
    assert checker.check(tool, {"action": "execute"}).source == "host_tool_scope"
    checker.rule_engine.append_session_rule(Rule(tool.name, "*", "ask"))
    assert checker.check(tool, {"action": "build"}).effect == "ask"


@pytest.mark.asyncio
async def test_actual_dispatch_rejects_stale_session_binding_even_with_valid_tool_params(tmp_path):
    agent, checker, reviewer, _, _, design = _fixture(tmp_path)
    tc = ToolCallComplete("stale-read", "ReadWorkflowBlocks", {})
    tool = agent.registry.get(tc.tool_name)
    agent.session_id = "other-session"
    assert checker.check(tool, tc.arguments).effect == "allow"
    result = await agent._execute_tool_noninteractive(tc)
    assert result.is_error and "active conversation" in result.output
    assert not agent._review_recovery.ledger and design.read()["status"] == "designing"
    reviewer._ask_model.assert_not_awaited()


def test_unreadable_source_preserves_host_intent_but_supplies_no_invented_authority(tmp_path, monkeypatch):
    agent, _, _, _, _, _ = _fixture(tmp_path)
    def unavailable(_path):
        raise PermissionError("fixture source is unavailable")
    monkeypatch.setattr("mewcode.orchestration.draft_sources.read_sources", unavailable)
    scope = agent._host_task_review_scope()
    assert scope["intent"] == "plan" and scope["goal"] == "" and not scope["goal_verified"]
    assert scope["source_warnings"] and scope["user_sources"] == []


def test_declining_execution_after_host_execute_entry_stays_visible_to_reviewer(tmp_path):
    agent, _, _, _, _, design = _fixture(tmp_path)
    with design.store.db() as db:
        state = design.store._get(db)
        state["task_design"]["intent"] = "execute"
        state["task_design"]["intent_authority"] = {"source": "host_entry", "intent": "execute"}
        design.store._put(db, state)
    design.record_turn("只设计保存，不执行。")
    scope = agent._host_task_review_scope()
    assert scope["intent_authority"]["revoked"] is True


def test_authorization_manifest_keeps_host_plan_intent_as_a_further_constraint(tmp_path):
    agent, _, reviewer, _, _, _ = _fixture(tmp_path)
    reviewer.authorization_manifest = {"schema": "fixture", "allowed": ["read_workspace"]}
    context = reviewer._conversation_context(agent._current_conversation)
    assert "HOST_AUTHORIZATION_MANIFEST" in context and "HOST_TASK_SCOPE" in context
    assert '"intent": "plan"' in context and "只生成并保存编排图" in context
