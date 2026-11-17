"""Run-scoped maintenance actions are previewed, confirmed and persisted safely."""
import asyncio
import copy
import json

import httpx
import pytest

from frontend import server
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.node_ledger import ledger_for_run, successful_receipt
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.maintenance import (
    SCHEMA, confirmed_run_action_ids, preview_proposal,
)
from mewcode.orchestration.maintenance_revision import (
    apply_run_actions, apply_spec_run_actions, future_revision,
)
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.pipeline.store import PipelineStore
from tests.test_orchestration_maintenance import maintenance_env, spec_for


def run_snapshot(spec, *, work_state=None, run_id="a" * 32):
    return {"run_id": run_id, "status": "paused", "locked_flow": to_expert_flow(spec).as_dict(),
        "node_states": {"work": copy.deepcopy(work_state or {"status": "failed"})},
        "stages": {}, "run_inputs": {}, "loop_states": {}, "pipeline_runs": {},
        "command_receipts": {}, "review_queues": {}, "review_queue_refs": {}}


def test_preview_requires_explicit_confirmation_for_tools_inputs_and_run_values():
    raw = spec_for().as_dict()
    raw["goal_contract"] = {"objective": "核验", "acceptance": ["通过"],
                            "run_inputs": [{"name": "mode", "required": True}]}
    spec = OrchestrationSpec.from_value(raw)
    snapshot = run_snapshot(spec)
    snapshot["run_inputs"] = {"mode": "survey"}
    actions = [
        {"action": "set_node_tools", "node_id": "work", "tools": ["Read"]},
        {"action": "set_known_inputs", "node_id": "work", "known_inputs": ["新输入"]},
        {"action": "set_run_inputs", "values": {"mode": "audit"}},
    ]
    proposal = preview_proposal(spec.as_dict(), "fix", [], {"summary": "修正运行配置", "run_actions": actions},
                                run_snapshot=snapshot)
    reviewed = proposal["run_actions"]
    assert all(item["requires_confirmation"] for item in reviewed)
    assert reviewed[2]["before"] == {"mode": "survey"}
    assert reviewed[2]["after"] == {"mode": "audit"}
    with pytest.raises(ValueError, match="确认 ID"):
        confirmed_run_action_ids(reviewed, ["not-in-preview"])

    unconfirmed = apply_spec_run_actions(OrchestrationSpec.from_value(proposal["spec"]), reviewed, [])
    assert unconfirmed.node_map()["work"].tools == spec.node_map()["work"].tools
    assert unconfirmed.node_map()["work"].plan_brief.known_inputs == spec.node_map()["work"].plan_brief.known_inputs
    ids = [item["id"] for item in reviewed]
    confirmed = apply_spec_run_actions(OrchestrationSpec.from_value(proposal["spec"]), reviewed, ids)
    assert confirmed.node_map()["work"].tools == ("Read",)
    assert confirmed.node_map()["work"].plan_brief.known_inputs == ("新输入",)
    changed_run = apply_run_actions(snapshot, reviewed, [ids[2]], maintenance_id="m" )["snapshot"]
    assert changed_run["run_inputs"] == {"mode": "audit"}


def test_rebase_advances_only_incomplete_node_and_keeps_receipt_and_artifact_history():
    spec = spec_for()
    receipt = {"request_id": "r-success", "status": "completed", "command_started": True,
               "effect_class": "readonly", "stdout": "Python 3.12", "exit_code": 0}
    snapshot = run_snapshot(spec, work_state={"status": "failed", "command_receipts": [receipt],
                                               "artifacts": [{"path": "out/report.json"}]})
    action = {"id": "rebase", "action": "rebase_node", "node_id": "work", "requires_confirmation": False,
              "reason": "恢复本节点执行"}
    result = apply_run_actions(snapshot, [action], maintenance_id="maintenance-1")
    view = ledger_for_run(result["snapshot"]).view("work")
    assert result["rebased_node_ids"] == ["work"]
    assert view.epoch == 1 and view.phase == "ready"
    assert any(successful_receipt(fact.payload) for fact in view.current_receipts)
    assert [fact.payload.get("path") for fact in view.artifacts] == ["out/report.json"]
    assert result["snapshot"]["node_states"]["work"]["command_receipts"] == [receipt]

    done = run_snapshot(spec, work_state={"status": "completed", "passed": True,
                                           "artifacts": [{"path": "out/final.json"}]})
    preserved = apply_run_actions(done, [action], maintenance_id="maintenance-2")
    assert preserved["rebased_node_ids"] == []
    assert preserved["snapshot"]["node_states"]["work"]["status"] == "completed"
    assert preserved["snapshot"]["node_states"]["work"]["artifacts"] == [{"path": "out/final.json"}]


