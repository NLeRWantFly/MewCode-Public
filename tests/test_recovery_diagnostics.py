"""Health facts must reflect durable work and preserve unavailable measurements."""
from mewcode.experts.recovery_diagnostics import (
    extract_tool_error_code, measure_segment, read_result_fingerprint,
)
from mewcode.experts.recovery_state import progress_marker


def snapshot():
    value = {"run_id": "a" * 32, "node_states": {"work": {
        "node_attempt": 1, "started_at": 100.0, "completed_at": 200.0,
        "duration_ms": 100000, "input_revision": "r1", "tools": {}}}}
    value["node_states"]["work"]["slice_progress_baseline"] = progress_marker(value, "work")
    return value


def test_effective_calls_and_durable_progress_are_distinct():
    value = snapshot()
    stage = value["node_states"]["work"]
    stage["tools"] = {
        "r": {"tool_id": "r", "name": "ReadFile", "args": {"path": "queue.json"},
              "status": "completed", "declared_at": 105, "started_at": 106, "completed_at": 107},
        "w": {"tool_id": "w", "name": "EditFile", "args": {"path": "result.md"},
              "status": "completed", "declared_at": 120, "started_at": 121, "completed_at": 125},
        "f": {"tool_id": "f", "name": "WriteFile", "args": {"path": "missing.md"},
              "status": "failed", "error_code": "file_not_found", "declared_at": 130,
              "started_at": 131, "completed_at": 132},
    }
    health = measure_segment(value, "work", terminal_status="budget_exhausted")
    assert health["first_effective_call_seconds"] == 20
    assert health["effective_calls"] == 1 and health["terminal_calls"] == 3
    assert health["work_delta"]["write_receipts"] == 1
    assert health["path_error_count"] == 1 and health["path_call_count"] == 3
    assert health["timing"]["tool_seconds"] == 6
    assert health["timing"]["model_seconds"] is None
    assert health["sampling"] == "tool_complete"


def test_first_hashed_read_is_effective_and_repeats_are_observations():
    value = snapshot()
    stage = value["node_states"]["work"]
    stage["tools"] = {str(index): {"tool_id": str(index), "name": "ReadFile",
        "args": {"path": "./queue.json", "start": 0, "end": 100},
        "result_sha256": read_result_fingerprint("same bytes"),
        "status": "completed", "declared_at": 110 + index,
        "started_at": 110 + index, "completed_at": 110.5 + index}
        for index in range(5)}
    health = measure_segment(value, "work", terminal_status="budget_exhausted")
    assert health["first_effective_call_seconds"] == 10
    assert health["first_effective_call_censored"] is False
    assert health["effective_calls"] == 1
    assert health["effective_call_ratio"] == .2
    assert health["keyed_reads"] == 5
    assert health["repeated_reads"] == 4
    assert health["work_delta"]["write_receipts"] == 0
    stage["input_revision"] = "r2"
    assert measure_segment(value, "work")["repeated_reads"] == 4  # Within the new revision only.


def test_changed_read_result_is_not_counted_as_repeat_and_old_receipts_are_unkeyed():
    value = snapshot()
    stage = value["node_states"]["work"]
    stage["tools"] = {
        str(index): {"tool_id": str(index), "name": "ReadFile",
            "args": {"path": "./queue.json", "start": 0, "end": 100},
            "result_sha256": read_result_fingerprint(f"revision-{index}"),
            "status": "completed", "declared_at": 110 + index,
            "started_at": 110 + index, "completed_at": 110.5 + index}
        for index in range(4)}
    health = measure_segment(value, "work")
    assert health["repeated_reads"] == 0
    assert health["unkeyed_reads"] == 0
    del stage["tools"]["0"]["result_sha256"]
    old = measure_segment(value, "work")
    assert old["repeated_reads"] == 0
    assert old["unkeyed_reads"] == 1
    assert read_result_fingerprint("", [{"type": "image", "data": "pixel-a"}]) != \
        read_result_fingerprint("", [{"type": "image", "data": "pixel-b"}])


