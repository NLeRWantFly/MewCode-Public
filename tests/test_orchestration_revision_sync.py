from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from mewcode import expert_web
from mewcode.agent import StreamText
from mewcode.conversation import ConversationManager, ToolUseBlock, ToolResultBlock
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowStore
from mewcode.orchestration import OrchestrationSpec, to_expert_flow, to_pipeline_graph
from mewcode.orchestration.plan_session import PlanSessionStore, build_plan_session
from mewcode.orchestration.revisions import (
    CHAT_PLAN_MARKER, STAGE_PLAN_MARKER, execution_fingerprint,
    refresh_chat_plan, refresh_resumed_stage_plan, saved_execution_flow,
)
from tests.test_expert_flow import _profile


def spec_for(model="old-model", goal="旧实验计划", effort="low"):
    return OrchestrationSpec.from_value({
        "spec_id": "editable", "label": "实验流程", "version": 1,
        "require_confirm_gate": False, "finalizer_node_id": "experiment",
        "meta": {"pipeline_graph_id": "g_editable"},
        "nodes": [
            {"node_id": "prepare", "kind": "plan", "label": "准备", "goal": "已完成准备",
             "model": {"provider_key": "p", "model_id": "prepare-model"}},
            {"node_id": "experiment", "kind": "reduce", "label": "修改实验", "goal": goal,
             "model": {"provider_key": "p", "model_id": model, "effort": effort}},
        ],
        "edges": [{"from_node": "prepare", "to_node": "experiment"}],
    })


@pytest.fixture
def stores(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    plans = PlanSessionStore(tmp_path / "plans.json")
    flows = ExpertFlowStore(tmp_path / "flows.json")
    runs = ExpertRunStore(tmp_path / "runs")
    monkeypatch.setattr(server.SESSION, "work_dir", str(project))
    monkeypatch.setattr(server, "get_plan_session_store", lambda: plans)
    monkeypatch.setattr(server, "EXPERT_FLOWS_STORE", flows)
    monkeypatch.setattr(server, "EXPERT_RUNS", runs)
    monkeypatch.setattr(server, "WS_EVENTS", SimpleNamespace(send_json=AsyncMock()))
    return SimpleNamespace(project=project, plans=plans, flows=flows, runs=runs)


def put_plan(stores, spec, conversation_id=""):
    from mewcode.orchestration.conversation_scope import ConversationStore
    ConversationStore(stores.plans, conversation_id).put(stores.project, build_plan_session(spec, draft_plan=True))


@pytest.mark.asyncio
async def test_canvas_save_updates_all_views_without_touching_progress(stores):
    run_id = "a" * 32
    old = spec_for()
    put_plan(stores, old)
    stores.flows.put(stores.project, to_expert_flow(old))
    stores.runs.create(run_id=run_id, conversation_id="c", task="修改实验", attachments=[], profile=_profile().as_dict(),
                      strategy="stable", flow=to_expert_flow(old))
    stores.runs.mutate(run_id, lambda value: value.update(
        stage_cursor="experiment", node_states={"prepare": {"status": "completed", "text": "已完成"},
                                              "experiment": {"status": "paused"}},
        loop_states={"review": {"round": 2, "usage": 300}}))
    before = stores.runs.load(run_id)
    graph = to_pipeline_graph(old, graph_id="g_editable").model_dump(mode="json", by_alias=True)
    node = next(item for item in graph["nodes"] if item["id"] == "experiment")
    node.update(model="new-model", effort="high", goal="新实验计划")
    payload = json.loads(graph["variables"]["__orch.payload__"])
    payload["nodes"]["experiment"]["model"] = {"provider_key": "new-provider", "model_id": "new-model", "effort": "high"}
    graph["variables"]["__orch.payload__"] = json.dumps(payload)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/canvas-sync", json={"project": str(stores.project), "graph": graph})
        assert response.status_code == 200, response.text
        saved = response.json()
        # Save twice so the old payload version differs from the canvas revision.
        response = await client.post("/api/orchestration/canvas-sync", json={"graph": saved["graph"]})
        assert response.status_code == 200, response.text
        saved = response.json()
        current = (await client.get("/api/orchestration/current")).json()
        assert current["flow"] == saved["flow"]
        assert current["control_plan"] == saved["control_plan"]
        assert stores.flows.get(stores.project).as_dict() == saved["flow"]
        assert stores.runs.load(run_id) == before
        assert [run["run_id"] for run in stores.runs.list_for_conversation("c")] == [run_id]
        opened = await client.post("/api/orchestration/open-canvas", json={"graph_id": "g_editable"})
        assert opened.status_code == 200, opened.text
        assert opened.json()["spec"]["version"] == saved["spec"]["version"]
        latest = saved_execution_flow(stores.project, flow_store=stores.flows, plan_store=stores.plans)
        assert execution_fingerprint(latest) == execution_fingerprint(saved["flow"])
        changed = latest.node_map["experiment"]
        assert changed.model.model_id == "new-model"
        assert changed.model.provider_key == "new-provider"
        assert changed.model.effort == "high"
        assert "新实验计划" in changed.responsibility
    assert server.WS_EVENTS.send_json.await_args.args[0]["type"] == "orchestration_saved"


@pytest.mark.asyncio
async def test_old_popup_cannot_write_into_another_project(stores):
    graph = to_pipeline_graph(spec_for(), graph_id="g_editable")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/canvas-sync", json={
            "project": str(stores.project / "different"), "graph": graph.model_dump(mode="json")})
    assert response.status_code == 409
    assert stores.plans.get(stores.project) is None
    assert stores.flows.get(stores.project) is None


