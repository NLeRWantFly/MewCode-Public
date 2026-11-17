import asyncio
import json

import httpx
import pytest

from frontend import orch_maintenance, server
from mewcode.agent import Agent, AskUserRequest, StreamRetryEvent, StreamText, TurnComplete
from mewcode.conversation import ConversationManager
from mewcode.orchestration.maintenance import SCHEMA, MaintenanceStore, partial_reply_text
from mewcode.orchestration.maintenance_agent import FixTurn
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta
from tests.test_orchestration_maintenance import maintenance_env, poll, spec_for
from tests.test_agent import MockLLMClient


def test_partial_reply_holds_unpaired_surrogates_until_emoji_pair_is_complete():
    prefix = '{"schema":"' + SCHEMA + '","reply":"safe \\ud83d'
    assert partial_reply_text(prefix) == "safe "
    assert partial_reply_text(prefix + '\\ude00 and ready"') == "safe 😀 and ready"


@pytest.mark.asyncio
async def test_fix_streams_reply_before_question_and_retries_replace_discarded_text(tmp_path):
    store = MaintenanceStore(tmp_path / "maintenance.sqlite")
    project, conversation_id = str(tmp_path), "fix-stream"
    base = spec_for(conversation_id).as_dict()
    state = store.open(project, conversation_id, base, mode="fix")
    state = store.begin(project, conversation_id, state["id"], revision=0, message="调查依赖", base=base)

    stale = '{"schema":"' + SCHEMA + '","reply":"旧响应残片'
    current = '{"schema":"' + SCHEMA + '","reply":"已定位依赖问题'
    final = json.dumps({"schema": SCHEMA, "reply": "依赖问题已定位，等待确认。", "proposal": None}, ensure_ascii=False)
    question = {"name": "choice", "type": "radio", "message": "是否继续？", "options": ["继续"]}

    class ScriptedAgent:
        agent_id = "fix-agent"
        total_input_tokens = 0
        total_output_tokens = 0
        total_tool_calls = 0

        def __init__(self, conversation):
            self._current_conversation = conversation

        async def run(self, _conversation):
            future = asyncio.get_running_loop().create_future()
            yield StreamText(stale)
            yield StreamRetryEvent("retry", attempt=1, max_attempts=1, discard_chars=len(stale))
            yield StreamText(current)
            yield AskUserRequest("ask-tool", "AskUserQuestion", [question], future)
            await future
            yield TurnComplete(1)
            yield StreamText(final)
            yield TurnComplete(2)

    conversation = ConversationManager()
    conversation.add_user_message("调查依赖")
    turn = FixTurn(store, state, ScriptedAgent(conversation), conversation)
    result_task = asyncio.create_task(turn.run(lambda _event: None))
    for _ in range(100):
        if turn.pending is not None:
            break
        await asyncio.sleep(.01)
    assert turn.pending is not None

    waiting = store.get(project, conversation_id, state["id"])
    visible = [message for message in waiting["messages"] if message.get("kind") == "reply"]
    assert len(visible) == 1
    assert visible[0]["text"] == "已定位依赖问题"
    assert "旧响应残片" not in visible[0]["text"]
    assert SCHEMA not in visible[0]["text"] and "proposal" not in visible[0]["text"]

    turn.answer({"request_id": turn.info["pending"]["request_id"], "answers": {"choice": "继续"}})
    raw = await asyncio.wait_for(result_task, 2)
    completed = store.complete(project, conversation_id, state["id"], call_id=state["call_id"],
                               response=json.loads(raw), base=base)
    replies = [message for message in completed["messages"] if message.get("kind") == "reply"]
    assert len(replies) == 1
    assert replies[0]["id"] == state["call_id"] + ":reply"
    assert replies[0]["text"] == "依赖问题已定位，等待确认。"


@pytest.mark.asyncio
async def test_update_partial_reply_survives_stop_and_reopen_with_split_emoji(maintenance_env, monkeypatch):
    app, owner, _current, _durable, _spec = maintenance_env
    emitted, clients = asyncio.Event(), []
    partial = '{"schema":"' + SCHEMA + '","reply":"更新进行中 \\ud83d'

    class BlockingClient:
        async def stream(self, _conversation, system="", tools=None):
            yield TextDelta(text=partial)
            await asyncio.sleep(.30)
            yield TextDelta(text='\\ude00，已保留部分进度')
            emitted.set()
            await asyncio.Event().wait()

    final = json.dumps({"schema": SCHEMA, "reply": "已从原维护会话继续完成", "proposal": None}, ensure_ascii=False)
    clients.extend([BlockingClient(), MockLLMClient([[TextDelta(text=final), StreamEnd(stop_reason="end_turn")]])])

    def builder(*, client, state, **_kwargs):
        conversation = ConversationManager()
        if state.get("agent_history"):
            from mewcode.expert_runs import deserialize_messages
            conversation.restore_history(deserialize_messages(state["agent_history"]))
        conversation.add_user_message(state["messages"][-1]["text"])
        agent = Agent(client, create_default_registry(), "anthropic", work_dir=owner["project"], stall_policy=False)
        return agent, conversation

    monkeypatch.setattr(orch_maintenance, "build_maintenance_agent", builder)
    monkeypatch.setattr(server, "_new_orchestration_client", lambda _context, _effort: clients.pop(0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        opened = (await client.post("/api/orchestration/maintenance/open",
                                    json={**owner, "mode": "update", "node_ids": ["work"]})).json()
        started = await client.post("/api/orchestration/maintenance/send", json={**owner,
            "id": opened["id"], "revision": opened["revision"], "message": "work 增加复核"})
        assert started.status_code == 200, started.text
        await asyncio.wait_for(emitted.wait(), 5)
        running = (await client.get("/api/orchestration/maintenance/state",
                                    params={**owner, "id": opened["id"]})).json()
        replies = [message for message in running["messages"] if message.get("kind") == "reply"]
        assert len(replies) == 1 and replies[0]["text"] == "更新进行中 😀，已保留部分进度"

        stopped = await client.post("/api/orchestration/maintenance/stop", json={**owner,
            "id": opened["id"], "revision": started.json()["revision"]})
        assert stopped.status_code == 200, stopped.text
        for _ in range(100):
            latest = (await client.get("/api/orchestration/maintenance/state",
                                      params={**owner, "id": opened["id"]})).json()
            if latest["status"] != "running":
                break
            await asyncio.sleep(.01)
        reopened = (await client.post("/api/orchestration/maintenance/open",
                                      json={**owner, "mode": "update", "id": opened["id"]})).json()
        assert reopened["messages"][-1]["text"] == "更新进行中 😀，已保留部分进度"
        resumed = await client.post("/api/orchestration/maintenance/send", json={**owner, "id": opened["id"],
            "revision": reopened["revision"], "message": "继续完成"})
        assert resumed.status_code == 200, resumed.text
        done = await poll(client, owner, opened)

    replies = [message for message in done["messages"] if message.get("kind") == "reply"]
    assert len(replies) == 2
    assert replies[0]["text"] == "更新进行中 😀，已保留部分进度"
    assert replies[1]["text"] == "已从原维护会话继续完成"
    assert SCHEMA not in replies[0]["text"] and "proposal" not in replies[0]["text"]
