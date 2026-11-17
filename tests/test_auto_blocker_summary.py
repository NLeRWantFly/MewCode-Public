from __future__ import annotations

import json
from pathlib import Path

from scripts.summarize_auto_blockers import main, summarize_inputs


def _row(**fields):
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":")) + "\n"


def test_summary_groups_rows_and_deduplicates_operations_without_echoing_private_fields(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    first = audit_dir / "decisions.jsonl"
    first.write_text(
        _row(
            decision="deny",
            source_layer="auto_hard_deny",
            subcondition="auto_hard_deny",
            rule_id="rule-12",
            review_route={"status": "not_used"},
            permission_mode="auto",
            session_id="session-A",
            operation_id="tool-call-1",
            params_summary_truncated=True,
            params_summary="PRIVATE_ARGUMENTS",
            reason="PRIVATE_REASON",
        )
        + _row(
            decision="review",
            source_layer="auto_safety_reviewer",
            subcondition="review_required",
            review_route={"status": "unavailable"},
            session_id="session-A",
            operation_id="tool-call-1",
            params_summary="x" * 200 + "...",
            reason="ANOTHER_PRIVATE_REASON",
        ),
        encoding="utf-8",
    )
    (audit_dir / "decisions.1.jsonl").write_text(
        _row(
            decision="an unsafe value!",
            source_layer="bad value with spaces",
            subcondition="",
            review_route=[],
            permission_mode=None,
            params_summary_truncated="true",
            params_summary="PRIVATE_OLD_ARGUMENTS",
        ),
        encoding="utf-8",
    )

    report = summarize_inputs([first, audit_dir])

    assert report["counts"]["decision_records"] == 3
    assert report["counts"]["deduplicated_identifiable_operations"] == 1
    assert report["counts"]["decision_records_with_operation_id"] == 2
    assert report["counts"]["decision_records_without_valid_operation_id"] == 1
    assert report["counts"]["duplicate_decision_records_for_identified_operations"] == 1
    assert report["counts"]["total_operation_count"] is None
    assert report["aggregates"]["decision"] == {"deny": 1, "review": 1, "unknown": 1}
    assert report["aggregates"]["source_layer"]["unknown"] == 1
    assert report["aggregates"]["subcondition"]["missing"] == 1
    assert report["aggregates"]["rule_id"]["missing"] == 2
    assert report["aggregates"]["review_route_status"] == {
        "missing": 1,
        "not_used": 1,
        "unavailable": 1,
    }
    assert report["permission_mode"]["known_auto_records"] == 1
    assert report["permission_mode"]["missing_mode_records"] == 2
    assert report["permission_mode"]["unknown_mode_records"] == 0
    assert report["parameter_truncation"]["explicitly_truncated_records"] == 1
    assert report["parameter_truncation"]["missing_marker_records"] == 1
    assert report["parameter_truncation"]["invalid_marker_records"] == 1
    assert report["parameter_truncation"]["possible_legacy_truncated_previews"] == 1
    assert report["input_coverage"]["duplicate_references_ignored"] == 1
    encoded = json.dumps(report, ensure_ascii=False)
    for private_text in ("PRIVATE_ARGUMENTS", "PRIVATE_REASON", "ANOTHER_PRIVATE_REASON", "PRIVATE_OLD_ARGUMENTS", "tool-call-1", "session-A"):
        assert private_text not in encoded
    assert report["turn_stop_events"] == {
        "count": None,
        "status": "unknown",
        "reason": "these audit rows do not record turn-stop events",
    }
    assert report["production_attribution"].startswith("not inferred")


def test_directory_scan_is_one_level_and_cli_writes_aggregate_json(tmp_path: Path):
    audit_dir = tmp_path / "audit"
    nested = audit_dir / "nested"
    nested.mkdir(parents=True)
    (audit_dir / "decisions.jsonl").write_text(_row(decision="allow"), encoding="utf-8")
    (nested / "not-scanned.jsonl").write_text(_row(decision="deny"), encoding="utf-8")
    output = tmp_path / "summary.json"

    assert main([str(audit_dir), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["counts"]["decision_records"] == 1
    assert report["aggregates"]["decision"] == {"allow": 1}
    assert report["input_coverage"]["discovered_jsonl_files"] == 1
    assert report["input_coverage"]["files_scanned"] == 1


def test_bad_oversized_and_partial_rows_are_reported_with_read_limits(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    valid = b'{"decision":"allow"}\n'
    partial = b'{"decision":"deny"}'
    path.write_bytes(valid + b"x" * 129 + b"\nnot-json\n" + partial)

    report = summarize_inputs(
        [path],
        max_file_bytes=len(valid) + 129 + 1 + len(b"not-json\n") + 5,
        max_line_bytes=128,
    )

    assert report["counts"]["decision_records"] == 1
    assert report["coverage"]["invalid_rows"] == 1
    assert report["coverage"]["oversized_lines"] == 1
    assert report["coverage"]["incomplete_lines_omitted"] == 1
    assert report["input_coverage"]["bytes_omitted"] == len(partial) - 5
    assert report["read_limits"]["max_line_bytes"] == 128


def test_file_and_total_byte_limits_count_omitted_inputs(tmp_path: Path):
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    first.write_text(_row(decision="allow"), encoding="utf-8")
    second.write_text(_row(decision="deny"), encoding="utf-8")

    by_file_count = summarize_inputs([first, second], max_files=1)
    assert by_file_count["counts"]["decision_records"] == 1
    assert by_file_count["input_coverage"]["files_omitted_by_file_limit"] == 1
    assert by_file_count["input_coverage"]["bytes_omitted"] == second.stat().st_size

    by_total_bytes = summarize_inputs([first, second], max_total_bytes=0)
    assert by_total_bytes["counts"]["decision_records"] == 0
    assert by_total_bytes["input_coverage"]["files_omitted_by_total_byte_limit"] == 2
    assert by_total_bytes["input_coverage"]["bytes_omitted"] == first.stat().st_size + second.stat().st_size
