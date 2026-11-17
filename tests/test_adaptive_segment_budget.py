import pytest

from mewcode.experts.adaptive_segment_budget import (
    choose_segment_budget, observed_segment, reduce_execution_slice,
)


def test_only_productive_measured_slices_buy_larger_execution_slice():
    baseline = choose_segment_budget(base_iterations=12, base_seconds=900,
        remaining_total_seconds=1200, history=[])
    assert baseline["max_iterations"] == 12
    assert baseline["timeout_seconds"] == 900
    history = [observed_segment(iterations=12, wall_seconds=850, progressed=True),
               observed_segment(iterations=12, wall_seconds=880, progressed=True)]
    choice = choose_segment_budget(base_iterations=12, base_seconds=900,
        remaining_total_seconds=1200, history=history)
    assert 12 < choice["max_iterations"] <= 200
    assert 900 < choice["timeout_seconds"] <= 1200
    assert choice["soft_grace_seconds"] > 0
    assert choice["increases_total_budget"] is False
    assert choice["changes_contract"] is False


def test_no_progress_never_inflates_and_parent_ceiling_stays_authoritative():
    history = [observed_segment(iterations=12, wall_seconds=890, progressed=False)] * 8
    choice = choose_segment_budget(base_iterations=12, base_seconds=900,
        remaining_total_seconds=100, history=history)
    assert choice["max_iterations"] == 12
    assert choice["timeout_seconds"] == 100
    assert choice["reason"] == "insufficient_productive_samples"


def test_invalid_budgets_fail_closed():
    with pytest.raises(ValueError):
        choose_segment_budget(base_iterations=0, base_seconds=900,
                              remaining_total_seconds=None)
    with pytest.raises(ValueError):
        choose_segment_budget(base_iterations=12, base_seconds=900,
                              remaining_total_seconds=-1)


def test_reduced_profile_only_shrinks_one_slice():
    original = choose_segment_budget(base_iterations=12, base_seconds=900,
                                     remaining_total_seconds=1000)
    reduced = reduce_execution_slice(original)
    assert 1 <= reduced["max_iterations"] < original["max_iterations"]
    assert 0 < reduced["timeout_seconds"] < original["timeout_seconds"]
    assert reduced["parent_remaining_seconds"] == original["parent_remaining_seconds"]
    assert reduced["increases_total_budget"] is False
    assert reduced["changes_contract"] is False
