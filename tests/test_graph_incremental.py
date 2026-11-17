"""Incremental graph checkpoints: never replay successfully materialized raw text."""
import asyncio
import json

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration import draft_sources
from mewcode.orchestration.draft_sources import advance_cursor, estimate_chunks, next_chunk, read_sources
from mewcode.orchestration.draft_store import DraftStore
from tests.test_graph_generation import API, generate, model, poll
from tests.test_workflow_drafts import api_env, cited, entry, model_patch, record, write_history


def test_reused_sources_bridge_only_completed_gaps():
    sources = [{"id": str(i), "text": "source", "role": "user"} for i in range(7)]
    reused = ["0", "2", "5"]
    assert [s["id"] for s in next_chunk(sources, ["0"], source_limit=2, reused_ids=reused)] == ["1", "3"]
    assert estimate_chunks(sources, ["0"], source_limit=2, reused_ids=reused) == 2
    cursor = advance_cursor(sources, ["0"], ["1", "3"], reused_ids=reused)
    assert cursor == ["0", "1", "2", "3"]
    assert [s["id"] for s in next_chunk(sources, cursor, reused_ids=reused)] == ["4", "6"]
    assert advance_cursor(sources, cursor, ["4", "6"], reused_ids=reused) == [str(i) for i in range(7)]
    with pytest.raises(ValueError, match="跳过"):
        advance_cursor(sources, ["0"], ["3"], reused_ids=reused)
    with pytest.raises(ValueError, match="发生变化"):
        next_chunk(sources, [], reused_ids=["missing"])
    with pytest.raises(ValueError, match="发生变化"):
        next_chunk(sources, ["1"], reused_ids=reused)
    assert estimate_chunks(sources, [], reused_ids=["missing"]) == 0
    all_ids = [s["id"] for s in sources]
    assert not next_chunk(sources, [], reused_ids=all_ids)
    assert estimate_chunks(sources, [], reused_ids=all_ids) == 0
    assert advance_cursor(sources, [], reused_ids=all_ids) == all_ids


@pytest.mark.parametrize("source_changed", [False, True])
def test_explicit_rebuild_resume_does_not_reimport_previous_selected_receipts(tmp_path, source_changed):
    path = tmp_path / "history.jsonl"
    write_history(path, [record("user", "第一块"), record("assistant", "已确认的第二块")])
    store = DraftStore(tmp_path, "paper")
    store.refresh(*read_sources(path))
    ids = [s["id"] for s in store.sources()]
    selected = store.begin(None, selection=[ids[1]], graph_first=True)
    selected = store.checkpoint(selected, selected["document"], [ids[1]], "ready")
    store.confirm(selected["id"], selected["sequence"])
    if source_changed:
        write_history(path, [record("user", "第一块"), record("assistant", "已修改的第二块")])
        store.refresh(*read_sources(path))
        ids = [s["id"] for s in store.sources()]
    replay = store.begin(None, rebuild=True, graph_first=True)
    paused = store.checkpoint(replay, replay["document"], [ids[0]], "paused")
    resumed = DraftStore(tmp_path, "paper").begin(None, graph_first=True)
    assert resumed["id"] == paused["id"]
    assert resumed["reused_ids"] == [ids[0]], "only successful blocks from this replay are reusable"
    assert [s["id"] for s in next_chunk(store.sources(), resumed["processed_ids"], reused_ids=resumed["reused_ids"])] == [ids[1]]


def test_legacy_pending_candidate_can_reuse_all_remaining_selected_sources(tmp_path):
    store = DraftStore(tmp_path, "paper")
    sources = [{"id": "selected", "role": "assistant", "text": "建议"}]
    store.refresh(sources, [], {})
    selected = store.begin(None, selection=["selected"])
    selected = store.checkpoint(selected, selected["document"], ["selected"], "ready")
    store.confirm(selected["id"], selected["sequence"])
    legacy = store.begin(None)
    assert "reused_ids" not in legacy
    migrated = store.begin(None, graph_first=True)
    assert migrated["reused_ids"] == ["selected"]
    assert not next_chunk(sources, migrated["processed_ids"], reused_ids=migrated["reused_ids"])
    assert advance_cursor(sources, migrated["processed_ids"], reused_ids=migrated["reused_ids"]) == ["selected"]


