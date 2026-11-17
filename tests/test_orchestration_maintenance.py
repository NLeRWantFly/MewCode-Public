"""Maintenance changes stay narrow, durable, versioned and outside chat."""
import asyncio
import base64
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from frontend import orch_maintenance, server
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowStore
from mewcode.memory.session import SessionManager
from mewcode.orchestration.consistent_save import recover_pending_save, save_consistently
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.maintenance import (
    SCHEMA, MaintenanceStore, parse_maintenance_command, preview_proposal, project_key, resolve_targets,
)
from mewcode.orchestration.maintenance_revision import future_revision
from mewcode.orchestration.pipeline_bridge import from_pipeline_graph, to_pipeline_graph
from mewcode.orchestration.plan_session import PlanSessionStore, build_plan_session
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.pipeline.store import PipelineStore, VersionConflictError


def spec_for(cid="test", graph_id="maintenance-test"):
    return OrchestrationSpec.from_value({"spec_id": "maintenance", "label": "维护测试", "goal": "核验后交付",
        "require_confirm_gate": False, "finalizer_node_id": "done",
        "nodes": [{"node_id": identity, "label": identity, "kind": kind, "goal": "保留验收和成果",
            "model": {"provider_key": "selected", "model_id": "selected-model"},
            "plan_brief": {"summary": "原有要求", "outputs": ["交付文件"], "acceptance": ["验证通过"]}}
            for identity, kind in (("first", "implement"), ("work", "implement"), ("done", "reduce"))],
        "edges": [{"from_node": "first", "to_node": "work"}, {"from_node": "work", "to_node": "done"}],
        "meta": {"conversation_id": cid, "pipeline_graph_id": graph_id}})


def update_proposal(identity="work", **changes):
    return {"summary": "更新该环节的具体要求", "node_changes": [{"node_id": identity, "changes": changes or {"goal": "新增核验要求"}}]}


@pytest.mark.parametrize("text,expected", [("/fix", {"mode": "fix", "request": ""}),
    (" /UPDATE work 增加核验\n保留原稿 ", {"mode": "update", "request": "work 增加核验\n保留原稿"}),
    ("讨论 /fix 命令", None), ("/fixer 改图", None), ("/update-now", None), (None, None)])
def test_commands_are_anchored(text, expected):
    assert parse_maintenance_command(text) == expected


def test_targets_validate_before_hashing_and_respect_direction():
    spec = spec_for()
    assert resolve_targets(spec, "update", "增加核验", stage_cursor="work") == (["work"], "增加核验")
    assert resolve_targets(spec, "update", "当前方向调整", stage_cursor="work")[0] == []
    assert resolve_targets(spec, "fix", "work 修依赖") == (["work"], "work 修依赖")
    for bad in ([[{}]], ["missing"], ["work", "work"]):
        with pytest.raises(ValueError):
            resolve_targets(spec, "fix", node_ids=bad)


@pytest.mark.parametrize("changes", [{"model": {}}, {"tools": ["Bash"]}, {"max_audit_retries": 999},
    {"kind": "implement"}, {"plan_brief": {"acceptance": []}}, {"plan_brief": {"unknown": "x"}}, {"goal": ""}])
def test_updates_cannot_change_execution_authority_or_empty_requirements(changes):
    with pytest.raises(ValueError):
        preview_proposal(spec_for().as_dict(), "update", ["work"], update_proposal(**changes))


def test_update_scope_and_completed_impact_are_explicit():
    spec = spec_for().as_dict()
    proposal = preview_proposal(spec, "update", ["work"], update_proposal(goal="新增逐项验证", plan_brief={"steps": ["核验每项"]}))
    assert proposal["affected_node_ids"] == ["done", "work"]
    assert proposal["spec"]["nodes"][0] == spec["nodes"][0]
    assert proposal["spec"]["nodes"][1]["model"] == spec["nodes"][1]["model"]
    assert proposal["spec"]["nodes"][1]["plan_brief"]["acceptance"] == ["验证通过"]
    with pytest.raises(ValueError):
        preview_proposal(spec, "update", ["first"], update_proposal())
    with pytest.raises(ValueError):
        preview_proposal(spec, "update", ["work"], {"summary": "换方向", "goal": "新方向"})


