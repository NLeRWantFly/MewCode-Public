"""Technical trial failures stay private and converge before explicit adoption."""
import asyncio
import copy
import json

import httpx
import pytest

from frontend import orch_drafts
from mewcode.orchestration.draft_sources import read_sources
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_inspection import diagnose
from mewcode.orchestration.graph_local_repair import SCHEMA
from mewcode.orchestration.graph_readiness import readiness
from tests.test_graph_autobuild import make_app
from tests.test_graph_local_repair import API, prepare
from tests.test_graph_review_insertion import publication_graph
from tests.test_workflow_drafts import api_env, ready


def repair(ticket, edits, **extra):
    return {"schema": SCHEMA, "ticket": ticket, "summary": "补齐交付与返修结构，保留原有职责和验收。",
            "upserts": edits, **extra}


@pytest.mark.asyncio
async def test_cycle_trial_is_private_until_corrected_and_explicitly_adopted(api_env, monkeypatch):
    _, plans, path, owner = api_env
    store = DraftStore(owner["project"], "paper")
    original_proposal = prepare(store, read_sources(path)[0])
    before = copy.deepcopy(store.get()["candidate"])
    back = next(e["id"] for e in before["document"]["entries"] if e["kind"] == "edge" and e["data"]["condition"] == "loop")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def model(client, **kwargs):
        assert kwargs["total_timeout"] is None
        data = json.loads(kwargs["user_text"])
        calls.append(data)
        if len(calls) == 1:
            return json.dumps(repair(data["ticket"], [*original_proposal["upserts"],
                {"id": back, "data": {"condition": "always"}, "rationale": "模拟未声明的前向环"}]))
        assert any("cycle" in d.get("message", "") or d.get("code") == "forward_cycle" for d in data["diagnostics"])
        entered.set()
        await release.wait()
        return json.dumps(repair(data["ticket"], [
            {"id": back, "data": {"condition": "loop"}, "rationale": "使用原有循环声明调度返修"}]))
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        pending = asyncio.create_task(client.post(API + "/repair", json={**owner, "action": "local"}))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert store.get()["candidate"] == before
            assert not store.get().get("repair_preview")
            activity = (await client.get(API + "/progress", params=owner)).json()
            assert activity["task"]["saved_changes"] == 2
            assert "work" not in activity["task"]
            legacy = (await client.get("/api/orchestration/draft", params=owner)).json()
            assert "work" not in legacy.get("local_repair_run", {})
            assert "forward_cycle" not in json.dumps(activity, ensure_ascii=False)
            assert "未声明环" not in json.dumps(activity, ensure_ascii=False)
            release.set()
            response = await pending
            assert response.status_code == 200, response.text
            preview = response.json()
            assert preview["validation"]["valid"] and preview["repair_status"] == "ready"
            assert preview["round_count"] == len(calls) == 2
            assert store.get()["candidate"] == before and plans.get(owner["project"]) is None
            adopted = await client.post(API + "/repair", json={**owner, "action": "adopt", "preview_id": preview["id"]})
            assert adopted.status_code == 200, adopted.text
            after = store.get()["candidate"]["document"]
            assert [e for e in after["entries"] if e["kind"] == "node"] == [e for e in before["document"]["entries"] if e["kind"] == "node"]
            assert readiness(after, None, conversation_id="paper", revision=1)["ready"]
        finally:
            release.set()
            await pending


@pytest.mark.asyncio
async def test_model_stop_preserves_private_draft_and_resume_fixes_it(api_env, monkeypatch):
    _, _, path, owner = api_env
    store = DraftStore(owner["project"], "paper")
    proposal = prepare(store, read_sources(path)[0])
    before = copy.deepcopy(store.get()["candidate"])
    back = next(e["id"] for e in before["document"]["entries"] if e["kind"] == "edge" and e["data"]["condition"] == "loop")
    count = 0
    async def model(client, **kwargs):
        nonlocal count
        count += 1
        ticket = json.loads(kwargs["user_text"])["ticket"]
        if count == 1:
            return json.dumps(repair(ticket, [*proposal["upserts"], {"id": back, "data": {"condition": "always"}, "rationale": "试修"}],
                control={"action": "stop", "reason": "本次先保存草案，稍后继续完善。", "next_step": ""}))
        return json.dumps(repair(ticket, [{"id": back, "data": {"condition": "loop"}, "rationale": "回到原有返修调度"}]))
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        first = await client.post(API + "/repair", json={**owner, "action": "local"})
        assert first.status_code == 200 and first.json()["repair_status"] == "model_stopped"
        assert store.get()["candidate"] == before
        assert len(store.get()["local_repair_run"]["work"]["upserts"]) == 2
        second = await client.post(API + "/repair", json={**owner, "action": "local"})
        assert second.status_code == 200, second.text
        assert second.json()["validation"]["valid"] and count == 2
        assert store.get()["candidate"] == before


def test_chat_repair_targets_execution_handoff_and_review_gaps(tmp_path):
    store = DraftStore(tmp_path, "publication")
    doc = publication_graph()
    ready(store, [{"id": "s", "role": "user", "text": "发布后独立核验再交付"}], doc)
    assert diagnose(doc, None, conversation_id="publication", revision=1)["valid"]
    assert not readiness(doc, None, conversation_id="publication", revision=1)["ready"]
    packet = store.request_graph_repair(None)
    assert "N12A" in packet["entry_ids"]
