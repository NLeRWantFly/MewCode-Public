from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from mewcode.conversation import ConversationManager
from mewcode.orchestration.task_design import TaskDesign
from tests.test_task_design_entry import task_api_env


class _ResumeSocket:
    def __init__(self, events, on_send=None):
        self.events = events
        self.on_send = on_send

    async def send_json(self, value):
        self.events.append(value)
        if self.on_send is not None:
            await self.on_send(value)


def _mock_main_chat_dispatch(monkeypatch, owner, *, on_send=None):
    runtime = SimpleNamespace(
        conversation=ConversationManager(),
        conversation_id=owner["conversation_id"],
        work_dir=owner["project"],
        execution_kind="",
        starting=False,
    )
    socket_events = []
    dispatches = []
    attached = []

    monkeypatch.setattr(server.SESSION, "capture_conversation_runtime", lambda: runtime)
    monkeypatch.setattr(server, "ConversationWebSocket",
                        lambda *_args: _ResumeSocket(socket_events, on_send))
    monkeypatch.setattr(server.WS_EVENTS, "send_json", lambda *_a, **_k: asyncio.sleep(0))

    async def run_chat(ws, text, **kwargs):
        dispatches.append({"ws": ws, "text": text, **kwargs})

    def attach_conversation_task(captured, task):
        assert captured is runtime
        captured.starting = False
        attached.append(task)

    monkeypatch.setattr(server, "run_chat", run_chat)
    monkeypatch.setattr(server.SESSION, "attach_conversation_task", attach_conversation_task)
    return runtime, socket_events, dispatches, attached


async def _start_and_pause(client, owner, *, intent="plan", goal="整理现有任务并继续设计"):
    started = await client.post("/api/orchestration/task-design", json={
        **owner, "action": "start", "intent": intent,
    })
    assert started.status_code == 200, started.text
    design = TaskDesign(owner["project"], owner["conversation_id"])
    current = design.read()
    if goal:
        current = design.record_turn(goal)
    paused = await client.post("/api/orchestration/task-design", json={
        **owner,
        "action": "pause",
        "intent": intent,
        "expected_task_id": current["task_id"],
        "expected_revision": current["revision"],
    })
    assert paused.status_code == 200, paused.text
    return design, paused.json()["task_design"]


