"""Host identity, interrupted commits and stopped-run maintenance acceptance."""
import copy
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import orch_maintenance, server
from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowStore
from mewcode.orchestration.chat_routing import dialogue_focus, set_dialogue_focus
from mewcode.orchestration.consistent_save import recover_pending_save, save_consistently
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.execution_scope import prepare_orchestration_scope
from mewcode.orchestration.maintenance import SCHEMA
from mewcode.orchestration.maintenance_revision import execution_direction, future_revision
from mewcode.orchestration.pipeline_bridge import to_pipeline_graph
from mewcode.orchestration.plan_session import PlanSessionStore, build_plan_session
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.pipeline.store import PipelineStore
from tests.test_chat_task_routing import ingress as ingress
from tests.test_directed_loop_repair import five_stage_spec
from tests.test_expert_task_identity import runtime_for
from tests.test_orchestration_maintenance import maintenance_env as maintenance_env, spec_for, update_proposal


def test_new_canvas_commit_creates_directory_and_preserves_visual_metadata(tmp_path):
    plans, flows = PlanSessionStore(tmp_path / "host/plans.json"), ExpertFlowStore(tmp_path / "host/flows.json")
    project = tmp_path / "project"
    pipeline, spec = PipelineStore(project), spec_for()
    graph = to_pipeline_graph(spec, graph_id="maintenance-test")
    from mewcode.pipeline.models import Position
    graph.nodes[0] = graph.nodes[0].model_copy(update={"position": Position(x=120, y=230)})
    payload = json.loads(graph.variables["__orch.payload__"])
    payload["nodes"]["first"]["canvas_size"] = {"width": 400, "height": 210}
    graph.variables.update(custom="keep", **{"__orch.payload__": json.dumps(payload)})
    saved, canonical, flow, _ = save_consistently(project, spec=spec, graph=graph,
        pipeline_store=pipeline, plan_store=plans, flow_store=flows)
    assert saved.version == canonical.version == flow.version == 1
    assert pipeline.get_pipeline(saved.graph_id).nodes[0].position == saved.nodes[0].position
    assert saved.variables["custom"] == "keep"
    assert json.loads(saved.variables["__orch.payload__"])["nodes"]["first"]["canvas_size"] == {"width": 400, "height": 210}
    assert not project.joinpath(".mewcode/pipelines/.orchestration-commit.json").exists()


@pytest.mark.parametrize("bad", ["different_project", "wrong_path", "corrupt_ledger", "corrupt_backup"])
def test_commit_recovery_never_overwrites_another_host_store(tmp_path, bad):
    plans, flows = PlanSessionStore(tmp_path / "host/plans.json"), ExpertFlowStore(tmp_path / "host/flows.json")
    project = tmp_path / "project"
    pipeline, spec = PipelineStore(project), spec_for()
    graph = to_pipeline_graph(spec, graph_id="maintenance-test")
    save_consistently(project, spec=spec, graph=graph, pipeline_store=pipeline, plan_store=plans, flow_store=flows)
    original = {p: p.read_bytes() for p in (plans.path, flows.path, project / ".mewcode/pipelines/maintenance-test.json")}
    if bad == "corrupt_ledger":
        plans.path.write_text("[]", encoding="utf-8")
        original[plans.path] = b"[]"
        with pytest.raises(ValueError, match="存储损坏"):
            save_consistently(project, spec=spec, graph=graph, pipeline_store=pipeline, plan_store=plans, flow_store=flows)
    else:
        from mewcode.orchestration.maintenance import project_key
        paths = [project / ".mewcode/pipelines/maintenance-test.json", plans.path, flows.path]
        journal = plans.path.with_name("." + plans.path.name + ".orchestration-commit.json")
        record = {"graph_id": graph.graph_id, "project": project_key(project),
                  "paths": [str(p.resolve()) for p in paths], "backup": [None] * 3}
        if bad == "wrong_path":
            record["paths"][1] = str(tmp_path / "unrelated.json")
        if bad == "corrupt_backup":
            record["backup"][1] = "malformed base64!"
        journal.write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(ValueError):
            recover_pending_save(tmp_path / "other" if bad == "different_project" else project,
                                 plan_store=plans, flow_store=flows)
        assert journal.exists()
    assert all(p.read_bytes() == value for p, value in original.items())


