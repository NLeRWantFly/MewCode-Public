import copy
from dataclasses import asdict
import hashlib
import time

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.experts.delivery import prior_handoff_source
from mewcode.experts.recovery_state import observed_progress_marker
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.briefs import begin_stage_attempt
from mewcode.kernel.work_continuation import checkpoint_matches, retain_checkpoint, work_checkpoint, remaining_segment_policy
from mewcode.orchestration.collab_compile import compile_plan
from tests.test_collaboration_mode import CATALOG, PROFILE, RUN_ID, core, delivery, flow, store_for


def partial():
    return ExpertRoleResult(text="saved real work", status="continuing", error_kind="internal_work",
        handoff={"status": "needs_attention", "blocker_kind": "internal_work", "open_items": ["finish report"],
                 "inline_outputs": [{"name": "partial", "content": "observed result"}]})


def checkpoint_fixture():
    value = core()
    value["contracts"]["work"]["bound_dependencies"] = []
    attempt = ledger.dispatch(value, "work")
    ledger.mark_started(value, attempt["id"])
    attempt = value["attempts"][attempt["id"]]
    record = {"run_id": RUN_ID, "status": "blocked", "collaboration": value,
              "node_states": {"work": {"node_id": "work", "node_attempt": 1, "status": "running",
                  "input_revision": attempt["dependencies"], "tools": {}, "safety_review_state": {}}}}
    stage = record["node_states"]["work"]
    stage["slice_progress_baseline"] = observed_progress_marker(record, "work")
    attempt.update(delivery=asdict(partial()), status="rejected")
    stage.update(status="continuing", handoff=copy.deepcopy(attempt["delivery"]["handoff"]))
    return record, attempt


def test_productive_checkpoint_preserves_partial_result_and_charges_next_attempt():
    record, attempt = checkpoint_fixture()
    original = copy.deepcopy(attempt)
    assert retain_checkpoint(record, attempt["id"])
    assert checkpoint_matches(attempt)
    assert attempt["status"] == original["status"] and attempt["delivery"] == original["delivery"]
    assert ledger.failed_attempts(record["collaboration"], "work") == []
    next_attempt = ledger.dispatch(record["collaboration"], "work")
    assert next_attempt["continued_from"] == attempt["id"]
    assert next_attempt["number"] == 2
    reservations = record["collaboration"]["lineage_budget"]["reservations"]
    assert len(reservations) == 2 and next_attempt["budget_reservation_id"] in reservations


@pytest.mark.parametrize("change", ["no_progress", "identity", "input", "question", "permission", "unknown",
                                    "held", "quality_report", "unfinished_tool", "epoch", "changed_handoff"])
def test_hard_boundaries_and_unproven_progress_never_buy_continuations(change):
    record, attempt = checkpoint_fixture()
    stage = record["node_states"]["work"]
    if change == "no_progress":
        stage["slice_progress_baseline"] = observed_progress_marker(record, "work")
    elif change == "identity":
        stage["node_attempt"] = 2
    elif change == "input":
        stage["input_revision"] = "different"
    elif change == "question":
        attempt["delivery"]["handoff"]["question"] = "approve?"
        stage["handoff"] = copy.deepcopy(attempt["delivery"]["handoff"])
    elif change == "permission":
        stage["pending_permission"] = {"tool_id": "permission"}
    elif change == "unknown":
        record["kernel_calls"] = {"call": {"node_id": "work", "state": "unknown"}}
    elif change == "held":
        record["collaboration"]["contracts"]["work"]["held"] = True
    elif change == "quality_report":
        attempt["report"] = {"passed": False}
    elif change == "unfinished_tool":
        stage["tools"]["call"] = {"name": "Bash", "status": "queued"}
    elif change == "epoch":
        record["collaboration"]["coordinator_epoch"] += 1
    else:
        stage["handoff"] = {}
    assert work_checkpoint(record, attempt) is None
    assert not checkpoint_matches(attempt)


def test_a_changed_delivery_cannot_reuse_a_checkpoint_or_hide_a_quality_failure():
    record, attempt = checkpoint_fixture()
    retain_checkpoint(record, attempt["id"])
    attempt["delivery"]["text"] = "changed"
    assert not checkpoint_matches(attempt)
    assert ledger.failed_attempts(record["collaboration"], "work") == [attempt]
    attempt["delivery"]["text"] = "saved real work"
    attempt["report"] = {"passed": False}
    assert not checkpoint_matches(attempt)


