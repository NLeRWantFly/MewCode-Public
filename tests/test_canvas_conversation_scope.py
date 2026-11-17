from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from mewcode.experts.flow import ExpertFlowStore
from mewcode.orchestration import get_template, to_pipeline_graph, to_expert_flow, from_expert_flow
from mewcode.orchestration.activity import ActivityStore
from mewcode.orchestration.conversation_scope import ConversationStore
from mewcode.orchestration.plan_session import PlanSessionStore, build_plan_session
from mewcode.orchestration.spec import OrchestrationError
from mewcode.pipeline.store import PipelineStore


@pytest.mark.parametrize("kind", ["plan", "flow"])
def test_scoped_store_preserves_legacy_and_other_conversations(tmp_path, kind):
    raw = (PlanSessionStore if kind == "plan" else ExpertFlowStore)(tmp_path / "store.json")
    spec = get_template("linear", goal="legacy drawing")
    make = (lambda s: build_plan_session(s)) if kind == "plan" else to_expert_flow
    raw.put(tmp_path, make(spec))
    legacy = raw.get(tmp_path).as_dict()
    a, b = ConversationStore(raw, "paper"), ConversationStore(raw, "drawing")
    assert a.get(tmp_path) is b.get(tmp_path) is None
    a.put(tmp_path, make(replace(spec, goal="paper")))
    b.put(tmp_path, make(replace(spec, goal="drawing")))
    assert raw.get(tmp_path).as_dict() == legacy
    get_spec = lambda x: x.spec if kind == "plan" else x
    assert get_spec(a.get(tmp_path)).meta["conversation_id"] == "paper"
    assert get_spec(b.get(tmp_path)).meta["conversation_id"] == "drawing"
    with pytest.raises(OrchestrationError):
        b.put(tmp_path, a.get(tmp_path))
    a.delete(tmp_path)
    assert a.get(tmp_path) is None
    assert b.get(tmp_path) is not None and raw.get(tmp_path).as_dict() == legacy


def test_conversation_survives_flow_roundtrip():
    spec = replace(get_template("linear"), meta={"conversation_id": "paper"})
    assert from_expert_flow(to_expert_flow(spec)).meta["conversation_id"] == "paper"


@pytest.fixture
def env(tmp_path, monkeypatch):
    plans = PlanSessionStore(tmp_path / "plans.json")
    flows = ExpertFlowStore(tmp_path / "flows.json")
    current = {"conversation": "paper"}
    monkeypatch.setattr(server.SESSION, "work_dir", str(tmp_path))
    monkeypatch.setattr(server, "_active_conversation_id", lambda: current["conversation"])
    monkeypatch.setattr(server, "get_plan_session_store", lambda: plans)
    monkeypatch.setattr(server, "EXPERT_FLOWS_STORE", flows)
    monkeypatch.setattr(server.EXPERT_RUNS, "list_for_conversation", lambda _: [])
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _: SimpleNamespace(session=None, task=None))
    return current, plans, flows