def test_fix_rejects_gate_bypass_and_undeclared_cycles():
    raw = spec_for().as_dict()
    raw["nodes"].insert(2, {"node_id": "approval", "label": "人工验收", "kind": "gate"})
    raw["edges"] = [{"from_node": "first", "to_node": "work"}, {"from_node": "work", "to_node": "approval"},
                    {"from_node": "approval", "to_node": "done"}]
    with pytest.raises(ValueError, match="门禁"):
        preview_proposal(raw, "fix", [], {"summary": "旁路", "edges": [*raw["edges"], {"from_node": "work", "to_node": "done"}]})
    with pytest.raises(ValueError):
        preview_proposal(raw, "fix", [], {"summary": "循环", "edges": [*raw["edges"], {"from_node": "work", "to_node": "first"}]})
    with pytest.raises(ValueError):
        preview_proposal(raw, "fix", [], update_proposal(kind="implement"))


def test_store_reopens_isolated_history_and_refuses_late_stale_outputs(tmp_path):
    store = MaintenanceStore(tmp_path / "maintenance.sqlite")
    spec = spec_for().as_dict()
    state = store.open(tmp_path, "test", spec, mode="update", node_ids=["work"])
    running = store.begin(tmp_path, "test", state["id"], revision=0, message="增加核验", base=spec)
    assert MaintenanceStore(store.path).open(tmp_path, "test", spec, mode="update", node_ids=["work"])["messages"] == running["messages"]
    with pytest.raises(ValueError):
        store.get(tmp_path, "other", state["id"])
    stopped = store.fail(tmp_path, "test", state["id"], call_id=running["call_id"], error="stopped")
    response = {"schema": SCHEMA, "reply": "这是待应用修订", "proposal": update_proposal()}
    with pytest.raises(ValueError, match="迟到"):
        store.complete(tmp_path, "test", state["id"], call_id=running["call_id"], response=response, base=spec)
    rerun = store.begin(tmp_path, "test", state["id"], revision=stopped["revision"], message="重新核对", base=spec)
    changed = copy.deepcopy(spec)
    changed["version"] += 1
    finished = store.complete(tmp_path, "test", state["id"], call_id=rerun["call_id"], response=response, base=changed)
    assert finished["status"] == "stale" and finished["proposal"] is None
    assert len(finished["messages"]) == 3 and all(m["purpose"] == "orchestration_maintenance" for m in finished["messages"])


def test_consistent_save_rolls_back_late_flow_failure_and_recovers_crash(tmp_path, monkeypatch):
    plans, flows, pipeline = PlanSessionStore(tmp_path / "plans.json"), ExpertFlowStore(tmp_path / "flows.json"), PipelineStore(str(tmp_path))
    spec = spec_for()
    graph = pipeline.create_pipeline(to_pipeline_graph(spec, graph_id="maintenance-test"))
    plans.put(tmp_path, build_plan_session(spec))
    flows.put(tmp_path, to_expert_flow(spec))
    original = plans.get(tmp_path).as_dict()
    old_graph = pipeline.get_pipeline(graph.graph_id).model_dump(mode="json")
    def fail(*args):
        raise OSError("late write failed")
    monkeypatch.setattr(flows, "put", fail)
    with pytest.raises(OSError, match="late write"):
        save_consistently(tmp_path, spec=replace(spec, goal="新目标"), graph=graph, pipeline_store=pipeline,
            plan_store=plans, flow_store=flows, previous=plans.get(tmp_path))
    assert plans.get(tmp_path).as_dict() == original
    assert pipeline.get_pipeline(graph.graph_id).model_dump(mode="json") == old_graph
    paths = [tmp_path / ".mewcode/pipelines/maintenance-test.json", plans.path, flows.path]
    backup = [base64.b64encode(p.read_bytes()).decode() for p in paths]
    journal = plans.path.with_name("." + plans.path.name + ".orchestration-commit.json")
    journal.write_text(json.dumps({"graph_id": graph.graph_id, "project": project_key(tmp_path),
                                  "paths": [str(p.resolve()) for p in paths], "backup": backup}))
    paths[0].write_text("incomplete")
    recover_pending_save(tmp_path, plan_store=plans, flow_store=flows)
    assert pipeline.get_pipeline(graph.graph_id).model_dump(mode="json") == old_graph and not journal.exists()


