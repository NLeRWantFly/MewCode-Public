import copy

import pytest

from mewcode.experts.recovery_state import (
    binding,
    blockers,
    digest,
    finish_interaction,
    reconcile,
    save_interaction,
)


def stage():
    value = {"attempt": 1, "node_attempt": 1, "tools": {}, "safety_review_state": {},
             "scheduler_recovery": {"phase": "awaiting_input", "decision_attempts": 1,
                "decisions": [{"action": "human", "decision_tokens": 0}]}}
    value["pending_interaction"] = {"request_id": "old", "tool": "SchedulerRecovery", "binding": binding(value)}
    return value


def progress(value):
    value["tools"]["new"] = {"tool_id": "new", "name": "ReadReviewQueue", "status": "completed", "output": "revision 241"}


def test_progress_retires_only_derived_prompt_and_preserves_budget():
    value = stage()
    progress(value)
    assert reconcile(value)
    assert "pending_interaction" not in value
    assert value["interaction_history"][-1]["answered"] is False
    assert value["scheduler_recovery"]["decision_attempts"] == 1
    assert not reconcile(value)


def test_historical_paid_decision_cannot_pin_new_host_question():
    value = stage()
    old = {"decision_id": "round-3", "action": "human", "decision_tokens": 12539,
           "host_facts": {"loop_id": "quality", "round": 3},
           "execution_status": "等待人工"}
    current = {"decision_id": "round-8", "action": "human", "decision_tokens": 0,
               "host_facts": {"loop_id": "quality", "round": 8},
               "execution_status": "等待人工"}
    value["scheduler_recovery"]["decisions"] = [old, current]
    progress(value)
    assert reconcile(value)
    assert old["execution_status"] == "等待人工"  # Never rewrite earlier decisions.
    assert current["execution_status"].startswith("旧调度提示已失效")
    assert "pending_interaction" not in value


def test_current_paid_decision_remains_a_real_gate():
    value = stage()
    value["scheduler_recovery"]["decisions"] = [
        {"decision_id": "round-3", "action": "human", "decision_tokens": 12539,
         "host_facts": {"loop_id": "quality", "round": 3}},
        {"decision_id": "round-8", "action": "human", "decision_tokens": 17,
         "host_facts": {"loop_id": "quality", "round": 8}},
    ]
    progress(value)
    assert not reconcile(value)
    assert value["pending_interaction"]["request_id"] == "old"


def test_scheduler_question_binds_exact_decision_id():
    snapshot = {"node_states": {"work": {"tools": {}, "scheduler_recovery": {
        "decisions": [{"decision_id": "current", "action": "human", "decision_tokens": 0}]}}}}
    saved = save_interaction(snapshot, "node_states", "work", {
        "request_id": "one", "tool": "SchedulerRecovery"})
    assert saved["scheduler_decision_id"] == "current"
    stage_value = snapshot["node_states"]["work"]
    stage_value["scheduler_recovery"]["decisions"].append({
        "decision_id": "later", "action": "human", "decision_tokens": 0})
    progress(stage_value)
    assert reconcile(stage_value)  # A later unrelated decision cannot own this question.
    assert stage_value["scheduler_recovery"]["decisions"][-1]["decision_tokens"] == 0


def test_scheduler_question_does_not_erase_later_paid_decision():
    snapshot = {"node_states": {"work": {"tools": {}, "scheduler_recovery": {
        "decisions": [{"decision_id": "current", "action": "human", "decision_tokens": 0}]}}}}
    save_interaction(snapshot, "node_states", "work", {
        "request_id": "one", "tool": "SchedulerRecovery"})
    stage_value = snapshot["node_states"]["work"]
    stage_value["scheduler_recovery"]["decisions"].append({
        "decision_id": "later", "action": "human", "decision_tokens": 5})
    progress(stage_value)
    assert not reconcile(stage_value)
    assert stage_value["pending_interaction"]["request_id"] == "one"