def test_verify_unknown_receipt_registers_effect_check_without_marking_success():
    spec = spec_for()
    receipt = {"request_id": "r-unknown", "status": "outcome_unknown", "command_started": True,
               "effect_class": "write", "request": {"command": "write-data"}}
    snapshot = run_snapshot(spec, work_state={"status": "failed", "command_receipts": [receipt]})
    action = {"id": "verify", "action": "resolve_receipts", "node_id": "work", "requires_confirmation": False,
              "receipt_ids": ["r-unknown"], "mode": "verify"}
    result = apply_run_actions(snapshot, [action], maintenance_id="maintenance-verify")
    value = result["snapshot"]
    view = ledger_for_run(value).view("work")
    assert value["node_states"]["work"]["status"] == "needs_attention"
    assert any(fact.payload.get("kind") == "unknown_effect" for fact in view.active_blockers)
    assert any(fact.payload.get("kind") == "effect_verification" for fact in view.history if fact.kind == "assistance")
    assert not any(successful_receipt(fact.payload) for fact in view.current_receipts)


def test_graph_revision_can_skip_existing_receipts_only_for_an_explicit_rebase():
    spec = spec_for()
    receipt = {"request_id": "r-success", "status": "completed", "command_started": True,
               "effect_class": "readonly", "exit_code": 0}
    snapshot = run_snapshot(spec, work_state={"status": "failed", "command_receipts": [receipt],
                                               "artifacts": [{"path": "out/report.json"}]})
    action = {"id": "rebase", "action": "rebase_node", "node_id": "work", "requires_confirmation": False,
              "reason": "恢复本节点执行"}
    prepared = apply_run_actions(snapshot, [action], maintenance_id="maintenance-3")
    changed = replace_goal(spec)
    revision = future_revision(prepared["snapshot"], changed, affected=["work"], maintenance_id="maintenance-3",
                               base_spec=spec, rebased_node_ids=prepared["rebased_node_ids"])
    view = ledger_for_run(revision).view("work")
    assert revision["node_states"]["work"]["status"] == "pending"
    assert any(successful_receipt(fact.payload) for fact in view.current_receipts)
    assert [fact.payload.get("path") for fact in view.artifacts] == ["out/report.json"]
    with pytest.raises(ValueError, match="执行记录"):
        future_revision(snapshot, changed, affected=["work"], maintenance_id="maintenance-4", base_spec=spec)


def replace_goal(spec):
    value = spec.as_dict()
    value["nodes"] = [dict(node, goal="增加逐项核验") if node["node_id"] == "work" else node
                      for node in value["nodes"]]
    return OrchestrationSpec.from_value(value)


