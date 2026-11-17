import hashlib
import json
from pathlib import Path

import pytest

from mewcode.experts.recovery_replay import (
    METRIC_SOURCE_KINDS,
    analyze_replay,
    compare_recovery_metrics,
    export_activity,
    load_replay,
    sanitize_activity,
)
from scripts.gate_recovery_replay import gate_replays, main
from scripts.replay_scheduler_recovery import main as replay_main


def event(kind, seq, **values):
    return sanitize_activity({"kind": kind, "node_id": values.pop("node_id", "node-secret"),
        "role": values.pop("role", "executor"), "attempt": values.pop("attempt", 1),
        "member_id": values.pop("member_id", "member-secret"),
        "source_seq": seq, **values}, run_id="run-secret", source_seq=seq)


def full_records(*, manual=3, stops=2, tokens=(10, 20)):
    records = []
    for kind in ("expert_role_wait", "ask_user", "human_oncall")[:manual]:
        records.append(event(kind, len(records) + 1, active=True, elapsed=5,
                             arguments={"questions": ["private question"]}))
    if stops >= 1:
        records.extend([
            event("expert_node_started", 10, status="running", attempt=1),
            event("expert_node_completed", 11, status="paused", attempt=1),
            event("expert_stage", 12, status="paused", attempt=1),  # duplicate transition
        ])
    if stops >= 2:
        records.extend([
            event("expert_node_started", 13, status="running", attempt=2),
            event("expert_flow_state", 14, status="blocked", attempt=2),
        ])
    if tokens is not None:
        for index, value in enumerate(tokens, 1):
            records.append(event("expert_usage", 20 + index, billable_tokens=value,
                                 input_tokens=value - 10, output_tokens=10))
    return records


def test_new_event_allowlist_redacts_questions_roles_and_non_token_usage():
    ask = sanitize_activity({"kind": "ask_user", "node_id": "n", "role": "private-role",
        "arguments": {"questions": ["secret customer name"]}, "text": "private body",
        "request_id": "request-private", "at": "2026-09-10T10:00:00Z"}, run_id="run", source_seq=4)
    usage = sanitize_activity({"kind": "expert_usage", "node_id": "n", "role": "executor",
        "input_tokens": 12, "output_tokens": 3, "billable_tokens": 15,
        "total_cost_usd": 999, "system_hash": "private-hash"}, run_id="run", source_seq=5)
    assert ask["event_type"] == "ask_user" and ask["role"] == "other"
    assert "interaction_key" in ask and "secret customer name" not in json.dumps(ask)
    assert usage["usage"] == {"input_tokens": 12, "output_tokens": 3, "billable_tokens": 15}
    assert "999" not in json.dumps(usage) and "private-hash" not in json.dumps(usage)


def test_three_metrics_use_observed_events_and_explicit_denominators():
    report = analyze_replay(full_records(), mode="all", pages=2, source_rows=30)
    metrics = report["recovery_metrics"]
    assert metrics["human_interventions"]["value"] == 3
    assert metrics["human_interventions"]["counts_by_event_type"] == {
        "ask_user": 1, "expert_role_wait": 1, "human_oncall": 1}
    assert metrics["human_interventions"]["denominator"] == 30
    assert metrics["shutdown_count"]["value"] == 2
    assert metrics["shutdown_count"]["coverage"] == "complete"
    assert metrics["shutdown_count"]["same_stopped_state_signals_suppressed"] == 1
    assert metrics["shutdown_count"]["denominator"] == 5
    assert metrics["billable_tokens_per_page"]["total_tokens"] == 20
    assert metrics["billable_tokens_per_page"]["value"] == 10
    assert metrics["billable_tokens_per_page"]["denominator"] == 2
    assert report["simulation"]["verified_successes"] is None


def test_missing_usage_transitions_and_denominators_are_explicitly_unknown():
    missing_usage = event("expert_usage", 1, input_tokens=40, output_tokens=3)
    missing_flow_status = event("expert_flow_state", 2, phase="recovery")
    metrics = analyze_replay([missing_usage, missing_flow_status], mode="measure")["recovery_metrics"]
    assert metrics["human_interventions"]["value"] is None
    assert metrics["human_interventions"]["coverage"] == "unknown"
    assert metrics["shutdown_count"]["value"] is None
    assert metrics["shutdown_count"]["coverage"] == "partial_or_unknown"
    assert metrics["billable_tokens_per_page"]["value"] is None
    assert metrics["billable_tokens_per_page"]["coverage"] == "partial_or_unknown"
    assert metrics["billable_tokens_per_page"]["unknown_reason"]