@pytest.mark.asyncio
async def test_latest_plan_overrides_legacy_flow_and_invalid_plan_never_falls_back(stores):
    stores.flows.put(stores.project, to_expert_flow(spec_for()))
    put_plan(stores, spec_for("new-model"))
    latest = saved_execution_flow(stores.project, flow_store=stores.flows, plan_store=stores.plans)
    assert latest.node_map["experiment"].model.model_id == "new-model"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        current = (await client.get("/api/orchestration/current")).json()
    assert current["flow"] == latest.as_dict()
    raw = spec_for().as_dict()
    raw["nodes"][1]["model"] = None
    put_plan(stores, OrchestrationSpec.from_value(raw))
    with pytest.raises(ValueError, match="缺少模型绑定"):
        saved_execution_flow(stores.project, flow_store=stores.flows, plan_store=stores.plans)


def test_revision_fingerprint_ignores_layout_but_detects_execution_edits():
    flow = to_expert_flow(spec_for()).as_dict()
    layout = copy.deepcopy(flow)
    layout["version"] = 99
    layout["nodes"][0]["position"] = {"x": 100, "y": 800}
    layout["meta"]["pipeline_graph_version"] = 99
    assert execution_fingerprint(flow) == execution_fingerprint(layout)
    for key, value in [("model", {"provider_key": "p", "model_id": "new", "effort": "high"}),
                       ("responsibility", "新计划")]:
        edited = copy.deepcopy(flow)
        edited["nodes"][1][key] = value
        assert execution_fingerprint(flow) != execution_fingerprint(edited)
    for model, goal, effort in [("old-model", "旧实验计划", "high"), ("old-model", "新计划", "low")]:
        assert execution_fingerprint(flow) != execution_fingerprint(to_expert_flow(spec_for(model, goal, effort)))


def test_plan_reminders_replace_only_own_context_preserving_tool_history(stores):
    conversation = ConversationManager()
    conversation.add_user_message("历史请求")
    conversation.add_assistant_message("", tool_uses=[ToolUseBlock("t", "ReadFile", {"path": "artifact"})])
    conversation.add_tool_results_message([ToolResultBlock("t", "已有实验产物")])
    original = list(conversation.history)
    refresh_resumed_stage_plan(conversation, "即将过期的节点计划")
    refresh_resumed_stage_plan(conversation, "新节点安排")
    assert conversation.history[:3] == original
    assert sum(STAGE_PLAN_MARKER in m.content for m in conversation.history) == 1
    assert "新节点安排" in conversation.history[-1].content
    assert "即将过期的节点计划" not in conversation.history[-1].content
    put_plan(stores, spec_for())
    refresh_chat_plan(conversation, stores.project, plan_store=stores.plans)
    put_plan(stores, spec_for("new-model", "新实验计划"))
    refresh_chat_plan(conversation, stores.project, plan_store=stores.plans)
    assert sum(CHAT_PLAN_MARKER in m.content for m in conversation.history) == 1
    assert "new-model" in conversation.history[-1].content
    assert "新实验计划" in conversation.history[-1].content
    refresh_chat_plan(conversation, stores.project / "other", plan_store=stores.plans)
    assert not any(CHAT_PLAN_MARKER in m.content for m in conversation.history)
    assert conversation.history[:3] == original


