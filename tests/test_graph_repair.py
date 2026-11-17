"""Real stores/API; simulated conversation suggestions, no paid calls or live graphs."""
import copy
import json

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_repair import SCHEMA
from tests.test_graph_generation import API, poll
from tests.test_workflow_drafts import api_env, model_patch, record, write_history


@pytest.fixture
def conflict_model(monkeypatch):
    calls = []
    async def generate(client, **kw):
        data = json.loads(kw["user_text"]); calls.append(data)
        patch = model_patch(data); patch["upserts"][0]["state"] = "conflict"
        return json.dumps(patch)
    monkeypatch.setattr(orch_drafts, "collect_model_text", generate)
    return calls


async def handoff(client, owner):
    assert (await client.post(API + "/update", json=owner)).status_code == 202
    state = await poll(client, owner)
    response = await client.post(API + "/repair", json={**owner, "action": "request"})
    assert response.status_code == 200, response.text
    packet = response.json()
    proposal = {"schema": SCHEMA, "ticket": packet["ticket"], "summary": "澄清分析节点职责，保留原有验收与复核。",
                "changes": [{"entry_id": "analyze", "reason": "按来源只澄清职责；不更改范围和验收。",
                             "changes": {"body": "保留原件并分析资料，产出送独立复核。", "state": "active"}}]}
    return state, packet, proposal


@pytest.mark.asyncio
async def test_conversation_repair_preview_adopt_save_preserves_sources_and_pending_chat(api_env, conflict_model):
    _, plans, path, owner = api_env
    store = DraftStore(owner["project"], "paper")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        initial, packet, proposal = await handoff(client, owner)
        before = store.get()["candidate"]
        assert not initial["can_preview"] and packet["entry_ids"] == ["analyze"]
        assert "mewcode-graph-repair" in packet["prompt"] and "不要求我手动连线" in packet["prompt"]
        # Chat discussion is appended without pretending it was part of the original graph scan.
        write_history(path, [record("user", packet["prompt"]), record("assistant", "建议如下")], append=True)
        response = await client.post(API + "/repair", json={**owner, "action": "preview", "proposal": proposal})
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["changes"][0]["before"]["state"] == "conflict"
        assert preview["changes"][0]["after"]["state"] == "active"
        assert store.get()["candidate"] == before and not plans.get(owner["project"])
        adopted = await client.post(API + "/repair", json={**owner, "action": "adopt", "preview_id": preview["id"]})
        assert adopted.status_code == 200, adopted.text
        view = adopted.json()
        assert view["coverage"]["mode"] == "repair" and view["can_preview"]
        assert view["coverage"]["history_total"] > view["coverage"]["total"]
        candidate = store.get()["candidate"]
        assert candidate["processed_ids"] == before["processed_ids"]
        old = {e["id"]: e for e in before["document"]["entries"]}
        new = {e["id"]: e for e in candidate["document"]["entries"]}
        assert new["analyze"]["sources"] == old["analyze"]["sources"]
        assert new["analyze"]["data"] == old["analyze"]["data"]
        assert new["audit"] == old["audit"] and new["analyze"]["reviewed"]
        assert len(conflict_model) == 1 and plans.get(owner["project"]) is None
        graph = (await client.post(API + "/graph", json=owner)).json()
        result = await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": graph["id"]})
        assert result.status_code == 200, result.text
        assert server._conversation_plan_store("paper").get(owner["project"]).status == "drafting"
        reopened = (await client.get(API, params=owner)).json()
        assert reopened["coverage"]["pending"] > 0, "repair chat was not falsely marked as scanned"
        assert store.get()["revision"] == 1 and len(conflict_model) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["candidate", "canvas", "history", "conversation", "ticket"])
async def test_repair_stale_bindings_never_overwrite(api_env, conflict_model, change):
    current, plans, path, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        state, packet, proposal = await handoff(client, owner)
        if change == "candidate":
            c = state["candidate"]
            response = await client.post(API + "/action", json={**owner, "action": "edit", "candidate_id": c["id"],
                "sequence": c["sequence"], "entry_id": "analyze", "changes": {"body": "后来的人工修改"}})
            assert response.status_code == 200
        elif change == "canvas":
            from mewcode.orchestration.workflow_draft import project_graph
            doc = copy.deepcopy(DraftStore(owner["project"], "paper").get()["candidate"]["document"])
            doc["entries"][0]["state"] = "active"
            spec = project_graph(doc, None, conversation_id="paper", revision=0).as_dict()
            assert (await client.post("/api/orchestration/open-canvas", json={**owner, "spec": spec})).status_code == 200
        elif change == "history":
            write_history(path, [record("user", "被改写的原始来源")])
        elif change == "conversation":
            current["cid"] = "another"
        else:
            proposal["ticket"] = "0" * 32
        before = DraftStore(owner["project"], "paper").get()["candidate"]
        rejected = await client.post(API + "/repair", json={**owner, "action": "preview", "proposal": proposal})
        assert rejected.status_code == 409, rejected.text
        assert DraftStore(owner["project"], "paper").get()["candidate"] == before
        assert len(conflict_model) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["scope", "duplicate", "sources", "delete", "reference", "empty", "kind", "reviewed"])
async def test_repair_malformed_or_overbroad_edits_fail_atomically(api_env, conflict_model, bad):
    _, _, _, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        _, _, proposal = await handoff(client, owner)
        edit = proposal["changes"][0]
        if bad == "scope": edit["entry_id"] = "audit"
        elif bad == "duplicate": proposal["changes"].append(copy.deepcopy(edit))
        elif bad == "sources": edit["changes"]["sources"] = []
        elif bad == "delete": edit["changes"]["state"] = "retired"
        elif bad == "reference": edit["changes"]["data"] = {"parent_id": "missing"}
        elif bad == "empty": proposal["changes"] = []
        elif bad == "kind": edit["changes"]["kind"] = "gate"
        else: edit["changes"]["reviewed"] = True
        before = DraftStore(owner["project"], "paper").get()["candidate"]
        response = await client.post(API + "/repair", json={**owner, "action": "preview", "proposal": proposal})
        assert response.status_code == 409, response.text
        assert DraftStore(owner["project"], "paper").get()["candidate"] == before


@pytest.mark.asyncio
async def test_adoption_rechecks_version_and_uses_stored_preview_only(api_env, conflict_model):
    _, _, _, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        state, _, proposal = await handoff(client, owner)
        preview = (await client.post(API + "/repair", json={**owner, "action": "preview", "proposal": proposal})).json()
        c = state["candidate"]
        assert (await client.post(API + "/action", json={**owner, "action": "edit", "candidate_id": c["id"],
            "sequence": c["sequence"], "entry_id": "analyze", "changes": {"body": "人工修订优先"}})).status_code == 200
        assert (await client.post(API + "/repair", json={**owner, "action": "adopt", "preview_id": preview["id"]})).status_code == 409
        assert (await client.post(API + "/repair", json={**owner, "action": "adopt", "proposal": proposal})).status_code == 409


@pytest.mark.asyncio
async def test_new_chat_before_handoff_does_not_require_rescanning_to_discuss_conflict(api_env, conflict_model):
    _, _, path, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        await poll(client, owner)
        write_history(path, [record("user", "这个冲突是什么意思？")], append=True)
        packet = await client.post(API + "/repair", json={**owner, "action": "request"})
        assert packet.status_code == 200, packet.text
        state = (await client.get(API, params=owner)).json()
        assert state["coverage"]["pending"] == 0 and state["coverage"]["new_sources"] == 1
        assert len(conflict_model) == 1, "new dialogue must not enter the frozen repair input"
