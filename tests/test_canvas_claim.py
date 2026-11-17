from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from mewcode.experts.flow import ExpertFlowStore
from mewcode.orchestration import get_template, to_pipeline_graph
from mewcode.orchestration.conversation_scope import ConversationStore
from mewcode.orchestration.plan_session import PlanSessionStore, build_plan_session
from mewcode.pipeline.store import PipelineStore


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


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test")


@pytest.mark.asyncio
async def test_unclaimed_graph_opens_and_reports_unclaimed(tmp_path, env):
    current, plans, _ = env
    store = PipelineStore(tmp_path)
    store.create_pipeline(to_pipeline_graph(get_template("linear", goal="科研绘图"), graph_id="g_legacy"))
    async with _client() as client:
        opened = await client.post("/api/orchestration/open-canvas", json={"graph_id": "g_legacy"})
        assert opened.status_code == 200, opened.text
        assert opened.json()["unclaimed"] is True
        assert opened.json()["graph_id"] == "g_legacy"
        scope = {"project": str(tmp_path), "conversation_id": "paper", "graph_id": "g_legacy"}
        state = await client.get("/api/orchestration/run-state", params=scope)
        assert state.status_code == 200, state.text
        assert state.json()["run"] is None
        assert state.json()["unclaimed"] is True


@pytest.mark.asyncio
async def test_claim_requires_explicit_header_then_stamps_owner(tmp_path, env):
    current, plans, _ = env
    store = PipelineStore(tmp_path)
    graph = store.create_pipeline(
        to_pipeline_graph(get_template("linear", goal="科研绘图"), graph_id="g_legacy"))
    # A project-level (un-scoped) plan that produced this graph moves with it.
    plans.put(tmp_path, build_plan_session(
        replace(get_template("linear", goal="科研绘图"), meta={"pipeline_graph_id": "g_legacy"})))
    scope = {"project": str(tmp_path), "conversation_id": "paper", "graph_id": "g_legacy"}
    async with _client() as client:
        denied = await client.post("/api/orchestration/claim-canvas", json=scope)
        assert denied.status_code == 403
        claimed = await client.post("/api/orchestration/claim-canvas", json=scope,
                                    headers={"X-Mew-Canvas-Claim": "1"})
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["claimed"] is True
        assert "already" not in claimed.json()
        persisted = PipelineStore(tmp_path).get_pipeline("g_legacy")
        meta = json.loads(persisted.variables["__orch.payload__"])["meta"]
        assert meta["conversation_id"] == "paper"
        assert persisted.version == graph.version + 1
        scoped = ConversationStore(plans, "paper").get(tmp_path)
        assert scoped is not None and scoped.spec.meta["conversation_id"] == "paper"
        assert "conversation_id" not in (plans.get(tmp_path).spec.meta or {})
        again = await client.post("/api/orchestration/claim-canvas", json=scope,
                                  headers={"X-Mew-Canvas-Claim": "1"})
        assert again.status_code == 200
        assert again.json()["already"] is True


@pytest.mark.asyncio
async def test_foreign_owned_graph_rejects_claim_and_run_state(tmp_path, env):
    current, plans, _ = env
    store = PipelineStore(tmp_path)
    foreign = replace(get_template("linear", goal="别人的图"), meta={"conversation_id": "other-conv"})
    store.create_pipeline(to_pipeline_graph(foreign, graph_id="g_other"))
    scope = {"project": str(tmp_path), "conversation_id": "paper", "graph_id": "g_other"}
    async with _client() as client:
        claimed = await client.post("/api/orchestration/claim-canvas", json=scope,
                                    headers={"X-Mew-Canvas-Claim": "1"})
        assert claimed.status_code == 409
        assert "属于另一个对话" in claimed.json()["error"]
        state = await client.get("/api/orchestration/run-state", params=scope)
        assert state.status_code == 409
        assert "属于另一个对话" in state.json()["error"]