def test_consistent_save_has_one_revision_in_all_three_representations(tmp_path):
    plans, flows, pipeline = PlanSessionStore(tmp_path / "plans.json"), ExpertFlowStore(tmp_path / "flows.json"), PipelineStore(str(tmp_path))
    spec = spec_for()
    graph = pipeline.create_pipeline(to_pipeline_graph(spec, graph_id="maintenance-test"))
    saved, canonical, flow, _ = save_consistently(tmp_path, spec=replace(spec, goal="新目标"), graph=graph,
        pipeline_store=pipeline, plan_store=plans, flow_store=flows)
    assert saved.version == canonical.version == flow.version == plans.get(tmp_path).spec.version == 2
    assert from_pipeline_graph(saved).meta["pipeline_graph_version"] == canonical.meta["pipeline_graph_version"] == 2
    with pytest.raises(VersionConflictError):
        save_consistently(tmp_path, spec=spec, graph=graph, pipeline_store=pipeline, plan_store=plans, flow_store=flows)


@pytest.mark.parametrize("blocker", ["completed", "receipt", "pipeline"])
def test_run_revision_never_replays_completed_or_unknown_work(blocker):
    spec = spec_for()
    snapshot = {"status": "paused", "locked_flow": to_expert_flow(spec).as_dict(), "node_states": {
        "first": {"status": "completed", "text": "成果"}, "work": {"status": "failed"}}, "command_receipts": {}}
    if blocker == "completed":
        snapshot["node_states"]["work"]["status"] = "completed"
    if blocker == "receipt":
        snapshot["node_states"]["work"]["command_receipts"] = [{"request_id": "unknown", "command_started": None}]
    if blocker == "pipeline":
        snapshot["pipeline_runs"] = {"work": {"status": "unknown"}}
    original = copy.deepcopy(snapshot)
    with pytest.raises(ValueError):
        future_revision(snapshot, spec, affected=["work"], maintenance_id="repair")
    assert snapshot == original


def test_run_revision_preserves_budget_identity_models_receipts_and_artifacts():
    spec = spec_for()
    snapshot = {"status": "paused", "locked_flow": to_expert_flow(spec).as_dict(), "host_execution_scope": {"task_id": "task"},
        "node_states": {"first": {"status": "completed", "text": "原成果", "command_receipts": [{"result": "known"}]},
                        "work": {"status": "pending"}}, "loop_states": {}, "budget": {"tokens": 2000},
        "locked_models": {"work": {"model_id": "selected-model"}}, "gate_confirmations": {"old": True}}
    changed = OrchestrationSpec.from_value(preview_proposal(spec.as_dict(), "update", ["work"], update_proposal())["spec"])
    result = future_revision(snapshot, changed, affected=["work"], maintenance_id="repair")
    for key in ("host_execution_scope", "budget", "locked_models", "gate_confirmations"):
        assert result[key] == snapshot[key]
    assert result["node_states"]["first"] == snapshot["node_states"]["first"] and result["status"] == "paused"


