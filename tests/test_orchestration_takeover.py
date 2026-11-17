"""Control-transfer tests: no concurrent writers, one main task, same run return."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.conversation import ConversationManager, Message, ToolResultBlock
from mewcode.memory.session import Session, SessionMeta
from mewcode.orchestration.interventions import deliver_interventions
from mewcode.orchestration.takeover import (
    TakeoverController,
    ReturnDecision,
    ReturnToOrchestration,
    pause_dialogue_anchor,
    paused_dialogue_handoff_prompt,
    read_paused_dialogue_window,
    recover_legacy_failed_handoff_anchor,
)


class Store:
    def __init__(self):
        self.value = {"run_id": "run-a", "conversation_id": "chat-a", "status": "running",
                      "task": "原任务", "node_states": {"done": {"status": "completed"}, "work": {"status": "running"}}}

    def load(self, identity):
        return copy.deepcopy(self.value) if identity == "run-a" else None

    def mutate(self, identity, fn, **kwargs):
        assert identity == "run-a"
        fn(self.value)
        return self.load(identity)


def setup():
    session = SimpleNamespace(conversation_id="chat-a", expert_run_id="run-a", expert_control_action="pause",
                              cancel_requested=False, status_payload=lambda: {}, tasks_payload=lambda: [])
    events = []
    ws = SimpleNamespace(send_json=AsyncMock(side_effect=events.append))
    store = Store()
    controller = TakeoverController(session, store, ws)
    session.expert_takeover_controller = controller
    return controller, session, store, events


@pytest.mark.asyncio
@pytest.mark.parametrize("relation,context", [("unrelated", ""), ("context_changed", "使用新的统计口径")])
async def test_interrupt_waits_for_checkpoint_then_main_then_same_run_resume(relation, context):
    controller, session, store, events = setup()
    started, cleanup_release = asyncio.Event(), asyncio.Event()
    sequence, calls = [], []

    async def flow(ws, text, **kwargs):
        calls.append(kwargs)
        if not kwargs.get("resume_run_id"):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sequence.append("cancelling")
                await cleanup_release.wait()
                store.value["status"] = "paused"
                store.value["node_states"]["work"].update(status="paused", text="保留的部分结果")
                sequence.append("checkpoint_saved")
        else:
            assert store.value["node_states"]["done"]["status"] == "completed"
            assert store.value["node_states"]["work"]["text"] == "保留的部分结果"
            assert kwargs["supplemental"] == context
            sequence.append("resumed")
            store.value["status"] = "completed"
        await ws.send_json({"type": "done", "run_id": "run-a"})

    async def main(ws, runtime, snapshot, item):
        assert sequence[-1] == "checkpoint_saved"
        assert snapshot["status"] == "paused"
        assert item["text"] == "临时分析"
        assert not deliver_interventions(snapshot, ConversationManager(), node_id="other", node_attempt=1)
        sequence.append("main_completed")
        return {"action": "resume", "relation": relation, "summary": "临时任务已完成", "resume_context": context}

    task = asyncio.create_task(controller.run("原任务", flow_runner=flow, dialogue_runner=main, kwargs={"session": session}))
    await started.wait()
    result = controller.submit("input-a", "临时分析")
    assert result["new"]
    assert not controller.submit("input-a", "临时分析")["new"]
    await asyncio.sleep(0)
    assert sequence == ["cancelling"]  # main must not race outstanding cleanup
    cleanup_release.set()
    await asyncio.wait_for(task, 2)
    assert sequence == ["cancelling", "checkpoint_saved", "main_completed", "resumed"]
    assert calls[1]["resume_run_id"] == "run-a"
    assert len([e for e in events if e["type"] == "done"]) == 1
    assert store.value["interventions"][0]["status"] == "completed"
    assert session.expert_takeover_controller is None


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [None, {"action": "hold", "relation": "needs_user", "summary": "需要选择口径"}])
async def test_missing_or_hold_decision_does_not_resume(decision):
    controller, session, store, events = setup()
    controller.submit("input-a", "临时分析")
    flow_calls = []

    async def flow(ws, text, **kwargs):
        flow_calls.append(text)
        store.value["status"] = "paused"
        await ws.send_json({"type": "done"})

    await controller.run("原任务", flow_runner=flow, dialogue_runner=AsyncMock(return_value=decision), kwargs={})
    assert len(flow_calls) == 1
    assert store.value["status"] == "paused"
    assert store.value["interventions"][0]["status"] == "held"
    assert store.value["pause_dialogue_anchor"]["attempt"] == 0


@pytest.mark.asyncio
async def test_storage_failure_still_reports_original_error_and_releases_controller(monkeypatch):
    controller, session, store, events = setup()
    controller.submit('input-a', '临时任务')
    before = copy.deepcopy(store.value)
    def full(*_args, **_kwargs):
        raise OSError('运行数据盘空间不足')
    monkeypatch.setattr(store, 'mutate', full)
    async def flow(ws, text, **kwargs):
        await ws.send_json(dict(type='done',run_id='run-a'))
    main = AsyncMock()
    await controller.run('原任务',flow_runner=flow,dialogue_runner=main,kwargs={})
    assert store.value == before
    assert not main.called
    errors = [e for e in events if e['type']=='error']
    assert len(errors)==1 and '空间不足' in errors[0]['text']
    held = [e for e in events if e['type']=='expert_takeover_state']
    assert held[-1]['persisted'] is False and held[-1]['phase']=='held'
    assert len([e for e in events if e['type']=='done'])==1
    assert session.expert_takeover_controller is None
    assert controller.accepting is False


@pytest.mark.asyncio
async def test_flow_storage_failure_is_not_mislabeled_as_a_takeover_failure(monkeypatch):
    controller, session, store, events = setup()
    def full(*_args, **_kwargs):
        raise OSError('运行数据盘空间不足')
    monkeypatch.setattr(store, 'mutate', full)
    async def flow(ws, text, **kwargs):
        raise OSError('运行数据盘空间不足')
    await controller.run('原任务',flow_runner=flow,dialogue_runner=AsyncMock(),kwargs={})
    message = next(e['text'] for e in events if e['type']=='error')
    assert '编排执行失败' in message and '临时接管' not in message
    assert session.expert_takeover_controller is None


@pytest.mark.asyncio
async def test_stop_during_main_never_resumes_even_if_main_swallows_cancellation():
    controller, session, store, events = setup()
    controller.submit("input-a", "临时分析")
    main_started = asyncio.Event()
    flow_calls = []

    async def flow(ws, text, **kwargs):
        flow_calls.append(text)
        store.value["status"] = "paused"

    async def main(*args):
        main_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"action": "resume", "relation": "unrelated", "summary": "不应恢复"}

    task = asyncio.create_task(controller.run("原任务", flow_runner=flow, dialogue_runner=main, kwargs={}))
    await main_started.wait()
    task.cancel()
    await asyncio.wait_for(task, 2)
    assert len(flow_calls) == 1
    assert store.value["interventions"][0]["status"] == "interrupted"
    assert store.value["takeover_state"]["phase"] == "held"


@pytest.mark.asyncio
async def test_manual_pause_captures_live_run_id_and_writes_dialogue_anchor():
    controller, session, store, events = setup()
    controller.run_id = ""
    session.expert_run_id = ""
    started = asyncio.Event()

    async def flow(ws, text, **kwargs):
        session.expert_run_id = "run-a"
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            store.value["status"] = "paused"
            await ws.send_json({"type": "done", "run_id": "run-a"})

    task = asyncio.create_task(controller.run(
        "原任务", flow_runner=flow, dialogue_runner=AsyncMock(), kwargs={},
    ))
    await started.wait()
    task.cancel()
    await asyncio.wait_for(task, 2)

    assert controller.run_id == "run-a"
    assert store.value["pause_dialogue_anchor"]["attempt"] == 0


@pytest.mark.asyncio
async def test_more_input_during_main_is_serial_and_precedes_flow_return():
    controller, session, store, events = setup()
    controller.submit("input-a", "第一条")
    seen = []

    async def flow(ws, text, **kwargs):
        store.value["status"] = "paused" if not kwargs.get("resume_run_id") else "completed"
        seen.append("flow")

    async def main(ws, runtime, snapshot, item):
        seen.append(item["text"])
        if item["id"] == "input-a":
            controller.submit("input-b", "第二条")
        return {"action": "resume", "relation": "unrelated", "summary": "已回答"}

    await controller.run("原任务", flow_runner=flow, dialogue_runner=main, kwargs={})
    assert seen == ["flow", "第一条", "第二条", "flow"]


@pytest.mark.asyncio
async def test_return_tool_requires_relevant_handoff_and_drops_unrelated_context():
    tool = ReturnToOrchestration()
    result = await tool.execute(ReturnDecision(action="resume", relation="needs_user", summary="待决定"))
    assert result.is_error and tool.decision is None
    result = await tool.execute(ReturnDecision(action="resume", relation="context_changed", summary="变化"))
    assert result.is_error
    result = await tool.execute(ReturnDecision(action="resume", relation="unrelated", summary="已回答", resume_context="无关聊天"))
    assert not result.is_error
    assert tool.decision["resume_context"] == ""

    result = await tool.execute(ReturnDecision(
        action="resume", relation="plan_changed", summary="流程变了",
        resume_context="修改后续阶段",
    ))
    assert result.is_error
    result = await tool.execute(ReturnDecision(
        action="resume", relation="context_changed", summary="完成节点失效",
        resume_context="调试改动影响旧产物",
        invalidated_completed_node_ids=["done"],
    ))
    assert result.is_error


def test_pause_dialogue_window_reads_only_post_anchor_records(tmp_path):
    sessions = tmp_path / ".mewcode" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / "chat-a.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        session = Session("chat-a", handle, SessionMeta(id="chat-a"), sessions)
        session.append(Message(role="user", content="暂停前的问题"))
        snapshot = {"attempt": 2, "last_checkpoint": {"checkpoint_id": "cp-2"}}
        snapshot["pause_dialogue_anchor"] = pause_dialogue_anchor(session, snapshot)
        session.append(Message(role="user", content="<system-reminder>内部提醒</system-reminder>"))
        session.append(Message(role="user", content="暂停后我修复了读取错误"))
        session.append(Message(role="assistant", content="已运行测试，三项通过"))

        window = read_paused_dialogue_window(session, snapshot)

    assert window["count"] == 2
    assert "暂停前的问题" not in window["text"]
    assert "内部提醒" not in window["text"]
    assert "修复了读取错误" in window["text"]
    assert "三项通过" in window["text"]
    assert window["start_offset"] < window["end_offset"]
    assert window["digest"]


def test_pause_dialogue_window_flags_individual_evidence_truncation(tmp_path):
    sessions = tmp_path / ".mewcode" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / "chat-a.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        session = Session("chat-a", handle, SessionMeta(id="chat-a"), sessions)
        snapshot = {"attempt": 1}
        snapshot["pause_dialogue_anchor"] = pause_dialogue_anchor(session, snapshot)
        session.append(Message(role="user", content="请核对下面的结果"))
        session.append(Message(role="user", content="", tool_results=[
            ToolResultBlock(tool_use_id="t1", content="a" * 1700),
        ]))
        window = read_paused_dialogue_window(session, snapshot)

    assert window["truncated"] is True
    assert "entry_limit" in window["truncation_reasons"]
    assert "工具结果已截断" in window["text"]


def test_paused_dialogue_prompt_requires_hold_for_changed_plan_and_permissions():
    prompt = paused_dialogue_handoff_prompt(
        {"run_id": "run-a", "task": "原任务", "node_states": {"done": {"status": "completed"}}},
        {"text": "[用户]\n换一个验收标准"},
    )
    assert "hold/plan_changed" in prompt
    assert "不能自动扩大工具权限" in prompt
    assert "invalidated_completed_node_ids" in prompt
    assert "PAUSED_DIALOGUE_BEGIN" in prompt


@pytest.mark.asyncio
async def test_continue_uses_host_checkpoint_without_reviewing_paused_dialogue(tmp_path, monkeypatch):
    from frontend import server

    sessions = tmp_path / ".mewcode" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / "chat-a.jsonl"
    handle = log.open("a", encoding="utf-8")
    durable = Session("chat-a", handle, SessionMeta(id="chat-a"), sessions)
    durable.append(Message(role="user", content="原编排任务"))
    snapshot = {
        "run_id": "run-a", "conversation_id": "chat-a", "status": "paused",
        "updated_at": "v1", "attempt": 1, "task": "原任务", "attachments": [],
        "node_states": {"done": {"status": "completed"}, "work": {"status": "paused"}},
    }
    snapshot["pause_dialogue_anchor"] = pause_dialogue_anchor(durable, snapshot)
    durable.append(Message(role="user", content="我在普通对话里修复了读取错误"))
    durable.append(Message(role="assistant", content="验证测试已通过"))
    store = Store()
    store.value = copy.deepcopy(snapshot)
    runtime = SimpleNamespace(
        session=durable, conversation_id="chat-a", conversation=ConversationManager(),
        status_payload=lambda: {}, tasks_payload=lambda: [],
    )
    events = []
    ws = SimpleNamespace(send_json=AsyncMock(side_effect=events.append))
    reviews, resumes = [], []

    async def review(*_args):
        reviews.append(1)
        pytest.fail("retired dialogue review must not run")

    async def resume(*args, **kwargs):
        resumes.append(kwargs)

    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    monkeypatch.setattr(server, "_run_takeover_dialogue", review)
    monkeypatch.setattr(server, "run_expert_chat", resume)
    try:
        await server._resume_expert_after_paused_dialogue(
            ws, runtime, snapshot, supplemental="用户显式补充",
        )
    finally:
        handle.close()

    assert not reviews
    assert "dialogue_handoff_reviews" not in store.value
    assert len(resumes) == 1
    assert resumes[0]["supplemental"] == "用户显式补充"
    assert "pending_dialogue_handoff" not in store.value
    assert store.value["node_states"] == snapshot["node_states"]
    assert store.value["pause_dialogue_anchor"] == snapshot["pause_dialogue_anchor"]
    assert any(event.get("phase") == "returning" for event in events)


@pytest.mark.asyncio
async def test_continue_holds_when_run_changes_at_return_boundary(tmp_path, monkeypatch):
    from frontend import server

    sessions = tmp_path / ".mewcode" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / "chat-a.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        durable = Session("chat-a", handle, SessionMeta(id="chat-a"), sessions)
        snapshot = {
            "run_id": "run-a", "conversation_id": "chat-a", "status": "paused",
            "updated_at": "v1", "attempt": 1, "task": "原任务", "attachments": [],
            "node_states": {"work": {"status": "paused"}},
        }
        snapshot["pause_dialogue_anchor"] = pause_dialogue_anchor(durable, snapshot)
        durable.append(Message(role="user", content="补充调试结果"))
        store = Store()
        store.value = copy.deepcopy(snapshot)
        runtime = SimpleNamespace(
            session=durable, conversation_id="chat-a", conversation=ConversationManager(),
            status_payload=lambda: {}, tasks_payload=lambda: [],
        )
        events = []
        def on_event(event):
            events.append(event)
            if event.get("phase") == "returning":
                store.value["updated_at"] = "v2"
        ws = SimpleNamespace(send_json=AsyncMock(side_effect=on_event))
        review = AsyncMock()

        resume = AsyncMock()
        monkeypatch.setattr(server, "EXPERT_RUNS", store)
        monkeypatch.setattr(server, "_run_takeover_dialogue", review)
        monkeypatch.setattr(server, "run_expert_chat", resume)
        await server._resume_expert_after_paused_dialogue(ws, runtime, snapshot)

    resume.assert_not_awaited()
    review.assert_not_awaited()
    assert events[-2]["phase"] == "held"
    assert "状态已变化" in events[-2]["text"]
    assert "pending_dialogue_handoff" not in store.value


@pytest.mark.asyncio
async def test_pending_handoff_cannot_be_bypassed_without_new_dialogue(tmp_path, monkeypatch):
    from frontend import server

    sessions = tmp_path / ".mewcode" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / "chat-a.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        durable = Session("chat-a", handle, SessionMeta(id="chat-a"), sessions)
        snapshot = {
            "run_id": "run-a", "conversation_id": "chat-a", "status": "paused",
            "updated_at": "v1", "attempt": 1, "task": "原任务", "attachments": [],
            "node_states": {"work": {"status": "paused"}},
            "pending_dialogue_handoff": {
                "action": "hold", "relation": "needs_user", "summary": "请先确认统计口径",
            },
        }
        snapshot["pause_dialogue_anchor"] = pause_dialogue_anchor(durable, snapshot)
        store = Store()
        store.value = copy.deepcopy(snapshot)
        runtime = SimpleNamespace(
            session=durable, conversation_id="chat-a", conversation=ConversationManager(),
            status_payload=lambda: {}, tasks_payload=lambda: [],
        )
        events = []
        ws = SimpleNamespace(send_json=AsyncMock(side_effect=events.append))
        review, resume = AsyncMock(), AsyncMock()
        monkeypatch.setattr(server, "EXPERT_RUNS", store)
        monkeypatch.setattr(server, "_run_takeover_dialogue", review)
        monkeypatch.setattr(server, "run_expert_chat", resume)

        await server._resume_expert_after_paused_dialogue(ws, runtime, snapshot)

    review.assert_not_awaited()
    resume.assert_not_awaited()
    assert events[-2]["phase"] == "held"
    assert "确认统计口径" in events[-2]["text"]
    assert events[-1]["type"] == "done"


def test_rejects_cross_conversation_and_conflicting_duplicates():
    controller, session, store, events = setup()
    controller.submit("input-a", "one")
    with pytest.raises(ValueError):
        controller.submit("input-a", "two")
    session.conversation_id = "chat-b"
    with pytest.raises(RuntimeError):
        controller.submit("input-b", "cross")


@pytest.mark.asyncio
async def test_server_main_dialogue_uses_current_agent_restores_tool_and_goal(monkeypatch):
    from frontend import server
    from mewcode.tools import ToolRegistry
    controller, runtime, store, events = setup()
    runtime.agent = SimpleNamespace(registry=ToolRegistry())
    runtime.goal = "原持续目标"
    runtime.conversation = ConversationManager()
    original = ReturnToOrchestration()
    runtime.agent.registry.register(original)
    runtime.agent.registry.disable(original.name)

    async def fake_chat(ws, text, **kwargs):
        assert kwargs["runtime"] is runtime and not kwargs["record_user"]
        assert runtime.goal == ""
        assert runtime.conversation.history[-1].content == "临时分析"
        assert "原编排已暂停" in runtime.conversation.history[-2].content
        assert runtime.conversation.history[-2].transient
        tool = runtime.agent.registry.get("ReturnToOrchestration")
        await tool.execute(ReturnDecision(action="resume", relation="unrelated", summary="答案"))
        runtime.conversation.add_assistant_message("答案")
        return {"interrupted": False, "error": None}

    monkeypatch.setattr(server, "run_chat", fake_chat)
    decision = await server._run_takeover_dialogue(controller.ws, runtime, store.value, {"text": "临时分析"})
    assert decision["action"] == "resume"
    assert runtime.goal == "原持续目标"
    assert runtime.agent.registry.get(original.name) is original
    assert not runtime.agent.registry.is_enabled(original.name)
    assert [message.content for message in runtime.conversation.history] == ["临时分析", "答案"]


@pytest.mark.asyncio
async def test_delayed_handoff_uses_short_context_and_one_missing_decision_retry(monkeypatch):
    from frontend import server
    from mewcode.tools import ToolRegistry

    controller, runtime, store, events = setup()
    runtime.agent = SimpleNamespace(registry=ToolRegistry())
    runtime.goal = "原持续目标"
    original = ConversationManager()
    original.add_user_message("旧对话不应进入本次接力模型上下文")
    runtime.conversation = original
    calls = []

    async def fake_chat(_ws, _text, **kwargs):
        assert kwargs["runtime"] is runtime and not kwargs["record_user"]
        assert runtime.conversation is not original
        assert "旧对话不应进入本次接力模型上下文" not in str(runtime.conversation.history)
        assert runtime.agent.registry.is_enabled("ReturnToOrchestration")
        calls.append(len(runtime.conversation.history))
        if len(calls) == 2:
            tool = runtime.agent.registry.get("ReturnToOrchestration")
            await tool.execute(ReturnDecision(
                action="resume", relation="context_changed", summary="已核实新上下文",
                resume_context="仅更新执行路径", affected_node_ids=["work"],
            ))
        runtime.conversation.add_assistant_message(f"接力回复 {len(calls)}")
        return {"interrupted": False, "error": None}

    monkeypatch.setattr(server, "run_chat", fake_chat)
    decision = await server._run_takeover_dialogue(
        controller.ws, runtime, store.value,
        {"text": "审阅暂停区间", "pause_dialogue_window": {"text": "新的执行路径"}},
    )

    assert decision["action"] == "resume"
    assert len(calls) == 2
    assert runtime.conversation is original
    assert [message.content for message in original.history] == [
        "旧对话不应进入本次接力模型上下文", "接力回复 1", "接力回复 2",
    ]
    assert runtime.goal == "原持续目标"
    assert runtime.handoff_failure_kind == ""
    assert runtime.agent.registry.get("ReturnToOrchestration") is None


@pytest.mark.asyncio
async def test_interrupted_legacy_review_is_archived_without_repeating_review(tmp_path, monkeypatch):
    from frontend import server

    sessions = tmp_path / ".mewcode" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / "chat-a.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        durable = Session("chat-a", handle, SessionMeta(id="chat-a"), sessions)
        snapshot = {
            "run_id": "run-a", "conversation_id": "chat-a", "status": "paused",
            "updated_at": "v1", "attempt": 1, "task": "原任务", "attachments": [],
            "node_states": {"work": {"status": "paused"}},
        }
        snapshot["pause_dialogue_anchor"] = pause_dialogue_anchor(durable, snapshot)
        original_offset = snapshot["pause_dialogue_anchor"]["record_offset"]
        snapshot["pending_dialogue_handoff"] = {
            "action": "hold", "relation": "needs_user", "retryable": True,
            "failure_kind": "interrupted", "summary": "旧接力分析已打断",
        }
        durable.append(Message(role="user", content="补充了执行环境信息"))
        store = Store()
        store.value = copy.deepcopy(snapshot)
        runtime = SimpleNamespace(
            session=durable, conversation_id="chat-a", conversation=ConversationManager(),
            status_payload=lambda: {}, tasks_payload=lambda: [],
        )
        events = []
        ws = SimpleNamespace(send_json=AsyncMock(side_effect=events.append))
        review = AsyncMock()
        resume = AsyncMock()
        monkeypatch.setattr(server, "EXPERT_RUNS", store)
        monkeypatch.setattr(server, "_run_takeover_dialogue", review)
        monkeypatch.setattr(server, "run_expert_chat", resume)
        await server._resume_expert_after_paused_dialogue(ws, runtime, snapshot)
        assert store.value["pause_dialogue_anchor"]["record_offset"] == original_offset
        archive = store.value["retired_paused_handoff_reviews"][0]
        assert archive["decision"] == snapshot["pending_dialogue_handoff"]
        assert archive["status"] == "retired_not_approved"

    review.assert_not_awaited()
    resume.assert_awaited_once()
    assert "pending_dialogue_handoff" not in store.value


@pytest.mark.asyncio
async def test_truncated_dialogue_preview_is_not_a_resume_gate(tmp_path, monkeypatch):
    from frontend import server

    sessions = tmp_path / ".mewcode" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / "chat-a.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        durable = Session("chat-a", handle, SessionMeta(id="chat-a"), sessions)
        snapshot = {
            "run_id": "run-a", "conversation_id": "chat-a", "status": "paused",
            "updated_at": "v1", "attempt": 1, "task": "原任务", "attachments": [],
            "node_states": {"work": {"status": "paused"}},
        }
        snapshot["pause_dialogue_anchor"] = pause_dialogue_anchor(durable, snapshot)
        durable.append(Message(role="user", content="大量新记录"))
        store = Store()
        store.value = copy.deepcopy(snapshot)
        runtime = SimpleNamespace(
            session=durable, conversation_id="chat-a", conversation=ConversationManager(),
            status_payload=lambda: {}, tasks_payload=lambda: [],
        )
        ws = SimpleNamespace(send_json=AsyncMock())
        monkeypatch.setattr(server, "EXPERT_RUNS", store)
        def forbidden_read(*_args):
            pytest.fail("resume must not classify or read a dialogue preview")
        monkeypatch.setattr("mewcode.orchestration.takeover.read_paused_dialogue_window", forbidden_read)
        review = AsyncMock(return_value={
            "action": "resume", "relation": "unrelated", "summary": "无关",
            "resume_context": "", "affected_node_ids": [],
            "invalidated_completed_node_ids": [],
        })
        monkeypatch.setattr(server, "_run_takeover_dialogue", review)
        resume = AsyncMock()
        monkeypatch.setattr(server, "run_expert_chat", resume)
        await server._resume_expert_after_paused_dialogue(ws, runtime, snapshot)

    resume.assert_awaited_once()
    review.assert_not_awaited()
    assert "pending_dialogue_handoff" not in store.value
    assert store.value["pause_dialogue_anchor"]["record_offset"] == snapshot["pause_dialogue_anchor"]["record_offset"]


def test_legacy_no_decision_anchor_can_be_recovered_only_from_matching_bytes(tmp_path):
    sessions = tmp_path / ".mewcode" / "sessions"
    sessions.mkdir(parents=True)
    log = sessions / "chat-a.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        durable = Session("chat-a", handle, SessionMeta(id="chat-a"), sessions)
        original = pause_dialogue_anchor(durable, {"attempt": 1})["record_offset"]
        durable.append(Message(role="user", content="接力前的原始补充"))
        end = log.stat().st_size
        import hashlib
        digest = hashlib.sha256(log.read_bytes()[original:end]).hexdigest()
        durable.append(Message(role="assistant", content="接力分析被中断"))
        consumed = pause_dialogue_anchor(durable, {"attempt": 1})
        summary = "接力分析没有形成有效决定，原编排保持暂停。"
        snapshot = {
            "pause_dialogue_anchor": consumed,
            "pending_dialogue_handoff": {"action": "hold", "summary": summary},
            "dialogue_handoff_reviews": [{
                "window": {"start_offset": original, "end_offset": end, "digest": digest},
                "decision": {"action": "hold", "summary": summary},
            }],
        }
        recovered = recover_legacy_failed_handoff_anchor(durable, snapshot)
        assert recovered is not snapshot
        assert recovered["pause_dialogue_anchor"]["record_offset"] == original
        assert snapshot["pause_dialogue_anchor"]["record_offset"] == consumed["record_offset"]

        bad = copy.deepcopy(snapshot)
        bad["dialogue_handoff_reviews"][-1]["window"]["digest"] = "0" * 64
        assert recover_legacy_failed_handoff_anchor(durable, bad) is bad


@pytest.mark.asyncio
async def test_acceptance_does_not_mutate_live_main_context(monkeypatch):
    from frontend import server
    controller, runtime, store, events = setup()
    messages = []
    runtime.session = SimpleNamespace(append=messages.append)
    runtime.conversation = ConversationManager()
    result = await server._accept_expert_intervention(runtime, "input-a", "先回答我")
    assert result["new"] and len(messages) == 1
    assert not runtime.conversation.history
    assert not (await server._accept_expert_intervention(runtime, "input-a", "先回答我"))["new"]
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_settle_tasks_waits_for_cancel_cleanup_without_touching_other_tasks():
    from mewcode.agents.task_manager import TaskManager
    manager = TaskManager()
    started, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            manager._async_tasks.pop("owned", None)
            cleanup.set()
            await release.wait()

    owned = asyncio.create_task(worker())
    unrelated = asyncio.create_task(asyncio.Event().wait())
    manager._async_tasks.update(owned=owned, unrelated=unrelated)
    await started.wait()
    settlement = asyncio.create_task(manager.settle_tasks(["owned"], cancel=True))
    await cleanup.wait()
    assert not settlement.done() and not unrelated.done()
    release.set()
    await settlement
    unrelated.cancel()
    await asyncio.gather(unrelated, return_exceptions=True)


@pytest.mark.asyncio
async def test_input_at_return_boundary_is_handled_before_any_node_restarts():
    controller, session, store, events = setup()
    controller.submit("input-a", "第一条")
    sequence = []
    submitted = False

    async def publish(event):
        nonlocal submitted
        events.append(event)
        if event.get("phase") == "returning" and not submitted:
            submitted = True
            controller.submit("input-b", "返回瞬间的新消息")

    controller.ws.send_json = publish

    async def flow(ws, text, **kwargs):
        sequence.append("flow")
        store.value["status"] = "paused" if not kwargs.get("resume_run_id") else "completed"
        if kwargs.get("resume_run_id"):
            assert kwargs["supplemental"] == "第一条\n\n返回瞬间的新消息"

    async def main(ws, runtime, snapshot, item):
        sequence.append(item["text"])
        return {"action": "resume", "relation": "context_changed", "summary": "已核对", "resume_context": item["text"]}

    await controller.run("原任务", flow_runner=flow, dialogue_runner=main, kwargs={})
    assert sequence == ["flow", "第一条", "返回瞬间的新消息", "flow"]


@pytest.mark.asyncio
async def test_second_input_during_cancellation_does_not_cancel_cleanup_again():
    controller, session, store, events = setup()
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def flow(ws, text, **kwargs):
        if kwargs.get("resume_run_id"):
            return
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()  # A second cancel here would destroy the checkpoint.
            store.value["status"] = "paused"

    main = AsyncMock(return_value={"action": "resume", "relation": "unrelated", "summary": "已回答"})
    task = asyncio.create_task(controller.run("原任务", flow_runner=flow, dialogue_runner=main, kwargs={}))
    await started.wait()
    controller.submit("input-a", "第一条")
    await cleaning.wait()
    controller.submit("input-b", "第二条")
    await asyncio.sleep(0)
    assert not controller.active.done()
    release.set()
    await asyncio.wait_for(task, 2)
    assert main.await_count == 2


def test_restart_marks_inflight_takeover_without_replaying_it(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.orchestration.interventions import enqueue_intervention
    store = ExpertRunStore(tmp_path)
    run_id = "a" * 32
    store.create(run_id=run_id, conversation_id="chat", task="原任务", attachments=[], profile={}, strategy="stable")
    store.mutate(run_id, lambda value: enqueue_intervention(value, "input-a", "临时任务", kind="takeover"))
    store.transition(run_id, "paused")
    store.mutate(run_id, lambda value: value["interventions"][0].update(status="handling"))
    store.recover_stale_runs()
    snapshot = store.load(run_id)
    assert snapshot["status"] == "paused"
    assert snapshot["interventions"][0]["status"] == "interrupted"
    assert snapshot["takeover_state"]["phase"] == "held"


@pytest.mark.asyncio
async def test_takeover_http_requires_header_and_active_conversation(tmp_path, monkeypatch):
    from frontend import server
    from tests.test_chat_queue import _owner_runtime, _request_with_queue_header
    from starlette.requests import Request
    owner, runtime, task = _owner_runtime(tmp_path)
    runtime.expert_mode = False  # A mode toggle cannot revoke an actual live takeover.
    runtime.execution_kind = "orchestration"
    runtime.expert_run_id = "run-a"
    runtime.session.append = lambda message: None
    runtime.expert_takeover_controller = TakeoverController(runtime, Store(), SimpleNamespace(send_json=AsyncMock()))
    monkeypatch.setattr(server, "SESSION", owner)
    body = server.ChatQueueBody(conversation_id="chat-a", queue_id="input-a", text="插入任务")
    try:
        missing_header = Request({"type": "http", "headers": []})
        assert (await server.api_orchestration_intervention(missing_header, body)).status_code == 403
        assert (await server.api_orchestration_intervention(_request_with_queue_header(), body)).status_code == 200
        runtime.expert_mode = True
        runtime.execution_kind = "chat"
        assert (await server.api_orchestration_intervention(_request_with_queue_header(), body)).status_code == 409
        runtime.execution_kind = "orchestration"
        foreign = body.model_copy(update={"conversation_id": "chat-b"})
        assert (await server.api_orchestration_intervention(_request_with_queue_header(), foreign)).status_code == 409
        def full(*_args, **_kwargs):
            raise OSError('运行数据盘空间不足')
        monkeypatch.setattr(runtime.expert_takeover_controller, 'submit', full)
        failed = await server.api_orchestration_intervention(_request_with_queue_header(), body)
        assert failed.status_code == 409 and '空间不足' in json.loads(failed.body)['error']
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_composer_sends_expert_text_to_takeover_without_waiting_queue():
    import subprocess
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "frontend/static/app.js").read_text(encoding="utf-8")
    start = source.index("  async function queueCurrentComposer(")
    function = source[start:source.index("  async function sendChat(", start)]
    script = r'''
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
let chatQueueSubmissionPending = false, pendingAttachments = [], readonlyView = false;
let currentStatus = {expert_mode: false, execution_kind: 'orchestration'}, calls = [], users = [], queued = 0;
const inputEl = {value:'临时任务', style:{}, focus(){}};
const window = {MewChatQueue:{enqueue: async()=>queued++}};
const syncActiveStreaming=()=>{}, notify=()=>{}, renderAttachmentTray=()=>{}, hideSlashMenu=()=>{};
const activeConversationId=()=> 'chat-a';
const addUserMsg=(text)=>users.push(text);
const fetch=async(url, options)=>{calls.push({url,...options});return{ok:true,json:async()=>({new:true,message:'已接管'})}};
''' + function + r'''
(async()=>{
  assert(await queueCurrentComposer('临时任务', [], 'chat-a', '临时任务'));
  assert.equal(queued,0);
  assert.equal(calls[0].url,'/api/orchestration/interventions');
  assert.equal(calls[0].headers['X-Mew-Chat-Queue'],'1');
  assert.equal(JSON.parse(calls[0].body).conversation_id,'chat-a');
  assert.deepEqual(users,['临时任务']);
  assert.equal(inputEl.value,'');
  assert.equal(inputEl.disabled,false);
  currentStatus.expert_mode=true;
  currentStatus.execution_kind='chat';
  await queueCurrentComposer('普通任务', [], 'chat-a', '');
  assert.equal(queued,1);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