def test_attempt_change_and_reconnect_are_not_evidence():
    value = stage()
    value.update(attempt=9, node_attempt=30, input_revision="new")
    assert not reconcile(value)


@pytest.mark.parametrize("boundary", ["permission", "question", "write", "browser", "safety", "low_confidence", "deadline"])
def test_progress_never_overrides_hard_boundary(boundary):
    value = stage()
    progress(value)
    error_kind = ""
    if boundary == "permission":
        value["pending_permission"] = {"tool_id": "approval"}
    elif boundary == "question":
        value["pending_interaction"]["tool"] = "AskUserQuestion"
    elif boundary in {"write", "browser"}:
        value["tools"]["unknown"] = {"name": "Bash" if boundary == "write" else "Browser", "status": "outcome_unknown"}
    elif boundary == "safety":
        value["safety_review_state"] = {"pending_call_ref": "operation"}
    elif boundary == "low_confidence":
        value["scheduler_recovery"]["decisions"][-1].update(review_choice="continue_checkpoint", confidence=.6)
    else:
        error_kind = "run_deadline"
    assert not reconcile(value, error_kind=error_kind)
    assert value["pending_interaction"]


def test_cancelled_reads_are_not_unknown_writes():
    value = {"tools": {"a": {"name": "ReadFile", "status": "running"},
                       "b": {"name": "Bash", "status": "running"}}}
    reconcile(value)
    assert value["tools"]["a"]["status"] == "interrupted_read_only"
    assert value["tools"]["b"]["status"] == "running"
    assert [b["kind"] for b in blockers(value)] == ["unknown_effect"]


def test_replan_write_reconciles_only_against_its_exact_host_receipt():
    args = {"reason": "method invalid", "evidence_refs": ["proof-1"]}
    value = {"node_attempt": 2, "input_revision": "source-v2", "tools": {
        "request": {"tool_id": "request", "name": "RequestReplan", "args": args,
                    "status": "running"}}}
    assert blockers(value) == [{"kind": "unknown_effect", "ref": "request",
                                "clearance": "independent_effect_receipt",
                                "clearance_satisfied": False}]
    request = {**args, "node_attempt": 2, "input_revision": "source-v2"}
    value["adaptive_request"] = {**request, "request_id": digest(request), "status": "pending"}
    reconcile(value)
    assert value["tools"]["request"]["status"] == "completed"
    assert value["tools"]["request"]["recovery_reason"] == "matching_host_replan_receipt"
    assert blockers(value) == []


def test_replan_write_without_receipt_can_retry_but_conflicting_request_cannot():
    args = {"reason": "method invalid", "evidence_refs": ["proof-1"]}
    base = {"node_attempt": 2, "input_revision": "source-v2", "tools": {
        "request": {"tool_id": "request", "name": "RequestReplan", "args": args,
                    "status": "running"}}}
    absent = copy.deepcopy(base)
    reconcile(absent)
    assert absent["tools"]["request"]["status"] == "not_executed"
    assert blockers(absent) == []
    conflict = copy.deepcopy(base)
    conflict["adaptive_request"] = {"request_id": "different", "status": "pending"}
    reconcile(conflict)
    assert conflict["tools"]["request"]["status"] == "running"
    assert blockers(conflict) == [{"kind": "unknown_effect", "ref": "request",
                                   "clearance": "independent_effect_receipt",
                                   "clearance_satisfied": False}]


def test_call_that_never_executed_is_not_a_permanent_unknown_effect():
    """A write parked before execution must not strand every later resume."""
    value = {"tools": {"v": {"name": "WriteFile", "status": "interrupted_before_execution"},
                       "e": {"name": "EditFile", "status": "interrupted_before_execution"}}}
    assert blockers(value) == []
    reconcile(value)
    assert {tool["status"] for tool in value["tools"].values()} == {"not_executed"}
    assert blockers(value) == []