@pytest.fixture
def maintenance_env(tmp_path, monkeypatch):
    from mewcode.agent import Agent
    from mewcode.agents.task_manager import TaskManager
    from mewcode.client import LLMClient
    from mewcode.conversation import ConversationManager
    from mewcode.tools import ToolRegistry
    from mewcode.tools.base import TextDelta, StreamEnd
    from mewcode.orchestration.maintenance import model_prompt
    durable = SessionManager(str(tmp_path)).create()
    cid = durable.session_id
    owner = {"project": str(tmp_path), "conversation_id": cid}
    current = {"cid": cid, "busy": False}
    monkeypatch.setattr(server.SESSION, "session", durable)
    monkeypatch.setattr(server.SESSION, "work_dir", str(tmp_path))
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _: None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda *a: current["busy"])
    monkeypatch.setattr(server.SESSION, "task_manager", TaskManager())
    monkeypatch.setattr(server.SESSION, "_agent_tool", None)
    monkeypatch.setattr(server.SESSION, "_conversation_store_dir", lambda: str(tmp_path))
    # Existing graph/storage tests keep their scripted model transport while
    # exercising the real interactive Agent + TaskManager lifecycle for Fix.
    def scripted_fix_agent(*, client, state, diagnostics, **kwargs):
        class ScriptedClient(LLMClient):
            async def stream(self, *args, **kw):
                raw = await orch_maintenance.collect_model_text(client, system="fixture",
                    user_text=model_prompt(state, diagnostics))
                yield TextDelta(text=raw)
                yield StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)
        agent = Agent(ScriptedClient(), ToolRegistry(), "anthropic", work_dir=str(tmp_path), stall_policy=False)
        agent.session_id = state["conversation_id"]
        conversation = ConversationManager()
        conversation.add_user_message(state["messages"][-1]["text"])
        return agent, conversation
    monkeypatch.setattr(orch_maintenance, "build_fix_agent", scripted_fix_agent)
    monkeypatch.setattr(orch_maintenance, "build_maintenance_agent", scripted_fix_agent)
    monkeypatch.setattr(server, "_active_conversation_id", lambda: current["cid"])
    plans, flows = PlanSessionStore(tmp_path / "plans.json"), ExpertFlowStore(tmp_path / "flows.json")
    monkeypatch.setattr(server, "get_plan_session_store", lambda: plans)
    monkeypatch.setattr(server, "EXPERT_FLOWS_STORE", flows)
    monkeypatch.setattr(server, "EXPERT_RUNS", ExpertRunStore(tmp_path / "runs"))
    monkeypatch.setattr(server, "MAINTENANCE_STORE", MaintenanceStore(tmp_path / "maintenance.sqlite"), raising=False)
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    from mewcode.sandbox import native_recovery
    monkeypatch.setattr(native_recovery, "diagnose_native_runtime", lambda: {"ready": True, "reason": "fixture"})
    monkeypatch.setattr(server, "_orchestration_attempt_context", lambda: {"model": "selected-model", "requested_effort": "high"})
    monkeypatch.setattr(server, "_new_orchestration_client", lambda context, effort: (context["model"], effort))
    spec = spec_for(cid)
    server._conversation_plan_store(cid).put(tmp_path, build_plan_session(spec))
    server._conversation_flow_store(cid).put(tmp_path, to_expert_flow(spec))
    PipelineStore(str(tmp_path)).create_pipeline(to_pipeline_graph(spec, graph_id="maintenance-test"))
    app = FastAPI()
    orch_maintenance.install(app, server)
    return app, owner, current, durable, spec


async def poll(client, owner, state):
    for _ in range(100):
        response = await client.get("/api/orchestration/maintenance/state", params={**owner, "id": state["id"]})
        assert response.status_code == 200, response.text
        latest = response.json()
        if latest["status"] != "running":
            return latest
        await asyncio.sleep(.01)
    pytest.fail("maintenance did not finish")


@pytest.mark.asyncio
async def test_api_update_is_independent_until_explicit_apply(maintenance_env, monkeypatch):
    app, owner, current, durable, spec = maintenance_env
    calls = []
    history = durable._sessions_dir / (durable.session_id + ".jsonl")
    original = history.read_bytes() if history.exists() else None
    async def model(client, **kwargs):
        assert client == ("selected-model", "high")
        data = json.loads(kwargs["user_text"])
        calls.append(data)
        assert data["allowed_node_ids"] == ["work"] and data["history"][-1]["text"] == "新增核验要求"
        return json.dumps({"schema": SCHEMA, "reply": "已核对图和宿主诊断，等待应用。", "proposal": update_proposal()})
    monkeypatch.setattr(orch_maintenance, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "update", "request": "work 新增核验要求"})).json()
        assert state["initial_message"] == "新增核验要求" and not calls
        current["busy"] = True  # Maintenance does not cancel or enqueue the business run.
        response = await client.post("/api/orchestration/maintenance/send", json={**owner, "id": state["id"], "revision": state["revision"], "message": state["initial_message"]})
        assert response.status_code == 200, response.text
        done = await poll(client, owner, state)
        assert done["proposal"] and "spec" not in done["proposal"]
        assert server._conversation_plan_store().get(owner["project"]).spec.as_dict() == spec.as_dict()
        apply = {**owner, "id": state["id"], "revision": done["revision"], "proposal_id": done["proposal"]["id"]}
        assert (await client.post("/api/orchestration/maintenance/apply", json=apply)).status_code == 409
        current["busy"] = False
        response = await client.post("/api/orchestration/maintenance/apply", json=apply)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "applied" and result["result"]["spec"]["version"] == 2
        assert server._conversation_plan_store().get(owner["project"]).spec.node_map()["work"].goal == "新增核验要求"
        assert (await client.post("/api/orchestration/maintenance/apply", json=apply)).status_code == 409
    assert len(calls) == 1
    assert (history.read_bytes() if history.exists() else None) == original