def test_cumulative_billable_counters_are_differenced_per_exact_stream():
    records = full_records(manual=0, stops=0, tokens=(10, 20))
    metric = analyze_replay(records, mode="measure", pages=1, source_rows=2)[
        "recovery_metrics"]["billable_tokens_per_page"]
    assert metric["total_tokens"] == 20
    assert metric["value"] == 20
    assert metric["coverage"] == "complete"


def test_counter_reset_and_unidentifiable_or_legacy_events_fail_closed():
    reset = [event("expert_usage", seq, billable_tokens=value)
             for seq, value in ((1, 10), (2, 20), (3, 3))]
    metric = analyze_replay(reset, mode="measure", pages=1, source_rows=3)[
        "recovery_metrics"]["billable_tokens_per_page"]
    assert metric["coverage"] == "partial_or_unknown"
    assert metric["value"] is None and metric["total_tokens"] is None
    assert metric["reset_streams"] == 1
    assert "decreased" in metric["unknown_reason"]

    missing_identity = event("expert_usage", 4, billable_tokens=4, member_id=None)
    metric = analyze_replay([missing_identity], mode="measure", pages=1, source_rows=1)[
        "recovery_metrics"]["billable_tokens_per_page"]
    assert metric["coverage"] == "partial_or_unknown"
    assert metric["value"] is None and metric["unidentifiable_events"] == 1

    legacy = event("expert_usage", 5, input_tokens=10, output_tokens=2)
    metric = analyze_replay([legacy], mode="measure", pages=1, source_rows=1)[
        "recovery_metrics"]["billable_tokens_per_page"]
    assert metric["coverage"] == "partial_or_unknown"
    assert metric["value"] is None and metric["missing_token_events"] == 1


def test_conflicting_source_sequence_is_excluded_and_marks_coverage_unknown():
    ask = event("ask_user", 7)
    conflicting_wait = event("expert_role_wait", 7)
    metrics = analyze_replay([ask, conflicting_wait], mode="measure", source_rows=12)[
        "recovery_metrics"]
    manual = metrics["human_interventions"]
    assert manual["observed_count"] == 0
    assert manual["value"] is None and manual["coverage"] == "partial_or_unknown"
    assert metrics["source_coverage"]["conflicting_sanitized_rows"] == 2


def test_unrecognized_source_event_kind_prevents_false_complete_coverage(tmp_path):
    from mewcode.orchestration.activity import ActivityStore

    store = ActivityStore(tmp_path / "work")
    store.expert({"type": "ask_user", "conversation_id": "c", "run_id": "r",
                  "node_id": "n", "seq": 1})
    store.expert({"type": "future_manual_kind", "conversation_id": "c", "run_id": "r",
                  "node_id": "n", "seq": 2})
    manifest = export_activity(store.path, tmp_path / "out", run_id="r")
    assert manifest["source_coverage"]["unrecognized_activity_kind_rows"] == 1
    metric = manifest["recovery_metrics"]["human_interventions"]
    assert metric["observed_count"] == 1
    assert metric["value"] is None and metric["coverage"] == "partial_or_unknown"


def _metric(value, denominator=10, coverage="complete"):
    return {"value": value, "numerator": value, "denominator": denominator, "coverage": coverage}


def test_gate_requires_all_three_strict_improvements_with_complete_coverage():
    baseline = {"human_interventions": _metric(20), "shutdown_count": _metric(8),
                "billable_tokens_per_page": _metric(1000)}
    candidate = {"human_interventions": _metric(19), "shutdown_count": _metric(7),
                 "billable_tokens_per_page": _metric(999)}
    assert compare_recovery_metrics(baseline, candidate)["status"] == "PASS"

    candidate["shutdown_count"] = _metric(8)
    decision = compare_recovery_metrics(baseline, candidate)
    assert decision["status"] == "FAIL"
    assert decision["comparisons"]["shutdown_count"]["improved"] is False


