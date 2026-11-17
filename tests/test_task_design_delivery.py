"""Future capability gaps do not preempt preparation, but fail before dispatch."""
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel.capabilities import compile_manifest
from mewcode.kernel.preflight import inspect_node
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


def test_future_missing_interpreter_only_blocks_its_consumer(tmp_path):
    flow = ExpertFlowSpec.from_value({"flow_id": "sample", "finalizer_node_id": "consume", "edges": [],
        "nodes": [{"node_id": "prepare", "role": "planner", "capabilities": {"command_execution": False}},
                  {"node_id": "consume", "role": "executor", "capabilities": {
                      "command_execution": False, "interpreters": ["missing-runtime/python.exe"]}}]})
    manifest = compile_manifest(flow, workspace=str(tmp_path), approved=True)
    assert inspect_node(manifest, "prepare") == []
    failed = inspect_node(manifest, "consume")
    assert failed[0]["code"] == "base_interpreter" and failed[0]["severity"] == "error"
    assert failed[0]["entry_ids"] == ["consume"]
    path = tmp_path / "missing-runtime/python.exe"
    path.parent.mkdir()
    path.write_bytes(b"fixture")
    assert all(row["severity"] != "error" for row in inspect_node(manifest, "consume"))


def test_missing_node_scope_never_becomes_an_empty_allowed_scope(tmp_path):
    findings = inspect_node({"approved": True, "workspace": str(tmp_path), "nodes": {}}, "missing")
    assert findings[0]["code"] == "undeclared_capabilities"
    assert findings[0]["severity"] == "error"


@pytest.mark.asyncio
async def test_real_run_entry_checks_both_dispatch_callbacks_and_keeps_finished_run_id(tmp_path, monkeypatch):
    from mewcode import expert_web
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from tests.test_expert_flow import _profile
    project = tmp_path / "project"
    project.mkdir()
    flow = ExpertFlowSpec.from_value({"flow_id": "design", "finalizer_node_id": "work", "edges": [],
        "nodes": [{"node_id": "work", "role": "executor", "capabilities": {
            "command_execution": False, "interpreters": ["absent/python.exe"]}}],
        "meta": {"task_design": {"task_id": "fixture"}}})
    runs = ExpertRunStore(tmp_path / "runs")
    flows = ExpertFlowStore(tmp_path / "flows.json")
    durable = SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None,
                              append_expert_run=lambda *a, **kw: None)
    runtime = SimpleNamespace(work_dir=str(project), conversation_id="fixture", multi_agent=False,
        session=durable, conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    checked = []
    class Harness:
        def __init__(self, **callbacks):
            self.callbacks = callbacks
        async def run(self, *args, **kwargs):
            node = kwargs["flow"].node_map["work"]
            normal = await self.callbacks["node_runner"](node, _profile().executor, "", SimpleNamespace())
            pipeline = await self.callbacks["pipeline_runner"](node, {}, None, AsyncMock())
            checked.extend([normal, pipeline])
            # The actual scheduler handles the environment_fault recovery; this
            # boundary test stops before any recovery model can be requested.
            raise RuntimeError("fixture stopped after both dispatch checks")
    monkeypatch.setattr(expert_web, "ExpertGroupOrchestrator", Harness)
    monkeypatch.setattr(expert_web, "_build_role_agent", lambda *a: pytest.fail("missing environment dispatched a model"))
    events = []
    await expert_web.run_expert_chat(SimpleNamespace(send_json=AsyncMock(side_effect=events.append)), "样本任务",
        session=runtime, confirmed_new_flow=flow,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}), resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda p: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs)
    assert len(checked) == 2, events
    assert all(result.error_kind == "environment_fault" for result in checked)
    assert not any(e.get("error_code") == "capability_preflight" for e in events)
    assert runtime.expert_run_id == "" and runtime.last_expert_run_id
    record = runs.load(runtime.last_expert_run_id)
    assert record["stage_preflight"]["work"][0]["code"] == "base_interpreter"
