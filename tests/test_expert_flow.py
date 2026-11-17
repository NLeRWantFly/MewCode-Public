from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mewcode.cordis import PluginSpec
from mewcode.conversation import ConversationManager
from mewcode.expert_group import (
    ExpertGroupOrchestrator,
    ExpertProfile,
    ExpertRoleResult,
)
from mewcode.expert_runs import ExpertRunStore, SNAPSHOT_VERSION
from mewcode.expert_web import run_expert_chat
from mewcode.experts import (
    DEFAULT_EXPERT_FLOW,
    ExpertFlowError,
    ExpertFlowSpec,
    ExpertFlowStore,
)
from mewcode.model_registry import ModelRef
from mewcode.pipeline.models import GraphEdge, GraphNode, NodeType, PipelineGraph
from mewcode.pipeline.store import PipelineStore
from mewcode.plugins import ApplicationHost
from mewcode.plugins.services import (
    EXPERTS,
    EXPERT_FINALIZER_SLOT,
    EXPERT_FLOWS,
    EXPERT_FLOW_SLOT,
    EXPERT_REDUCER_SLOT,
    EXPERT_ROLE_SLOT,
    EXPERT_ROUTER_SLOT,
)


def _profile() -> ExpertProfile:
    model = ModelRef("openai", "gpt-5.6", "")
    return ExpertProfile(
        manager=model,
        planner=model,
        executor=model,
        auditor=model,
    )


def _pass_verdict() -> dict[str, object]:
    return {"verdict": "PASS", "issues": [], "evidence": []}


def _needs_attention() -> dict[str, object]:
    return {
        "verdict": "NEEDS_ATTENTION",
        "issues": [{"description": "repair required"}],
        "evidence": [],
    }


def test_default_flow_is_serial_and_validates_plugin_references() -> None:
    flow = DEFAULT_EXPERT_FLOW
    assert [node.node_id for node in flow.nodes] == [
        "manager.intake",
        "planner",
        "executor",
        "auditor",
        "manager.finalize",
    ]
    assert flow.topological_batches() == tuple(
        (node.node_id,) for node in flow.nodes
    )
    assert flow.finalizer_plugin == "model"

    legacy = flow.as_dict()
    legacy["finalizer"] = legacy.pop("finalizer_node_id")
    legacy.pop("finalizer_plugin")
    assert ExpertFlowSpec.from_value(legacy) == flow

    with pytest.raises(ExpertFlowError, match="unknown role"):
        flow.validate(roles={"manager"})
    with pytest.raises(ExpertFlowError, match="unknown finalizer"):
        flow.validate(finalizers={"custom"})

    cyclic = flow.as_dict()
    cyclic["edges"].append({
        "from_node": "manager.finalize",
        "to_node": "manager.intake",
        "condition": "always",
        "router": "default",
    })
    with pytest.raises(ExpertFlowError, match="DAG"):
        ExpertFlowSpec.from_value(cyclic)


def test_pipeline_node_references_a_validated_reusable_graph() -> None:
    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.embedded-pipeline",
        "version": 1,
        "label": "Embedded Pipeline",
        "nodes": [
            {"node_id": "intake", "role": "manager", "label": "Intake"},
            {
                "node_id": "delivery",
                "kind": "pipeline",
                "label": "Delivery Pipeline",
                "pipeline": {
                    "graph_id": "delivery.graph",
                    "entry_node_id": "prepare",
                    "output_key": "release",
                    "budget_total": 12000,
                },
            },
            {"node_id": "final", "role": "manager", "label": "Final"},
        ],
        "edges": [
            {"from": "intake", "to": "delivery"},
            {"from": "delivery", "to": "final"},
        ],
        "finalizer_node_id": "final",
    })
    pipeline_node = flow.node_map["delivery"]
    assert pipeline_node.role == "pipeline"
    assert pipeline_node.pipeline is not None
    assert pipeline_node.pipeline.output_key == "release"
    flow.validate(
        roles={"manager"}, pipelines={"delivery.graph"},
        routers={"default"}, reducers={"collect"}, finalizers={"model"},
    )

    with pytest.raises(ExpertFlowError, match="unknown pipeline"):
        flow.validate(pipelines={"another.graph"})
    invalid = flow.as_dict()
    invalid["nodes"][1]["members"] = [{
        "member_id": "nested", "name": "Nested", "task": "not allowed",
    }]
    with pytest.raises(ExpertFlowError, match="cannot define members"):
        ExpertFlowSpec.from_value(invalid)
    invalid = flow.as_dict()
    invalid["nodes"][1]["pipeline"]["graph_id"] = "../outside"
    with pytest.raises(ExpertFlowError, match="local graph identifier"):
        ExpertFlowSpec.from_value(invalid)


