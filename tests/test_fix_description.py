"""Fix uses an error description to repair root causes across the whole flow."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from frontend import orch_maintenance, server
from mewcode.orchestration.maintenance import SCHEMA, model_prompt, preview_proposal, resolve_targets
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.maintenance_revision import future_revision
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.pipeline.store import PipelineStore
from tests.test_expert_task_identity import runtime_for
from tests.test_orchestration_maintenance import maintenance_env as maintenance_env, poll, spec_for, update_proposal
from tests.test_orchestration_maintenance_safety import paused_run


DESCRIPTION = "work 的 LLM 提示上游执行说明互相矛盾，导致汇总阶段没有核验结果；请定位根因并修复。"


def cross_node_repair():
    return {"summary": "从执行说明的矛盾定位到准备与汇总阶段，一并澄清核验顺序",
        "node_changes": [
            {"node_id": "first", "changes": {"goal": "先核对原始输入，再交付原约定的成果"}},
            {"node_id": "done", "changes": {"plan_brief": {"steps": ["核对上游结果，再按原验收要求汇总"]}}}]}


def test_fix_preserves_error_text_and_focus_is_only_a_diagnostic_hint():
    spec = spec_for()
    focus, text = resolve_targets(spec, "fix", DESCRIPTION, stage_cursor="work")
    assert focus == ["work"] and text == DESCRIPTION
    state = {"id": "maintenance", "mode": "fix", "node_ids": focus, "base": spec.as_dict(),
             "messages": [{"role": "user", "text": text}]}
    prompt = json.loads(model_prompt(state, {}))
    assert prompt["repair_scope"] == "entire_graph" and prompt["focus_node_ids"] == ["work"]
    assert prompt["allowed_node_ids"] == ["first", "work", "done"]
    assert prompt["history"][-1]["text"] == DESCRIPTION
    preview = preview_proposal(spec.as_dict(), "fix", focus, cross_node_repair())
    assert [row["id"] for row in preview["changes"]] == ["first", "done"]
    assert preview["graph_changed"] is True and preview["affected_node_ids"] == ["done", "first", "work"]
    assert preview["spec"]["goal"] == spec.goal
    for node in preview["spec"]["nodes"]:
        before = spec.node_map()[node["node_id"]].as_dict()
        assert node["model"] == before["model"]
        for key in ("known_inputs", "outputs", "acceptance"):
            assert node["plan_brief"][key] == before["plan_brief"][key]


@pytest.mark.parametrize("field,value", [("known_inputs", ["替换输入"]), ("outputs", ["替换产出"]),
    ("acceptance", ["减少验收"]), ("acceptance", [])])
def test_fix_cannot_change_existing_inputs_deliverables_or_acceptance(field, value):
    with pytest.raises(ValueError, match="保留原有"):
        preview_proposal(spec_for().as_dict(), "fix", ["work"], update_proposal(plan_brief={field: value}))


@pytest.mark.parametrize("changes", [{"model": {}}, {"tools": ["Bash"]}, {"max_audit_retries": 999}, {"kind": "implement"}])
def test_description_driven_fix_cannot_grant_authority(changes):
    with pytest.raises(ValueError):
        preview_proposal(spec_for().as_dict(), "fix", ["work"], update_proposal(**changes))


@pytest.mark.parametrize("actions", ["repair_runtime", ["run_command"], ["repair_runtime", "repair_runtime"], [{}], [True]])
def test_model_cannot_propose_arbitrary_host_actions(actions):
    with pytest.raises(ValueError):
        preview_proposal(spec_for().as_dict(), "fix", [], {"summary": "恢复环境", "runtime_actions": actions})


def test_runtime_only_repair_is_a_concrete_preview_without_graph_changes():
    original = spec_for().as_dict()
    proposal = {"summary": "Task ID 与原任务不一致，由宿主恢复原身份", "runtime_actions": ["repair_runtime"]}
    preview = preview_proposal(original, "fix", ["work"], proposal)
    assert preview["graph_changed"] is False and preview["affected_node_ids"] == []
    assert preview["runtime_actions"] == ["repair_runtime"] and preview["spec"] == original
    assert preview["changes"][0]["kind"] == "runtime"
    with pytest.raises(ValueError):
        preview_proposal(original, "update", [], proposal)


def test_fixing_a_nested_refinement_protects_its_completed_execution_parent():
    raw = spec_for().as_dict()
    child = copy.deepcopy(raw["nodes"][1])
    child.update(node_id="child", label="细化步骤", goal="依据原输入细化核验")
    raw["nodes"].append(child)
    raw["edges"].append({"from_node": "work", "to_node": "child", "condition": "always"})
    raw["meta"]["canvas_groups"] = {"work": {"children": ["child"], "expanded": True}}
    spec = OrchestrationSpec.from_value(raw)
    old = to_expert_flow(spec)
    assert "child" not in old.node_map
    preview = preview_proposal(raw, "fix", ["done"], update_proposal("child", goal="先核对原输入再执行细化核验"))
    assert {"child", "work", "done"} <= set(preview["affected_node_ids"])
    snapshot = {"status": "paused", "locked_flow": old.as_dict(), "node_states": {"work": {"status": "completed"}}}
    with pytest.raises(ValueError, match="已完成"):
        # Even a caller supplying only the visible child cannot omit the
        # scheduling parent that actually owns its execution contract.
        future_revision(snapshot, OrchestrationSpec.from_value(preview["spec"]), affected=["child"],
                        maintenance_id="fix", base_spec=spec)


async def ask(client, owner, description, *, focus=None):
    opened = await client.post("/api/orchestration/maintenance/open", json={**owner, "mode": "fix", "request": description,
        **({"node_ids": focus} if focus is not None else {})})
    assert opened.status_code == 200, opened.text
    state = opened.json()
    response = await client.post("/api/orchestration/maintenance/send", json={**owner, "id": state["id"],
        "revision": state["revision"], "message": state["initial_message"]})
    assert response.status_code == 200, response.text
    state = await poll(client, owner, state)
    assert state["proposal"], state
    return {**owner, "id": state["id"], "revision": state["revision"], "proposal_id": state["proposal"]["id"]}, state


def bind_checker(owner, monkeypatch):
    _, checker = runtime_for(Path(owner["project"]))
    authorization = TaskAuthorization.from_user_scope({"allowed_tools": ["ReadFile"]}, task_id="original-task")
    records = {"task_authorizations": {owner["conversation_id"] + ":original-task": authorization.as_dict()}}
    monkeypatch.setattr(server.SESSION, "agent", SimpleNamespace(permission_checker=checker,
        safety_reviewer=SimpleNamespace(authorization_version="")))
    monkeypatch.setattr(server.SESSION, "web_state", copy.deepcopy(records))
    return checker, authorization, records


def history_bytes(durable):
    path = durable._sessions_dir / (durable.session_id + ".jsonl")
    return path.read_bytes() if path.exists() else None


@pytest.mark.asyncio
async def test_error_description_locates_and_applies_multiple_related_nodes(maintenance_env, monkeypatch):
    app, owner, _, durable, original = maintenance_env
    history = history_bytes(durable)
    calls = []
    async def model(client, **kwargs):
        assert client == ("selected-model", "high")
        prompt = json.loads(kwargs["user_text"])
        assert prompt["history"][-1]["text"] == DESCRIPTION
        assert prompt["focus_node_ids"] == ["work"] and prompt["allowed_node_ids"] == ["first", "work", "done"]
        calls.append(prompt)
        return json.dumps({"schema": SCHEMA, "reply": "症状在 work，根因在上游执行说明，并影响汇总步骤。",
                           "proposal": cross_node_repair()})
    monkeypatch.setattr(orch_maintenance, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request, state = await ask(client, owner, DESCRIPTION)
        assert server._conversation_plan_store().get(owner["project"]).spec.as_dict() == original.as_dict()
        assert [row["id"] for row in state["proposal"]["changes"]] == ["first", "done"]
        response = await client.post("/api/orchestration/maintenance/apply", json=request)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["spec"]["version"] == 2 and result["graph_changed"] is True
        saved = server._conversation_plan_store().get(owner["project"]).spec
        assert saved.node_map()["first"].goal != original.node_map()["first"].goal
        assert saved.node_map()["done"].plan_brief.steps == ("核对上游结果，再按原验收要求汇总",)
        assert saved.node_map()["work"] == original.node_map()["work"]
    assert len(calls) == 1 and history_bytes(durable) == history


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed,native_ready,unknown", [(False, True, False), (False, False, False),
    (True, True, False), (True, True, True)])
async def test_llm_runtime_diagnosis_dispatches_fixed_host_repair(maintenance_env, monkeypatch, mixed, native_ready, unknown):
    app, owner, _, durable, spec = maintenance_env
    identity = paused_run(owner, spec, unknown=unknown)
    original = server.EXPERT_RUNS.load(identity)
    checker, authorization, records = bind_checker(owner, monkeypatch)
    history = history_bytes(durable)
    graph_before = PipelineStore(owner["project"]).get_pipeline("maintenance-test").model_dump(mode="json")
    calls = []
    from mewcode.sandbox import native_recovery
    def recover():
        calls.append(True)
        return {"ready": native_ready, "action": "restored" if native_ready else "blocked", "reason": "需要原验收材料"}
    monkeypatch.setattr(native_recovery, "recover_native_runtime", recover)
    async def model(*args, **kwargs):
        prompt = json.loads(kwargs["user_text"])
        assert prompt["runtime"]["task_identity_error"]
        proposal = update_proposal(goal="依据原输入逐项核验，保留原验收") if mixed else {"summary": "恢复原任务身份与已授权环境"}
        proposal["runtime_actions"] = ["repair_runtime"]
        return json.dumps({"schema": SCHEMA, "reply": "从宿主诊断定位到原任务身份绑定问题，调用固定宿主检修。", "proposal": proposal})
    monkeypatch.setattr(orch_maintenance, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request, state = await ask(client, owner, "LLM 报错 TaskID_required；请自己定位并修复。", focus=["work"])
        assert not calls  # Only the concrete applied action mutates the host.
        response = await client.post("/api/orchestration/maintenance/apply", json=request)
        assert response.status_code == 200, response.text
        data, changed = response.json(), server.EXPERT_RUNS.load(identity)
        assert data["result"]["runtime_repaired"] is native_ready
        assert data["status"] == ("applied" if native_ready or mixed else "ready")
        assert changed["status"] == "paused" and changed["task"] == original["task"]
        # Derived ledger projections may refresh during adoption; completed
        # business evidence and its execution status must remain untouched.
        stable = lambda stage: {k: v for k, v in stage.items() if k not in {"ledger_view", "command_receipts"}}
        assert stable(changed["node_states"]["first"]) == stable(original["node_states"]["first"])
        assert changed["node_states"]["first"].get("command_receipts", []) == original["node_states"]["first"].get("command_receipts", [])
        assert changed["budget"] == original["budget"] and changed["locked_models"] == original["locked_models"]
        assert checker.task_id == "original-task" and checker.task_authorization == authorization
        assert server.SESSION.web_state == records and calls == [True]
        assert data["result"]["run_adopted"] is (mixed and not unknown)
        if mixed:
            assert data["result"]["spec"]["version"] == 2
        else:
            assert data["result"]["spec"]["version"] == 1
            assert PipelineStore(owner["project"]).get_pipeline("maintenance-test").model_dump(mode="json") == graph_before
            assert server._conversation_plan_store().get(owner["project"]).spec.as_dict() == spec.as_dict()
        if not native_ready:
            assert "阻塞" in data["error"] and data["proposal"]
            if unknown:
                assert changed["node_states"]["work"]["command_receipts"] == original["node_states"]["work"]["command_receipts"]
                with pytest.raises(ValueError) as rejected:
                    server.EXPERT_RUNS.resume(identity, strategy="stable", flow=data["result"]["flow"])
                assert rejected.value.run_adoption["status"] == "choice_required"
                assert {choice["id"] for choice in rejected.value.run_adoption["choices"]} == {
                    "rebase_node", "use_old_graph", "new_run"}
    assert history_bytes(durable) == history


@pytest.mark.asyncio
async def test_mixed_repair_reports_saved_graph_if_host_operation_fails(maintenance_env, monkeypatch):
    app, owner, _, _, _ = maintenance_env
    from mewcode.sandbox import native_recovery
    def recover():
        raise OSError("host repair unavailable")
    monkeypatch.setattr(native_recovery, "recover_native_runtime", recover)
    async def model(*args, **kwargs):
        proposal = update_proposal(goal="澄清原有执行说明")
        proposal["runtime_actions"] = ["repair_runtime"]
        return json.dumps({"schema": SCHEMA, "reply": "修复执行说明并检修环境", "proposal": proposal})
    monkeypatch.setattr(orch_maintenance, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request, _ = await ask(client, owner, "执行说明冲突并有环境错误，请修复。")
        response = await client.post("/api/orchestration/maintenance/apply", json=request)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["spec"]["version"] == 2 and "新图已保存" in result["warning"]
        assert "宿主检修未完成" in result["warning"]
        assert server._conversation_plan_store().get(owner["project"]).spec.version == 2