def test_queue_reads_are_observations_not_effective_record_calls():
    value = snapshot()
    stage = value["node_states"]["work"]
    fingerprint = read_result_fingerprint('{"next":"item"}')
    stage["tools"] = {
        "next-1": {"tool_id": "next-1", "name": "ReviewWorkQueue",
                   "args": {"action": "next", "limit": 5}, "result_sha256": fingerprint,
                   "status": "completed", "declared_at": 110, "started_at": 110,
                   "completed_at": 111},
        "next-2": {"tool_id": "next-2", "name": "ReviewWorkQueue",
                   "args": {"action": "next", "limit": 5}, "result_sha256": fingerprint,
                   "status": "completed", "declared_at": 120, "started_at": 120,
                   "completed_at": 121},
        "record": {"tool_id": "record", "name": "ReviewWorkQueue",
                   "args": {"action": "record", "item_id": "item"},
                   "status": "completed", "declared_at": 130, "started_at": 130,
                   "completed_at": 131},
    }
    measured = measure_segment(value, "work")
    assert measured["terminal_calls"] == 3
    assert measured["effective_calls"] == 1
    assert measured["repeated_reads"] == 1
    assert measured["unkeyed_reads"] == 0


def test_missing_baseline_and_old_receipts_do_not_become_zero_progress():
    value = snapshot()
    stage = value["node_states"]["work"]
    stage.pop("slice_progress_baseline")
    stage["tools"] = {"old": {"name": "ReadFile", "status": "completed", "args": {"path": "x"}}}
    health = measure_segment(value, "work")
    assert health["work_delta"]["known"] is False
    assert health["work_delta"]["total_units"] is None
    assert health["sampling"] == "partial"
    assert health["terminal_calls"] == 0 and health["repeated_reads"] == 0
    assert health["first_effective_call_seconds"] is None
    assert health["eta"]["status"] == "unknown"


def test_eta_requires_three_productive_segments():
    value = snapshot()
    stage = value["node_states"]["work"]
    stage["review_progress"] = {"total": 10, "complete": 4}
    first = measure_segment(value, "work")
    assert first["eta"]["status"] == "unknown"
    stage["segment_health_history"] = [
        {"segment_id": "old-1", "work_delta": {"total_units": 1}, "timing": {"wall_seconds": 20}},
        {"segment_id": "old-2", "work_delta": {"total_units": 2}, "timing": {"wall_seconds": 40}},
        {"segment_id": "old-3", "work_delta": {"total_units": 1}, "timing": {"wall_seconds": 30}},
    ]
    second = measure_segment(value, "work")
    assert second["eta"]["status"] == "estimated"
    assert second["eta"]["seconds"] == 120
    assert second["eta"]["sample_count"] == 3


def test_machine_error_codes_only_no_prose_guess():
    assert extract_tool_error_code('{"error":{"code":"file_not_found"}}') == "file_not_found"
    assert extract_tool_error_code("PNG cannot be read as text") is None
    assert extract_tool_error_code("", [{"error_code": "tool_input_type_mismatch"}]) == "tool_input_type_mismatch"
    assert extract_tool_error_code('{"error_code": {"unexpected": "shape"}}') is None


def test_clock_rollback_uses_monotonic_duration_and_marks_wall_sampling_partial():
    value = snapshot()
    stage = value["node_states"]["work"]
    stage.update(started_at=200.0, completed_at=100.0, duration_ms=-100000,
                 duration_monotonic_ms=10000)
    health = measure_segment(value, "work")
    assert health["timing"]["wall_seconds"] == 10
    assert health["sampling"] == "partial"


def test_reporting_tool_is_not_durable_work_or_effective_call():
    value = snapshot()
    value["node_states"]["work"]["tools"] = {"r": {"tool_id": "r", "name": "report_step",
        "status": "completed", "declared_at": 110, "started_at": 111, "completed_at": 112}}
    health = measure_segment(value, "work")
    assert health["effective_calls"] == 0
    assert health["work_delta"]["write_receipts"] == 0


def test_model_review_tool_wait_and_host_time_are_exclusive():
    value = snapshot()
    stage = value["node_states"]["work"]
    stage["role"] = "auditor"
    stage["model_spans"] = [{"started_at": 110, "completed_at": 140,
                             "duration_monotonic_ms": 30000}]
    stage["safety_review_spans"] = [{"started_at": 125, "completed_at": 130,
                                     "duration_monotonic_ms": 5000}]
    stage["tools"] = {"one": {"tool_id": "one", "name": "ReadFile", "args": {"path": "x"},
                            "status": "completed", "declared_at": 144,
                            "started_at": 145, "completed_at": 155}}
    stage["wait_intervals"] = [{"start": 160, "end": 170}]
    timing = measure_segment(value, "work")["timing"]
    assert timing["model_seconds"] == 25
    assert timing["business_review_model_seconds"] == 25
    assert timing["safety_review_seconds"] == 5
    assert timing["tool_seconds"] == 10
    assert timing["wait_seconds"] == 10
    assert timing["host_seconds"] == 50
    assert timing["coverage"] == "complete"