def test_expert_node_canvas_position_round_trips_and_rejects_non_finite_values() -> None:
    value = DEFAULT_EXPERT_FLOW.as_dict()
    value["nodes"][0]["position"] = {"x": 120.5, "y": 80}
    flow = ExpertFlowSpec.from_value(value)

    assert flow.nodes[0].position is not None
    assert flow.nodes[0].position.x == 120.5
    assert flow.as_dict()["nodes"][0]["position"] == {"x": 120.5, "y": 80.0}

    value["nodes"][0]["position"] = {"x": float("inf"), "y": 0}
    with pytest.raises(ExpertFlowError, match="finite"):
        ExpertFlowSpec.from_value(value)


def test_flow_store_is_project_scoped_atomic_and_deletable(tmp_path: Path) -> None:
    store = ExpertFlowStore(tmp_path / "expert-flows.json")
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    custom = DEFAULT_EXPERT_FLOW.as_dict()
    custom.update({"flow_id": "project.a", "version": 7, "label": "Project A"})

    saved = store.put(project_a, custom)
    assert saved.version == 7
    assert store.get(project_a) == saved
    assert store.get(project_a / ".") == saved
    assert store.get(project_b) is None
    assert not store.path.with_suffix(".json.tmp").exists()

    store.delete(project_a)
    assert store.get(project_a) is None
    store.path.write_text("not json", encoding="utf-8")
    assert store.get(project_a) is None


def test_application_host_tracks_dynamic_expert_slots(tmp_path: Path) -> None:
    host = ApplicationHost(str(tmp_path), discover_plugins=False).start()
    flow = {
        "flow_id": "plugin.critic",
        "version": 1,
        "label": "Critic",
        "nodes": [{
            "node_id": "critic",
            "role": "critic",
            "label": "Critic",
            "reducer": "merge",
        }],
        "edges": [],
        "finalizer_node_id": "critic",
        "finalizer_plugin": "summary",
    }

    def apply(context):
        context.slots.register(EXPERT_ROLE_SLOT, {
            "name": "critic",
            "tools": [],
            "responsibility": "Review the result.",
            "may_write": False,
            "may_delegate": False,
        }, item_key="critic")
        context.slots.register(
            EXPERT_FLOW_SLOT, flow, item_key="plugin.critic"
        )
        context.slots.register(
            EXPERT_ROUTER_SLOT, lambda *_args: True, item_key="branch"
        )
        context.slots.register(
            EXPERT_REDUCER_SLOT, lambda *_args: "merged", item_key="merge"
        )
        context.slots.register(
            EXPERT_FINALIZER_SLOT, lambda **_kwargs: "done", item_key="summary"
        )

    try:
        host.context.register_plugin(PluginSpec("expert-extension", apply))
        host.context.load("expert-extension")
        assert host.context.get(EXPERTS).get("critic") is not None
        assert host.context.get(EXPERT_FLOWS).get("plugin.critic") is not None
        assert "branch" in host.context.slots.resolve(EXPERT_ROUTER_SLOT)
        assert "merge" in host.context.slots.resolve(EXPERT_REDUCER_SLOT)
        assert "summary" in host.context.slots.resolve(EXPERT_FINALIZER_SLOT)

        host.context.unload("expert-extension")
        assert host.context.get(EXPERTS).get("critic") is None
        assert host.context.get(EXPERT_FLOWS).get("plugin.critic") is None
        assert "summary" not in host.context.slots.resolve(EXPERT_FINALIZER_SLOT)
    finally:
        host.close()


