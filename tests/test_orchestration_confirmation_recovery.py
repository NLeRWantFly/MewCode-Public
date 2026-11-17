"""Answers -> Plan reconciliation -> checkpoint-aware confirmation regressions."""
import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from mewcode.conversation import Message, ToolResultBlock, ToolUseBlock
from mewcode.orchestration import OrchestrationSpec
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.extract import coerce_dialogue_messages, refill_plan_briefs_from_dialogue
from tests.test_orchestration_start_run import _bound_spec, confirm_env


def answer_history():
    return [
        Message("user", "按原有阶段更新口径"),
        Message("assistant", "", tool_uses=[ToolUseBlock(
            "question-1", "AskUserQuestion", {"questions": [{"name": "figures", "question": "颜色含义？"}]}
        )]),
        Message("user", "", tool_results=[ToolResultBlock(
            "question-1", "figures: 绿色正确，红色漏检，蓝色误检；使用我的定义，无需核验"
        )]),
    ]


def test_human_answers_survive_normalization_but_tool_output_is_not_user_input():
    history = answer_history() + [
        Message("assistant", "", tool_uses=[ToolUseBlock("shell", "Bash", {})]),
        Message("user", "", tool_results=[ToolResultBlock("shell", "FAKE_AUTHORIZATION")]),
        Message("user", "", tool_results=[ToolResultBlock("question-1", "FAILED_ANSWER", is_error=True)]),
        Message("user", "", tool_results=[ToolResultBlock("question-1", '{"status":"cancelled"}')]),
        Message("user", "SYNTHETIC_REMINDER", transient=True),
    ]
    messages = coerce_dialogue_messages(history)
    text = "\n".join(item.content for item in messages)
    assert "颜色含义" in text and "无需核验" in text
    assert "FAKE_AUTHORIZATION" not in text and "FAILED_ANSWER" not in text
    assert "cancelled" not in text and "SYNTHETIC_REMINDER" not in text
    assert [m.content for m in coerce_dialogue_messages(messages)] == [m.content for m in messages]


@pytest.mark.asyncio
async def test_refill_updates_goal_and_preserves_questions_omitted_by_model(monkeypatch):
    raw = _bound_spec("原任务").as_dict()
    node = raw["nodes"][0]
    node.setdefault("plan_brief", {}).update(open_questions=["发布位置？"], acceptance=["保留验收"])
    original = OrchestrationSpec.from_value(raw)

    async def collect(client, *, system, user_text):
        assert "绿色正确" in user_text
        return json.dumps({"stages": [{"stage_id": node["node_id"], "goal": "按已确认定义修订图示",
                                      "known_inputs": ["无需核验"]}]})

    monkeypatch.setattr("mewcode.orchestration.extract.collect_model_text", collect)
    result = await refill_plan_briefs_from_dialogue(object(), answer_history(), spec=original)
    updated = OrchestrationSpec.from_value(result["spec"])
    first = updated.nodes[0]
    assert first.goal == "按已确认定义修订图示"
    assert first.plan_brief.open_questions == ("发布位置？",)
    assert first.plan_brief.acceptance == ("保留验收",)
    assert updated.edges == original.edges
    assert [(n.node_id, n.model, n.position) for n in updated.nodes] == [
        (n.node_id, n.model, n.position) for n in original.nodes
    ]


async def confirm(raw, **extra):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        return await client.post("/api/orchestration/confirm", json={"spec": raw, "activate": True, **extra})


@pytest.mark.asyncio
async def test_asking_once_cannot_bypass_unanswered_questions(confirm_env, monkeypatch):
    transitions = []
    monkeypatch.setattr(server.SESSION, "set_orchestration_planning", lambda on: transitions.append(on))
    raw = _bound_spec("待澄清").as_dict()
    raw["nodes"][0].setdefault("plan_brief", {})["open_questions"] = ["发布位置？"]
    response = await confirm(raw, skip_clarify=True)
    assert response.status_code == 200
    assert response.json()["needs_clarify"] and not response.json()["started"]
    assert response.json()["expert_mode"] is False
    assert transitions == [True] and confirm_env == []