@pytest.mark.asyncio
async def test_planning_chat_receives_latest_canvas_without_rewriting_transcript(stores):
    from tests.test_parallel_conversations import _Agent, _SessionHandle, _Socket

    prompts = []

    class PlanningAgent(_Agent):
        async def run(self, conversation):
            prompts.append(copy.deepcopy(conversation.history))
            async for event in super().run(conversation):
                yield event

    agent = PlanningAgent("已按新配置核对")
    durable = _SessionHandle("planning-chat")
    runtime = server.ConversationRuntime(
        owner=SimpleNamespace(session_start=0.0, tasks_payload=lambda: []),
        conversation_id=durable.session_id, scope="project", work_dir=str(stores.project),
        session=durable, conversation=ConversationManager(), agent=agent,
        client=SimpleNamespace(effort=""), slot=None, effort="", context_tier=200_000,
        multi_agent=False, expert_mode=False, goal="", task_manager=SimpleNamespace(),
        trace_manager=SimpleNamespace(), agent_tool=None, worktree_manager=None, team_manager=None,
        orchestration_planning=True,
    )
    runtime.conversation.add_assistant_message("历史方案使用 old-model")
    put_plan(stores, spec_for("new-model", "新实验计划"), "planning-chat")
    await server.run_chat(_Socket(), "按保存后的方案继续讨论", runtime=runtime)
    put_plan(stores, spec_for("latest-model", "最新实验计划"), "planning-chat")
    await server.run_chat(_Socket(), "刚才又改了模型", runtime=runtime)
    for history, model in zip(prompts, ("new-model", "latest-model")):
        reminders = [m for m in history if CHAT_PLAN_MARKER in m.content]
        assert len(reminders) == 1
        assert model in reminders[0].content
        assert any(m.content == "历史方案使用 old-model" for m in history)
    assert not any(CHAT_PLAN_MARKER in m.content for m in durable.messages)