def test_old_evidence_cannot_earn_another_segment_by_reappearing():
    record, first = checkpoint_fixture()
    retain_checkpoint(record, first["id"])
    value = record["collaboration"]
    second = ledger.dispatch(value, "work")
    value["attempts"][second["id"]].update(delivery=asdict(partial()), status="rejected")
    stage = record["node_states"]["work"]
    stage.update(node_attempt=2, slice_progress_baseline={
        "schema_version": 2, "observed_sources": [], "inline_outputs": [], "write_receipts": [],
        "review_complete": 0, "review_recorded": 0, "submitted_items": [], "submitted_revisions": []})
    assert work_checkpoint(record, value["attempts"][second["id"]]) is None


def test_independent_context_preserves_a_host_manifest_denial_without_granting_the_call(tmp_path):
    from mewcode.kernel.capabilities import call_gap, declaration
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    record, attempt = checkpoint_fixture()
    record["authorization_manifest"] = {"schema_version": 2, "approved": True, "workspace": str(tmp_path),
        "nodes": {"work": declaration({})}}
    call = {"tool": "Bash", "arguments": {"command": "forbidden_command"}}
    facts = ledger_for_run(record)
    facts.record("work", "blocker", {"kind": "missing_authorization", "gate_kind": "authorization_extension",
        "requested_call": call}, source="host", identity="denied")
    project_node_view(record, "work")
    old = copy.deepcopy(record["node_ledgers"])
    manifest = copy.deepcopy(record["authorization_manifest"])
    assert call_gap(manifest, "work", **{"tool_name": call["tool"], "arguments": call["arguments"]})
    observed = retain_checkpoint(record, attempt["id"])
    assert observed and observed["ungranted_call_fact_ids"]
    assert record["node_ledgers"] == old and record["authorization_manifest"] == manifest
    assert call_gap(record["authorization_manifest"], "work", call["tool"], call["arguments"])
    record["node_states"]["work"]["pending_permission"] = {"tool_id": "denied"}
    assert work_checkpoint(record, attempt) is None


def test_starting_a_new_segment_archives_receipts_and_preserves_safety_usage_and_source_version():
    record, attempt = checkpoint_fixture()
    record.update(status="running", attempt=3)
    stage = record["node_states"]["work"]
    stage.update(tools={"old": {"name": "Bash", "status": "completed"}}, partial_reasoning="private history",
                 safety_review_state={"requests": {"denied": {"decision": "deny"}}}, usage={"input_tokens": 500},
                 iteration_recovery={"requests": 2})
    original = copy.deepcopy(stage)
    assert begin_stage_attempt(record, {"node_id": "work", "node_attempt": 2, "input_revision": "new-input"})
    archived = record["kernel_attempt_archives"][stage["last_archive_id"]]["data"]
    assert archived == original
    assert stage["tools"] == {} and stage["partial_reasoning"] == "" and stage["status"] == "running"
    assert record["stage_cursor"] == "work"
    for key in ("safety_review_state", "usage", "iteration_recovery", "handoff"):
        assert stage[key] == original[key]
    assert prior_handoff_source("work", stage, input_revision="new-input") is None
    assert prior_handoff_source("work", stage, input_revision=attempt["dependencies"])["handoff"] == original["handoff"]


def test_same_attempt_resume_keeps_receipts_and_late_starts_cannot_reactivate_a_pause():
    record, _ = checkpoint_fixture()
    record["status"] = "running"
    stage = record["node_states"]["work"]
    stage["tools"] = {"old": {"name": "ReadFile", "status": "completed"}}
    assert begin_stage_attempt(record, {"node_id": "work", "node_attempt": 1})
    assert "old" in stage["tools"] and not record.get("kernel_attempt_archives")
    record["status"] = "paused"
    before = copy.deepcopy(record)
    assert not begin_stage_attempt(record, {"node_id": "work", "node_attempt": 2})
    assert record == before


def test_legacy_cursor_cannot_make_a_settled_service_error_the_current_blocker():
    from mewcode.expert_web import _blocked_run_reason
    from tests.test_expert_runs import _blocked_result
    record, _ = checkpoint_fixture()
    record.update(stage_cursor="old-review")
    record["node_states"]["old-review"] = {"status": "needs_attention", "request_error": "old balance failure"}
    assert _blocked_run_reason(_blocked_result("partial\nreport"), record) == "已有执行进展，仍有 1 项内部工作；可从保存点继续。"


