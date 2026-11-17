from mewcode.experts.recovery_actions import (
    ACTION_ORDER, action_key, plan_recovery_actions, verify_attempt,
)


def _plan(kind, **kwargs):
    return plan_recovery_actions(diagnosis={"primary_kind": kind},
        allowed=["continue_checkpoint", "diagnose_path"], checkpoint_present=True,
        hard_boundary=False, marker={"submitted_items": []}, input_revision="rev1", **kwargs)


def test_ladder_does_not_pretend_unimplemented_methods_exist():
    plan = _plan("model_service_fault")
    rows = {entry["action"]: entry for entry in plan["actions"]}
    assert rows["retry_same_checkpoint"]["status"] == "available"
    assert rows["reduce_execution_profile"]["status"] == "unavailable"
    assert rows["switch_declared_route"]["status"] == "unavailable"
    assert rows["human_decision"]["status"] == "available"
    assert "reduce_execution_profile" in ACTION_ORDER


def test_reduced_profile_requires_declared_executor_and_checkpoint():
    ready = plan_recovery_actions(diagnosis={"primary_kind": "idle_stall"},
        allowed=["reduce_execution_profile"], checkpoint_present=True,
        hard_boundary=False, marker={}, input_revision="rev1")
    row = next(item for item in ready["actions"]
               if item["action"] == "reduce_execution_profile")
    assert row["status"] == "available"
    assert row["legacy_action"] == "reduce_execution_profile"
    missing = plan_recovery_actions(diagnosis={"primary_kind": "idle_stall"},
        allowed=["reduce_execution_profile"], checkpoint_present=False,
        hard_boundary=False, marker={}, input_revision="rev1")
    assert next(item for item in missing["actions"]
                if item["action"] == "reduce_execution_profile")["status"] == "unavailable"


def test_declared_backup_is_available_only_when_host_explicitly_allows_switch():
    plan = plan_recovery_actions(diagnosis={"primary_kind": "model_service_fault"},
        allowed=["continue_checkpoint", "switch_declared_route"],
        checkpoint_present=True, hard_boundary=False,
        declared_backup_present=True, marker={}, input_revision="rev1")
    rows = {entry["action"]: entry for entry in plan["actions"]}
    assert rows["switch_declared_route"]["status"] == "available"
    blocked = plan_recovery_actions(diagnosis={"primary_kind": "model_service_fault"},
        allowed=["continue_checkpoint", "switch_declared_route"],
        checkpoint_present=True, hard_boundary=True,
        declared_backup_present=True, marker={}, input_revision="rev1")
    assert next(row for row in blocked["actions"] if row["action"] == "switch_declared_route")["status"] == "blocked"


def test_hard_boundary_only_leaves_human_and_never_grants_dispatch():
    plan = plan_recovery_actions(diagnosis={"primary_kind": "capacity_shortfall"},
        allowed=["continue_checkpoint"], checkpoint_present=True, hard_boundary=True,
        marker={}, input_revision="rev1")
    assert [row["status"] for row in plan["actions"]] == ["blocked", "blocked", "available"]
    assert not any(row["legacy_action"] for row in plan["actions"] if row["status"] == "available")


def test_review_retry_requires_recheck_receipt():
    assert _plan("review_infrastructure")["actions"][0]["status"] == "unavailable"
    assert _plan("review_infrastructure", review_route_present=True)["actions"][0]["status"] == "available"


def test_attempt_key_is_stable_and_bound_to_input_and_durable_cursor():
    args = dict(diagnosis_kind="capacity_shortfall", action="retry_same_checkpoint",
                input_revision="rev1", marker={"submitted_items": ["a"]})
    assert action_key(**args) == action_key(**args)
    assert action_key(**args) != action_key(**{**args, "input_revision": "rev2"})
    assert action_key(**args) != action_key(**{**args, "marker": {"submitted_items": ["a", "b"]}})
    prior = [{"id": action_key(**args)}]
    plan = plan_recovery_actions(diagnosis={"primary_kind": "capacity_shortfall"},
        allowed=["continue_checkpoint"], checkpoint_present=True, hard_boundary=False,
        prior_attempts=prior, input_revision="rev1", marker=args["marker"])
    assert plan["actions"][0]["status"] == "blocked"


def test_verification_uses_only_durable_progress_or_cleared_review():
    before = {"review_complete": 0, "review_recorded": 0,
              "submitted_items": [], "write_receipts": [], "submitted_revisions": []}
    attempt = {"diagnosis_kind": "capacity_shortfall", "progress_before": before}
    unchanged = verify_attempt(attempt, marker=before, terminal_status="completed")
    assert unchanged["status"] == "no_verified_progress"
    assert verify_attempt(attempt, marker={**before, "write_receipts": ["w1"]},
                          terminal_status="failed")["verification"] == "durable_progress"
    review = {**attempt, "diagnosis_kind": "review_infrastructure"}
    assert verify_attempt(review, marker=before, terminal_status="completed")["verification"] == "infrastructure_cleared"
    assert verify_attempt(review, marker=before, terminal_status="failed")["status"] == "no_verified_progress"