async def _settle(tasks):
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_resume_with_goal_dispatches_main_model_without_recording_user_turn(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    runtime, socket_events, dispatches, attached = _mock_main_chat_dispatch(monkeypatch, owner)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, paused = await _start_and_pause(client, owner)
        response = await client.post("/api/orchestration/task-design", json={
            **owner,
            "action": "resume",
            "intent": paused["intent"],
            "expected_task_id": paused["task_id"],
            "expected_revision": paused["revision"],
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["resumed"] is True
        assert body["continuation"] == {
            "kind": "design",
            "message": "已继续设计，主模型正在处理剩余步骤。",
        }
        assert body["task_design"]["status"] == "designing"
        assert design.read()["status"] == "designing"
        await asyncio.sleep(0)
        await _settle(attached)

    assert runtime.execution_kind == "chat"
    assert len(dispatches) == 1
    assert dispatches[0]["text"] == ""
    assert dispatches[0]["record_user"] is False
    assert dispatches[0]["runtime"] is runtime
    assert len(socket_events) == 1
    assert socket_events[0]["type"] == "turn_started"
    assert socket_events[0]["client_echoed"] is True
    assert any("task-design-resume" in message.content for message in runtime.conversation.history)


@pytest.mark.asyncio
async def test_resume_rejects_busy_chat_without_changing_paused_task(task_api_env, monkeypatch):
    current, _, _, owner, _ = task_api_env
    _, _, dispatches, attached = _mock_main_chat_dispatch(monkeypatch, owner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, paused = await _start_and_pause(client, owner)
        current["busy"] = True
        response = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "resume", "intent": paused["intent"],
            "expected_task_id": paused["task_id"], "expected_revision": paused["revision"],
        })
    assert response.status_code == 409
    assert "当前对话仍在运行" in response.json()["error"]
    assert design.read()["status"] == "paused"
    assert not dispatches and not attached


@pytest.mark.asyncio
async def test_duplicate_resume_is_rejected_after_the_first_dispatch(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    _, _, dispatches, attached = _mock_main_chat_dispatch(monkeypatch, owner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, paused = await _start_and_pause(client, owner)
        payload = {
            **owner, "action": "resume", "intent": paused["intent"],
            "expected_task_id": paused["task_id"], "expected_revision": paused["revision"],
        }
        first = await client.post("/api/orchestration/task-design", json=payload)
        assert first.status_code == 200, first.text
        await asyncio.sleep(0)
        duplicate = await client.post("/api/orchestration/task-design", json=payload)
        await _settle(attached)
    assert duplicate.status_code == 409
    assert "任务规格已变化" in duplicate.json()["error"] or "仅暂停的任务" in duplicate.json()["error"]
    assert design.read()["status"] == "designing"
    assert len(dispatches) == 1


@pytest.mark.asyncio
async def test_failed_resume_dispatch_returns_task_to_paused(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    design, paused = None, None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, paused = await _start_and_pause(client, owner)
        monkeypatch.setattr(server.SESSION, "capture_conversation_runtime",
                            lambda: (_ for _ in ()).throw(RuntimeError("当前对话尚未准备好")))
        monkeypatch.setattr(server.WS_EVENTS, "send_json", lambda *_a, **_k: asyncio.sleep(0))
        response = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "resume", "intent": paused["intent"],
            "expected_task_id": paused["task_id"], "expected_revision": paused["revision"],
        })
    assert response.status_code == 409
    assert "当前对话尚未准备好" in response.json()["error"]
    current = design.read()
    assert current["status"] == "paused"
    assert "当前对话尚未准备好" in current["reason"]


@pytest.mark.asyncio
async def test_resume_without_goal_returns_input_without_dispatch(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    _, _, dispatches, attached = _mock_main_chat_dispatch(monkeypatch, owner)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, paused = await _start_and_pause(client, owner, goal="")
        response = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "resume", "intent": paused["intent"],
            "expected_task_id": paused["task_id"], "expected_revision": paused["revision"],
        })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumed"] is True
    assert body["continuation"] == {"kind": "input", "message": "任务已恢复，请在对话中填写目标。"}
    assert body["task_design"]["status"] == "designing"
    assert design.read()["status"] == "designing"
    assert not dispatches and not attached


@pytest.mark.asyncio
async def test_resume_with_run_id_preserves_existing_execution_path(task_api_env, monkeypatch):
    _, plans, _, owner, _ = task_api_env
    monkeypatch.setattr(server.EXPERT_RUNS, 'load', lambda run_id: {'run_id':run_id, 'conversation_id':owner['conversation_id'], 'run_inputs':{}})
    _, _, dispatches, attached = _mock_main_chat_dispatch(monkeypatch, owner)
    confirmations = []
    spec = SimpleNamespace(as_dict=lambda: {"nodes": [{"id": "saved"}], "meta": {"task_design": {"intent": "execute"}}})
    monkeypatch.setattr(server, "_conversation_plan_store",
                        lambda _cid=None: SimpleNamespace(get=lambda project: SimpleNamespace(spec=spec)))

    async def confirm(request):
        confirmations.append(await request.json())
        return {"started": True}

    monkeypatch.setattr(server, "api_orchestration_confirm", confirm)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, paused = await _start_and_pause(client, owner, intent="execute")
        with_run = design.transition(paused, "paused", "已存在执行检查点。", run_id="existing-run")
        response = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "resume", "intent": "execute",
            "expected_task_id": with_run["task_id"], "expected_revision": with_run["revision"],
        })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumed"] is True
    assert body["continuation"]["kind"] == "execution"
    assert body["task_design"]["status"] == "running"
    assert body["task_design"]["run_id"] == "existing-run"
    assert len(confirmations) == 1 and confirmations[0]["activate"] is True
    assert confirmations[0]["spec"]["nodes"][0]["id"] == "saved"
    assert not dispatches and not attached


@pytest.mark.asyncio
async def test_scope_switch_before_dispatch_prevents_sending_to_another_chat(task_api_env, monkeypatch):
    current, _, _, owner, _ = task_api_env

    async def switch_after_started(_event):
        current["cid"] = "other-conversation"

    _, socket_events, dispatches, attached = _mock_main_chat_dispatch(monkeypatch, owner, on_send=switch_after_started)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, paused = await _start_and_pause(client, owner)
        response = await client.post("/api/orchestration/task-design", json={
            **owner, "action": "resume", "intent": paused["intent"],
            "expected_task_id": paused["task_id"], "expected_revision": paused["revision"],
        })
    assert response.status_code == 409
    assert "项目或对话已切换" in response.json()["error"]
    assert socket_events and socket_events[0]["type"] == "turn_started"
    assert design.read()["status"] == "paused"
    assert not dispatches and not attached
