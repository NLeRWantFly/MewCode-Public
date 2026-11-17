import pytest

from mewcode.experts.run_supervisor import (
    arm_supervised_adjustment, count_mapped_issue_areas, supervise_segments,
)


def health(i, *, effective=1, calls=10, units=1, eta=500,
           sampling="tool_complete", input_revision="r1",
           read_calls=0, repeated_reads=0, unkeyed_reads=0):
    return {"segment_id": str(i), "sampling": sampling,
            "input_revision": input_revision,
            "terminal_calls": calls, "effective_calls": effective,
            "read_calls": read_calls,
            "keyed_reads": read_calls - unkeyed_reads,
            "unkeyed_reads": unkeyed_reads,
            "repeated_reads": repeated_reads,
            "work_delta": {"known": True, "total_units": units},
            "timing": {"wall_seconds": 100},
            "eta": {"status": "estimated", "seconds": eta}}


def test_two_bad_complete_windows_request_only_method_replan():
    history = [health(i) for i in range(3)]
    first = supervise_segments(history, target_eta_seconds=200)
    assert first["signals"] == ["low_effective_call_ratio", "eta_over_target"]
    assert first["requested_action"] is None
    assert first["throughput_items_per_second"] == .01
    duplicate = supervise_segments(history, previous=first, target_eta_seconds=200)
    assert duplicate["consecutive_bad_windows"] == 1
    second = supervise_segments([*history, health(3)], previous=first, target_eta_seconds=200)
    assert second["requested_action"] == "replan_execution_method"
    assert second["changes_acceptance"] is False


def test_partial_or_small_samples_do_not_trigger_and_good_window_resets():
    partial = supervise_segments([health(i, sampling="partial") for i in range(3)])
    assert partial["sampling"] == "partial"
    assert partial["effective_call_ratio"] is None
    assert partial["requested_action"] is None
    small = supervise_segments([health(i, calls=1) for i in range(3)])
    assert small["signals"] == []
    prior = supervise_segments([health(i) for i in range(3)])
    good = supervise_segments([health(3, effective=8), health(4, effective=8), health(5, effective=8)],
                              previous=prior)
    assert good["consecutive_bad_windows"] == 0
    assert good["requested_action"] is None


def test_scope_concentration_is_decision_only():
    result = supervise_segments([], issue_counts={"validation": 18, "other": 2},
                                input_revision="r1",
                                scope_context={"contract_hash": "frozen", "item_count": 20,
                                               "recorded_item_count": 18,
                                               "unreviewed_item_count": 2})
    assert result["scope_decision"]["decision_required"] is True
    assert result["scope_decision"]["automatic_scope_change"] is False
    assert result["scope_decision"]["original_scope"] == {
        "contract_hash": "frozen", "item_count": 20,
        "recorded_item_count": 18, "status": "unchanged"}
    assert result["scope_decision"]["evidence_impact"]["outside_focus_issue_count"] == 2
    assert result["scope_decision"]["evidence_impact"]["unreviewed_item_count"] == 2
    assert result["scope_decision"]["time_estimate"]["revised_eta_seconds"] is None
    assert [row["id"] for row in result["scope_decision"]["options"]] == [
        "keep_original", "request_impact_analysis", "request_formal_revision"]
    assert result["requested_action"] is None
    growth = supervise_segments([], issue_counts={"validation": 27, "other": 3},
                                input_revision="r1",
                                scope_context={"contract_hash": "frozen", "item_count": 30})
    assert growth["scope_decision"]["decision_id"] == result["scope_decision"]["decision_id"]


def test_scope_signal_requires_complete_frozen_issue_area_mapping():
    snapshot = {"review_queues": {"a": {"records": {
        "page-1": {"state": "needs_repair", "issue_ids": ["v1", "v2"]},
        "page-2": {"state": "needs_repair", "issue_ids": ["v1", "o1"]},
        "done": {"state": "complete", "issue_ids": []},
    }}}}
    assert count_mapped_issue_areas(snapshot, {"v1": "validation", "v2": "validation",
                                                "o1": "other"}) == {"validation": 3, "other": 1}
    assert count_mapped_issue_areas(snapshot, {"v1": "validation", "v2": "validation"}) is None
    assert count_mapped_issue_areas(snapshot, None) is None