@pytest.mark.asyncio
async def test_apply_uses_stored_confirmation_ids_and_persists_stopped_run_by_cas(
        maintenance_env, monkeypatch):
    app, owner, current, durable, spec = maintenance_env
    identity = "b" * 32
    server.EXPERT_RUNS.create(run_id=identity, conversation_id=owner["conversation_id"], task="原任务",
        attachments=[], profile={"name": "locked"}, strategy="stable", flow=to_expert_flow(spec))
    receipt = {"request_id": "r-success", "status": "completed", "command_started": True,
               "effect_class": "readonly", "exit_code": 0}
    initial = server.EXPERT_RUNS.mutate(identity, lambda value: value.update(status="paused", stage_cursor="work",
        node_states={"work": {"status": "failed", "command_receipts": [receipt],
                               "artifacts": [{"path": "out/report.json"}]}}))
    durable.meta.dialogue_focus = {"kind": "orchestration", "run_id": identity}
    actions = [
        {"action": "set_node_tools", "node_id": "work", "tools": ["Read"]},
        {"action": "set_known_inputs", "node_id": "work", "known_inputs": ["已确认的新输入"]},
        {"action": "rebase_node", "node_id": "work", "reason": "恢复受阻节点"},
    ]
    store = server.MAINTENANCE_STORE
    state = store.open(owner["project"], owner["conversation_id"], spec.as_dict(), mode="fix", run_id=identity)
    running = store.begin(owner["project"], owner["conversation_id"], state["id"], revision=state["revision"],
                          message="调整工作节点输入", base=spec.as_dict())
    done = store.complete(owner["project"], owner["conversation_id"], state["id"], call_id=running["call_id"],
        response={"schema": SCHEMA, "reply": "已准备维护预览。",
                  "proposal": {"summary": "逐项修复工作节点", "run_actions": actions}},
        base=spec.as_dict(), run_snapshot=initial)
    preview = done["proposal"]["run_actions"]
    confirm_id = next(row["id"] for row in preview if row["action"] == "set_known_inputs")
    cas_calls = []
    original_cas = server.EXPERT_RUNS.mutate_stopped
    def track_cas(run_id, mutator, *, conversation_id, expected_updated_at):
        cas_calls.append((run_id, conversation_id, expected_updated_at))
        return original_cas(run_id, mutator, conversation_id=conversation_id,
                            expected_updated_at=expected_updated_at)
    monkeypatch.setattr(server.EXPERT_RUNS, "mutate_stopped", track_cas)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        bad = await client.post("/api/orchestration/maintenance/apply", json={**owner, "id": done["id"],
            "revision": done["revision"], "proposal_id": done["proposal"]["id"],
            "confirmed_run_action_ids": ["forged"]})
        assert bad.status_code == 409
        assert server.EXPERT_RUNS.load(identity)["updated_at"] == initial["updated_at"]
        response = await client.post("/api/orchestration/maintenance/apply", json={**owner, "id": done["id"],
            "revision": done["revision"], "proposal_id": done["proposal"]["id"],
            "confirmed_run_action_ids": [confirm_id]})
        assert response.status_code == 200, response.text
        result = response.json()
    saved = server.EXPERT_RUNS.load(identity)
    graph_node = server._conversation_plan_store(owner["conversation_id"]).get(owner["project"]).spec.node_map()["work"]
    assert graph_node.plan_brief.known_inputs == ("已确认的新输入",)
    assert graph_node.tools == spec.node_map()["work"].tools  # unchecked tool action was ignored
    assert saved["status"] == "paused" and saved["node_states"]["work"]["status"] == "pending"
    assert saved["node_states"]["work"]["command_receipts"] == [receipt]
    assert saved["node_states"]["work"]["artifacts"] == [{"path": "out/report.json"}]
    assert ledger_for_run(saved).view("work").epoch == 1
    assert result["result"]["run_adopted"] is True
    assert cas_calls == [(identity, owner["conversation_id"], initial["updated_at"])]


@pytest.mark.asyncio
async def test_failed_automatic_adoption_returns_choices_and_use_old_graph_is_persisted(maintenance_env):
    app, owner, current, durable, spec = maintenance_env
    identity = "c" * 32
    server.EXPERT_RUNS.create(run_id=identity, conversation_id=owner["conversation_id"], task="原任务",
        attachments=[], profile={}, strategy="stable", flow=to_expert_flow(spec))
    initial = server.EXPERT_RUNS.mutate(identity, lambda value: value.update(status="paused", stage_cursor="work",
        node_states={"work": {"status": "failed", "tool_calls": [{"name": "Read", "status": "completed"}]}}))
    durable.meta.dialogue_focus = {"kind": "orchestration", "run_id": identity}
    store = server.MAINTENANCE_STORE
    state = store.open(owner["project"], owner["conversation_id"], spec.as_dict(), mode="fix", run_id=identity)
    running = store.begin(owner["project"], owner["conversation_id"], state["id"], revision=state["revision"],
                          message="确认节点输入", base=spec.as_dict())
    done = store.complete(owner["project"], owner["conversation_id"], state["id"], call_id=running["call_id"],
        response={"schema": SCHEMA, "reply": "图修订需要你选择如何接纳。", "proposal": {"summary": "补充节点输入",
            "run_actions": [{"action": "set_known_inputs", "node_id": "work", "known_inputs": ["新版输入"]}]}},
        base=spec.as_dict(), run_snapshot=initial)
    action_id = done["proposal"]["run_actions"][0]["id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        applied = await client.post("/api/orchestration/maintenance/apply", json={**owner, "id": done["id"],
            "revision": done["revision"], "proposal_id": done["proposal"]["id"],
            "confirmed_run_action_ids": [action_id]})
        assert applied.status_code == 200, applied.text
        state_after_apply = applied.json()
        assert state_after_apply["run_adoption"]["status"] == "choice_required"
        assert {item["id"] for item in state_after_apply["run_adoption"]["choices"]} == {
            "rebase_node", "use_old_graph", "new_run"}
        chosen = await client.post("/api/orchestration/maintenance/choose-adoption", json={**owner,
            "id": done["id"], "revision": state_after_apply["revision"],
            "proposal_id": done["proposal"]["id"], "choice": "use_old_graph"})
        assert chosen.status_code == 200, chosen.text
        choice_result = chosen.json()
    saved = server.EXPERT_RUNS.load(identity)
    assert saved["maintenance_use_old_graph"] == done["id"]
    assert saved["locked_flow"] == initial["locked_flow"]
    assert choice_result["run_adoption"]["status"] == "adopted"
    assert server._conversation_plan_store(owner["conversation_id"]).get(owner["project"]).spec.node_map()["work"].plan_brief.known_inputs == ("新版输入",)