def test_run_scope_restores_exact_authorization_after_interleaved_chat(tmp_path):
    runtime, checker = runtime_for(tmp_path)
    runtime.work_dir = str(tmp_path)
    receipt = prepare_orchestration_scope(runtime, task="原方向", run_id="a" * 32)
    identity = receipt["task_id"]
    auth = TaskAuthorization.from_user_scope({"allowed_tools": ["ReadFile"]}, task_id=identity)
    runtime.owner.web_state["task_authorizations"] = {f"{runtime.conversation_id}:{identity}": auth.as_dict()}
    original = copy.deepcopy(runtime.owner.web_state)
    set_dialogue_focus(runtime, "chat", task="独立对话")
    restored = prepare_orchestration_scope(runtime, task="新方向", run_id="a" * 32,
                                           checkpoint={"host_execution_scope": receipt})
    assert restored["task_id"] == checker.task_id == identity
    assert checker.task_authorization == auth and runtime.owner.web_state == original


@pytest.mark.parametrize("field,value", [("conversation_id", "other"), ("run_id", "b" * 32),
    ("workspace", "different"), ("schema", "model-created"), ("task_id", " ")])
def test_invalid_saved_identity_is_rejected_before_focus_changes(tmp_path, field, value):
    runtime, checker = runtime_for(tmp_path)
    runtime.work_dir = str(tmp_path)
    receipt = prepare_orchestration_scope(runtime, task="原方向", run_id="a" * 32)
    before = copy.deepcopy(dialogue_focus(runtime))
    receipt[field] = value
    with pytest.raises(ValueError):
        prepare_orchestration_scope(runtime, task="新方向", run_id="a" * 32, checkpoint={"host_execution_scope": receipt})
    assert dialogue_focus(runtime) == before and checker.task_id == before["task_id"]


@pytest.mark.parametrize("failure", ["callback", "late_write"])
def test_stopped_mutation_rolls_back_memory_and_disk(tmp_path, monkeypatch, failure):
    store, identity = ExpertRunStore(tmp_path / "runs"), "a" * 32
    store.create(run_id=identity, conversation_id="owner", task="original", attachments=[], profile={}, strategy="stable")
    store.transition(identity, "paused")
    original = store.load(identity)
    original_disk = ExpertRunStore(store.root).load(identity)
    if failure == "late_write":
        real, calls = store._persist, []
        def persist(snapshot):
            real(snapshot)
            calls.append(snapshot)
            if len(calls) == 1:
                raise OSError("after disk replacement")
        monkeypatch.setattr(store, "_persist", persist)
    def apply(snapshot):
        snapshot["task"] = "changed"
        if failure == "callback":
            raise ValueError("callback refused")
    with pytest.raises((ValueError, OSError)):
        store.mutate_stopped(identity, apply, conversation_id="owner", expected_updated_at=original["updated_at"])
    assert store.load(identity) == original
    assert ExpertRunStore(store.root).load(identity) == original_disk


def test_future_revision_refuses_unreviewed_base_and_only_accepts_applied_direction():
    spec = spec_for()
    snapshot = {"task": "原请求", "status": "paused", "locked_flow": to_expert_flow(spec).as_dict(),
        "supplemental_requirements": [{"text": "过去的补充", "attempt": 1}], "node_states": {}}
    other = spec.as_dict()
    other["nodes"][0]["model"]["model_id"] = "different-model"
    with pytest.raises(ValueError, match="基准"):
        future_revision(snapshot, spec, affected=["work"], maintenance_id="apply", base_spec=OrchestrationSpec.from_value(other))
    changed = future_revision(snapshot, replace(spec, goal="新方向"), affected=["work"], maintenance_id="apply",
                              base_spec=spec, direction="新方向")
    assert changed["task"] == snapshot["task"] and changed["supplemental_requirements"][0] == snapshot["supplemental_requirements"][0]
    changed["supplemental_requirements"].append({"text": "未应用的方向", "source": "maintenance:unapplied"})
    assert execution_direction(changed) == "已应用的当前业务方向：新方向"
    assert execution_direction(snapshot) == ""