@pytest.mark.asyncio
async def test_dag_runs_parallel_members_routes_reducer_and_finalizer(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    calls: list[tuple[str, str | None]] = []
    executor_prompts: list[str] = []
    router_calls: list[tuple[str, str]] = []
    planner_active = 0
    max_planner_active = 0

    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.parallel",
        "version": 1,
        "label": "Parallel",
        "nodes": [
            {"node_id": "intake", "role": "manager", "label": "Intake"},
            {"node_id": "plan.a", "role": "planner", "label": "Plan A"},
            {"node_id": "plan.b", "role": "planner", "label": "Plan B"},
            {
                "node_id": "execute",
                "role": "executor",
                "label": "Execute",
                "reducer": "merge",
                "members": [
                    {"member_id": "ui", "name": "UI", "task": "Build UI"},
                    {"member_id": "api", "name": "API", "task": "Build API"},
                ],
            },
            {"node_id": "audit", "role": "auditor", "label": "Audit"},
            {"node_id": "final", "role": "manager", "label": "Final"},
        ],
        "edges": [
            {"from": "intake", "to": "plan.a", "condition": "selected", "router": "branch"},
            {"from": "intake", "to": "plan.b", "condition": "selected", "router": "branch"},
            {"from": "plan.a", "to": "execute"},
            {"from": "plan.b", "to": "execute"},
            {"from": "execute", "to": "audit"},
            {"from": "audit", "to": "final"},
        ],
        "finalizer_node_id": "final",
        "finalizer_plugin": "summary",
    })

    async def fallback_runner(*_args):
        raise AssertionError("node_runner should own graph execution")

    async def node_runner(node, _model, prompt, _policy, member=None):
        nonlocal planner_active, max_planner_active
        calls.append((node.node_id, member.member_id if member else None))
        if member is not None:
            await asyncio.sleep(0.005)
            return ExpertRoleResult(text=f"member:{member.member_id}")
        if node.role == "planner":
            planner_active += 1
            max_planner_active = max(max_planner_active, planner_active)
            await asyncio.sleep(0.01)
            planner_active -= 1
        if node.node_id == "execute":
            executor_prompts.append(prompt)
        verdict = _pass_verdict() if node.role == "auditor" else None
        return ExpertRoleResult(text=f"node:{node.node_id}", verdict=verdict)

    async def branch(condition, source, _result, _results):
        router_calls.append((condition, source.node_id))
        return condition == "selected"

    async def finalizer(**kwargs):
        assert kwargs["node"].node_id == "final"
        assert kwargs["inputs"][0]["from_node"] == "audit"
        return {"text": "plugin final answer", "status": "completed"}

    def reducer(_node, member_results):
        return "MERGED " + ",".join(result.text for _, result in member_results)

    orchestrator = ExpertGroupOrchestrator(
        runner=fallback_runner,
        node_runner=node_runner,
        event_sink=lambda event: events.append(event),
        archive_root=tmp_path / "parallel-archives",
    )
    result = await orchestrator.run(
        "build a feature",
        _profile(),
        {"providers": []},
        strategy="stable",
        flow=flow,
        routers={"branch": branch},
        reducers={"merge": reducer},
        finalizers={"summary": finalizer},
    )

    assert result.status == "completed"
    assert result.final_summary == "plugin final answer"
    # Same topological batch runs declaration-order serial (no sibling gather).
    assert max_planner_active == 1
    assert {member for node, member in calls if node == "execute" and member} == {
        "ui", "api",
    }
    assert "MERGED member:ui,member:api" in executor_prompts[0]
    assert router_calls == [("selected", "intake"), ("selected", "intake")]
    assert any(event["type"] == "expert_join_completed" for event in events)
    final_event = next(
        event for event in events
        if event["type"] == "expert_node_completed" and event["node_id"] == "final"
    )
    assert final_event["finalizer"] == "summary"
    assert isinstance(final_event["duration_ms"], int)


