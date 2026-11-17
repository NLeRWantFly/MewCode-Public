from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from mewcode import continuation as cc
from mewcode.conversation import ConversationManager, ThinkingBlock
from mewcode.memory.session import SessionMeta
from mewcode.tools.base import StreamEnd, TextDelta


def runtime():
    session = SimpleNamespace(meta=SessionMeta(id="a"), save_meta=lambda: None)
    return SimpleNamespace(session=session, conversation=ConversationManager(), goal="", cancel_requested=False)


def decision(kind="continue", *, relation="none", ids=None):
    return {"decision": kind, "reason": "还有 D09 可执行" if kind == "continue" else "验收完成",
            "next_step": "继续 D09，保留 D35/D36" if kind == "continue" else "",
            "relation": relation, "evidence_ids": ids or ["reply"]}


async def ignore(_):
    pass


@pytest.mark.parametrize("text,expected", [("停止", "pause"), ("不要继续了", "pause"), ("暂停，解释一下", "pause"),
    ("終止", ""), ("终止任务，另做一件事", "terminate"), ("不要停止", ""), ("暂停功能怎么用？", "")])
def test_controls_are_direct_not_quoted_or_hypothetical(text, expected):
    assert cc.control_intent(text) == expected


@pytest.mark.parametrize("text", ["继续，别问我，后续一直执行", "修复全部测试，一直执行直到完成", "keep working until done", "不要停", "/goal 修复问题"])
def test_explicit_activation(text):
    assert cc.persistent_intent(text)


@pytest.mark.parametrize("text", ["解释持续执行功能", "一直执行会怎么样？", "不要一直执行", "普通问答", "文档写着“一直执行”", "暂停"])
def test_normal_text_not_activation(text):
    assert not cc.persistent_intent(text)


def test_original_goal_survives_continue_pause_restart_and_unrelated_chat(tmp_path):
    r = runtime()
    r.session.meta.dialogue_focus = {"kind": "chat", "task": "完成论文的 D09 D35 D36"}
    assert cc.prepare_turn(r, "继续，别问我，后续一直执行")
    assert cc.state_for(r)["objective"] == "完成论文的 D09 D35 D36"
    cc.hold(r, "暂停")
    path = tmp_path / "session.meta"
    r.session.meta.save(path)
    r.session.meta = SessionMeta.load(path)
    assert not cc.prepare_turn(r, "解释一下这个术语")
    assert cc.state_for(r)["status"] == "paused"
    assert cc.prepare_turn(r, "继续")
    assert cc.state_for(r)["objective"] == "完成论文的 D09 D35 D36"
    cc.hold(r, "终止", terminate=True)
    assert not cc.prepare_turn(r, "继续")


def test_old_goal_does_not_capture_new_task_and_goal_command_still_activates():
    r = runtime()
    r.goal = "旧目标：完成论文"
    assert not cc.prepare_turn(r, "安装另一个工具")
    assert not cc.prepare_turn(r, "继续")
    assert cc.prepare_turn(r, "旧目标：完成论文")
    assert cc.state_for(r)["objective"] == r.goal


def test_explicit_new_persistent_task_is_not_replaced_by_legacy_goal():
    r = runtime()
    r.goal = "旧论文目标"
    assert cc.prepare_turn(r, "修复代码，不要停")
    assert cc.state_for(r)["objective"] == "修复代码，不要停"


def test_legacy_continue_only_focus_recovers_actual_task():
    r = runtime()
    r.conversation.add_user_message("完成论文的 D09 D35 D36")
    r.conversation.add_user_message("继续，别问我，后续一直执行")
    r.session.meta.dialogue_focus = {"kind": "chat", "task": "继续，别问我，后续一直执行"}
    assert cc.prepare_turn(r, "继续，别问我，后续一直执行")
    assert cc.state_for(r)["objective"] == "完成论文的 D09 D35 D36"


def test_directive_with_no_task_does_not_create_empty_goal():
    r = runtime()
    assert not cc.prepare_turn(r, "不要停")


def test_new_goal_after_termination_can_start_but_bare_continue_cannot():
    r = runtime()
    r.goal = "修复代码"
    assert cc.prepare_turn(r, r.goal)
    cc.hold(r, "终止", terminate=True)
    assert not cc.prepare_turn(r, "继续")
    r.goal = "改进文档"
    assert cc.prepare_turn(r, r.goal)
    assert cc.state_for(r)["objective"] == "改进文档"


@pytest.mark.asyncio
async def test_stage_end_is_reviewed_and_checkpoint_has_real_evidence_not_thinking():
    r = runtime()
    cc.prepare_turn(r, "完成 D09 D35 D36，不要停")
    payloads = []
    async def reviewer(payload):
        payloads.append(payload)
        return decision()
    c = cc.ContinuousChat(r, reviewer, ignore)
    c.evidence("tool-1", "Bash", "D35 D36 passed", False)
    r.conversation.add_assistant_message("D35 D36 完成，下一步 D09", thinking_blocks=[ThinkingBlock(thinking="private chain", signature="")])
    assert await c.gate(r.conversation, SimpleNamespace(text="下一步 D09", stop_reason="end_turn")) == "continue"
    assert cc.state_for(r)["status"] == "running"
    assert "private chain" not in json.dumps(payloads)
    assert payloads[0]["evidence"][0]["id"] == "tool-1"
    assert "D09" in str(r.conversation.history)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, {}, decision("complete", ids=["invented"]), {**decision(), "next_step": ""}, decision("complete")])