@pytest.mark.parametrize("progress", [None, "round", "tokens", "elapsed", "completed_items", "history", "scores"])
def test_maintenance_only_rebinds_loops_without_execution_progress(progress):
    spec = five_stage_spec()
    state = {"status": "running", "round": 0, "tokens": 0, "elapsed": 0,
             "contract_hash": "legacy", "loop_resource_policy": {"contract_hash": "legacy", "token_budget": 1000}}
    snapshot = {"status": "paused", "locked_flow": to_expert_flow(spec).as_dict(), "loop_states": {"atomic": state}}
    if progress:
        state[progress] = 1 if progress in {"round", "tokens", "elapsed"} else ["existing"]
        original = copy.deepcopy(snapshot)
        with pytest.raises(ValueError, match="进度"):
            future_revision(snapshot, spec, affected=["second"], maintenance_id="apply")
        assert snapshot == original
    else:
        result = future_revision(snapshot, spec, affected=["second"], maintenance_id="apply")
        changed = result["loop_states"]["atomic"]
        assert changed["status"] == "blocked" and changed["contract_hash"] != "legacy"
        assert changed["loop_resource_policy"]["contract_hash"] == changed["contract_hash"]
        assert changed["loop_resource_policy"]["token_budget"] == 1000


def paused_run(owner, spec, *, unknown=False):
    from mewcode.orchestration.execution_scope import SCHEMA as SCOPE_SCHEMA
    identity = "a" * 32
    server.EXPERT_RUNS.create(run_id=identity, conversation_id=owner["conversation_id"], task="原业务请求",
                            attachments=[], profile={}, strategy="stable", flow=to_expert_flow(spec))
    receipt = {"schema": SCOPE_SCHEMA, "conversation_id": owner["conversation_id"], "run_id": identity,
               "task_id": "original-task", "workspace": str(Path(owner["project"]).resolve())}
    def initialize(snapshot):
        snapshot.update(status="paused", stage_cursor="work", host_execution_scope=receipt,
            node_states={"first": {"status": "completed", "text": "已完成成果"}, "work": {"status": "pending"}},
            budget={"tokens": 1200}, locked_models={"work": {"provider_key": "selected", "model_id": "selected-model"}})
        if unknown:
            snapshot["node_states"]["work"]["command_receipts"] = [{"command_started": None, "request_id": "unknown"}]
    server.EXPERT_RUNS.mutate(identity, initialize)
    server.SESSION.session.meta.dialogue_focus = {"kind": "orchestration", "task_id": receipt["task_id"], "run_id": identity}
    return identity