@pytest.mark.asyncio
async def test_dag_embeds_pipeline_with_handoffs_events_and_resume(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    role_calls: list[str] = []
    pipeline_calls: list[dict[str, object]] = []
    flow = ExpertFlowSpec.from_value({
        "flow_id": "test.pipeline-runtime",
        "version": 2,
        "label": "Pipeline Runtime",
        "nodes": [
            {"node_id": "intake", "role": "manager", "label": "Intake"},
            {
                "node_id": "build",
                "kind": "pipeline",
                "label": "Build Pipeline",
                "responsibility": "Ship only the requested scope.",
                "pipeline": {"graph_id": "build.graph", "output_key": "release"},
            },
            {"node_id": "final", "role": "manager", "label": "Final"},
        ],
        "edges": [
            {"from": "intake", "to": "build"},
            {"from": "build", "to": "final"},
        ],
        "finalizer_node_id": "final",
    })
    saved_pipeline = {
        "run_id": "nested-run",
        "graph_id": "build.graph",
        "status": "interrupted",
    }

    async def role_runner(node, _model, prompt, _policy, member=None):
        assert member is None
        role_calls.append(node.node_id)
        if node.node_id == "final":
            assert "pipeline result" in prompt
        return ExpertRoleResult(text=f"role:{node.node_id}")

    async def pipeline_runner(node, trigger, resume, relay):
        pipeline_calls.append({"node": node.node_id, "trigger": trigger, "resume": resume})
        assert trigger["instruction"] == "Ship only the requested scope."
        assert trigger["handoffs"][0]["from_node"] == "intake"
        assert resume == saved_pipeline
        await relay({
            "type": "expert_pipeline_step_started",
            "pipeline_node_id": "compile",
            "label": "Compile",
            "status": "running",
        })
        await relay({
            "type": "expert_pipeline_step_completed",
            "pipeline_node_id": "compile",
            "status": "completed",
            "duration_ms": 14,
        })
        return ExpertRoleResult(
            text="pipeline result", input_tokens=21, output_tokens=8,
        )

    orchestrator = ExpertGroupOrchestrator(
        runner=lambda *_args: None,  # type: ignore[arg-type]
        node_runner=role_runner,
        pipeline_runner=pipeline_runner,
        event_sink=lambda event: events.append(event),
        archive_root=tmp_path / "pipeline-archives",
    )
    result = await orchestrator.run(
        "build release",
        _profile(),
        {"providers": []},
        strategy="stable",
        checkpoint={"pipeline_runs": {"build": saved_pipeline}},
        flow=flow,
    )

    assert result.status == "completed"
    assert result.node_summaries["build"] == "pipeline result"
    assert role_calls == ["intake", "final"]
    assert len(pipeline_calls) == 1
    assert any(event["type"] == "expert_pipeline_started" for event in events)
    assert any(
        event["type"] == "expert_pipeline_step_completed"
        and event["node_id"] == "build"
        for event in events
    )
    assert any(event["type"] == "expert_pipeline_completed" for event in events)


@pytest.mark.asyncio
async def test_expert_web_runs_real_embedded_pipeline_and_persists_checkpoint(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    pipeline_store = PipelineStore(str(project))
    pipeline_store.create_pipeline(PipelineGraph(
        graph_id="delivery.graph",
        name="Delivery",
        nodes=[
            GraphNode(id="start", type=NodeType.START, label="Start"),
            GraphNode(
                id="deliver", type=NodeType.TASK, title="Deliver",
                goal="Handle {{input.task}}", auto_split=False,
                output_key="release", model="gpt-5.6",
            ),
            GraphNode(id="end", type=NodeType.END, label="End"),
        ],
        edges=[
            GraphEdge(id="start-deliver", **{"from": "start", "to": "deliver"}),
            GraphEdge(id="deliver-end", **{"from": "deliver", "to": "end"}),
        ],
    ))
    flow_store = ExpertFlowStore(tmp_path / "flows.json")
    flow_store.put(project, {
        "flow_id": "project.delivery",
        "version": 1,
        "label": "Delivery",
        "nodes": [{
            "node_id": "delivery",
            "kind": "pipeline",
            "label": "Delivery Pipeline",
            "pipeline": {
                "graph_id": "delivery.graph", "output_key": "release",
            },
        }],
        "edges": [],
        "finalizer_node_id": "delivery",
    })
    run_store = ExpertRunStore(tmp_path / "expert-runs")
    ws_messages: list[dict[str, object]] = []
    pipeline_prompts: list[str] = []

    class FakeWebSocket:
        async def send_json(self, payload):
            ws_messages.append(dict(payload))

    class FakeDurableSession:
        def __init__(self):
            self.meta = SimpleNamespace(message_count=0)
            self.messages = []

        def append(self, message):
            self.messages.append(message)
            self.meta.message_count += 1

        def append_expert_run(self, *_args, **_kwargs):
            return None

    class FakeSlots:
        @staticmethod
        def resolve(_slot):
            return {}

    class FakeContext:
        slots = FakeSlots()

        @staticmethod
        def get(_service, default=None):
            return default

    class FakeTasks:
        @staticmethod
        def list_tasks():
            return []

        @staticmethod
        def mark_notified(_task_ids):
            return None

    session = SimpleNamespace(
        work_dir=str(project),
        conversation_id="conversation",
        multi_agent=False,
        session=FakeDurableSession(),
        conversation=ConversationManager(),
        context=FakeContext(),
        task_manager=FakeTasks(),
        expert_run_id="",
        expert_control_action="pause",
        expert_child_event_callback=None,
        status_payload=lambda: {},
        tasks_payload=lambda: [],
    )

    class Profiles:
        @staticmethod
        def get(_project):
            return _profile()

    class Registry:
        @staticmethod
        async def catalog(*, local_only=False):
            return {"providers": []}

    async def pipeline_agent(prompt, **_kwargs):
        pipeline_prompts.append(prompt)
        return "pipeline result", {"input_tokens": 13, "output_tokens": 5}

    async def capabilities():
        return {
            "models": [{"id": "gpt-5.6", "efforts": ["low", "medium"]}],
            "current_model": "gpt-5.6",
        }

    await run_expert_chat(
        FakeWebSocket(),
        "ship feature",
        confirmed_new_flow=flow_store.get(project),
        session=session,
        registry_service=Registry(),  # type: ignore[arg-type]
        profile_store=Profiles(),
        flow_store=flow_store,
        run_store=run_store,
        pipeline_agent_factory=pipeline_agent,
        pipeline_capabilities_provider=capabilities,
    )

    assert pipeline_prompts and "ship feature" in pipeline_prompts[0]
    summary = next(
        message for message in ws_messages if message.get("type") == "expert_summary"
    )
    assert summary["text"] == "pipeline result"
    saved = run_store.load(str(summary["run_id"]))
    assert saved is not None
    assert saved["status"] == "completed"
    assert saved["pipeline_runs"]["delivery"]["context_outputs"]["release"] == "pipeline result"
    assert any(
        message.get("type") == "expert_pipeline_step_completed"
        for message in ws_messages
    )


@pytest.mark.asyncio
async def test_audit_recovery_and_node_checkpoint_resume(tmp_path: Path) -> None:
    flow_value = DEFAULT_EXPERT_FLOW.as_dict()
    for node in flow_value["nodes"]:
        if node["node_id"] == "auditor":
            node["failure_policy"] = {
                "action": "route",
                "max_attempts": 1,
                "target_node_id": "planner",
            }
    flow = ExpertFlowSpec.from_value(flow_value)
    calls: list[str] = []
    events: list[dict[str, object]] = []
    audits = 0

    async def runner(node, _model, _prompt, _policy, member=None):
        nonlocal audits
        assert member is None
        calls.append(node.node_id)
        if node.node_id == "auditor":
            audits += 1
            verdict = _needs_attention() if audits == 1 else _pass_verdict()
            return ExpertRoleResult(text=f"audit:{audits}", verdict=verdict)
        return ExpertRoleResult(text=f"result:{node.node_id}")

    orchestrator = ExpertGroupOrchestrator(
        runner=lambda *_args: None,  # type: ignore[arg-type]
        node_runner=runner,
        event_sink=lambda event: events.append(event),
        archive_root=tmp_path / "recovery-archives",
    )
    result = await orchestrator.run(
        "repair",
        _profile(),
        {"providers": []},
        strategy="stable",
        flow=flow,
    )
    assert result.status == "completed"
    assert calls.count("planner") == 2
    assert calls.count("executor") == 2
    assert calls.count("auditor") == 2
    assert any(
        event.get("type") == "expert_flow_state"
        and event.get("phase") == "recovery"
        for event in events
    )

    resumed_calls: list[str] = []

    async def resumed_runner(node, _model, _prompt, _policy, member=None):
        resumed_calls.append(node.node_id)
        verdict = _pass_verdict() if node.role == "auditor" else None
        return ExpertRoleResult(text=f"fresh:{node.node_id}", verdict=verdict)

    resumed = ExpertGroupOrchestrator(
        runner=lambda *_args: None,  # type: ignore[arg-type]
        node_runner=resumed_runner,
        archive_root=tmp_path / "resume-archives",
    )
    checkpoint = {
        "node_states": {
            "manager.intake": {"status": "completed", "text": "saved intake"},
            "planner": {"status": "completed", "text": "saved plan"},
        },
        "node_attempts": {"manager.intake": 1, "planner": 1},
    }
    resumed_result = await resumed.run(
        "resume",
        _profile(),
        {"providers": []},
        strategy="stable",
        checkpoint=checkpoint,
        flow=DEFAULT_EXPERT_FLOW,
    )
    assert resumed_result.status == "completed"
    assert "manager.intake" not in resumed_calls
    assert "planner" not in resumed_calls
    assert resumed_calls == ["executor", "auditor", "manager.finalize"]


def test_run_snapshot_locks_flow_and_migrates_legacy_roles(tmp_path: Path) -> None:
    run_store = ExpertRunStore(tmp_path / "runs")
    flow_store = ExpertFlowStore(tmp_path / "flows.json")
    project = tmp_path / "project"
    project.mkdir()
    first = DEFAULT_EXPERT_FLOW.as_dict()
    first.update({"flow_id": "project.locked", "version": 3})
    second = copy.deepcopy(first)
    second["version"] = 4
    run_id = "1" * 32
    run_store.create(
        run_id=run_id,
        conversation_id="conversation",
        task="task",
        attachments=[],
        profile=_profile().as_dict(),
        strategy="stable",
        flow=first,
    )
    run_store.transition(run_id, "paused", reason="test")
    flow_store.put(project, second)
    resumed = run_store.resume(run_id, strategy="stable")
    assert resumed["locked_flow"]["version"] == 3
    assert flow_store.get(project).version == 4

    legacy_id = "2" * 32
    legacy_path = run_store._path(legacy_id)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps({
        "version": 1,
        "run_id": legacy_id,
        "conversation_id": "conversation",
        "status": "paused",
        "stage_cursor": "executor",
        "stages": {
            "manager": {"status": "completed", "text": "intake"},
            "planner": {"status": "completed", "text": "plan"},
            "executor": {"status": "running", "partial_text": "work"},
        },
    }), encoding="utf-8")
    migrated = run_store.load(legacy_id)
    assert migrated is not None
    assert migrated['version'] == 1
    assert 'migrated_from_version' not in migrated
    assert migrated["stage_cursor"] == "executor"
    assert migrated['stages']['manager']['text'] == 'intake'
    assert migrated['stages']['executor']['status'] == 'running'
    assert migrated['stages']['executor']['partial_text'] == 'work'
    assert run_store.public(legacy_id)['migration_required']
    from mewcode.kernel.migration import migrate
    imported = migrate(run_store, migrated, workspace=str(project))
    assert imported['version'] == SNAPSHOT_VERSION
    assert imported['node_states']['manager.intake']['text'] == 'intake'
    assert imported['stage_cursor'] == 'executor'


@pytest.mark.asyncio
async def test_expert_flow_api_get_put_validate_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path / "user-data"))
    import frontend.server as server

    store = ExpertFlowStore(tmp_path / "api-flows.json")
    project = tmp_path / "project"
    project.mkdir()
    pipeline_store = PipelineStore(str(project))
    pipeline_store.create_pipeline(PipelineGraph(
        graph_id="project.pipeline",
        name="Project Pipeline",
        nodes=[
            GraphNode(id="start", type=NodeType.START, label="Start"),
            GraphNode(id="end", type=NodeType.END, label="End"),
        ],
        edges=[GraphEdge(id="edge", **{"from": "start", "to": "end"})],
    ))
    monkeypatch.setattr(server, "EXPERT_FLOWS_STORE", store)
    monkeypatch.setattr(server.SESSION, "work_dir", str(project))
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "")
    transport = httpx.ASGITransport(app=server.app)

    custom = DEFAULT_EXPERT_FLOW.as_dict()
    custom.update({"flow_id": "project.api", "version": 2, "label": "API Flow"})
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        initial = await client.get("/api/expert-flow")
        assert initial.status_code == 200
        assert initial.json()["custom"] is False
        assert "model" in initial.json()["finalizers"]
        assert initial.json()["pipelines"][0]["graph_id"] == "project.pipeline"
        assert initial.json()["pipelines"][0]["nodes"][0]["type"] == "start"
        canvas = await client.get("/expert-flow")
        assert canvas.status_code == 200
        assert "专家团 Pipeline" in canvas.text

        saved = await client.put("/api/expert-flow", json={"flow": custom})
        assert saved.status_code == 200
        assert saved.json()["flow"]["version"] == 2
        assert (await client.get("/api/expert-flow")).json()["custom"] is True

        invalid = copy.deepcopy(custom)
        invalid["nodes"][0]["role"] = "missing-role"
        rejected = await client.put("/api/expert-flow", json={"flow": invalid})
        assert rejected.status_code == 400
        assert "unknown role" in rejected.json()["error"]
        assert store.get(project).version == 2

        embedded = copy.deepcopy(custom)
        embedded["version"] = 3
        embedded["nodes"][2] = {
            "node_id": "executor",
            "kind": "pipeline",
            "label": "Project Pipeline",
            "pipeline": {"graph_id": "project.pipeline"},
        }
        accepted = await client.put("/api/expert-flow", json={"flow": embedded})
        assert accepted.status_code == 200
        assert accepted.json()["flow"]["nodes"][2]["kind"] == "pipeline"

        embedded["nodes"][2]["pipeline"]["graph_id"] = "missing.pipeline"
        rejected_pipeline = await client.put(
            "/api/expert-flow", json={"flow": embedded}
        )
        assert rejected_pipeline.status_code == 400
        assert "unknown pipeline" in rejected_pipeline.json()["error"]

        deleted = await client.delete("/api/expert-flow")
        assert deleted.status_code == 200
        assert deleted.json()["custom"] is False
        assert store.get(project) is None