@pytest.mark.asyncio
async def test_running_edit_pauses_at_boundary_and_resume_uses_new_model_and_prompt(stores, monkeypatch):
    put_plan(stores, spec_for())
    stores.flows.put(stores.project, to_expert_flow(spec_for("stale-confirmed-model")))
    calls = []
    events = []
    interrupt_experiment = True

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = 1
        total_output_tokens = 1

        def __init__(self, model):
            self.model = model

        def set_review_authorization_context(self, _context):
            pass

        async def run(self, conversation):
            nonlocal interrupt_experiment
            calls.append((self.model, copy.deepcopy(conversation.history)))
            if self.model.model_id == "prepare-model":
                # User saves while this first model request is in flight.
                put_plan(stores, spec_for("new-model", "新实验计划", "high"))
            elif interrupt_experiment:
                interrupt_experiment = False
                conversation.add_assistant_message("已检查实验产物")
                raise asyncio.CancelledError()
            yield StreamText(text="完成本节点")

    def build_agent(_session, _registry, _role, model, _policy, _holder):
        return FakeAgent(model), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())

    monkeypatch.setattr(expert_web, "_build_role_agent", build_agent)
    durable = SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda m: None,
                              append_expert_run=lambda *a, **kw: None)
    session = SimpleNamespace(work_dir=str(stores.project), conversation_id="c", multi_agent=False,
        session=durable, conversation=ConversationManager(),
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_run_id="", expert_control_action="pause", expert_child_event_callback=None,
        status_payload=lambda: {}, tasks_payload=lambda: [])
    registry = SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}), resolve=lambda ref: SimpleNamespace(ref=ref))
    kwargs = dict(session=session, registry_service=registry, profile_store=SimpleNamespace(get=lambda p: _profile()),
                  flow_store=stores.flows, plan_store=stores.plans, run_store=stores.runs)
    ws = SimpleNamespace(send_json=AsyncMock(side_effect=lambda event: events.append(event)))
    await expert_web.run_expert_chat(ws, "修改实验", confirmed_new_flow=to_expert_flow(spec_for()), **kwargs)
    run_id = next(e["run_id"] for e in events if e["type"] == "done")
    paused = stores.runs.load(run_id)
    assert paused["status"] == "paused", events
    assert paused["plan_revision_pending"] is True
    assert paused["node_states"]["prepare"]["status"] == "completed"
    assert [model.model_id for model, _ in calls] == ["prepare-model"]
    assert paused["locked_flow"]["nodes"][1]["model"]["model_id"] == "old-model"
    await expert_web.run_expert_chat(ws, "继续", resume_run_id=run_id, **kwargs)
    second = stores.runs.load(run_id)
    assert second["status"] == "paused", events
    assert second["node_states"]["prepare"]["status"] == "completed"
    assert calls[-1][0].model_id == "new-model"
    assert calls[-1][0].effort == "high"
    assert any("新实验计划" in m.content for m in calls[-1][1])
    put_plan(stores, spec_for("latest-model", "继续核验表格一致性", "medium"))
    await expert_web.run_expert_chat(ws, "继续", resume_run_id=run_id, **kwargs)
    finished = stores.runs.load(run_id)
    assert finished["status"] == "completed", events
    assert [m.model_id for m, _ in calls] == ["prepare-model", "new-model", "latest-model"]
    assert calls[-1][0].effort == "medium"
    assert any("已检查实验产物" in m.content for m in calls[-1][1])
    reminders = [m for m in calls[-1][1] if STAGE_PLAN_MARKER in m.content]
    assert len(reminders) == 1
    assert "继续核验表格一致性" in reminders[0].content
    assert finished["node_states"]["prepare"]["status"] == "completed"
    assert "plan_revision_pending" not in finished
    points = stores.runs.checkpoint_archive.list(run_id)
    assert len([point for point in points if point["kind"] == "turn_end"]) == 3
    assert any(point["kind"] == "node_end" and point["node_id"] == "prepare" for point in points)
    assert any(point["kind"] == "node_end" and point["node_id"] == "experiment" for point in points)
    running_events = [e for e in events if e["type"] == "expert_run_state" and e["status"] == "running"]
    assert [e["attempt"] for e in running_events] == [1, 2, 3]
    assert all("attempt" in e and "node_attempt" in e for e in events if e.get("node_id"))


@pytest.mark.asyncio
async def test_running_chat_input_reaches_current_and_next_nodes_with_file_receipts(stores, monkeypatch):
    from mewcode.agent import ToolUseEvent, ToolResultEvent, TurnComplete
    from mewcode.permissions.sandbox import PathSandbox
    from mewcode.orchestration.interventions import enqueue_intervention
    put_plan(stores, spec_for())
    events, seen = [], []
    output = stores.project / "process.md"

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = 1
        total_output_tokens = 1
        permission_checker = SimpleNamespace(sandbox=PathSandbox(str(stores.project)))

        def __init__(self, model):
            self.model = model

        def set_review_authorization_context(self, context):
            pass

        async def run(self, conversation):
            if self.model.model_id == "prepare-model":
                stores.runs.mutate(session.expert_run_id, lambda value: enqueue_intervention(value, "during-run", "补充误差分析"))
                yield TurnComplete(turn=1)
                assert "补充误差分析" in conversation.history[-1].content
                yield ToolUseEvent(tool_id="write-1", tool_name="WriteFile", arguments={"file_path": str(output)})
                output.write_text("verified output", encoding="utf-8")
                yield ToolResultEvent(tool_id="write-1", tool_name="WriteFile", output="ok", is_error=False, elapsed=.01)
            seen.append(copy.deepcopy(conversation.history))
            yield StreamText(text="完成本节点")

    monkeypatch.setattr(expert_web, "_build_role_agent", lambda s, r, role, model, policy, holder:
        (FakeAgent(model), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())))
    durable = SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda m: None,
                              append_expert_run=lambda *a, **kw: None)
    session = SimpleNamespace(work_dir=str(stores.project), conversation_id="c", multi_agent=False,
        session=durable, conversation=ConversationManager(),
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_run_id="", expert_control_action="pause", expert_child_event_callback=None,
        status_payload=lambda: {}, tasks_payload=lambda: [])
    await expert_web.run_expert_chat(SimpleNamespace(send_json=AsyncMock(side_effect=lambda event: events.append(event))),
        "修改实验", session=session,
        confirmed_new_flow=to_expert_flow(spec_for()),
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}), resolve=lambda ref: SimpleNamespace(ref=ref)),
        profile_store=SimpleNamespace(get=lambda p: _profile()), flow_store=stores.flows,
        plan_store=stores.plans, run_store=stores.runs)
    run_id = next(event["run_id"] for event in events if event["type"] == "done")
    snapshot = stores.runs.load(run_id)
    assert snapshot["status"] == "completed", events
    assert len(seen) == 2
    assert all(any("补充误差分析" in str(message.content) for message in history) for history in seen)
    assert {target["node_id"] for target in snapshot["intervention_receipts"][0]["delivered_to"]} == {"prepare", "experiment"}
    assert snapshot["process_files"][0]["preview"] == "verified output"
    assert snapshot["process_file_receipts"][0]["node_id"] == "prepare"
    assert "process_files" not in stores.runs.public(run_id)