@pytest.mark.asyncio
async def test_new_run_choice_creates_and_launches_new_graph_without_mutating_old_run(maintenance_env, monkeypatch):
    app, owner, current, durable, spec = maintenance_env
    identity = "d" * 32
    server.EXPERT_RUNS.create(run_id=identity, conversation_id=owner["conversation_id"], task="保留的旧任务",
        attachments=[], profile={"name": "locked-profile"}, strategy="stable", flow=to_expert_flow(spec))
    initial = server.EXPERT_RUNS.mutate(identity, lambda value: value.update(status="paused", stage_cursor="work",
        node_states={"work": {"status": "failed", "tool_calls": [{"name": "Read", "status": "completed"}]}}))
    durable.meta.dialogue_focus = {"kind": "orchestration", "run_id": identity}
    store = server.MAINTENANCE_STORE
    state = store.open(owner["project"], owner["conversation_id"], spec.as_dict(), mode="fix", run_id=identity)
    running = store.begin(owner["project"], owner["conversation_id"], state["id"], revision=state["revision"],
                          message="确认节点输入", base=spec.as_dict())
    done = store.complete(owner["project"], owner["conversation_id"], state["id"], call_id=running["call_id"],
        response={"schema": SCHEMA, "reply": "图修订需要你选择如何接纳。", "proposal": {"summary": "补充节点输入",
            "run_actions": [{"action": "set_known_inputs", "node_id": "work", "known_inputs": ["新版输入"]}]}},
        base=spec.as_dict(), run_snapshot=initial)
    action_id = done["proposal"]["run_actions"][0]["id"]
    launched = []
    async def launch(snapshot):
        launched.append(copy.deepcopy(snapshot))
        return {"run_id": snapshot["run_id"], "status": "running"}
    monkeypatch.setattr(server, "launch_expert_resume", launch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        applied = await client.post("/api/orchestration/maintenance/apply", json={**owner, "id": done["id"],
            "revision": done["revision"], "proposal_id": done["proposal"]["id"],
            "confirmed_run_action_ids": [action_id]})
        assert applied.status_code == 200, applied.text
        state_after_apply = applied.json()
        assert state_after_apply["run_adoption"]["status"] == "choice_required"
        chosen = await client.post("/api/orchestration/maintenance/choose-adoption", json={**owner,
            "id": done["id"], "revision": state_after_apply["revision"],
            "proposal_id": done["proposal"]["id"], "choice": "new_run"})
        assert chosen.status_code == 200, chosen.text
        choice_result = chosen.json()
    new_id = choice_result["result"]["run_snapshot"]["run_id"]
    new_snapshot = server.EXPERT_RUNS.load(new_id)
    old_snapshot = server.EXPERT_RUNS.load(identity)
    assert len(launched) == 1 and launched[0]["run_id"] == new_id
    assert new_snapshot["locked_flow"]["version"] == 2
    assert new_snapshot["locked_profile"] == {"name": "locked-profile"}
    assert old_snapshot["locked_flow"] == initial["locked_flow"]
    assert old_snapshot["status"] == "paused" and old_snapshot["task"] == "保留的旧任务"
