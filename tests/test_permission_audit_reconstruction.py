from __future__ import annotations

import hashlib
import json
import sqlite3
import datetime as dt
from pathlib import Path

import pytest

from scripts.reconstruct_permission_audit_evidence import (
    assert_reconstruction,
    build_reconstruction_report,
)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_sources(
    root: Path,
    audit_rows: list[dict],
    calls: list[tuple[str, str, str, dict]],
) -> tuple[Path, Path, Path]:
    session_id = "session-alpha"
    audit = root / "decisions.jsonl"
    audit.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in audit_rows),
        encoding="utf-8",
    )

    session_log = root / f"{session_id}.jsonl"
    session_rows: list[dict] = []
    activity_rows: list[tuple[str, str]] = []
    for call_id, tool, start, arguments in calls:
        session_rows.append({
            "type": "assistant",
            "timestamp": start,
            "content": [{
                "type": "tool_use", "id": call_id,
                "name": tool, "input": arguments,
            }],
        })
        end = (
            dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
            + dt.timedelta(seconds=2)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        session_rows.append({
            "type": "tool_result", "timestamp": end,
            "tool_use_id": call_id, "content": "synthetic result",
        })
        activity_rows.append((call_id, json.dumps({
            "source_ref": call_id,
            "name": tool,
            "arguments": arguments,
            "at": start,
        }, ensure_ascii=False)))
    session_log.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in session_rows),
        encoding="utf-8",
    )

    activity_db = root / "node-activity.sqlite3"
    with sqlite3.connect(activity_db) as connection:
        connection.execute(
            "CREATE TABLE activity (conversation_id TEXT NOT NULL, data TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO activity(conversation_id, data) VALUES (?, ?)",
            [(session_id, data) for _, data in activity_rows],
        )
    return audit, session_log, activity_db


def test_reconstruction_requires_full_digest_and_keeps_statuses_separate(
    tmp_path: Path,
) -> None:
    complete_args = {"command": "python inspect.py --token private-placeholder"}
    long_args = {"file_path": "E:/synthetic/" + "x" * 300}
    truncated_preview = json.dumps(
        long_args, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )[:200]
    session_id = "session-alpha"
    audit_rows = [
        {
            "session_id": session_id,
            "operation_id": "call-one",
            "timestamp": "2026-09-20T00:00:01.000Z",
            "tool_name": "Bash",
            "params_summary": json.dumps(complete_args),
            "args_sha256": _canonical_digest(complete_args),
            "args_schema": "json-sort-keys-compact-utf8-v1",
            "params_summary_truncated": False,
        },
        {
            "session_id": session_id,
            "operation_id": "call-two",
            "timestamp": "2026-09-20T00:01:01.000Z",
            "tool_name": "ReadFile",
            "params_summary": truncated_preview,
            "args_sha256": _canonical_digest(long_args),
            "args_schema": "json-sort-keys-compact-utf8-v1",
            "params_summary_truncated": True,
        },
        {
            "session_id": session_id,
            "call_id": "call-one",
            "timestamp": "2026-09-20T00:00:01.500Z",
            "tool_name": "Bash",
            "params_summary": json.dumps(complete_args),
        },
    ]
    calls = [
        ("call-one", "Bash", "2026-09-20T00:00:00.000Z", complete_args),
        ("call-two", "ReadFile", "2026-09-20T00:01:00.000Z", long_args),
    ]
    audit, session_log, activity_db = _prepare_sources(tmp_path, audit_rows, calls)
    source_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (audit, session_log, activity_db)
    }

    report = build_reconstruction_report(
        audit, session_log, activity_db, pinned_source_lines=2
    )

    assert report["strict_call_reconstruction"]["exact_call_binding_count"] == 2
    assert report["strict_call_reconstruction"]["unknown_call_binding_count"] == 1
    assert report["strict_call_reconstruction"]["missing_call_id_count"] == 0
    assert report["strict_call_reconstruction"]["argument_digest_missing_count"] == 1
    assert report["strict_call_reconstruction"]["argument_digest_mismatch_count"] == 0
    parameters = report["parameter_summary_coverage"]
    assert parameters["nontruncated_summary_count"] == 2
    assert parameters["parsed_complete_summary_count"] == 2
    assert parameters["truncated_summary_count"] == 1
    assert report["rows"][0]["full_arguments"] == "verified"
    assert report["rows"][1]["parameter_summary"] == "truncated"
    assert report["rows"][1]["full_arguments"] == "verified"
    assert report["rows"][1]["call_binding"] == "exact"
    assert report["rows"][2]["call_binding"] == "unknown"
    assert report["rows"][2]["full_arguments"] == "unknown"
    assert report["rows"][2]["argument_digest"] == "missing"
    assert "missing_full_argument_digest" in report["rows"][2]["reasons"]
    activity_evidence = report["session_activity_crosscheck"]
    assert activity_evidence["tool_use_ids_with_one_activity_source_ref"] == 2
    assert activity_evidence["source_ref_name_and_full_arguments_verified"] == 2
    assert activity_evidence["this_crosscheck_does_not_bind_audit_rows"] is True

    baseline = report["baseline_count_evidence"]
    assert baseline["delta"] == 1
    assert baseline["current_prefix"]["line_range"] == [1, 2]
    assert baseline["current_excess_tail"]["line_range"] == [3, 3]
    raw_lines = audit.read_bytes().splitlines(keepends=True)
    assert baseline["current_prefix"]["sha256"] == hashlib.sha256(b"".join(raw_lines[:2])).hexdigest()
    assert baseline["current_excess_tail"]["sha256"] == hashlib.sha256(raw_lines[2]).hexdigest()

    # The evidence report proves source integrity without persisting input values.
    serialized_report = json.dumps(report, ensure_ascii=False)
    assert "private-placeholder" not in serialized_report
    assert "E:/synthetic/" not in serialized_report
    assert report["raw_parameters_persisted"] is False
    for path, digest in source_hashes.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest

    with pytest.raises(AssertionError, match="permission evidence reconstruction failed"):
        assert_reconstruction(report)