@pytest.mark.asyncio
async def test_stop_only_cancels_owned_maintenance_and_reopens_messages(maintenance_env, monkeypatch):
    app, owner, current, durable, spec = maintenance_env
    entered = asyncio.Event()
    async def model(*a, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(orch_maintenance, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix"})).json()
        request = {**owner, "id": state["id"], "revision": state["revision"], "message": "修依赖"}
        started = (await client.post("/api/orchestration/maintenance/send", json=request)).json()
        await asyncio.wait_for(entered.wait(), 5)
        current["busy"] = True
        wrong = await client.post("/api/orchestration/maintenance/stop", json={**owner, "conversation_id": "foreign", "id": state["id"], "revision": started["revision"]})
        assert wrong.status_code == 409
        stopped = await client.post("/api/orchestration/maintenance/stop", json={**owner, "id": state["id"], "revision": started["revision"]})
        assert stopped.status_code == 200, stopped.text
        await asyncio.sleep(.01)
        reopened = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix"})).json()
        assert reopened["id"] == state["id"] and reopened["messages"][0]["text"] == "修依赖"
        assert reopened["status"] == "ready" and current["busy"] is True
        assert server._conversation_plan_store().get(owner["project"]).spec.as_dict() == spec.as_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["conversation", "graph"])
async def test_late_model_result_cannot_cross_owner_or_graph_revision(maintenance_env, monkeypatch, change):
    app, owner, current, durable, spec = maintenance_env
    entered, release = asyncio.Event(), asyncio.Event()
    async def model(*a, **kwargs):
        entered.set()
        await release.wait()
        return json.dumps({"schema": SCHEMA, "reply": "迟到结果", "proposal": update_proposal()})
    monkeypatch.setattr(orch_maintenance, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "update"})).json()
        started = await client.post("/api/orchestration/maintenance/send", json={**owner, "id": state["id"], "revision": 0, "message": "改内容"})
        assert started.status_code == 200, started.text
        await asyncio.wait_for(entered.wait(), 5)
        if change == "conversation":
            current["cid"] = "other"
        else:
            server._conversation_plan_store().put(owner["project"], build_plan_session(replace(spec, version=2)))
        release.set()
        for _ in range(100):
            stored = server.MAINTENANCE_STORE.get(owner["project"], owner["conversation_id"], state["id"])
            if stored["status"] != "running":
                break
            await asyncio.sleep(.01)
        assert stored["proposal"] is None and stored["status"] in {"ready", "stale"}
        current["cid"] = owner["conversation_id"]
        response = await client.post("/api/orchestration/maintenance/apply", json={**owner, "id": state["id"], "revision": stored["revision"], "proposal_id": "late"})
        assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/chat/queue", "/api/chat/interventions", "/api/orchestration/interventions"])
async def test_maintenance_commands_bypass_all_business_queues(maintenance_env, path):
    _, owner, current, durable, _ = maintenance_env
    current["busy"] = True
    history = durable._sessions_dir / (durable.session_id + ".jsonl")
    original = history.read_bytes() if history.exists() else None
    route = {"/api/chat/queue": server.api_chat_queue, "/api/chat/interventions": server.api_chat_intervention,
             "/api/orchestration/interventions": server.api_orchestration_intervention}[path]
    app = FastAPI()
    app.post(path)(route)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, headers={"X-Mew-Chat-Queue": "1"}, json={"conversation_id": owner["conversation_id"],
            "queue_id": "maintenance-queue-test", "text": "/fix work 修依赖", "attachments": []})
        assert response.status_code == 200, response.text
        assert response.json()["maintenance"] == {"mode": "fix", "request": "work 修依赖"}
        assert response.json()["prompt"] == ""
        assert (history.read_bytes() if history.exists() else None) == original and current["busy"] is True
