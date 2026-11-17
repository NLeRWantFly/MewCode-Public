"""Real graph endpoints with fake planning responses and isolated histories."""
import asyncio
import json

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration.draft_store import DraftStore
from tests.test_graph_generation import API, poll
from tests.test_workflow_drafts import api_env as api_env, cited, entry, model_patch, record, write_history


def identity(owner, state):
    c = state["candidate"] or {}
    return {**owner, "candidate_id": c.get("id"), "sequence": c.get("sequence"),
            "expected_revision": state["revision"], "source_hash": state["basis"]["source_hash"],
            "base_graph_hash": state["base_graph_hash"]}


@pytest.mark.asyncio
async def test_effective_basis_exclude_restore_and_no_extra_model_call(api_env, monkeypatch):
    _, plans, path, owner = api_env
    write_history(path, [record("user", "处理四本 PDF。保持独立验收。数据库用 SQLite。噪声：测试报错。")])
    calls = []
    async def model(client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        calls.append(payload)
        assert "本轮先整理有效对话方案" in kwargs["system"]
        if len(calls) == 1:
            p = model_patch(payload)
            p["upserts"].append(cited(entry("db", "constraint", "数据库用 SQLite"), payload["sources"][0]))
            p["ignored"] = [{"source_id": payload["sources"][0]["id"], "quote": "测试报错", "category": "runtime", "reason": "测试运行动态"}]
            return json.dumps(p, ensure_ascii=False)
        assert payload["sources"][0]["text"] == "测试报错", "restore only the chosen span, never the entire mixed message"
        return json.dumps({"upserts": [cited(entry("test_fixture", "constraint", "测试报错"), payload["sources"][0])], "ignored": []})
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.post(API + "/update", json=owner)).status_code == 202
        state = await poll(client, owner)
        assert state["basis"]["frozen"] and state["can_preview"]
        original = path.read_bytes()
        selected = identity(owner, state)
        excluded = await client.post(API + "/action", json={**selected, "action": "exclude_content", "entry_id": "db"})
        assert excluded.status_code == 200, excluded.text
        state = excluded.json()
        assert state["basis"]["needs_update"] and not state["can_preview"]
        assert (await client.post(API + "/action", json={**selected, "action": "restore_content", "entry_id": "db"})).status_code == 409
        assert (await client.post(API + "/update", json=owner)).status_code == 202
        state = await poll(client, owner)
        assert len(calls) == 1 and state["can_preview"] and not state["basis"]["needs_update"]
        restored = await client.post(API + "/action", json={**identity(owner, state), "action": "restore_content", "entry_id": "db"})
        assert restored.status_code == 200, restored.text
        await client.post(API + "/update", json=owner)
        state = await poll(client, owner)
        assert any(e["id"] == "db" for e in state["basis"]["requirements"])
        ignored = next(e for e in state["basis"]["excluded"] if e["kind"] == "ignored")
        restored = await client.post(API + "/action", json={**identity(owner, state), "action": "restore_content",
            "entry_id": ignored["id"], "quote": "测试报错"})
        assert restored.status_code == 200, restored.text
        await client.post(API + "/update", json=owner)
        state = await poll(client, owner)
        assert state["can_preview"] and len(calls) == 2
        assert any(e["id"] == "test_fixture" for e in state["basis"]["requirements"])
        assert path.read_bytes() == original and plans.get(owner["project"]) is None
        no_op = await client.post(API + "/update", json=owner)
        assert no_op.status_code == 200 and no_op.json()["already_included"] and len(calls) == 2


@pytest.mark.asyncio
async def test_append_during_model_does_not_change_frozen_input_and_can_update_afterwards(api_env, monkeypatch):
    _, _, path, owner = api_env
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def model(client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        calls.append(payload)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return json.dumps(model_patch(payload), ensure_ascii=False)
        return json.dumps({"upserts": [cited(entry("model_rule", "constraint", "主模型保持 medium"), payload["sources"][0])], "ignored": []})
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        await asyncio.wait_for(entered.wait(), 3)
        write_history(path, [record("user", "主模型保持 medium")], append=True)
        progress = (await client.get(API + "/progress", params=owner)).json()
        assert progress["new_history_available"]
        during = (await client.get(API, params=owner)).json()
        assert during["coverage"]["total"] == 1 and during["coverage"]["new_sources"] == 1
        release.set()
        state = await poll(client, owner)
        assert state["can_preview"] and state["coverage"]["new_sources"] == 1
        assert "主模型保持 medium" not in json.dumps(calls[0], ensure_ascii=False)
        await client.post(API + "/update", json={**owner, "include_new": True})
        state = await poll(client, owner)
        assert state["can_preview"] and state["coverage"]["new_sources"] == 0
        assert [s["text"] for s in calls[1]["sources"]] == ["主模型保持 medium"]
        assert any(e["id"] == "bilingual" for e in state["basis"]["requirements"])


@pytest.mark.asyncio
async def test_select_actual_reply_and_keep_old_value_available_for_restoration(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", "处理四本 PDF，模型保持原绑定"), record("assistant", "方案 A：导出 CSV 再验收"),
                        record("assistant", "方案 B：写入 SQLite 再验收")])
    calls = []
    async def model(client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        calls.append(payload)
        sources = payload["sources"]
        plan = next((s for s in sources if "方案 A" in s["text"]), None) if payload["history_basis"]["manual_plan_source_ids"] else sources[-1]
        if len(calls) == 1:
            p = model_patch(payload)
            for item in p["upserts"]:
                if item["kind"] in {"node", "edge"}:
                    item["sources"] = [{"source_id": plan["id"], "quote": plan["text"]}]
            p["current_plan"] = {"source_ids": [plan["id"]], "mode": "suggested", "reason": "最新完整方案"}
            return json.dumps(p, ensure_ascii=False)
        p = model_patch({"sources": [plan]})
        p["upserts"] = [item for item in p["upserts"] if item["kind"] in {"node", "edge"}]
        return json.dumps(p, ensure_ascii=False)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        state = await poll(client, owner)
        assert state["can_preview"]
        assert state["can_preview"] and state["basis"]["current_plan"]["mode"] == "suggested"
        plan_a = state["basis"]["plans"][0]
        choose = await client.post(API + "/action", json={**identity(owner, state), "action": "select_plan", "source_ids": plan_a["source_ids"]})
        assert choose.status_code == 200, choose.text
        state = choose.json()
        assert state["basis"]["current_plan"]["mode"] == "selected" and not state["can_preview"]
        await client.post(API + "/update", json=owner)
        state = await poll(client, owner)
        assert len(calls) == 2 and all("方案 A" in s["text"] for s in calls[-1]["sources"])
        assert any(e["id"] == "bilingual" for e in state["basis"]["requirements"])
        assert DraftStore(owner["project"], owner["conversation_id"]).get()["candidate"]["history_basis"]["manual_plan_source_ids"] == plan_a["source_ids"]
