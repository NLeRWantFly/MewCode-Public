"""Fix edits real node bindings without losing versions or run isolation."""
import copy
import json

import httpx
import pytest

from frontend import orch_maintenance, server
from mewcode.orchestration.maintenance import SCHEMA, preview_proposal
from mewcode.orchestration.maintenance_agent import FIX_SYSTEM
from mewcode.orchestration.maintenance_revision import future_revision
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.spec import OrchestrationSpec
from tests.test_orchestration_maintenance import maintenance_env, poll, spec_for, update_proposal
from tests.test_orchestration_maintenance_safety import paused_run


ROUTES = [{"provider_key": "selected", "models": [
    {"model_id": "selected-model", "efforts": ["low", "high", "max"]},
    {"model_id": "deepseek-flash", "efforts": ["low", "high", "max"]}]}]
BINDING = {"provider_key": "selected", "model_id": "deepseek-flash", "effort": "max"}


def publication_spec():
    ids = [("N02", "gate"), ("N11", "gate"), ("N12A", "implement"),
           ("N12A_RESULT_AUDIT", "audit"), ("N12B", "reduce")]
    return OrchestrationSpec.from_value({"spec_id": "publish", "label": "发布核验", "goal": "完整核验后交付",
        "finalizer_node_id": "N12B", "nodes": [
            {"node_id": identity, "label": identity, "kind": kind, "goal": "保留原有职责",
             "model": None if kind == "gate" or identity == "N12A_RESULT_AUDIT" else
                {"provider_key": "selected", "model_id": "selected-model"},
             "bind_later": identity == "N12A_RESULT_AUDIT",
             "plan_brief": {"outputs": ["明确结果"], "acceptance": ["完整核对原始要求"]}}
            for identity, kind in ids],
        "edges": [{"from_node": left[0], "to_node": right[0], "condition": "pass"}
                  for left, right in zip(ids, ids[1:])]})


def test_fix_can_bind_only_the_missing_audit_and_leave_gates_and_other_nodes_identical():
    before = publication_spec().as_dict()
    original = copy.deepcopy(before)
    preview = preview_proposal(before, "fix", [], update_proposal("N12A_RESULT_AUDIT", model=BINDING), model_routes=ROUTES)
    after = {node["node_id"]: node for node in preview["spec"]["nodes"]}
    assert before == original
    assert after["N12A_RESULT_AUDIT"]["model"] == BINDING
    assert not after["N12A_RESULT_AUDIT"]["bind_later"]
    for node in before["nodes"]:
        if node["node_id"] != "N12A_RESULT_AUDIT":
            assert after[node["node_id"]] == node
    assert [row["id"] for row in preview["changes"]] == ["N12A_RESULT_AUDIT"]
    assert preview["spec"]["edges"] == before["edges"]


def test_fix_can_change_effort_and_label_without_changing_model_route_or_requirements():
    before = spec_for().as_dict()
    preview = preview_proposal(before, "fix", ["work"], update_proposal(model={"effort": "max"}, label="核验阶段"), model_routes=ROUTES)
    changed = preview["spec"]["nodes"][1]
    assert changed["model"] == {**before["nodes"][1]["model"], "effort": "max"}
    assert changed["label"] == "核验阶段"
    assert changed["plan_brief"] == before["nodes"][1]["plan_brief"]


@pytest.mark.parametrize("binding,routes", [
    (None, ROUTES), ({}, ROUTES), ({"model_id": "missing"}, ROUTES),
    ({**BINDING, "provider_key": "unknown"}, ROUTES), ({**BINDING, "effort": "ultra"}, ROUTES),
    ({**BINDING, "effort": False}, ROUTES), ({**BINDING, "api_key": "never-store"}, ROUTES),
    ({"model_id": "", "provider_key": ""}, ROUTES), (BINDING, None), (BINDING, []),
])
def test_invalid_or_unconfigured_model_edits_are_rejected(binding, routes):
    with pytest.raises(ValueError):
        preview_proposal(spec_for().as_dict(), "fix", [], update_proposal(model=binding), model_routes=routes)


def test_gate_model_change_is_rejected_and_update_scope_remains_separate():
    with pytest.raises(ValueError, match="人工确认门禁"):
        preview_proposal(publication_spec().as_dict(), "fix", [], update_proposal("N02", model=BINDING), model_routes=ROUTES)
    with pytest.raises(ValueError):
        preview_proposal(spec_for().as_dict(), "update", ["work"], update_proposal(model=BINDING), model_routes=ROUTES)


