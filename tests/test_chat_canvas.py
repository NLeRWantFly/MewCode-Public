from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from mewcode.orchestration import chat_canvas as cc
from mewcode.orchestration.activity import ActivityStore
from mewcode.orchestration import get_template, to_pipeline_graph, to_expert_flow
from mewcode.orchestration.pipeline_bridge import from_pipeline_graph
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.pipeline.store import PipelineStore


def record(store, conv, run, key, path="", **fields):
    return store.record(conv, run, key, {"kind": "tool", "status": "completed", "name": "Bash",
        "arguments": {"command": f"python {path}"} if path else {}, **fields})


def chat_graph(workdir, conv, graph_id="g_chat_view"):
    """Legacy chat_activity canvases can only be constructed by hand now."""
    spec = OrchestrationSpec.from_value({
        "label": "当前对话进度",
        "goal": "按实际工具路径归档历史与实时记录；已记录不代表验收通过。",
        "nodes": [{"node_id": "activity_shared", "label": "对话与共享文件", "kind": "implement",
                   "bind_later": True,
                   "responsibility": "双击查看实际活动、工具输出和文件。此卡片不执行工作。",
                   "position": {"x": 80, "y": 100}}],
        "edges": [],
        "meta": {"canvas_mode": cc.MODE, "conversation_id": conv}})
    return PipelineStore(workdir).create_pipeline(to_pipeline_graph(spec, graph_id=graph_id))


def test_chat_canvas_recognised_but_never_executable(tmp_path):
    graph = chat_graph(tmp_path, "c")
    assert cc.is_chat_canvas(graph)
    assert cc.metadata(graph)["conversation_id"] == "c"
    cc.validate(graph, "c")
    with pytest.raises(ValueError):
        cc.validate(graph, "other")
    with pytest.raises(ValueError):
        cc.validate(graph, "c", "chat:other")
    with pytest.raises(ValueError):
        to_expert_flow(from_pipeline_graph(graph))
    from mewcode.pipeline.compiler import PipelineCompiler
    assert not PipelineCompiler.validate(graph).valid


def test_file_receipts_are_conversation_scoped(tmp_path):
    store = ActivityStore(tmp_path)
    file_id = store.artifact("c", "chat:old", {"id": "a", "path": "chain/unit/a.md", "preview": "safe"})
    foreign = store.artifact("other", "chat:old", {"id": "b", "path": "chain/unit/b.md", "preview": "private"})
    graph = chat_graph(tmp_path, "c")
    run = ActivityStore.chat_view_run("c")
    assert cc.read_file(tmp_path, "c", graph, run, file_id)["preview"] == "safe"
    with pytest.raises(LookupError):
        cc.read_file(tmp_path, "c", graph, run, foreign)


def test_opening_unowned_plan_never_binds_chat_and_planning_skips_activity(tmp_path):
    from frontend import server
    from frontend.orch_activity import bind_open_canvas
    store = PipelineStore(tmp_path)
    original = store.create_pipeline(to_pipeline_graph(get_template("linear"), graph_id="g_original"))
    chat_graph(tmp_path, "c")
    srv = SimpleNamespace(_active_conversation_id=lambda: "c", SESSION=SimpleNamespace(work_dir=str(tmp_path)))
    bind_open_canvas(srv, original)
    assert ActivityStore(tmp_path).bindings("c", "g_original") == []
    assert server._stored_orchestration_graph(store).graph_id == "g_original"


