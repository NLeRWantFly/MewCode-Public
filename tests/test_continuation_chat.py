"""No external model calls: real Agent loop + chat host + durable sessions."""
from __future__ import annotations

import asyncio
import time
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from frontend import server
from mewcode.agent import Agent, LoopComplete
from mewcode.client import LLMClient
from mewcode.continuation import prepare_turn, state_for
from mewcode.conversation import ConversationManager
from mewcode.memory.session import SessionManager
from mewcode.tools import create_default_registry
from mewcode.tools.base import TextDelta, StreamEnd


class Client(LLMClient):
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = 0

    async def stream(self, conversation, system="", tools=None):
        self.calls += 1
        yield TextDelta(next(self.answers))
        yield StreamEnd("end_turn", input_tokens=1, output_tokens=1)


class Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


def setup(tmp_path, answers):
    owner = server.WebSession.__new__(server.WebSession)
    owner.session_start = time.monotonic()
    owner.tasks_payload = lambda: []
    owner.conversation_runs = {}
    owner.chat_task = None
    client = Client(answers)
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(tmp_path))
    session = SessionManager(str(tmp_path)).create()
    owner.session = session
    runtime = server.ConversationRuntime(
        owner=owner, conversation_id=session.session_id, scope="project", work_dir=str(tmp_path),
        session=session, conversation=ConversationManager(), agent=agent, client=client,
        slot=None, effort="", context_tier=200000, multi_agent=False, expert_mode=False,
        goal="", task_manager=SimpleNamespace(), trace_manager=SimpleNamespace(),
        agent_tool=None, worktree_manager=None, team_manager=None,
    )
    owner.conversation_runs[runtime.conversation_id] = runtime
    return owner, runtime, Socket()


def result(kind="continue", relation="none"):
    return {"decision": kind, "reason": "D09 尚未完成" if kind == "continue" else "目标已完成",
            "next_step": "完成 D09，保留 D35/D36" if kind == "continue" else "",
            "evidence_ids": ["reply"], "relation": relation}


@pytest.mark.asyncio
async def test_real_agent_does_not_end_after_stage_report(tmp_path, monkeypatch):
    owner, r, socket = setup(tmp_path, ["D35/D36 已完成，下一步 D09", "D09 交付完成"])
    audits = []
    async def review(payload):
        audits.append(payload)
        return result("continue" if len(audits) == 1 else "complete")
    monkeypatch.setattr(server, "make_reviewer", lambda *a: review)
    try:
        outcome = await server.run_chat(socket, "完成所有文本交付，不要停", runtime=r)
        assert not outcome["interrupted"]
        assert r.client.calls == 2
        assert len(audits) == 2
        assert state_for(r)["status"] == "complete"
        assert sum(m["type"] == "done" for m in socket.messages) == 1
        assert r.agent.completion_gate is None
        assert r.session.meta.continuous_task["audits"][-1]["decision"] == "complete"
    finally:
        r.session.close()


@pytest.mark.asyncio
async def test_normal_qa_remains_one_turn_and_no_review(tmp_path, monkeypatch):
    _, r, socket = setup(tmp_path, ["普通答案"])
    monkeypatch.setattr(server, "make_reviewer", lambda *a: (_ for _ in ()).throw(AssertionError("no review")))
    try:
        await server.run_chat(socket, "解释变量", runtime=r)
        assert r.client.calls == 1
        assert not state_for(r)
    finally:
        r.session.close()


@pytest.mark.asyncio
async def test_unavailable_audit_not_success_and_agent_gate_restored(tmp_path, monkeypatch):
    _, r, socket = setup(tmp_path, ["这一小段完成"])
    async def fail(_):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(server, "make_reviewer", lambda *a: fail)
    try:
        outcome = await server.run_chat(socket, "完成全部，不要停", runtime=r)
        assert outcome["interrupted"]
        assert state_for(r)["status"] == "paused"
        assert not r.agent._evo_task_succeeded
        assert r.agent.completion_gate is None
        assert socket.messages[-1]["status"]["continuation"]["status"] == "paused"
    finally:
        r.session.close()


@pytest.mark.asyncio
async def test_real_agent_iteration_budget_still_wins(tmp_path):
    _, r, _ = setup(tmp_path, ["阶段完成"])
    r.agent.max_iterations = 1
    async def gate(*args):
        return "continue"
    r.agent.completion_gate = gate
    try:
        r.conversation.add_user_message("继续任务")
        events = [event async for event in r.agent.run(r.conversation)]
        assert not any(isinstance(e, LoopComplete) for e in events)
        assert r.client.calls == 1
        assert not r.agent._evo_task_succeeded
    finally:
        r.session.close()


@pytest.mark.asyncio
async def test_insertions_serialize_and_stop_clears_all_before_resume(tmp_path):
    owner, r, _ = setup(tmp_path, [])
    prepare_turn(r, "完成全部，不要停")
    task = asyncio.create_task(asyncio.Event().wait())
    r.task = task
    try:
        first = await server._accept_chat_intervention(r, "input-1", "解释一下")
        assert first["new"]
        assert task.cancelling()
        again = await server._accept_chat_intervention(r, "input-1", "解释一下")
        assert not again["new"]
        await server._accept_chat_intervention(r, "input-2", "再检查另一个问题")
        assert [item.id for item in r.queued_prompts] == ["input-1", "input-2"]
        assert all(item.continuation for item in r.queued_prompts)
        owner.interrupt_conversation(r.conversation_id)
        assert not r.queued_prompts
        assert state_for(r)["held"]
        assert not r.continuation_intervention
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        r.session.close()