async def test_invalid_or_unsupported_completion_is_held(result):
    r = runtime()
    cc.prepare_turn(r, "修复代码，不要停")
    async def reviewer(_):
        return result
    c = cc.ContinuousChat(r, reviewer, ignore)
    c.evidence("real", "Bash", "passed", False)
    assert await c.gate(r.conversation, SimpleNamespace(text="完成", stop_reason="end_turn")) == "paused"
    assert cc.state_for(r)["held"] is True
    assert cc.state_for(r)["status"] != "complete"


@pytest.mark.asyncio
async def test_late_audit_cannot_override_user_pause():
    r = runtime()
    cc.prepare_turn(r, "修复代码，不要停")
    started, release = asyncio.Event(), asyncio.Event()
    async def reviewer(_):
        started.set()
        await release.wait()
        return decision("complete")
    c = cc.ContinuousChat(r, reviewer, ignore)
    task = asyncio.create_task(c.gate(r.conversation, SimpleNamespace(text="完成", stop_reason="end_turn")))
    await started.wait()
    cc.hold(r, "用户暂停")
    release.set()
    assert await task == "paused"
    assert cc.state_for(r)["status"] == "paused"


@pytest.mark.asyncio
async def test_no_progress_limit_and_explicit_resume():
    r = runtime()
    cc.prepare_turn(r, "修复代码，不要停")
    async def reviewer(_):
        return decision()
    c = cc.ContinuousChat(r, reviewer, ignore)
    response = SimpleNamespace(text="下一步", stop_reason="end_turn")
    assert [await c.gate(r.conversation, response) for _ in range(4)] == ["continue"] * 3 + ["paused"]
    assert cc.prepare_turn(r, "继续")
    assert cc.state_for(r)["stagnant"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting_user", "blocked"])
async def test_missing_authority_or_external_block_is_not_complete(status):
    r = runtime()
    cc.prepare_turn(r, "修复代码，不要停")
    async def reviewer(_):
        return decision(status)
    c = cc.ContinuousChat(r, reviewer, ignore)
    assert await c.gate(r.conversation, SimpleNamespace(text="需要事实", stop_reason="end_turn")) == "paused"
    assert cc.state_for(r)["status"] == status
    assert cc.state_for(r)["held"]


def test_audit_model_is_fixed_isolated_and_never_route_label(monkeypatch):
    calls = []
    client = SimpleNamespace(set_effort=lambda effort: calls.append(effort))
    registry = SimpleNamespace(resolve=lambda ref, **kw: (calls.append(ref) or SimpleNamespace(client=client)))
    monkeypatch.setattr(cc, "load_review_model_settings", lambda: {"model_id": "codex-auto-review", "provider_key": "p"})
    assert cc.make_reviewer(registry).client is client
    assert calls[0].model_id == "gpt-6-astra"
    assert calls[0].effort == "high"
    assert calls[1] == "high"
    registry.resolve = lambda *a, **kw: (_ for _ in ()).throw(ValueError("unavailable"))
    assert cc.make_reviewer(registry).client is None


@pytest.mark.asyncio
async def test_reviewer_requires_stream_end_and_tools_are_disabled(monkeypatch):
    seen = []
    async def stream(client, conv, **kw):
        seen.append(kw)
        yield TextDelta(json.dumps(decision()))
    monkeypatch.setattr(cc, "scheduled_client_stream", stream)
    with pytest.raises(ValueError, match="不完整"):
        await cc.CompletionReviewer(object())({})
    assert seen[0]["tools"] == []


@pytest.mark.asyncio
async def test_review_timeout_is_held(monkeypatch):
    monkeypatch.setattr(cc, "AUDIT_TIMEOUT", .01)
    r = runtime()
    cc.prepare_turn(r, "修复代码，不要停")
    async def reviewer(_):
        await asyncio.Event().wait()
    c = cc.ContinuousChat(r, reviewer, ignore)
    assert await c.gate(r.conversation, SimpleNamespace(text="好了", stop_reason="end_turn")) == "paused"


@pytest.mark.asyncio
async def test_intervention_requires_explicit_safe_relation():
    r = runtime()
    cc.prepare_turn(r, "修复代码，不要停")
    r.continuation_intervention = True
    assert cc.prepare_turn(r, "解释这个问题")
    async def reviewer(_):
        return decision(relation="none")
    c = cc.ContinuousChat(r, reviewer, ignore)
    assert await c.gate(r.conversation, SimpleNamespace(text="说明", stop_reason="end_turn")) == "paused"


@pytest.mark.asyncio
async def test_output_budget_and_review_round_cap_do_not_trigger_more_calls():
    r = runtime()
    cc.prepare_turn(r, "修复代码，不要停")
    async def forbidden(_):
        raise AssertionError("must not review")
    c = cc.ContinuousChat(r, forbidden, ignore)
    assert await c.gate(r.conversation, SimpleNamespace(text="...", stop_reason="max_tokens")) == "paused"
    cc.prepare_turn(r, "继续")
    cc.state_for(r)["rounds"] = cc.MAX_REVIEWS
    assert await c.gate(r.conversation, SimpleNamespace(text="...", stop_reason="end_turn")) == "paused"