def test_reconstruction_fails_closed_on_time_or_digest_mismatch(tmp_path: Path) -> None:
    args = {"file_path": "E:/synthetic/ordinary.txt"}
    preview = "x" * 200
    audit_rows = [
        {
            "session_id": "session-alpha",
            "operation_id": "call-one",
            "timestamp": "2026-09-20T00:00:10.000Z",
            "tool_name": "ReadFile",
            "params_summary": preview,
            "args_sha256": _canonical_digest(args),
            "args_schema": "json-sort-keys-compact-utf8-v1",
            "params_summary_truncated": True,
        },
        {
            "session_id": "session-alpha",
            "operation_id": "call-one",
            "timestamp": "2026-09-20T00:00:01.000Z",
            "tool_name": "ReadFile",
            "params_summary": preview,
            "args_sha256": "0" * 64,
            "args_schema": "json-sort-keys-compact-utf8-v1",
            "params_summary_truncated": True,
        },
    ]
    audit, session_log, activity_db = _prepare_sources(
        tmp_path,
        audit_rows,
        [("call-one", "ReadFile", "2026-09-20T00:00:00.000Z", args)],
    )

    report = build_reconstruction_report(
        audit, session_log, activity_db, pinned_source_lines=2
    )

    outside_lifecycle, mismatched_digest = report["rows"]
    assert outside_lifecycle["call_binding"] == "unknown"
    assert outside_lifecycle["timestamp_relation"] == "unknown"
    assert outside_lifecycle["full_arguments"] == "verified"
    assert "timestamp_outside_call_lifecycle" in outside_lifecycle["reasons"]
    assert mismatched_digest["call_binding"] == "unknown"
    assert mismatched_digest["timestamp_relation"] == "verified_call_lifecycle"
    assert mismatched_digest["full_arguments"] == "unknown"
    assert mismatched_digest["argument_digest"] == "mismatch"
    assert "full_argument_digest_mismatch" in mismatched_digest["reasons"]