def test_gate_rejects_unknown_coverage_and_zero_or_missing_denominators():
    baseline = {"human_interventions": _metric(20), "shutdown_count": _metric(8),
                "billable_tokens_per_page": _metric(1000)}
    candidate = {"human_interventions": _metric(19), "shutdown_count": _metric(7),
                 "billable_tokens_per_page": _metric(999, denominator=None)}
    decision = compare_recovery_metrics(baseline, candidate)
    assert decision["status"] == "FAIL"
    assert decision["comparisons"]["billable_tokens_per_page"]["improved"] is None
    candidate["billable_tokens_per_page"] = _metric(999, coverage="partial_or_unknown")
    assert compare_recovery_metrics(baseline, candidate)["status"] == "FAIL"


def _write_replay(path: Path, records, *, source_rows=30):
    path.mkdir(parents=True)
    raw = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                   for record in records).encode("utf-8")
    (path / "events.jsonl").write_bytes(raw)
    counts = {kind: sum(row.get("event_type") == kind for row in records)
              for kind in sorted(METRIC_SOURCE_KINDS)}
    manifest = {"schema_version": 2, "exporter_version": "recovery-replay-export-v5",
        "metric_schema_version": 2, "records_file": "events.jsonl", "records": len(records),
        "source_rows": source_rows, "run_key": "opaque-run-key",
        "source_coverage": {"schema_version": 1, "source_rows": source_rows,
            "integrity": "complete", "integrity_reasons": [],
            "metric_event_counts": counts},
        "records_sha256": hashlib.sha256(raw).hexdigest()}
    manifest_path = path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest_path


def test_gate_command_reads_only_redacted_replays_and_fails_closed_on_legacy_manifest(tmp_path, capsys):
    baseline = _write_replay(tmp_path / "baseline", full_records(manual=3, stops=2, tokens=(100, 200)))
    candidate = _write_replay(tmp_path / "candidate", full_records(manual=2, stops=1, tokens=(80, 160)))
    decision = gate_replays(baseline, candidate, baseline_pages=2, candidate_pages=2)
    assert decision["status"] == "PASS"
    assert decision["comparisons"]["billable_tokens_per_page"]["candidate"]["value"] == 80

    fixture_manifest = Path(__file__).parent / "fixtures" / "runtime-self-recovery" / "manifest.json"
    if not fixture_manifest.is_file():
        pytest.skip("Private replay capture is excluded from the public snapshot")
    assert main([str(fixture_manifest), str(fixture_manifest), "--baseline-pages", "241",
                 "--candidate-pages", "241"]) == 1
    assert '"status": "FAIL"' in capsys.readouterr().out


def test_replay_assert_separates_integrity_from_complete_metric_coverage(tmp_path, capsys):
    records = [event("expert_role_wait", 1), event("expert_flow_state", 2, phase="recovery")]
    manifest = _write_replay(tmp_path / "partial", records, source_rows=30)
    assert replay_main([str(manifest), "--mode", "all", "--pages", "182",
                        "--assert-integrity"]) == 0
    integrity_report = json.loads(capsys.readouterr().out)
    assert integrity_report["recovery_metrics"]["shutdown_count"]["coverage"] == "partial_or_unknown"
    assert integrity_report["recovery_metrics"]["billable_tokens_per_page"]["coverage"] == "unknown"
    assert integrity_report["simulation"]["verified_successes"] is None

    assert replay_main([str(manifest), "--mode", "all", "--pages", "182", "--assert"]) == 1
    metric_report = json.loads(capsys.readouterr().out)
    assert metric_report["metric_coverage_assertion"]["status"] == "FAIL"
    assert metric_report["metric_coverage_assertion"]["incomplete_metrics"] == [
        "shutdown_count", "billable_tokens_per_page"]


