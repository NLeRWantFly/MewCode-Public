import json
from pathlib import Path

import pytest

from mewcode.experts.recovery_replay import (
    analyze_replay, export_activity, load_replay, read_activity, sanitize_activity, sanitize_health,
)
from mewcode.orchestration.activity import ActivityStore


def _fixture(tmp_path):
    store = ActivityStore(tmp_path / "work")
    store.expert({"type": "expert_tool", "conversation_id": "conversation-secret",
        "run_id": "run-secret", "node_id": "private-work-item", "seq": 1,
        "name": "ReadFile", "args": {"path": "C:/secret/customer-file.pdf", "page_idx": 4},
        "output": "customer private content", "at": "2026-09-11T10:00:00Z"})
    store.expert({"type": "expert_segment_health", "conversation_id": "conversation-secret",
        "run_id": "run-secret", "node_id": "private-work-item", "seq": 2,
        "at": "2026-09-11T10:00:01Z", "health": {
            "segment_id": "segment-secret", "node_id": "private-work-item", "node_attempt": 1,
            "started_at": 1.0, "ended_at": 2.0,
            "terminal_status": "budget_exhausted", "error_kind": "segment_timeout",
            "checkpoint_present": True, "blocker_kinds": [],
            "sampling": "partial", "terminal_calls": 4, "effective_calls": 1,
            "repeated_reads": 3, "first_effective_call_seconds": 1.0,
            "work_delta": {"known": True, "write_receipts": 1,
                           "review_complete": 0, "submitted_items": 0},
            "timing": {"wall_seconds": 1.0, "model_seconds": None, "coverage": "partial"},
            "progress_after": {"submitted_items": ["customer-secret"]},
            "evidence_refs": ["tool-secret"],
        }})
    return store.path


def test_exact_run_export_is_read_only_redacted_and_byte_stable(tmp_path):
    db_path = _fixture(tmp_path)
    before = db_path.stat().st_mtime_ns
    first = export_activity(db_path, tmp_path / "out1", run_id="run-secret")
    second = export_activity(db_path, tmp_path / "out2", run_id="run-secret")
    assert first == second and db_path.stat().st_mtime_ns == before
    assert (tmp_path / "out1" / "events.jsonl").read_bytes() == (tmp_path / "out2" / "events.jsonl").read_bytes()
    all_output = ((tmp_path / "out1" / "events.jsonl").read_text(encoding="utf-8") +
                  (tmp_path / "out1" / "manifest.json").read_text(encoding="utf-8"))
    for secret in ("run-secret", "conversation-secret", "private-work-item", "segment-secret",
                   "C:/secret/customer-file.pdf", "customer private content", "customer-secret", "tool-secret"):
        assert secret not in all_output
    manifest, records = load_replay(tmp_path / "out1" / "manifest.json")
    assert manifest["source_rows"] == 2 and manifest["health_records"] == 1
    assert records[0]["resource_key"] and records[1]["health"]["work_delta"]["write_receipts"] == 1


def test_replay_classifies_measured_health_but_does_not_invent_success(tmp_path):
    db_path = _fixture(tmp_path)
    export_activity(db_path, tmp_path / "out", run_id="run-secret")
    _, records = load_replay(tmp_path / "out" / "manifest.json")
    report = analyze_replay(records)
    assert report["historical"]["effective_call_ratio"] == .25
    assert report["classification"] == {"capacity_shortfall": 1}
    assert report["simulation"]["suggested_actions"] == {"retry_same_checkpoint": 1}
    assert report["simulation"]["verified_successes"] is None
    assert report["anchor_comparison"]["png_type_misuse"]["status"] == "different_definition_png_subset_not_identified"


def test_missing_run_and_tampered_replay_fail_closed(tmp_path):
    db_path = _fixture(tmp_path)
    with pytest.raises(LookupError):
        export_activity(db_path, tmp_path / "missing", run_id="other-run")
    export_activity(db_path, tmp_path / "out", run_id="run-secret")
    path = tmp_path / "out" / "events.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        load_replay(tmp_path / "out" / "manifest.json")


def test_read_activity_refuses_uncheckpointed_sqlite_sidecars(tmp_path):
    db_path = _fixture(tmp_path)
    Path(str(db_path) + "-wal").write_bytes(b"fixture sidecar sentinel")
    with pytest.raises(ValueError, match="WAL/SHM/journal sidecars"):
        read_activity(db_path, run_id="run-secret")


def test_legacy_events_remain_partial_and_no_prose_or_arguments_escape():
    event = sanitize_activity({"kind": "expert_node_completed", "node_id": "x",
        "status": "needs_attention", "text": "secret business evidence",
        "arguments": {"path": "secret.txt"}}, run_id="run", source_seq=3)
    assert event is not None
    assert "secret" not in json.dumps(event)
    report = analyze_replay([event])
    assert report["historical"]["measured_segments"] == 0
    assert report["historical"]["classification_coverage"] == "unavailable"
    assert report["classification"] == {}


@pytest.mark.parametrize("error,diagnosis", [
    ("context_limit", "context_window_limit"),
    ("output_window_limit", "output_window_limit"),
    ("semantic_uncertain", "reasoning_limit"),
])
def test_new_capacity_and_reasoning_errors_survive_redacted_replay(error, diagnosis):
    health = sanitize_health({"segment_id": "s", "node_id": "n", "error_kind": error,
        "terminal_status": "needs_attention", "sampling": "complete",
        "work_delta": {"known": True}, "timing": {"coverage": "complete"}}, run_id="run")
    assert health["error_kind"] == error
    report = analyze_replay([{"record_kind": "health", "source_seq": 1, "health": health}])
    assert report["classification"] == {diagnosis: 1}


def test_real_run_fixture_preserves_metric_definitions_and_contains_no_raw_inputs():
    fixture = Path(__file__).parent / "fixtures" / "runtime-self-recovery" / "manifest.json"
    if not fixture.is_file():
        pytest.skip("Private replay capture is excluded from the public snapshot")
    manifest, records = load_replay(fixture)
    assert manifest["source_rows"] == 20152
    assert manifest["health_records"] == 0
    report = analyze_replay(records)
    legacy = report["historical"]["legacy"]
    assert legacy["queue_total_declarations"] == 1240
    assert legacy["queue_actions"] == {
        "evidence": 750, "initialize": 9, "next": 104,
        "read": 22, "record": 321, "summary": 34,
    }
    assert legacy["queue_read_or_next_or_summary_declarations"] == 160
    assert legacy["path_error_text_candidates"] == 58
    assert legacy["png_readfile_declarations"] == 0
    assert report["historical"]["measured_segments"] == 0
    assert report["historical"]["effective_call_ratio"] is None
    assert report["anchor_comparison"]["queue_repeated_reads"]["status"] == "1240_is_total_queue_calls_not_repeated_reads"
    assert report["simulation"]["verified_successes"] is None
    text = (fixture.parent / "events.jsonl").read_text(encoding="utf-8")
    for raw in ("sample-project", "AppData", "8a71722d104442718d3b7fe2ed77778e", "example-user"):
        assert raw not in text