def test_unknown_write_with_side_effect_still_blocks():
    """Retrying a call that may have run is never treated as free."""
    value = {"tools": {"w": {"name": "WriteFile", "status": "outcome_unknown"}}}
    assert [b["kind"] for b in blockers(value)] == ["unknown_effect"]
    reconcile(value)
    assert value["tools"]["w"]["status"] == "outcome_unknown"
    assert [b["kind"] for b in blockers(value)] == ["unknown_effect"]


def test_segment_work_counts_as_progress_before_a_candidate_is_submitted():
    """A segment that fixed files did real work even with nothing submitted yet.

    Without this the host reports 有限恢复仍无进展 every round and parks the node on
    a pause-only prompt while the executor is in fact making progress.
    """
    from mewcode.experts.recovery_state import advanced, progress_marker
    snapshot = {"node_states": {"repair": {"tools": {
        "a": {"tool_id": "a", "name": "ReadFile", "status": "completed"}}}}}
    baseline = progress_marker(snapshot, "repair")
    assert baseline["write_receipts"] == []
    # A read is not work; the same marker must not look like progress.
    assert not advanced(baseline, progress_marker(snapshot, "repair"))
    snapshot["node_states"]["repair"]["tools"]["b"] = {
        "tool_id": "b", "name": "EditFile", "status": "completed"}
    marker = progress_marker(snapshot, "repair")
    assert marker["write_receipts"] == ["b:EditFile"]
    assert advanced(baseline, marker)


def test_failed_effect_and_unknown_effect_are_not_progress():
    """Only completed effects count; failures must not buy a continuation."""
    from mewcode.experts.recovery_state import advanced, progress_marker
    snapshot = {"node_states": {"repair": {"tools": {}}}}
    baseline = progress_marker(snapshot, "repair")
    snapshot["node_states"]["repair"]["tools"] = {
        "f": {"tool_id": "f", "name": "Bash", "status": "failed"},
        "u": {"tool_id": "u", "name": "WriteFile", "status": "outcome_unknown"},
        "r": {"tool_id": "r", "name": "EditFile", "status": "running"},
    }
    assert progress_marker(snapshot, "repair")["write_receipts"] == []
    assert not advanced(baseline, progress_marker(snapshot, "repair"))


def test_legacy_migration_keeps_unresolved_safety_review():
    value = stage()
    value.pop("safety_review_state")
    value["pending_interaction"].pop("binding")
    value["tools"]["failed"] = {"name": "Bash", "args": {"command": "x"}, "status": "failed",
                                "output": "AUTO_REVIEW_INCONCLUSIVE: scope"}
    progress(value)
    assert not reconcile(value)
    value["tools"]["resolved"] = {"name": "Bash", "args": {"command": "x"}, "status": "completed"}
    assert reconcile(value)


def test_interaction_binding_and_compare_and_set_completion():
    snapshot = {"node_states": {"work": {"attempt": 1}}}
    request = {"request_id": "one", "tool": "AskUserQuestion"}
    saved = save_interaction(snapshot, "node_states", "work", request)
    assert saved["binding"]["attempt"] == 1
    snapshot["node_states"]["work"]["attempt"] = 2
    assert save_interaction(snapshot, "node_states", "work", request) == saved
    before = copy.deepcopy(snapshot)
    with pytest.raises(ValueError):
        save_interaction(snapshot, "node_states", "work", {**request, "request_id": "two"})
    with pytest.raises(ValueError):
        finish_interaction(snapshot, "node_states", "work", "two")
    assert snapshot == before
    finish_interaction(snapshot, "node_states", "work", "one")
    assert snapshot["node_states"]["work"]["interaction_history"][-1]["answered"]


def test_question_payload_is_immutable_and_same_id_cannot_change_contract():
    snapshot = {}
    request = {"request_id": "one", "tool": "AskUserQuestion", "questions": [{"message": "original"}]}
    save_interaction(snapshot, "node_states", "work", request)
    request["questions"][0]["message"] = "changed"
    assert snapshot["node_states"]["work"]["pending_interaction"]["questions"][0]["message"] == "original"
    with pytest.raises(ValueError, match="contract"):
        save_interaction(snapshot, "node_states", "work", request)