def test_export_adds_metric_schema_without_changing_raw_source(tmp_path):
    from mewcode.orchestration.activity import ActivityStore

    store = ActivityStore(tmp_path / "work")
    store.expert({"type": "ask_user", "conversation_id": "c", "run_id": "r",
                  "node_id": "n", "seq": 1, "role": "executor", "text": "secret question"})
    store.expert({"type": "expert_role_wait", "conversation_id": "c", "run_id": "r",
                  "node_id": "n", "seq": 2, "role": "executor"})
    store.expert({"type": "human_oncall", "conversation_id": "c", "run_id": "r",
                  "node_id": "n", "seq": 3, "role": "executor"})
    store.expert({"type": "expert_node_started", "conversation_id": "c", "run_id": "r",
                  "node_id": "n", "node_attempt": 1, "seq": 4})
    store.expert({"type": "expert_node_completed", "conversation_id": "c", "run_id": "r",
                  "node_id": "n", "node_attempt": 1, "seq": 5, "status": "blocked"})
    store.expert({"type": "expert_stage", "conversation_id": "c", "run_id": "r",
                  "node_id": "n", "node_attempt": 1, "seq": 6, "status": "blocked"})
    store.expert({"type": "expert_flow_state", "conversation_id": "c", "run_id": "r",
                  "node_id": "n", "node_attempt": 1, "seq": 7})
    store.expert({"type": "expert_usage", "conversation_id": "c", "run_id": "r",
                  "node_id": "n", "node_attempt": 1, "member_id": "m",
                  "seq": 8, "role": "executor"})
    before = store.path.stat().st_mtime_ns
    manifest = export_activity(store.path, tmp_path / "out", run_id="r")
    assert store.path.stat().st_mtime_ns == before
    assert manifest["metric_schema_version"] == 2
    assert manifest["source_coverage"]["integrity"] == "complete"
    assert manifest["source_coverage"]["source_rows"] == 8
    assert manifest["recovery_metrics"]["human_interventions"]["value"] == 3
    assert manifest["recovery_metrics"]["human_interventions"]["coverage"] == "complete"
    assert manifest["recovery_metrics"]["shutdown_count"]["observed_stop_transitions"] == 1
    assert manifest["recovery_metrics"]["shutdown_count"]["same_stopped_state_signals_suppressed"] == 1
    assert manifest["recovery_metrics"]["shutdown_count"]["coverage"] == "partial_or_unknown"
    assert manifest["recovery_metrics"]["billable_tokens_per_page"]["missing_token_events"] == 1
    loaded, records = load_replay(tmp_path / "out" / "manifest.json")
    assert loaded["exporter_version"] == "recovery-replay-export-v5"
    assert "secret question" not in json.dumps(records)


def test_activity_store_persists_only_allowlisted_usage_and_safe_flow_phase(tmp_path):
    from mewcode.orchestration.activity import ActivityStore

    store = ActivityStore(tmp_path / "work")
    store.expert({"type": "expert_usage", "conversation_id": "c", "run_id": "r",
                  "node_id": "node-secret", "node_attempt": 2, "member_id": "member-secret",
                  "seq": 1, "role": "executor", "billable_tokens": 10,
                  "input_tokens": 8, "output_tokens": 2, "total_cost_usd": 123.45})
    store.expert({"type": "expert_flow_state", "conversation_id": "c", "run_id": "r",
                  "node_id": "node-secret", "node_attempt": 2, "member_id": "member-secret",
                  "seq": 2, "phase": "hitl_blocked"})
    store.expert({"type": "expert_flow_state", "conversation_id": "c", "run_id": "r",
                  "node_id": "node-secret", "node_attempt": 2, "member_id": "member-secret",
                  "seq": 3, "phase": "private-customer-label"})
    store.expert({"type": "expert_usage", "conversation_id": "c", "run_id": "r",
                  "node_id": "node-secret", "attempt": 1, "member_id": "member-secret",
                  "seq": 4, "role": "executor", "billable_tokens": 5})
    with store.db() as db:
        rows = db.execute("SELECT data FROM activity ORDER BY source_seq").fetchall()
    stored = [json.loads(row[0]) for row in rows]
    assert stored[0]["billable_tokens"] == 10
    assert "input_tokens" not in stored[0] and "output_tokens" not in stored[0]
    assert "total_cost_usd" not in stored[0]
    assert stored[1]["phase"] == "hitl_blocked"
    assert "phase" not in stored[2]
    assert stored[3]["attempt_identity_known"] is False
    unclear_stream = sanitize_activity(stored[3], run_id="r", source_seq=4)
    metric = analyze_replay([unclear_stream], mode="measure", pages=1, source_rows=1)[
        "recovery_metrics"]["billable_tokens_per_page"]
    assert metric["coverage"] == "partial_or_unknown"
    assert metric["value"] is None and metric["unidentifiable_events"] == 1