@pytest.mark.asyncio
async def test_live_insertion_unwinds_then_handles_message_and_resumes(tmp_path, monkeypatch):
    owner, r, socket = setup(tmp_path, ["原任务阶段完成", "先解释插话", "原任务最后交付"])
    entered = asyncio.Event()
    reviews = []
    async def review(payload):
        reviews.append(payload)
        if len(reviews) == 1:
            entered.set()
            await asyncio.Event().wait()
        return result("continue" if len(reviews) == 2 else "complete", "unrelated" if payload["intervention"] else "none")
    monkeypatch.setattr(server, "make_reviewer", lambda *a: review)
    monkeypatch.setattr(server, "WS_EVENTS", socket)
    owner.begin_conversation_turn = lambda runtime: None
    original = asyncio.create_task(server.run_chat(socket, "完成原任务，不要停", runtime=r))
    owner.attach_conversation_task(r, original)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await server._accept_chat_intervention(r, "i-1", "先解释一下这个术语")
        await original
        async with asyncio.timeout(8):
            while state_for(r).get("status") != "complete":
                await asyncio.sleep(.01)
        assert r.client.calls == 3
        assert reviews[1]["intervention"] == "先解释一下这个术语"
        assert reviews[1]["objective"] == "完成原任务，不要停"
        assert reviews[2]["intervention"] == ""
        assert any(m.get("text") == "先解释一下这个术语" and m["type"] == "turn_started" for m in socket.messages)
    finally:
        if r.task and not r.task.done():
            r.task.cancel()
            await asyncio.gather(r.task, return_exceptions=True)
        r.session.close()


@pytest.mark.asyncio
async def test_pause_text_does_not_call_executor(tmp_path):
    _, r, socket = setup(tmp_path, [])
    prepare_turn(r, "完成全部，不要停")
    try:
        await server.run_chat(socket, "暂停", runtime=r)
        assert r.client.calls == 0
        assert state_for(r)["status"] == "paused"
    finally:
        r.session.close()


@pytest.mark.asyncio
async def test_second_insertion_waits_for_first_and_audit_sees_both(tmp_path, monkeypatch):
    owner, r, socket = setup(tmp_path, ["原任务阶段完成", "第一条解释完成", "第二条解释完成", "最后交付"])
    original_review, side_started, release_side = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reviews = []
    base_stream = r.client.stream
    async def stream(*args, **kw):
        if r.client.calls == 1:
            side_started.set()
            await release_side.wait()
        async for event in base_stream(*args, **kw):
            yield event
    r.client.stream = stream
    async def review(payload):
        reviews.append(payload)
        if len(reviews) == 1:
            original_review.set()
            await asyncio.Event().wait()
        return result("continue" if payload["intervention"] else "complete", "unrelated" if payload["intervention"] else "none")
    monkeypatch.setattr(server, "make_reviewer", lambda *a: review)
    monkeypatch.setattr(server, "WS_EVENTS", socket)
    owner.begin_conversation_turn = lambda runtime: None
    original = asyncio.create_task(server.run_chat(socket, "完成任务，不要停", runtime=r))
    owner.attach_conversation_task(r, original)
    try:
        await asyncio.wait_for(original_review.wait(), 5)
        await server._accept_chat_intervention(r, "i-1", "解释甲")
        await asyncio.wait_for(side_started.wait(), 5)
        side = r.task
        await server._accept_chat_intervention(r, "i-2", "解释乙")
        assert not side.cancelling()
        release_side.set()
        async with asyncio.timeout(8):
            while state_for(r).get("status") != "complete":
                await asyncio.sleep(.01)
        assert reviews[1]["intervention"] == "解释甲\n\n解释乙"
        assert r.client.calls == 4
    finally:
        if r.task and not r.task.done():
            r.task.cancel()
            await asyncio.gather(r.task, return_exceptions=True)
        r.session.close()


@pytest.mark.asyncio
async def test_intervention_http_scope_headers_and_second_message_during_unwind(tmp_path, monkeypatch):
    owner, r, _ = setup(tmp_path, [])
    prepare_turn(r, "完成全部，不要停")
    task = asyncio.create_task(asyncio.Event().wait())
    r.task = task
    monkeypatch.setattr(server, "SESSION", owner)
    def request(headers):
        return Request({"type": "http", "method": "POST", "path": "/api/chat/interventions",
                        "headers": headers, "scheme": "http", "server": ("127.0.0.1", 80),
                        "client": ("127.0.0.1", 1), "query_string": b""})
    req = request([(b"x-mew-chat-queue", b"1")])
    body = server.ChatQueueBody(conversation_id=r.conversation_id, queue_id="i1", text="解释甲")
    try:
        assert (await server.api_chat_intervention(request([]), body)).status_code == 403
        assert (await server.api_chat_intervention(req, body.model_copy(update={"conversation_id": "other"}))).status_code == 409
        assert (await server.api_chat_intervention(req, body.model_copy(update={"attachments": ["asset"]}))).status_code == 409
        assert (await server.api_chat_intervention(req, body)).status_code == 200
        # The original cancellation has been requested but hasn't unwound yet.
        second = await server.api_chat_intervention(req, body.model_copy(update={"queue_id": "i2", "text": "解释乙"}))
        assert second.status_code == 200 and json.loads(second.body)["new"]
        replay = await server.api_chat_intervention(req, body)
        assert not json.loads(replay.body)["new"]
        assert len(r.queued_prompts) == 2
        owner.interrupt_conversation(r.conversation_id)
        assert (await server.api_chat_intervention(req, body)).status_code == 409
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        r.session.close()