async def ready_preview(client, owner, mode="update", proposal=None):
    state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": mode, "node_ids": ["work"]})).json()
    base = server._conversation_plan_store().get(owner["project"]).spec.as_dict()
    started = server.MAINTENANCE_STORE.begin(owner["project"], owner["conversation_id"], state["id"],
        revision=state["revision"], message="更新该环节", base=base)
    done = server.MAINTENANCE_STORE.complete(owner["project"], owner["conversation_id"], state["id"],
        call_id=started["call_id"], response={"schema": SCHEMA, "reply": "等待应用", "proposal": proposal or update_proposal()}, base=base)
    return {**owner, "id": done["id"], "revision": done["revision"], "proposal_id": done["proposal"]["id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_apply_paused_run_preserves_completed_work_and_unknown_receipts(maintenance_env, unknown):
    app, owner, _, _, spec = maintenance_env
    identity = paused_run(owner, spec, unknown=unknown)
    original = server.EXPERT_RUNS.load(identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request = await ready_preview(client, owner)
        response = await client.post("/api/orchestration/maintenance/apply", json=request)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["run_adopted"] is not unknown
        changed = server.EXPERT_RUNS.load(identity)
        if unknown:
            assert changed == original and "执行记录" in result["run_notice"]
            with pytest.raises(ValueError, match="维护接纳"):
                server.EXPERT_RUNS.resume(identity, strategy="stable", flow=result["flow"])
            assert server.EXPERT_RUNS.load(identity) == original
        else:
            for key in ("host_execution_scope", "budget", "locked_models", "task"):
                assert changed[key] == original[key]
            assert changed["node_states"]["first"] == original["node_states"]["first"]
            assert changed["status"] == "paused" and changed["locked_flow"]["version"] == 2
            assert any(item["kind"] == "before_maintenance" for item in server.EXPERT_RUNS.checkpoint_archive.list(identity))
            resumed = server.EXPERT_RUNS.resume(identity, strategy="stable", flow=result["flow"])
            assert resumed["status"] == "running" and resumed["host_execution_scope"] == original["host_execution_scope"]


@pytest.mark.asyncio
async def test_repair_runtime_refuses_stale_run_before_host_recovery(maintenance_env, monkeypatch):
    app, owner, _, _, spec = maintenance_env
    identity = paused_run(owner, spec)
    from mewcode.sandbox import native_recovery
    recovery = lambda: {"ready": True}
    calls = []
    monkeypatch.setattr(native_recovery, "recover_native_runtime", lambda: calls.append(identity) or recovery())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix"})).json()
        server.SESSION.session.meta.dialogue_focus["run_id"] = ""
        response = await client.post("/api/orchestration/maintenance/repair-runtime", json={**owner, "id": state["id"], "revision": state["revision"]})
        assert response.status_code == 409 and not calls


@pytest.mark.asyncio
async def test_old_maintenance_preview_cannot_apply_after_run_focus_changes(maintenance_env):
    app, owner, _, _, spec = maintenance_env
    paused_run(owner, spec)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request = await ready_preview(client, owner)
        server.SESSION.session.meta.dialogue_focus["run_id"] = ""
        response = await client.post("/api/orchestration/maintenance/apply", json=request)
        assert response.status_code == 409, response.text
        assert server._conversation_plan_store().get(owner["project"]).spec.as_dict() == spec.as_dict()


@pytest.mark.asyncio
async def test_reopened_runtime_repair_restores_only_original_task_authorization(maintenance_env, monkeypatch):
    app, owner, _, _, spec = maintenance_env
    identity = paused_run(owner, spec)
    _, checker = runtime_for(Path(owner["project"]))
    agent = SimpleNamespace(permission_checker=checker, safety_reviewer=SimpleNamespace(authorization_version=""))
    monkeypatch.setattr(server.SESSION, "agent", agent)
    authorization = TaskAuthorization.from_user_scope({"allowed_tools": ["ReadFile"]}, task_id="original-task")
    records = {"task_authorizations": {owner["conversation_id"] + ":original-task": authorization.as_dict()}}
    monkeypatch.setattr(server.SESSION, "web_state", copy.deepcopy(records))
    from mewcode.sandbox import native_recovery
    calls = []
    monkeypatch.setattr(native_recovery, "recover_native_runtime", lambda: calls.append(True) or {"ready": True})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix"})).json()
        assert state["diagnostics"]["task_identity_error"]
        response = await client.post("/api/orchestration/maintenance/repair-runtime", json={**owner, "id": state["id"], "revision": state["revision"]})
        assert response.status_code == 200, response.text
        assert not response.json()["diagnostics"]["task_identity_error"]
    assert checker.task_id == "original-task" and checker.task_authorization == authorization
    assert server.SESSION.web_state == records and calls == [True]
    assert server.EXPERT_RUNS.load(identity)["status"] == "paused"


@pytest.mark.asyncio
async def test_client_cleanup_failure_does_not_leave_maintenance_job_running(maintenance_env, monkeypatch):
    app, owner, _, _, _ = maintenance_env
    class Client:
        async def aclose(self):
            raise OSError("cleanup failed")
    monkeypatch.setattr(server, "_new_orchestration_client", lambda *args: Client())
    async def model(*args, **kwargs):
        return json.dumps({"schema": SCHEMA, "reply": "独立讨论", "proposal": None})
    monkeypatch.setattr(orch_maintenance, "collect_model_text", model)
    from tests.test_orchestration_maintenance import poll
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix"})).json()
        for _ in range(2):
            response = await client.post("/api/orchestration/maintenance/send", json={**owner, "id": state["id"], "revision": state["revision"], "message": "分析"})
            assert response.status_code == 200, response.text
            state = await poll(client, owner, state)
            assert state["status"] == "ready" and state["progress"] == {}


@pytest.mark.asyncio
async def test_late_maintenance_storage_failure_reports_saved_graph(maintenance_env, monkeypatch):
    app, owner, _, _, _ = maintenance_env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request = await ready_preview(client, owner)
        def fail(*args):
            raise sqlite3.OperationalError("late storage failure")
        monkeypatch.setattr(server.MAINTENANCE_STORE, "mutate", fail)
        response = await client.post("/api/orchestration/maintenance/apply", json=request)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "applied" and "已保存" in response.json()["result"]["warning"]
        assert server._conversation_plan_store().get(owner["project"]).spec.version == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/fix work 修复依赖", "/update 当前方向调整"])
async def test_raw_websocket_maintenance_never_enters_business_context(ingress, command):
    runtime = ingress.runtime
    set_dialogue_focus(runtime, "orchestration", task="原业务请求", run_id="a" * 32)
    focus = copy.deepcopy(dialogue_focus(runtime))
    path = runtime.session._sessions_dir / (runtime.conversation_id + ".jsonl")
    original = path.read_bytes() if path.exists() else None
    socket = ingress.Socket([{"text": command}])
    await server.ws_endpoint(socket)
    assert ingress.prompts == [] and runtime.conversation.history == []
    assert dialogue_focus(runtime) == focus and not runtime.queued_prompts
    assert (path.read_bytes() if path.exists() else None) == original
    assert any(item.get("maintenance") for item in socket.messages if item.get("type") == "command_result")


@pytest.mark.asyncio
async def test_applied_direction_reaches_resumed_stage_prompt_without_transcript_edit(maintenance_env, monkeypatch):
    from mewcode import expert_web
    from mewcode.agent import StreamText
    from tests.test_expert_flow import _profile
    _, owner, _, durable, spec = maintenance_env
    # This fixture exercises prompt revision with a text-only fake agent. State
    # its empty capability scope explicitly so environment preflight can remain
    # enabled without turning an unrelated undeclared scope into this test's stop.
    spec = replace(spec, nodes=tuple(replace(node, capabilities={"command_execution": False}) for node in spec.nodes))
    identity = paused_run(owner, spec)
    server.EXPERT_RUNS.mutate(identity, lambda value: value.update(node_states={}, stages={}, locked_profile=_profile().as_dict()))
    before = server.EXPERT_RUNS.load(identity)
    candidate = replace(spec, goal="新的交付方向", version=2)
    revised = future_revision(before, candidate, affected=["first", "work", "done"], maintenance_id="applied",
                              base_spec=spec, direction=candidate.goal)
    server.EXPERT_RUNS.mutate_stopped(identity, lambda value: (value.clear(), value.update(revised)),
        conversation_id=owner["conversation_id"], expected_updated_at=before["updated_at"])
    server._conversation_plan_store().put(owner["project"], build_plan_session(candidate))
    server._conversation_flow_store().put(owner["project"], to_expert_flow(candidate))
    seen = []
    class Agent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 1
        def set_review_authorization_context(self, context):
            pass
        async def run(self, conversation):
            seen.append(copy.deepcopy(conversation.history))
            yield StreamText(text="完成")
    monkeypatch.setattr(expert_web, "_build_role_agent", lambda *args:
        (Agent(), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())))
    runtime = SimpleNamespace(work_dir=owner["project"], conversation_id=owner["conversation_id"], session=durable,
        conversation=ConversationManager(), multi_agent=False, expert_run_id="", expert_control_action="pause",
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [],
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None))
    history = durable._sessions_dir / (durable.session_id + ".jsonl")
    original = history.read_bytes() if history.exists() else None
    events = []
    await expert_web.run_expert_chat(SimpleNamespace(send_json=AsyncMock(side_effect=events.append)), "继续", session=runtime, resume_run_id=identity,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}), resolve=lambda ref: SimpleNamespace(ref=ref)),
        profile_store=SimpleNamespace(get=lambda p: _profile()), flow_store=server._conversation_flow_store(),
        plan_store=server._conversation_plan_store(), run_store=server.EXPERT_RUNS)
    assert seen and all(any("新的交付方向" in str(message.content) for message in prompt) for prompt in seen), events
    assert server.EXPERT_RUNS.load(identity)["host_execution_scope"]["task_id"] == "original-task"
    # Scheduler execution may append its normal final result, but maintenance prose
    # never becomes a user transcript message.
    current = history.read_text(encoding="utf-8") if history.exists() else ""
    assert "已应用的当前业务方向" not in current
    assert original is None or history.read_bytes().startswith(original)