def test_supervised_method_trial_uses_disjoint_post_window_and_suppresses_no_benefit():
    history = [health(i) for i in range(4)]
    first = supervise_segments(history[:3], input_revision="r1")
    trigger = supervise_segments(history, previous=first, input_revision="r1")
    armed = arm_supervised_adjustment(trigger, decision_id="decision-1", input_revision="r1")
    assert arm_supervised_adjustment(armed, decision_id="decision-1", input_revision="r1") == armed
    assert armed["requested_action"] is None
    with pytest.raises(ValueError):
        arm_supervised_adjustment(armed, decision_id="decision-2", input_revision="r1")
    for index in range(4, 7):
        armed = supervise_segments([*history, *(health(j) for j in range(4, index + 1))],
                                    previous=armed, input_revision="r1")
        assert armed["requested_action"] is None
    trial = armed["active_adjustment"]
    assert trial["post_segment_ids"] == ["4", "5", "6"]
    assert trial["status"] == "rolled_back"
    assert trial["future_identical_replan_suppressed"] is True
    # New input generation permits a fresh diagnosis, never inheritance of a
    # rejected method trial or its old consecutive-window count.
    fresh = supervise_segments([health(7, input_revision="r2"),
                                health(8, input_revision="r2"),
                                health(9, input_revision="r2")],
                               previous=armed, input_revision="r2")
    assert fresh["active_adjustment"] is None
    assert fresh["consecutive_bad_windows"] == 1


def test_supervised_method_trial_verifies_improvement_but_never_acceptance():
    history = [health(i, units=0) for i in range(4)]
    first = supervise_segments(history[:3], input_revision="r1")
    trigger = supervise_segments(history, previous=first, input_revision="r1")
    armed = arm_supervised_adjustment(trigger, decision_id="decision-2", input_revision="r1")
    post = [health(i, effective=4, units=1) for i in range(4, 7)]
    verdict = supervise_segments([*history, *post], previous=armed, input_revision="r1")
    assert verdict["active_adjustment"]["status"] == "verified_benefit"
    assert verdict["active_adjustment"]["changes_acceptance"] is False
    assert verdict["requested_action"] is None


def test_partial_post_window_does_not_fabricate_benefit():
    history = [health(i) for i in range(4)]
    first = supervise_segments(history[:3], input_revision="r1")
    trigger = supervise_segments(history, previous=first, input_revision="r1")
    armed = arm_supervised_adjustment(trigger, decision_id="decision-3", input_revision="r1")
    post = [health(4), health(5, sampling="partial"), health(6)]
    result = supervise_segments([*history, *post], previous=armed, input_revision="r1")
    assert result["active_adjustment"]["status"] == "inconclusive"
    assert result["requested_action"] is None


def test_input_revision_change_excludes_old_health_window():
    old = [health(i) for i in range(4)]
    previous = supervise_segments(old, input_revision="r1")
    next_one = supervise_segments([*old, health(4, input_revision="r2")],
                                  previous=previous, input_revision="r2")
    assert next_one["sampling"] == "partial"
    assert next_one["consecutive_bad_windows"] == 0
    assert next_one["requested_action"] is None


def test_repeated_read_signal_requires_keyed_coverage_and_two_windows():
    rows = [health(i, effective=5, calls=10, read_calls=4,
                   repeated_reads=3, eta=50) for i in range(4)]
    first = supervise_segments(rows[:3], input_revision="r1")
    assert first["signals"] == ["high_repeated_read_ratio"]
    assert first["repeated_read_ratio"] == .75
    second = supervise_segments(rows, previous=first, input_revision="r1")
    assert second["requested_action"] == "replan_execution_method"
    unknown = [health(i, effective=5, calls=10, read_calls=4,
                      repeated_reads=2, unkeyed_reads=1, eta=50) for i in range(3)]
    partial = supervise_segments(unknown, input_revision="r1")
    assert partial["unkeyed_reads"] == 3
    assert "high_repeated_read_ratio" not in partial["signals"]
    assert partial["requested_action"] is None