@pytest.mark.asyncio
async def test_temporary_takeover_returns_through_real_scheduler_and_preserves_done_nodes(stores, monkeypatch):
    from mewcode.orchestration.takeover import TakeoverController
    put_plan(stores, spec_for())
    events, calls = [], []
    active = asyncio.Event()
    first_work = True

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = 1
        total_output_tokens = 1

        def __init__(self, model):
            self.model = model

        def set_review_authorization_context(self, context):
            pass

        async def run(self, conversation):
            nonlocal first_work
            calls.append((self.model.model_id, copy.deepcopy(conversation.history)))
            if self.model.model_id == "old-model" and first_work:
                first_work = False
                conversation.add_assistant_message("实验进度已保留")
                active.set()
                await asyncio.Event().wait()
            yield StreamText(text="完成本节点")

    monkeypatch.setattr(expert_web, "_build_role_agent", lambda s, r, role, model, policy, holder:
        (FakeAgent(model), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())))
    session = SimpleNamespace(work_dir=str(stores.project), conversation_id="c", multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda m: None,
                                append_expert_run=lambda *a, **kw: None), conversation=ConversationManager(),
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_run_id="", expert_control_action="pause", expert_child_event_callback=None,
        status_payload=lambda: {}, tasks_payload=lambda: [])
    kwargs = dict(session=session,
        confirmed_new_flow=to_expert_flow(spec_for()),
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}), resolve=lambda ref: SimpleNamespace(ref=ref)),
        profile_store=SimpleNamespace(get=lambda p: _profile()), flow_store=stores.flows,
        plan_store=stores.plans, run_store=stores.runs)
    ws = SimpleNamespace(send_json=AsyncMock(side_effect=events.append))
    controller = TakeoverController(session, stores.runs, ws)

    async def main(ws, runtime, snapshot, item):
        assert snapshot["status"] == "paused"
        assert snapshot["node_states"]["prepare"]["status"] == "completed"
        assert snapshot["node_states"]["experiment"]["status"] == "paused"
        assert item["text"] == "先帮我核对统计口径"
        return {"action": "resume", "relation": "context_changed", "summary": "口径已核对，继续实验",
                "resume_context": "采用新统计口径"}

    task = asyncio.create_task(controller.run("修改实验", flow_runner=expert_web.run_expert_chat,
                                              dialogue_runner=main, kwargs=kwargs))
    # Startup includes durable checkpoint writes; leave room for Windows disk
    # contention while keeping both waits bounded independently of run budgets.
    await asyncio.wait_for(active.wait(), 30)
    run_id = session.expert_run_id
    controller.submit("temporary-analysis", "先帮我核对统计口径")
    await asyncio.wait_for(task, 30)
    final = stores.runs.load(run_id)
    assert final["status"] == "completed", events
    assert [model for model, _ in calls] == ["prepare-model", "old-model", "old-model"]
    assert any("实验进度已保留" in str(m.content) for m in calls[-1][1])
    assert any("采用新统计口径" in str(m.content) for m in calls[-1][1])
    statements = [m for m in calls[-1][1] if "先帮我核对统计口径" in str(m.content)]
    assert statements and all('用户授权与表态' in str(m.content) or '已记录为用户表态' in str(m.content) for m in statements)
    assert len([event for event in events if event["type"] == "done"]) == 1