def test_segment_limits_carry_forward_without_resetting_budget_or_permissions():
    from mewcode.expert_group import ExpertExecutionPolicy
    policy = ExpertExecutionPolicy(strategy="stable", allow_delegation=False, max_direct_agents=0,
        token_budget=10000, timeout_seconds=120, deny_tools=("Bash",), node_epoch=7)
    outcome = partial()
    outcome.billable_tokens = 6200
    next_policy = remaining_segment_policy(policy, outcome, 30)
    assert next_policy.token_budget == 3800 and next_policy.timeout_seconds == 90
    assert not next_policy.resume_messages and next_policy.deny_tools == policy.deny_tools
    assert next_policy.node_epoch == 7 and not next_policy.allow_delegation
    assert remaining_segment_policy(next_policy, outcome, 1) is None
    outcome.billable_tokens = 0
    assert remaining_segment_policy(policy, outcome, 120) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,expected,progress", [(9, "completed", True), (1, "blocked", True), (9, "blocked", False)])
async def test_real_scheduler_continues_productive_work_with_original_budget_and_independent_acceptance(tmp_path, monkeypatch, limit, expected, progress):
    plan = compile_plan(flow())
    plan["policy"]["lineage_budget_limits"] = {"lineage": {"attempts": limit}}
    monkeypatch.setattr("mewcode.orchestration.collab_compile.compile_plan", lambda _: copy.deepcopy(plan))
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda record: ledger.initialize(record, plan))
    # This scenario explicitly exercises a fixed per-lineage allowance and
    # prerequisite independent review. New runs otherwise use elastic work
    # allocation and defer advisory quality checks to integrated acceptance.
    def fixed_boundary(record):
        value = record['collaboration']
        value['policy']['lineage_budget_mode'] = 'fixed'
        for check in value['contracts']['work']['checks']:
            if check['kind'] == 'review':
                check['blocking'] = True
    store.mutate(RUN_ID, fixed_boundary)
    calls, reviewed = [], []

    def event_sink(event):
        if event.get("type") == "expert_node_started":
            store.mutate(RUN_ID, lambda record: begin_stage_attempt(record, event))
        store.append_event(RUN_ID, event)

    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        outcome = partial() if calls == ["work"] else delivery("final " + node.node_id)
        if node.node_id == "work" and calls.count("work") == 2:
            assert not policy.resume_messages
            assert not ledger.valid_certificate(store.load(RUN_ID)["collaboration"], "work")
        def save(record):
            stage = record["node_states"][node.node_id]
            stage["slice_progress_baseline"] = observed_progress_marker(record, node.node_id)
            stamp = time.time()
            stage.update(status=outcome.status, handoff=copy.deepcopy(outcome.handoff),
                input_revision=policy.input_revision, started_at=stamp - 1, completed_at=stamp + 1,
                safety_review_state={}, messages=[{"role": "assistant", "content": outcome.text}])
            stage.setdefault("tools", {})["read-" + str(stage["node_attempt"])] = {
                "name": "ReadFile", "args": {"file_path": "result-" + str(stage["node_attempt"])},
                "result_sha256": hashlib.sha256(outcome.text.encode()).hexdigest(), "output": outcome.text,
                "status": "completed", "is_error": False, "node_attempt": stage["node_attempt"],
                "attempt": record["attempt"], "epoch": stage.get("epoch", 0),
                "started_at": stamp, "completed_at": stamp + .1}
            if not progress:
                stage["slice_progress_baseline"] = observed_progress_marker(record, node.node_id)
        store.mutate(RUN_ID, save)
        return outcome

    async def reviewer(model, payload, context):
        reviewed.append(context["node_id"])
        if context["node_id"] == "work":
            assert calls.count("work") == 2
            assert payload["host_execution_evidence"]["complete"]
            assert payload["host_execution_evidence"]["recorded_tool_count"] == 1
        return {"passed": True, "evidence": ["checked final delivery"]}

    orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, run_store=store,
        work_dir=tmp_path, archive_root=tmp_path / "archive", collaboration_review=reviewer, event_sink=event_sink)
    result = await orchestrator.run("交付测试结果", PROFILE, CATALOG, strategy="stable", run_id=RUN_ID,
        flow=flow(), checkpoint=store.load(RUN_ID))
    assert result.status == expected, result.final_summary
    value = store.load(RUN_ID)["collaboration"]
    first = next(row for row in value["attempts"].values() if row["contract_id"] == "work")
    assert checkpoint_matches(first) is progress and first["delivery"] == asdict(partial())
    assert ledger.failed_attempts(value, "work") == ([] if progress else [first])
    assert not value["lineage_budget"]["grants"]
    if not progress:
        assert calls == ["work"] and not reviewed
    elif limit == 1:
        assert calls == ["work"] and not reviewed
        assert any(row["kind"] == "lineage_budget_exhausted" for row in value["recovery_gates"].values())
    else:
        assert calls == ["work", "work", "deliver"]
        assert ledger.valid_certificate(value, "work") and "work" in reviewed