@pytest.mark.asyncio
async def test_same_project_different_tasks_never_share_canvas(tmp_path, env):
    current, plans, flows = env
    store = PipelineStore(tmp_path)
    legacy = store.create_pipeline(to_pipeline_graph(get_template("linear", goal="科研绘图"), graph_id="g_legacy"))
    legacy_path = tmp_path / ".mewcode/pipelines/g_legacy.json"
    legacy_bytes = legacy_path.read_bytes()
    plans.put(tmp_path, build_plan_session(get_template("linear", goal="科研绘图")))
    # Old buggy activity bindings are not proof of ownership.
    ActivityStore(tmp_path).bind("paper", legacy.graph_id, "chat:old", nodes=[])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        missing = (await client.get("/api/orchestration/current")).json()
        assert missing["canvas_missing"] and not missing["custom"] and missing["plan_session"] is None
        assert (await client.post("/api/orchestration/open-canvas", json={})).status_code in (400, 409)
        # A retired activity link recovers the single unclaimed legacy graph.
        activity = await client.post("/api/orchestration/open-canvas", json={"mode": "activity", "conversation_id": "paper"})
        assert activity.status_code == 200
        assert activity.json()["graph_id"] == "g_legacy" and activity.json()["unclaimed"] is True
        # Unclaimed legacy graphs open read-only instead of being rejected.
        legacy = await client.post("/api/orchestration/open-canvas", json={"graph_id": "g_legacy"})
        assert legacy.status_code == 200 and legacy.json()["unclaimed"] is True
        opened = await client.post("/api/orchestration/open-canvas", json={"spec": get_template("linear", goal="修改论文").as_dict(), "conversation_id": "paper"})
        assert opened.status_code == 200, opened.text
        paper = opened.json()
        assert paper["graph_id"] != "g_legacy"
        assert paper["spec"]["meta"]["conversation_id"] == "paper"
        assert paper["graph"]["edges"], "Task isolation must preserve native arrows"
        assert plans.get(tmp_path).goal == "科研绘图"
        paper_path = tmp_path / (".mewcode/pipelines/" + paper["graph_id"] + ".json")
        paper_bytes = paper_path.read_bytes()
        current["conversation"] = "drawing"
        assert (await client.get("/api/orchestration/current")).json()["canvas_missing"]
        for body in ({"graph_id": paper["graph_id"]}, {"graph_id": paper["graph_id"], "mode": "activity"},
                     {"spec": paper["spec"]}, {"conversation_id": "paper"}):
            denied = await client.post("/api/orchestration/open-canvas", json=body)
            assert denied.status_code in (400, 409), denied.text
        # Both honest stale tabs and clients forging metadata must be denied.
        forged = json.loads(json.dumps(paper["graph"]))
        payload = json.loads(forged["variables"]["__orch.payload__"])
        payload["meta"]["conversation_id"] = "drawing"
        forged["variables"]["__orch.payload__"] = json.dumps(payload)
        for graph in (paper["graph"], forged):
            denied = await client.post("/api/orchestration/canvas-sync", json={"graph": graph})
            assert denied.status_code == 409, denied.text
        scope = {"project": str(tmp_path), "conversation_id": "drawing", "graph_id": paper["graph_id"]}
        for endpoint in ("run-state", "node-history"):
            denied = await client.get("/api/orchestration/" + endpoint, params={**scope, "run_id": "chat:old"})
            assert denied.status_code == 409, denied.text
        drawing = await client.post("/api/orchestration/open-canvas", json={"spec": get_template("linear", goal="新绘图任务").as_dict()})
        assert drawing.status_code == 200, drawing.text
        assert drawing.json()["graph_id"] not in (paper["graph_id"], "g_legacy")
        current["conversation"] = "paper"
        assert (await client.get("/api/orchestration/current")).json()["spec"]["meta"]["pipeline_graph_id"] == paper["graph_id"]
        reopened = await client.post("/api/orchestration/open-canvas", json={})
        assert reopened.status_code == 200 and reopened.json()["graph_id"] == paper["graph_id"]
        stale = await client.post("/api/orchestration/open-canvas", json={"graph_id": "g_deleted"})
        assert stale.status_code == 409
        assert paper_path.read_bytes() == paper_bytes
        assert legacy_path.read_bytes() == legacy_bytes


@pytest.mark.asyncio
async def test_retired_activity_link_never_recovers_another_tasks_graph(tmp_path, env):
    from mewcode.orchestration.spec import OrchestrationSpec
    store = PipelineStore(tmp_path)
    drawing = replace(get_template("linear"), meta={"conversation_id": "drawing"})
    store.create_pipeline(to_pipeline_graph(drawing, graph_id="g_drawing"))
    old_spec = OrchestrationSpec.from_value({
        "label": "当前对话进度", "goal": "旧视图",
        "nodes": [{"node_id": "activity_shared", "label": "对话与共享文件", "kind": "implement",
                   "bind_later": True, "position": {"x": 80, "y": 100}}],
        "edges": [], "meta": {"canvas_mode": "chat_activity", "conversation_id": "paper"}})
    old = store.create_pipeline(to_pipeline_graph(old_spec, graph_id="g_chat_paper"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        result = await client.post("/api/orchestration/open-canvas", json={"graph_id": old.graph_id})
        assert result.status_code == 409 and result.json()["canvas_missing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_project", [False, True])
async def test_extraction_completing_after_switch_stays_with_original_task(tmp_path, env, monkeypatch, switch_project):
    current, plans, _ = env
    spec = get_template("linear", goal="paper draft")
    monkeypatch.setattr(server.SESSION, "client", object())
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda: False)
    monkeypatch.setattr(server.EXPERT_PROFILES, "get", lambda _: None)
    monkeypatch.setattr(server, "_orchestration_attempt_context", lambda _: {
        "work_dir": str(tmp_path), "provider_key": "test", "model": "test"})

    async def fake_recovery(*args):
        current["conversation"] = "drawing"
        if switch_project:
            server.SESSION.work_dir = str(tmp_path / "another-project")
        return {"spec": spec.as_dict(), "plan_session": build_plan_session(spec).as_dict()}

    monkeypatch.setattr(server, "_run_orchestration_with_recovery", fake_recovery)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/extract-from-chat", json={
            "conversation_id": "paper", "messages": [{"role": "user", "content": "Generate the paper workflow"}]})
        assert response.status_code == 200, response.text
        assert response.json()["conversation_id"] == "paper"
        assert response.json()["flow"]["meta"]["conversation_id"] == "paper"
        paper = ConversationStore(plans, "paper").get(tmp_path)
        assert paper and paper.goal == "paper draft"
        assert ConversationStore(plans, "drawing").get(tmp_path) is None
        assert ConversationStore(plans, "paper").get(tmp_path / "another-project") is None
        assert plans.get(tmp_path) is None
