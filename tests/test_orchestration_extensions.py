import asyncio
import pytest

from mewcode.experts.definitions import ExpertRegistry
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.orchestration.kinds import expert_kind_registry, kind_roles
from mewcode.orchestration.spec import OrchestrationSpec, OrchestrationError
from mewcode.orchestration.convert import to_expert_flow, from_expert_flow
from tests.test_expert_flow import _profile


def test_custom_kind_is_scoped_and_tools_roundtrip():
    registry = ExpertRegistry()
    registry.register({"name": "researcher", "kind": "research", "tools": ["ReadFile"]})
    registry.register({"name": "intruder", "kind": "audit", "tools": ["*"]})
    token = expert_kind_registry.set(registry)
    try:
        assert kind_roles()["audit"] == "auditor"
        spec = OrchestrationSpec.from_value({"spec_id": "extensions", "require_confirm_gate": False,
            "meta": {"parallel": True}, "nodes": [{"node_id": "read", "kind": "research", "tools": [], "deny_tools": ["Bash"]},
            {"node_id": "done", "kind": "reduce"}], "edges": [{"from": "read", "to": "done"}]})
        flow = to_expert_flow(spec)
        assert flow.nodes[0].role == "researcher" and flow.nodes[0].tools == ()
        restored = from_expert_flow(flow)
        assert restored.nodes[0].tools == () and restored.nodes[0].deny_tools == ("Bash",)
        assert restored.meta["parallel"] is True
    finally:
        expert_kind_registry.reset(token)
    with pytest.raises(OrchestrationError):
        OrchestrationSpec.from_value({"nodes": [{"node_id": "read", "kind": "research"}]})


@pytest.mark.asyncio
async def test_fallback_substitutes_without_rerunning_failed_node(tmp_path):
    flow = ExpertFlowSpec.from_value({"flow_id": "fallback", "finalizer_node_id": "done",
        "nodes": [{"node_id": "work", "role": "executor", "failure_policy": {
            "action": "fallback", "target_node_id": "alternate", "max_attempts": 2}},
            {"node_id": "alternate", "role": "executor"}, {"node_id": "done", "role": "manager"}],
        "edges": [{"from": "work", "to": "done"}]})
    calls = []
    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        return ExpertRoleResult("alternate output" if node.node_id == "alternate" else "result",
            status="failed" if node.node_id == "work" else "completed")
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path).run(
        "task", _profile(), {}, strategy="stable", flow=flow)
    assert calls.count("work") == 1
    assert "alternate" in calls
    assert calls.count("alternate") == 1
    assert result.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel,role,readonly,expected", [
    (False, "planner", True, 1), (True, "planner", True, 2), (True, "executor", False, 1),
    (True, "planner", False, 1), (True, "manager", False, 1)])
async def test_parallel_is_opt_in_and_writers_stay_serial(tmp_path, parallel, role, readonly, expected):
    flow = ExpertFlowSpec.from_value({"flow_id": "parallel", "meta": {"parallel": parallel}, "finalizer_node_id": "done",
        "nodes": [{"node_id": "a", "role": role, "tools": ["ReadFile"] if readonly else None},
            {"node_id": "b", "role": role, "tools": ["ReadFile"] if readonly else None},
            {"node_id": "done", "role": "manager"}],
        "edges": [{"from": "a", "to": "done"}, {"from": "b", "to": "done"}]})
    active = maximum = 0
    async def runner(node, model, prompt, policy, member=None):
        nonlocal active, maximum
        if node.node_id == "done":
            assert active == 0
            return ExpertRoleResult("done")
        assert policy.parallel_readonly == (parallel and readonly)
        active += 1
        maximum = max(active, maximum)
        try:
            await asyncio.sleep(.02)
            return ExpertRoleResult(node.node_id)
        finally:
            active -= 1
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path).run(
        "task", _profile(), {}, strategy="stable", flow=flow)
    assert result.status == "completed" and maximum == expected


def test_tools_hard_boundaries_and_empty_whitelist(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    from mewcode.expert_group import derive_execution_policy
    from mewcode.expert_web import _role_registry
    from mewcode.tools import ToolRegistry
    from mewcode.tools.bash import Bash
    from mewcode.tools.read_file import ReadFile
    from mewcode.tools.write_file import WriteFile
    registry = ToolRegistry()
    for tool in (Bash(), ReadFile(), WriteFile()):
        registry.register(tool)
    session = SimpleNamespace(agent=SimpleNamespace(registry=registry), work_dir=str(tmp_path))
    policy = replace(derive_execution_policy(False), node_tools=("*",), deny_tools=("WriteFile",))
    assert _role_registry(session, "manager", policy).get("Bash") is not None
    assert _role_registry(session, "executor", policy).get("WriteFile") is None
    for role in ("executor", "manager"):
        assert _role_registry(session, role, replace(policy, node_tools=())).get("ReadFile") is None
    empty_auditor = _role_registry(session, "auditor", replace(policy, node_tools=()))
    assert empty_auditor.get("InspectGitState") is None and empty_auditor.get("RunVerification") is None
    for scoped in (policy, replace(policy, audit_contract={"artifact_dir": str(tmp_path)}),
                   replace(policy, parallel_readonly=True), replace(policy, loop_context={"reviewer": True})):
        actual = _role_registry(session, "auditor", scoped)
        assert actual.get("Bash") is None and actual.get("WriteFile") is None
    readonly = _role_registry(session, "manager", replace(policy, parallel_readonly=True))
    assert readonly.get("Bash") is None and readonly.get("ReadFile") is not None


def test_tools_survive_canvas_roundtrip():
    from mewcode.orchestration import to_pipeline_graph, from_pipeline_graph
    spec = OrchestrationSpec.from_value({"spec_id": "tools", "nodes": [
        {"node_id": "done", "kind": "reduce", "tools": ["ReadFile", "Bash"], "deny_tools": ["WriteFile"]}]})
    actual = from_pipeline_graph(to_pipeline_graph(spec))
    assert actual.nodes[0].tools == ("ReadFile", "Bash")
    assert actual.nodes[0].deny_tools == ("WriteFile",)


def test_parallel_switch_is_semantic_but_false_preserves_legacy_fingerprint():
    from mewcode.orchestration.revisions import execution_fingerprint
    flow = {"nodes": [{"node_id": "work", "role": "planner"}], "edges": []}
    legacy = execution_fingerprint(flow)
    assert execution_fingerprint({**flow, "meta": {"parallel": False}}) == legacy
    assert execution_fingerprint({**flow, "meta": {"parallel": True}}) != legacy
