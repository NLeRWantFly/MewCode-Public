import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.kernel.escalation import action_count
from mewcode.experts.recovery_state import progress_marker
from mewcode.experts.waits import BranchWaitPaused
from tests.test_scheduler_recovery import controller, decide
from tests.test_run_supervisor import health
from mewcode.experts.run_supervisor import supervise_segments


@pytest.mark.asyncio
async def test_scheduler_continues_with_real_progress_and_persists_budget_action(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, _ = controller(tmp_path, monkeypatch)
    handler.model_decisions = False
    def seed(snapshot):
        stage = snapshot["node_states"]["extract"]
        stage.update(started_at=100.0, completed_at=120.0, duration_ms=20000,
                     node_attempt=1, input_revision="r1")
        stage["slice_progress_baseline"] = progress_marker(snapshot, "extract")
        snapshot["review_queues"] = {"extract": {"records": {"i": {"state": "complete"}}}}
    store.mutate(handler.run_id, seed)
    decision = await decide(handler, flow)
    assert decision["action"] == "continue_checkpoint"
    assert decision["controller"]["action"] == "progress_continue"
    assert not primary.calls and not jev.calls
    view = ledger_for_run(store.load(handler.run_id)).view("extract")
    assert action_count(view, "progress_continue") == 1
    assert action_count(view, "self_diagnose") == 0


@pytest.mark.asyncio
async def test_idle_stall_routes_to_diagnosis_not_plain_continue(tmp_path, monkeypatch):
    handler, flow, _, store, _, _, _ = controller(tmp_path, monkeypatch)
    handler.model_decisions = False
    def seed(snapshot):
        stage = snapshot["node_states"]["extract"]
        stage.update(started_at=100.0, completed_at=120.0, duration_ms=20000,
                     node_attempt=1, input_revision="r1")
        stage["slice_progress_baseline"] = progress_marker(snapshot, "extract")
        stage["tools"] = {str(i): {"tool_id": str(i), "name": "ReadFile", "status": "completed",
            "args": {"path": "queue.json"}, "declared_at": 101 + i,
            "started_at": 101 + i, "completed_at": 101.5 + i} for i in range(4)}
    store.mutate(handler.run_id, seed)
    decision = await decide(handler, flow)
    assert decision["action"] == "diagnose_path"
    assert decision["controller"]["action"] == "self_diagnose"
    view = ledger_for_run(store.load(handler.run_id)).view("extract")
    assert action_count(view, "self_diagnose") == 1
    assert action_count(view, "assist") == 0


@pytest.mark.asyncio
async def test_business_change_cannot_be_auto_resumed(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, events = controller(tmp_path, monkeypatch)
    handler.model_decisions = False
    frozen_contract = store.load(handler.run_id)["locked_flow"]
    with pytest.raises(BranchWaitPaused):
        await decide(handler, flow, result=ExpertRoleResult(
            text="rubric changed", status="needs_attention", error_kind="plan_revision_required",
            handoff={'question': '验收标准有变更，请确认采用哪个版本？',
                     'question_options': ['保持原标准', '提交修订后的标准']}))
    snapshot = store.load(handler.run_id)
    saved = snapshot["node_states"]["extract"]["scheduler_recovery"]
    decision = saved["decisions"][-1]
    assert decision["controller"]["action"] == "ask_human"
    assert decision["action"] == "human"
    assert saved["phase"] == "awaiting_input"
    blocker = next(f for f in ledger_for_run(snapshot).view("extract").active_blockers
                   if f.payload.get("kind") == "plan_revision_required")
    assert blocker.payload.get("clearance_satisfied") is not True
    assert snapshot["locked_flow"] == frozen_contract
    assert not primary.calls and not jev.calls
    assert any(event["type"] == "ask_user" for event in events)


@pytest.mark.asyncio
async def test_real_progress_takes_priority_over_supervisor_method_replan(tmp_path, monkeypatch):
    handler, flow, _, store, _, _, _ = controller(tmp_path, monkeypatch)
    handler.model_decisions = False
    handler.adaptive_methods = True
    first = supervise_segments([health(i) for i in range(3)], input_revision="r1")
    trigger = supervise_segments([health(i) for i in range(4)],
                                 previous=first, input_revision="r1")
    assert trigger["requested_action"] == "replan_execution_method"

    def seed(snapshot):
        stage = snapshot["node_states"]["extract"]
        stage.update(started_at=100.0, completed_at=120.0, duration_ms=20000,
                     node_attempt=1, input_revision="r1", run_supervisor=trigger)
        snapshot["review_queues"] = {"extract": {"records": {"i": {"state": "complete"}}}}
        stage["slice_progress_baseline"] = progress_marker(snapshot, "extract")

    store.mutate(handler.run_id, seed)
    selected = await decide(handler, flow)
    assert selected["action"] == "continue_checkpoint"
    assert selected["controller"]["action"] == "progress_continue"
    view = ledger_for_run(store.load(handler.run_id)).view("extract")
    assert action_count(view, "progress_continue") == 1
    assert action_count(view, "assist") == 0