def test_fix_prompt_does_not_contradict_node_model_edit_capability():
    assert "label/goal/plan_brief/model" in FIX_SYSTEM
    assert "available_model_routes" in FIX_SYSTEM
    assert "人工 gate 无需模型绑定" in FIX_SYSTEM
    for forbidden in ("不改模型绑定", "不得更改模型", "不降低 effort"):
        assert forbidden not in FIX_SYSTEM


def test_future_run_uses_new_node_binding_and_keeps_other_locks_and_receipts():
    before = spec_for()
    preview = preview_proposal(before.as_dict(), "fix", [], update_proposal(model=BINDING), model_routes=ROUTES)
    flow = to_expert_flow(before).as_dict()
    snapshot = {"status": "paused", "locked_flow": flow, "active_node_ids": [],
                "node_states": {"first": {"status": "completed", "text": "原成果"}, "work": {"status": "pending"}},
                "budget": {"tokens": 1200}, "host_execution_scope": {"task_id": "original"},
                "locked_models": {"first": {"model_id": "keep-first"}, "implement": {"model_id": "legacy-role"},
                                  "work": {"model_id": "old-node"}}}
    original = copy.deepcopy(snapshot)
    revised = future_revision(snapshot, OrchestrationSpec.from_value(preview["spec"]), affected=["work"],
                              maintenance_id="fix-model", base_spec=before)
    assert snapshot == original
    assert revised["locked_models"]["work"] == BINDING
    for identity in ("first", "implement"):
        assert revised["locked_models"][identity] == original["locked_models"][identity]
    for key in ("budget", "host_execution_scope"):
        assert revised[key] == original[key]
    assert revised["node_states"]["first"] == original["node_states"]["first"]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_state", ["none", "pending", "unknown"])
async def test_fix_model_preview_apply_and_run_adoption(maintenance_env, monkeypatch, run_state):
    app, owner, _, _, spec = maintenance_env
    monkeypatch.setattr("mewcode.orchestration.graph_models.available_model_routes", lambda _: copy.deepcopy(ROUTES))
    run_id = paused_run(owner, spec, unknown=run_state == "unknown") if run_state != "none" else None
    old_run = server.EXPERT_RUNS.load(run_id) if run_id else None
    async def model(client, **kwargs):
        context = json.loads(kwargs["user_text"])
        assert context["runtime"]["available_model_routes"] == ROUTES
        return json.dumps({"schema": SCHEMA, "reply": "已形成模型修改差异，等待应用", "proposal": update_proposal(model=BINDING)})
    monkeypatch.setattr(orch_maintenance, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        state = (await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix"})).json()
        await client.post("/api/orchestration/maintenance/send", json={**owner, "id": state["id"], "revision": state["revision"],
                                                                     "message": "仅把 work 模型改为 deepseek-flash，effort=max"})
        ready = await poll(client, owner, state)
        assert ready["proposal"] is not None and not ready["error"]
        assert server._conversation_plan_store().get(owner["project"]).spec.as_dict() == spec.as_dict()
        response = await client.post("/api/orchestration/maintenance/apply", json={**owner, "id": ready["id"],
            "revision": ready["revision"], "proposal_id": ready["proposal"]["id"]})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["spec"]["nodes"][1]["model"] == BINDING
        if run_id:
            current = server.EXPERT_RUNS.load(run_id)
            if run_state == "unknown":
                assert not result["run_adopted"] and current == old_run
            else:
                assert result["run_adopted"] and current["locked_models"]["work"] == BINDING
                assert current["node_states"]["first"] == old_run["node_states"]["first"]


@pytest.mark.asyncio
async def test_removed_model_route_invalidates_apply_before_any_graph_save(maintenance_env, monkeypatch):
    app, owner, _, _, spec = maintenance_env
    routes = copy.deepcopy(ROUTES)
    monkeypatch.setattr("mewcode.orchestration.graph_models.available_model_routes", lambda _: routes)
    opened = server.MAINTENANCE_STORE.open(owner["project"], owner["conversation_id"], spec.as_dict(), mode="fix", node_ids=[])
    started = server.MAINTENANCE_STORE.begin(owner["project"], owner["conversation_id"], opened["id"], revision=0,
                                           message="改模型", base=spec.as_dict())
    ready = server.MAINTENANCE_STORE.complete(owner["project"], owner["conversation_id"], opened["id"],
        call_id=started["call_id"], response={"schema": SCHEMA, "reply": "等待应用", "proposal": update_proposal(model=BINDING)},
        base=spec.as_dict(), model_routes=routes)
    routes.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/maintenance/apply", json={**owner, "id": ready["id"],
            "revision": ready["revision"], "proposal_id": ready["proposal"]["id"]})
        assert response.status_code == 409 and "可用配置" in response.json()["error"]
    assert server._conversation_plan_store().get(owner["project"]).spec.as_dict() == spec.as_dict()