@pytest.mark.asyncio
async def test_selected_then_full_scan_reuses_middle_source_and_never_sends_it_as_context(api_env, model, monkeypatch):
    _, _, path, owner = api_env
    monkeypatch.setattr(draft_sources, "CHUNK_CHARS", 200)
    write_history(path, [record("user", "必须保留原件"), record("assistant", "建议增加独立复核"), record("user", "必须保留用户签认")])
    ids = [s["id"] for s in read_sources(path)[0]]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post(API + "/update", json={**owner, "selection": {"texts": ["建议增加独立复核"]}})
        selected = await poll(client, owner)
        assert selected["can_preview"] and len(model) == 1
        preview = (await client.post(API + "/graph", json=owner)).json()
        assert (await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": preview["id"]})).status_code == 200
        before = (await client.get(API, params=owner)).json()["coverage"]
        assert (before["processed"], before["pending"], before["reused"], before["chunks_estimate"]) == (1, 2, 1, 2)
        await client.post(API + "/update", json=owner)
        full = await poll(client, owner)
        assert full["can_preview"] and full["coverage"]["processed"] == 3 and full["coverage"]["reused"] == 1
        assert len(model) == 3
        assert [s["id"] for p in model[1:] for s in p["sources"]] == [ids[0], ids[2]]
        assert all(p["context"] == [] for p in model[1:])
        assert all(p["document"]["entries"] for p in model[1:]), "reuse structure, not just omit old text"
        store = DraftStore(owner["project"], "paper")
        assert store.get()["candidate"]["processed_ids"] == ids
        progress = (await client.get(API + "/progress", params=owner)).json()
        assert progress["coverage"] == full["coverage"]


@pytest.mark.asyncio
async def test_unchanged_generation_is_noop_even_when_model_route_unavailable(api_env, model, monkeypatch):
    _, _, path, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        state, preview = await generate(client, owner)
        def unavailable(*args, **kwargs):
            pytest.fail("no new sources must not resolve a route or create a client")
        monkeypatch.setattr(server, "_orchestration_attempt_context", unavailable)
        monkeypatch.setattr(server, "_new_orchestration_client", unavailable)
        store = DraftStore(owner["project"], "paper")
        candidate_before = store.get()["candidate"]
        repeated = await client.post(API + "/update", json=owner)
        assert repeated.status_code == 200 and repeated.json()["already_included"]
        assert store.get()["candidate"] == candidate_before, "no-op must not invalidate a ready preview"
        assert (await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": preview["id"]})).status_code == 200
        confirmed = store.get()
        again = await client.post(API + "/update", json=owner)
        assert again.status_code == 200 and again.json()["already_included"]
        assert again.json()["candidate"] is None and again.json()["coverage"]["pending"] == 0
        assert store.get()["revision"] == confirmed["revision"] and len(model) == 1


@pytest.mark.asyncio
async def test_all_selected_sources_noop_whole_history_and_scope_isolation(api_env, model, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("assistant", "仅这一条建议")])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post(API + "/update", json={**owner, "selection": {"texts": ["仅这一条建议"]}})
        await poll(client, owner)
        preview = (await client.post(API + "/graph", json=owner)).json()
        assert (await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": preview["id"]})).status_code == 200
        repeated = await client.post(API + "/update", json=owner)
        assert repeated.json()["already_included"] and repeated.json()["coverage"]["pending"] == 0
        assert repeated.json()["can_preview"] and len(model) == 1
        assert (await client.post(API + "/graph", json=owner)).status_code == 200
        # An identical source ID in another conversation is not a completed receipt there.
        other = DraftStore(owner["project"], "another")
        other.refresh(*read_sources(path))
        candidate = other.begin(None, graph_first=True)
        assert not candidate["processed_ids"] and not candidate["reused_ids"]
        assert len(next_chunk(other.sources(), candidate["processed_ids"])) == 1


@pytest.mark.asyncio
async def test_append_only_sends_new_raw_text_and_retains_graph_source_links(api_env, model, monkeypatch):
    _, _, path, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        _, preview = await generate(client, owner)
        assert (await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": preview["id"]})).status_code == 200
        old_ids = {s["id"] for s in read_sources(path)[0]}
        calls = []
        async def extend(client, **kw):
            data = json.loads(kw["user_text"]); calls.append(data)
            assert not old_ids.intersection(s["id"] for s in data["sources"] + data["context"])
            return json.dumps({"upserts": [cited(entry("analyze", body="新增一项产出要求"), data["sources"][0])], "ignored": []})
        monkeypatch.setattr(orch_drafts, "collect_model_text", extend)
        write_history(path, [record("user", "新增一项产出要求")], append=True)
        await client.post(API + "/update", json=owner)
        result = await poll(client, owner)
        assert result["can_preview"] and len(calls) == 1 and len(calls[0]["sources"]) == 1
        updated = next(e for e in result["elements"] if e["id"] == "analyze")
        assert old_ids.intersection(r["source_id"] for r in updated["sources"]), "old citations remain host-owned"
        for source_id in old_ids:
            assert (await client.get(API + "/source/" + source_id, params=owner)).status_code == 200


@pytest.mark.asyncio
async def test_selected_context_does_not_replay_already_processed_raw_sources(api_env, model):
    _, _, path, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        _, preview = await generate(client, owner)
        assert (await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": preview["id"]})).status_code == 200
        write_history(path, [record("assistant", "建议增加测试阶段")], append=True)
        await client.post(API + "/update", json={**owner, "selection": {"texts": ["建议增加测试阶段"]}})
        result = await poll(client, owner)
        assert result["candidate"]["status"] == "ready"
        assert len(model) == 2 and model[-1]["context"] == []
        assert len(model[-1]["sources"]) == 1 and model[-1]["document"]["entries"]


@pytest.mark.asyncio
async def test_restart_resume_skips_successful_block_but_retries_cancelled_block(api_env, monkeypatch):
    _, _, path, owner = api_env
    monkeypatch.setattr(draft_sources, "CHUNK_CHARS", 200)
    write_history(path, [record("user", text) for text in ["首块要求", "第二块要求", "第三块要求"]])
    calls, started, hold = [], asyncio.Event(), asyncio.Event()
    async def complete(client, **kw):
        data = json.loads(kw["user_text"]); calls.append(data)
        if len(calls) == 2:
            started.set(); await hold.wait()
        return json.dumps(model_patch(data))
    monkeypatch.setattr(orch_drafts, "collect_model_text", complete)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        await asyncio.wait_for(started.wait(), 3)
        stopped = await client.post(API + "/action", json={**owner, "action": "stop"})
        assert stopped.json()["coverage"]["processed"] == 1
        # New installer/store instances emulate process restart; no in-memory dedupe can help.
        from fastapi import FastAPI
        restarted_app = FastAPI()
        orch_drafts.install(restarted_app, server)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted_app), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        result = await poll(client, owner)
        assert result["coverage"]["processed"] == 3 and result["can_preview"]
        assert len(calls) == 4
        assert calls[1]["sources"] == calls[2]["sources"], "cancelled block is not marked done"
        first = calls[0]["sources"][0]["id"]
        assert all(first not in {s["id"] for s in p["sources"] + p["context"]} for p in calls[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "selected"])
async def test_source_fingerprint_change_invalidates_reuse_without_model_call(api_env, model, mode):
    _, _, path, owner = api_env
    write_history(path, [record("assistant", "原建议必须有独立复核")])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post(API + "/update", json={**owner, **({"selection": {"texts": ["原建议必须有独立复核"]}} if mode == "selected" else {})})
        await poll(client, owner)
        preview = (await client.post(API + "/graph", json=owner)).json()
        assert (await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": preview["id"]})).status_code == 200
        before = DraftStore(owner["project"], "paper").get()
        write_history(path, [record("assistant", "变更建议不要独立复核")])
        stale = await client.post(API + "/update", json=owner)
        assert stale.status_code == 409 and "发生变化" in stale.json()["error"]
        state = (await client.get(API, params=owner)).json()
        assert state["coverage"]["history_changed"] and not state["can_preview"]
        assert any("发生变化" in issue for issue in state["issues"])
        assert (await client.post(API + "/graph", json=owner)).status_code == 409
        assert len(model) == 1 and DraftStore(owner["project"], "paper").get()["document"] == before["document"]
        # Only an explicit rebuild opts into re-reading history; it is never an automatic fallback.
        rebuilt = await client.post(API + "/update", json={**owner, "rebuild": True})
        assert rebuilt.status_code == 202
        await poll(client, owner)
        assert len(model) == 2 and "变更" in model[-1]["sources"][0]["text"]


@pytest.mark.asyncio
async def test_prior_selected_source_changed_after_new_selected_block_is_ready(api_env, model):
    _, _, path, owner = api_env
    original, added = record("assistant", "旧建议"), record("assistant", "新建议")
    write_history(path, [original])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post(API + "/update", json={**owner, "selection": {"texts": ["旧建议"]}})
        await poll(client, owner)
        preview = (await client.post(API + "/graph", json=owner)).json()
        assert (await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": preview["id"]})).status_code == 200
        write_history(path, [added], append=True)
        await client.post(API + "/update", json={**owner, "selection": {"texts": ["新建议"]}})
        await poll(client, owner)
        next_preview = (await client.post(API + "/graph", json=owner)).json()
        assert "id" in next_preview
        # The newly selected source is unchanged; its reused baseline is not.
        write_history(path, [record("assistant", "已被修改的旧建议"), added])
        rejected = await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": next_preview["id"]})
        assert rejected.status_code == 409
        state = (await client.get(API, params=owner)).json()
        assert state["coverage"]["history_changed"] and not state["can_preview"]
        assert state["revision"] == 1 and len(model) == 2


@pytest.mark.asyncio
async def test_new_input_does_not_resurrect_manually_removed_graph_nodes(api_env, model, monkeypatch):
    _, _, path, owner = api_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        _, preview = await generate(client, owner)
        applied = await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": preview["id"]})
        spec = applied.json()["spec"]
        spec["nodes"] = [n for n in spec["nodes"] if n["node_id"] != "audit"]
        spec["edges"] = []
        edited = await client.post("/api/orchestration/open-canvas", json={**owner, "spec": spec,
            "expected_version": spec["meta"]["pipeline_graph_version"]})
        assert edited.status_code == 200, edited.text
        async def unchanged(client, **kw):
            data = json.loads(kw["user_text"])
            assert "audit" not in {e["id"] for e in data["document"]["entries"]}
            assert len(data["sources"]) == 1 and not data["context"]
            return json.dumps({"upserts": [], "ignored": [{"source_id": data["sources"][0]["id"], "reason": "执行动态"}]})
        monkeypatch.setattr(orch_drafts, "collect_model_text", unchanged)
        write_history(path, [record("user", "执行动态：已收到")], append=True)
        _, updated = await generate(client, owner)
        assert "audit" not in {n["node_id"] for n in updated["spec"]["nodes"]}
