import json
from scripts.inspect_expert_snapshot_replay import analyze_snapshot, main


def test_snapshot_report_contains_only_allowlisted_structure_and_statuses():
    snapshot = {
        "version": 2,
        "run_id": "run-private-id",
        "conversation_id": "conversation-private-id",
        "task": "private prompt text",
        "status": "completed",
        "business_path:/customer/private": "do not expose this key or value",
        "node_states": {
            "private-node-id": {"status": "completed", "text": "private output"},
            "another-private-node": {"status": "customer-private-status", "secret": "hidden"},
        },
        "stages": {
            "private-node-id": {"status": "completed", "text": "private output"},
            "another-private-node": {"status": "customer-private-status", "secret": "hidden"},
        },
        "children": {"private-child-id": {"status": "paused", "worktree": "E:/business/private"}},
        "events": [
            {"type": "expert_node_completed", "status": "completed", "text": "private output"},
            {"type": "private-event-name", "prompt": "private prompt"},
        ],
        "credential-field": "private-token-value",
    }

    report = analyze_snapshot(snapshot)
    encoded = json.dumps(report, ensure_ascii=False)

    assert report["run_status"] == "completed"
    assert report["field_structure"]["collections"]["node_states"]["counts"] == {
        "completed": 1, "unknown": 1,
    }
    assert report["field_structure"]["collections"]["stages"]["deduplicated_to"] == "node_states"
    assert report["field_structure"]["collections"]["stages"]["records"] == 0
    assert report["deduplication"]["stages_alias"] == "node_states"
    assert "private-event-name" not in encoded
    for secret in ("run-private-id", "conversation-private-id", "private prompt", "private output",
                   "private-node-id", "private-child-id", "E:/business/private",
                   "private-token-value", "customer-private-status", "business_path:/customer/private"):
        assert secret not in encoded


def test_public_cache_envelope_reports_nested_summary_without_exposing_metadata():
    report = analyze_snapshot({
        "source_mtime_ns": 123456789,
        "source_size": 7000000,
        "event_log_size": 999999,
        "snapshot_version": 123,
        "projection_version": 4,
        "conversation_id": "private-conversation-id",
        "summary": {
            "version": 2,
            "run_id": "private-run-id",
            "status": "paused",
            "node_states": {"private-node-id": {"status": "completed"}},
            "events": [{"type": "expert_stage", "status": "completed"}],
        },
    })

    assert report["source_format"] == "public_summary_cache"
    assert report["cache_envelope"]["known_fields"] == [
        "conversation_id", "event_log_size", "projection_version", "snapshot_version",
        "source_mtime_ns", "source_size", "summary",
    ]
    assert report["cache_envelope"]["source_binding_validated"] is False
    assert report["run_status"] == "paused"
    assert report["field_structure"]["collections"]["node_states"]["counts"] == {
        "completed": 1,
    }
    encoded = json.dumps(report)
    for secret in ("private-conversation-id", "private-run-id", "private-node-id", "123456789", "7000000", "999999"):
        assert secret not in encoded


def test_token_counter_availability_never_substitutes_budget_or_text_length():
    snapshot = {
        "version": 2,
        "status": "paused",
        "node_states": {
            "node-1": {
                "status": "paused",
                "input_tokens": 123,
                "output_tokens": True,
                "billable_tokens": 45,
                "token_budget": 999999,
                "partial_text": "x" * 10_000,
                "usage": {"input_tokens": 120, "output_tokens": 20},
            },
        },
        "members": {"member-1": {"usage": {"output_tokens": 0, "token_budget": 1000}}},
        "children": {"child-1": {"usage": {"billable_tokens": "500"}}},
        "usage": {"total": {"input_tokens": 120, "output_tokens": 20, "source_count": 2}},
    }

    report = analyze_snapshot(snapshot)
    counters = report["token_counter_availability"]
    node = counters["sources"]["node_states"]["availability"]

    assert node["input_tokens"] == {"records_with_counter": 1, "availability": "available"}
    assert node["output_tokens"] == {"records_with_counter": 1, "availability": "available"}
    assert node["billable_tokens"] == {"records_with_counter": 1, "availability": "available"}
    assert counters["sources"]["members"]["availability"]["output_tokens"]["availability"] == "available"
    assert counters["sources"]["children"]["availability"]["billable_tokens"]["availability"] == "unknown"
    assert counters["billable_token_total"] is None
    assert counters["total_coverage"] == "unknown"
    assert counters["sources"]["usage.total"]["deduplicated_as"] == "derived_aggregate_excluded_from_source_counters"
    assert report["deduplication"]["reported_aggregate_source_count"] == 2
    assert report["deduplication"]["visible_node_usage_records"] == 1
    assert "999999" not in json.dumps(report)
    assert "10000" not in json.dumps(report)


def test_snapshot_aggregate_usage_events_are_excluded_from_source_availability():
    snapshot = {
        "version": 2,
        "status": "completed",
        "events": [
            {"type": "expert_usage", "node_id": "node", "node_attempt": 1,
             "role": "executor", "member_id": "member", "seq": 7,
             "billable_tokens": 100},
            {"type": "expert_usage", "node_id": "node", "node_attempt": 1,
             "role": "executor", "member_id": "member", "seq": 8,
             "usage": {"billable_tokens": 120}, "snapshot_aggregate": True},
            {"type": "expert_usage", "node_id": "node", "node_attempt": 1,
             "role": "executor", "member_id": "member", "seq": 7,
             "billable_tokens": 130},
        ],
    }

    report = analyze_snapshot(snapshot)
    dedup = report["deduplication"]
    source = report["token_counter_availability"]["sources"]["expert_usage_events"]

    assert dedup["usage_event_records"] == 3
    assert dedup["source_usage_event_records"] == 2
    assert dedup["snapshot_aggregate_event_records_excluded"] == 1
    assert dedup["records_with_complete_stream_identity"] == 2
    assert dedup["duplicate_source_sequence_rows"] == 1
    assert source["availability"]["billable_tokens"]["records_with_counter"] == 2
    assert report["token_counter_availability"]["billable_token_total"] is None


def test_empty_node_state_collection_falls_back_to_legacy_stages_for_counter_shape():
    report = analyze_snapshot({
        "version": 1,
        "node_states": {},
        "stages": {"private-node": {"usage": {"input_tokens": 9}}},
    })

    assert report["deduplication"]["stages_alias"] == "not_proven"
    assert report["token_counter_availability"]["sources"]["stages"]["availability"][
        "input_tokens"]["availability"] == "available"


def test_cli_requires_explicit_path_and_reads_synthetic_snapshot_without_writing(tmp_path, capsys):
    snapshot_path = tmp_path / "synthetic-snapshot.json"
    snapshot_path.write_text(json.dumps({"version": 2, "status": "paused", "attempts": []}),
                             encoding="utf-8")
    before = snapshot_path.stat().st_mtime_ns

    assert main([str(snapshot_path)]) == 0
    output = json.loads(capsys.readouterr().out)

    assert snapshot_path.stat().st_mtime_ns == before
    assert output["status"] == "OK"
    assert output["report"]["run_status"] == "paused"
    assert str(snapshot_path) not in json.dumps(output)


def test_cli_errors_do_not_echo_the_input_path(tmp_path, capsys):
    secret_path = tmp_path / "private-business-path" / "missing.json"

    assert main([str(secret_path)]) == 2
    output = capsys.readouterr().out

    assert output == '{"error": "input_unreadable_or_invalid_json", "status": "ERROR"}\n'
    assert str(secret_path) not in output