def test_frontend_projection_preserves_real_progress_and_rejects_stale_saves():
    source = (Path(__file__).resolve().parents[1] / "frontend/static/app.js").read_text(encoding="utf-8")
    helpers = []
    for name in ("orchestrationProjectKey", "applySavedOrchestration", "projectOrchestrationBoard"):
        start = source.index("function " + name + "(")
        helpers.append(source[start:source.index("\n  function ", start + 1)])
    script = "\n".join(helpers) + r'''
const assert = require('assert');
const savedOrchestrations = new Map(), orchStageBriefs = new Map();
let orchBoardGroups = {}, renders = 0;
const currentStatus = {work_dir: 'C:\\Project'};
const activeConversationId = () => 'conv';
const cacheOrchStageBriefs = () => {}, renderOrchStageBoard = () => renders++;
const flow = {flow_id: 'editable', nodes: [{node_id: 'experiment', model: {model_id: 'old'}}, {node_id:'prepare'}], meta: {pipeline_graph_id: 'g'}};
const current = {flow, runId: 'real-run', status: 'paused', statuses: new Map([['prepare', {status:'completed'}], ['experiment', {status:'paused'}]]), members: new Map(), handoffs: new Set(), loopStates: new Map([['loop', {round:2}]])};
const saved = {project:'c:/project', conversation_id:'conv', graph_id:'g', spec:{version:2}, flow:{...flow, nodes:[{node_id:'experiment', model:{model_id:'new', effort:'high'}}, {node_id:'prepare'}]}};
assert(applySavedOrchestration(saved));
const projected = projectOrchestrationBoard(current, savedOrchestrations.get(JSON.stringify(['c:/project','conv'])));
assert.strictEqual(projected.statuses, current.statuses);
assert.strictEqual(projected.loopStates, current.loopStates);
assert.strictEqual(projected.statuses.get('prepare').status, 'completed');
assert.strictEqual(projected.flow.nodes[0].model.model_id, 'new');
assert.strictEqual(projected.flow.nodes[0].model.effort, 'high');
assert.strictEqual(current.flow.nodes[0].model.model_id, 'old');
assert.strictEqual(projected.runId, 'real-run');
assert.strictEqual(projected.executionFlow, flow);
const replacement = projectOrchestrationBoard(current, {flow:{...saved.flow, nodes:[{node_id:'new_entry'}, saved.flow.nodes[0]]}});
assert.strictEqual(replacement.statuses.size, 0);
assert.strictEqual(replacement.loopStates.size, 0);
assert.strictEqual(replacement.runId, 'stored');
assert.strictEqual(current.statuses.get('prepare').status, 'completed');
assert(!applySavedOrchestration({...saved, spec:{version:1}}));
assert(!applySavedOrchestration({...saved, project:'c:/other', spec:{version:3}}));
assert.strictEqual(renders, 1);
for (const flow of [{...saved.flow, flow_id:'different'},
  {...saved.flow, meta:{pipeline_graph_id:'different'}}]) {
  const preview = projectOrchestrationBoard(current, {flow});
  assert.strictEqual(preview.flow, flow);
  assert.strictEqual(preview.runId, 'stored');
  assert.strictEqual(preview.previousRunId, current.runId);
  assert.strictEqual(preview.statuses.size, 0);
  assert.strictEqual(preview.loopStates.size, 0);
}
assert.strictEqual(current.flow, flow);
assert.strictEqual(current.statuses.get('prepare').status, 'completed');
'''
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert 'case "orchestration_saved":' in source
    root_listener = source.split('expertFlowRuntimeChannel?.addEventListener("message"', 1)[1].split("const savedOrchestrations", 1)[0]
    assert '"orchestration-canvas-sync"' in root_listener
    assert "applySavedOrchestration(message)" in root_listener