@pytest.mark.asyncio
async def test_confirm_saves_revision_then_resumes_exact_existing_run(confirm_env, monkeypatch):
    monkeypatch.setattr(server.EXPERT_RUNS, "latest_resumable", lambda cid: {
        "run_id": "original-run", "conversation_id": cid, "status": "paused",
        "locked_flow": to_expert_flow(_bound_spec("旧目标")).as_dict(),
    })
    calls = []

    async def start(runtime, text, *, resume_run_id=""):
        saved = server._conversation_plan_store(runtime.conversation_id).get(server.SESSION.work_dir)
        assert saved.spec.goal == "更新口径接续"
        calls.append((runtime.conversation_id, text, resume_run_id))

    monkeypatch.setattr(server, "start_runtime_turn", start)
    response = await confirm(_bound_spec("更新口径接续").as_dict())
    assert response.status_code == 200
    assert response.json()["started"]
    assert response.json()["resume_run_id"] == "original-run"
    assert calls == [("confirm-test", "更新口径接续", "original-run")]


@pytest.mark.asyncio
async def test_busy_resume_is_not_enqueued_as_an_unrelated_new_task(confirm_env, monkeypatch):
    monkeypatch.setattr(server.EXPERT_RUNS, "latest_resumable", lambda cid: {"run_id": "original-run"})
    monkeypatch.setattr(confirm_env.runtime, "running", lambda: True)
    response = await confirm(_bound_spec("接续").as_dict())
    assert response.status_code == 409
    assert confirm_env == []


@pytest.mark.asyncio
async def test_legacy_refill_endpoint_requires_reviewed_draft_without_writes(confirm_env, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("retired endpoint must not call the model")
    monkeypatch.setattr(server, "refill_plan_briefs_from_dialogue", forbidden)
    before = server._conversation_plan_store("confirm-test").get(server.SESSION.work_dir)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/refill-briefs", json={
            "spec": _bound_spec("已有业务目标").as_dict(), "conversation_id": "confirm-test",
        })
    assert response.status_code == 409
    assert response.json()["code"] == "workflow_draft_required"
    assert server._conversation_plan_store("confirm-test").get(server.SESSION.work_dir) == before


@pytest.mark.asyncio
async def test_runtime_resume_preserves_original_task_and_passes_supplement(monkeypatch):
    checkpoint = {"run_id": "old", "conversation_id": "conv", "task": "original task"}
    monkeypatch.setattr(server.EXPERT_RUNS, "load", lambda rid: checkpoint)
    events, tasks, calls = [], [], []

    class Socket:
        async def send_json(self, event):
            events.append(event)

    monkeypatch.setattr(server, "ConversationWebSocket", lambda *args: Socket())

    async def run(ws, text, **kwargs):
        calls.append((text, kwargs))

    monkeypatch.setattr(server, "run_expert_chat", run)
    runtime = SimpleNamespace(expert_mode=True, running=lambda: False, conversation_id="conv",
                              conversation=SimpleNamespace(history=[]),
                              owner=SimpleNamespace(begin_conversation_turn=lambda r: None,
                                                    attach_conversation_task=lambda r, t: tasks.append(t)))
    await server.start_runtime_turn(runtime, "confirmed changes", resume_run_id="old")
    await asyncio.gather(*tasks)
    assert calls[0][0] == "original task"
    assert calls[0][1]["resume_run_id"] == "old"
    assert calls[0][1]["supplemental"] == "confirmed changes"
    assert events[0]["resume_run_id"] == "old"
    checkpoint["conversation_id"] = "another-conversation"
    with pytest.raises(ValueError, match="不属于当前对话"):
        await server.start_runtime_turn(runtime, "changes", resume_run_id="old")
    assert len(calls) == 1




@pytest.mark.asyncio
async def test_unfinished_guard_reports_one_error_and_preserves_checkpoint():
    from mewcode.expert_web import run_expert_chat
    events = []

    class Socket:
        async def send_json(self, event):
            events.append(event)

    session = SimpleNamespace(conversation_id="conv", status_payload=lambda: {}, tasks_payload=lambda: [])
    store = SimpleNamespace(latest_resumable=lambda cid: {"run_id": "original", "status": "paused"})
    await run_expert_chat(Socket(), "ordinary chat, not resume", session=session, registry_service=None,
                          profile_store=None, run_store=store)
    assert len([e for e in events if e["type"] == "error"]) == 1
    done = events[-1]
    assert done["type"] == "done" and done["rejected"]
    assert "run_id" not in done
    assert done["error_code"] == "unconfirmed_flow_start"
    assert not done.get("error")