@pytest.mark.asyncio
async def test_http_progress_open_history_live_and_no_plan_write(tmp_path, monkeypatch):
    from frontend import server
    from mewcode.orchestration.plan_session import PlanSessionStore, build_plan_session
    from mewcode.experts.flow import ExpertFlowStore
    plans = PlanSessionStore(tmp_path / "plans.json")
    plans.put(tmp_path, build_plan_session(get_template("linear"), draft_plan=True))
    from dataclasses import replace
    owned_spec = replace(get_template("linear"), meta={"conversation_id": "c"})
    original = PipelineStore(tmp_path).create_pipeline(to_pipeline_graph(owned_spec, graph_id="g_original"))
    original.variables["user_constraints"] = "preserve this attachment and its connector"
    original = PipelineStore(tmp_path).update_pipeline(original)
    original_path = tmp_path / ".mewcode/pipelines/g_original.json"
    original_bytes = original_path.read_bytes()
    old_view = chat_graph(tmp_path, "c")
    before = (tmp_path / "plans.json").read_bytes()
    monkeypatch.setattr(server.SESSION, "work_dir", str(tmp_path))
    monkeypatch.setattr(server, "_active_conversation_id", lambda: "c")
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _: SimpleNamespace(session=None, task=None))
    monkeypatch.setattr(server, "get_plan_session_store", lambda: plans)
    monkeypatch.setattr(server, "EXPERT_FLOWS_STORE", ExpertFlowStore(tmp_path / "flows.json"))
    record(ActivityStore(tmp_path), "c", "chat:legacy:c", "one", "chain/D09/check.md")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        current = await client.get("/api/orchestration/current")
        assert current.status_code == 200, current.text
        assert not current.json().get("chat_canvas")
        opened = await client.post("/api/orchestration/open-canvas", json={"mode": "activity", "conversation_id": "c"})
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body["graph_id"] == original.graph_id
        assert body["graph"]["edges"] == original.model_dump(mode="json", by_alias=True)["edges"]
        assert body["graph"]["variables"] == original.variables
        recovered = await client.post("/api/orchestration/open-canvas", json={"graph_id": old_view.graph_id})
        assert recovered.json()["graph_id"] == original.graph_id
        assert recovered.json()["redirected_from"] == old_view.graph_id
        scope = {"project": str(tmp_path), "conversation_id": "c", "graph_id": body["graph_id"], "run_id": ActivityStore.chat_view_run("c")}
        status = await client.get("/api/orchestration/run-state", params=scope)
        assert status.status_code == 200, status.text
        assert status.json()["run"]["activity_count"] == 1
        record(ActivityStore(tmp_path), "c", "chat:later", "two", "chain/next/check.md")
        live = await client.get("/api/orchestration/run-state", params=scope)
        assert live.json()["run"]["activity_count"] == 2
        assert "graph" not in live.json(), "Polling must not replace nodes, constraints or arrows"
        history = await client.get("/api/orchestration/node-history", params=scope)
        assert history.status_code == 200 and len(history.json()["items"]) == 2
        assert original_path.read_bytes() == original_bytes
        assert (tmp_path / "plans.json").read_bytes() == before
        switched = await client.post("/api/orchestration/open-canvas", json={"mode": "activity", "conversation_id": "other"})
        assert switched.status_code == 409


@pytest.mark.asyncio
async def test_retired_chat_canvas_redirects_to_single_unclaimed_graph(tmp_path, monkeypatch):
    from frontend import server
    store = PipelineStore(tmp_path)
    store.create_pipeline(to_pipeline_graph(get_template("linear"), graph_id="g_unclaimed"))
    old_view = chat_graph(tmp_path, "c")
    monkeypatch.setattr(server.SESSION, "work_dir", str(tmp_path))
    monkeypatch.setattr(server, "_active_conversation_id", lambda: "c")
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _: SimpleNamespace(session=None, task=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        opened = await client.post("/api/orchestration/open-canvas", json={"graph_id": old_view.graph_id})
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body["graph_id"] == "g_unclaimed"
        assert body["unclaimed"] is True
        assert body["redirected_from"] == old_view.graph_id
        history = await client.get("/api/orchestration/node-history", params={
            "project": str(tmp_path), "conversation_id": "c",
            "graph_id": old_view.graph_id, "run_id": ActivityStore.chat_view_run("c")})
        assert history.status_code == 409
        assert "已停用" in history.json()["error"]


def test_chat_history_overlay_preserves_source_runs_and_manual_attribution(tmp_path):
    store = ActivityStore(tmp_path)
    run = store.bind_chat_history("c", "g", nodes=[{"id": "build", "label": "实现"}, {"id": "audit", "label": "检查"}])
    first = record(store, "c", "chat:legacy:c", "a", node_id="build", at="2026-09-11T00:00:00Z")
    second = record(store, "c", "chat:new", "b", at="2026-09-12T00:00:00Z")
    foreign = record(store, "other", "chat:new", "secret")
    record(store, "c", "expert:one", "not-chat")
    page = store.page("c", "g", run)
    assert {e["id"] for e in page["items"]} == {first, second}
    assert {e["run_id"] for e in page["items"]} == {"chat:legacy:c", "chat:new"}
    assert store.page("c", "g", run, node_id="build")["items"][0]["id"] == first
    assert store.page("c", "g", run, node_id="")["items"][0]["id"] == second
    store.assign("c", "g", run, second, "audit")
    assert store.page("c", "g", run, node_id="audit")["items"][0]["attribution"] == "manual"
    assert {e["node_id"] for e in store.overview("c", "g", run)["latest"]} == {"build", "audit"}
    store.assign("c", "g", run, second, None)
    assert store.page("c", "g", run, node_id="audit")["items"] == []
    with pytest.raises(LookupError):
        store.assign("c", "g", run, foreign, "audit")
    with pytest.raises(LookupError):
        store.page("other", "g", run)