def test_canvas_save_repaints_actual_conversation_board_with_new_model():
    source = (Path(__file__).resolve().parents[1] / "frontend/static/app.js").read_text(encoding="utf-8")
    helpers = []
    for name in ("orchestrationProjectKey", "applySavedOrchestration", "projectOrchestrationBoard", "renderRecoveryCaseCards"):
        start = source.index("function " + name + "(")
        helpers.append(source[start:source.index("\n  function ", start + 1)])
    start = source.index("function renderOrchStageBoard(")
    helpers.append(source[start:source.index('\n  orchBoardEl?.querySelector', start)])
    script = "\n".join(helpers) + r'''
const assert = require('assert');
class Element {
  constructor() {
    this.children = []; this.textContent = '';
    this.classList = {add() {}, remove() {}, toggle() {}};
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  replaceChildren(...children) { this.children = children; }
}
const document = {createElement: () => new Element()};
const orchBoardEl = new Element(), orchBoardStagesEl = new Element();
const orchBoardGoalEl = new Element(), orchBoardPositionEl = new Element(), orchBoardCountsEl = new Element();
const $ = () => null, expertTimelines = new Map();
let activeId = 'conv';
const activeConversationId = () => activeId;
const currentStatus = {work_dir: 'C:\\Project', expert_mode: false};
const savedOrchestrations = new Map(), orchStageBriefs = new Map();
let orchBoardGroups = {}, orchBoardTabUserPinned = false, orchBoardCollapsed = false;
const orchStepLights = [], renderOrchBoardLive = () => {};
const cacheOrchStageBriefs = () => {}, ensureOrchStageBriefs = () => {};
const pickExpertFlowForBoard = states => states[0];
const flattenOrchBoardNodes = flow => flow.nodes.map(node => ({node, depth: 0}));
const orchBoardStatusOf = (current, id) => current.statuses.get(id)?.status || 'pending';
const orchBoardGroupStatus = (current, flow, id) => orchBoardStatusOf(current, id);
const ORCH_BOARD_STATUS_LABELS = {completed: '已完成', paused: '已暂停'};
const displayOrchModelId = value => value, buildExpertRetryButton = () => null;
const abbreviateOrchTask = value => value, setOrchBoardTab = () => {};
const flow = {flow_id: 'editable', meta: {pipeline_graph_id: 'g'}, nodes: [
  {node_id: 'prepare', model: {model_id: 'prepare-model'}},
  {node_id: 'experiment', model: {model_id: 'old-model', effort: 'low'}}
]};
const current = {runId: 'real-run', flow, status: 'paused', statuses: new Map([
  ['prepare', {status: 'completed'}],
  ['experiment', {status: 'paused', model: 'old-model', effort: 'low'}]
])};
const pipelineStates = new Map([['conv', {expertFlows: new Map([['real-run', current]])}]]);
const textOf = el => [el.textContent, ...el.children.map(textOf)].join(' ');
renderOrchStageBoard();
assert(textOf(orchBoardStagesEl).includes('old-model'));
const saved = {project: 'c:/project', conversation_id: 'conv', graph_id: 'g', spec: {version: 2},
  flow: {...flow, nodes: [flow.nodes[0], {node_id: 'experiment', model: {model_id: 'new-model', effort: 'high'}}]}};
assert(applySavedOrchestration(saved)); // Calls the real renderer, not a render stub.
assert(textOf(orchBoardStagesEl).includes('new-model · high'));
assert(textOf(orchBoardStagesEl).includes('本次记录：old-model · low'));
assert(textOf(orchBoardStagesEl).includes('已完成'));
assert(textOf(orchBoardStagesEl).includes('已暂停'));
assert(orchBoardPositionEl.textContent.includes('新版配置'));
assert.strictEqual(current.flow, flow);
assert.strictEqual(current.flow.nodes[1].model.model_id, 'old-model');
assert.strictEqual(pipelineStates.get('conv').expertFlows.size, 1);
renderOrchStageBoard(); // Later runtime repaints must not revert the saved model.
assert(textOf(orchBoardStagesEl).includes('new-model · high'));
activeId = 'other';
renderOrchStageBoard();
assert.strictEqual(orchBoardStagesEl.children.length, 0);
activeId = 'conv';
renderOrchStageBoard();
assert(textOf(orchBoardStagesEl).includes('new-model · high'));
'''
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
