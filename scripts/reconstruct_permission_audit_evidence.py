"""Read-only reconstruction of audit rows against session and activity evidence.

This tool never replays or executes a recorded command. It writes only a
redacted evidence report to the caller-selected output path; source JSONL and
SQLite files are opened read-only. A row is reconstructed only when its audit
record explicitly carries an operation/call id and a versioned digest of the
complete arguments, and the session, lifecycle time, and node-activity
reference all agree. Truncated previews are never treated as complete arguments.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PINNED_SOURCE_LINES = 489
ARGS_SCHEMA = "json-sort-keys-compact-utf8-v1"
_ARGS_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CALL_ID_FIELDS = ("operation_id", "call_id", "tool_call_id", "tool_use_id")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _canonical_args_sha256(value: Any) -> str | None:
    try:
        return _sha256_bytes(_canonical_json(value))
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        return None


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _parse_parameter_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        result = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        try:
            result = ast.literal_eval(value)
        except (SyntaxError, ValueError, TypeError):
            return None
    return result if isinstance(result, dict) else None


def _parameter_summary_status(row: dict[str, Any]) -> str:
    summary = row.get("params_summary")
    if not isinstance(summary, str):
        return "missing"
    if row.get("params_summary_truncated") is True:
        return "truncated"
    if len(summary) >= 200 or summary.endswith(("...", "…")):
        return "truncated"
    if row.get("params_summary_truncated") is False:
        return "complete" if _parse_parameter_summary(summary) is not None else "invalid"
    return "complete" if _parse_parameter_summary(summary) is not None else "invalid"


def _audit_call_id(row: dict[str, Any]) -> tuple[str | None, str | None]:
    values = [row[key] for key in _CALL_ID_FIELDS if key in row]
    if not values:
        return None, None
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None, "invalid_call_id"
    if len(set(values)) != 1:
        return None, "conflicting_call_ids"
    return values[0], None


def _validated_argument_digest(row: dict[str, Any]) -> tuple[str | None, str | None]:
    supplied_digest = row.get("args_sha256")
    if not isinstance(supplied_digest, str):
        return None, "missing_full_argument_digest"
    if not _ARGS_SHA256_PATTERN.fullmatch(supplied_digest):
        return None, "invalid_full_argument_digest"
    if row.get("args_schema") != ARGS_SCHEMA:
        return None, "unsupported_argument_digest_schema"
    return supplied_digest, None


def _line_ranges(lines: list[int]) -> list[dict[str, int]]:
    if not lines:
        return []
    ordered = sorted(set(lines))
    ranges: list[dict[str, int]] = []
    start = end = ordered[0]
    for line in ordered[1:]:
        if line == end + 1:
            end = line
            continue
        ranges.append({"first": start, "last": end, "count": end - start + 1})
        start = end = line
    ranges.append({"first": start, "last": end, "count": end - start + 1})
    return ranges


def _load_jsonl(path: Path) -> tuple[bytes, list[tuple[int, dict[str, Any]]], int]:
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    decoded: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSONL row at {path.name}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"invalid JSONL object at {path.name}:{line_number}")
        decoded.append((line_number, value))
    return raw, decoded, len(lines)


def _load_session_calls(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    _, rows, physical_lines = _load_jsonl(path)
    calls: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    results: dict[str, list[dt.datetime]] = defaultdict(list)
    for line_number, row in rows:
        if row.get("type") == "assistant" and isinstance(row.get("content"), list):
            for item in row["content"]:
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                call_id = item.get("id")
                if not isinstance(call_id, str) or not call_id:
                    continue
                if call_id in calls:
                    duplicate_ids.add(call_id)
                    continue
                calls[call_id] = {
                    "call_id": call_id,
                    "tool_name": item.get("name"),
                    "arguments": item.get("input"),
                    "start": _parse_timestamp(row.get("timestamp")),
                    "end": None,
                    "event_times": set(),
                    "session_line": line_number,
                }
                if calls[call_id]["start"] is not None:
                    calls[call_id]["event_times"].add(calls[call_id]["start"])
        elif row.get("type") == "tool_result":
            call_id = row.get("tool_use_id")
            stamp = _parse_timestamp(row.get("timestamp"))
            if isinstance(call_id, str) and stamp is not None:
                results[call_id].append(stamp)
    for call_id, stamps in results.items():
        if call_id in calls:
            calls[call_id]["end"] = max(stamps)
            calls[call_id]["event_times"].update(stamps)
    for call_id in duplicate_ids:
        calls.pop(call_id, None)
    return list(calls.values()), physical_lines, len(rows)


def _load_activity_index(
    path: Path, session_ids: set[str]
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, int]]:
    wal_path = Path(str(path) + "-wal")
    shm_path = Path(str(path) + "-shm")
    if any(sidecar.exists() and sidecar.stat().st_size for sidecar in (wal_path, shm_path)):
        raise ValueError("activity database has SQLite sidecars; refusing an incomplete immutable snapshot")

    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name=?",
            ("table", "activity"),
        ).fetchone()
        if table is None:
            raise ValueError("activity database has no activity table")
        columns = {
            item[1]
            for item in connection.execute("PRAGMA table_info(activity)")
        }
        if not {"conversation_id", "data"}.issubset(columns):
            raise ValueError("activity table lacks conversation_id or data")
        source_rows = connection.execute(
            "SELECT conversation_id, data FROM activity"
        )
        index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        counters = Counter()
        for conversation_id, raw_data in source_rows:
            counters["activity_rows"] += 1
            if conversation_id not in session_ids:
                continue
            counters["session_activity_rows"] += 1
            try:
                data = json.loads(raw_data)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid JSON in matching activity row") from exc
            source_ref = data.get("source_ref") if isinstance(data, dict) else None
            if isinstance(source_ref, str) and source_ref:
                index[(conversation_id, source_ref)].append(data)
        return index, dict(counters)
    finally:
        if "connection" in locals():
            connection.close()


def _activity_verifies(
    rows: list[dict[str, Any]], *, tool_name: Any, arguments: Any
) -> bool:
    return (
        len(rows) == 1
        and rows[0].get("name") == tool_name
        and rows[0].get("arguments") == arguments
    )


def _reconstruct_row(
    line_number: int,
    row: dict[str, Any],
    *,
    call_index: dict[str, dict[str, Any]],
    activity_index: dict[tuple[str, str], list[dict[str, Any]]],
    session_log_id: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    call_id, call_id_error = _audit_call_id(row)
    session_id = row.get("session_id")
    summary_status = _parameter_summary_status(row)
    call: dict[str, Any] | None = None

    if call_id_error:
        reasons.append(call_id_error)
    elif call_id is None:
        reasons.append("missing_call_id")
    else:
        call = call_index.get(call_id)
        if call is None:
            reasons.append("call_id_not_in_session_log")

    if not isinstance(session_id, str) or not session_id:
        reasons.append("missing_session_id")
    elif session_id != session_log_id:
        reasons.append("session_id_does_not_match_session_log")

    arguments_verified = False
    timestamp_verified = False
    activity_verified = False
    supplied_digest, digest_error = _validated_argument_digest(row)
    if digest_error:
        reasons.append(digest_error)
        argument_digest_status = (
            "missing" if digest_error == "missing_full_argument_digest" else "invalid"
        )
    else:
        argument_digest_status = "present_unverified"
    if call is not None:
        if row.get("tool_name") != call.get("tool_name"):
            reasons.append("tool_name_mismatch")

        audit_time = _parse_timestamp(row.get("timestamp"))
        start = call.get("start")
        end = call.get("end")
        if audit_time is None:
            reasons.append("invalid_or_timezone_free_timestamp")
        elif start is None or end is None or not start <= audit_time <= end:
            reasons.append("timestamp_outside_call_lifecycle")
        else:
            timestamp_verified = True

        call_args = call.get("arguments")
        if supplied_digest is not None:
            if not isinstance(call_args, dict):
                digest_error = "source_arguments_not_object"
            else:
                expected_digest = _canonical_args_sha256(call_args)
                if expected_digest is None:
                    digest_error = "source_arguments_not_canonical_json"
                elif supplied_digest != expected_digest:
                    digest_error = "full_argument_digest_mismatch"
                else:
                    arguments_verified = True
                    argument_digest_status = "verified"
            if digest_error:
                reasons.append(digest_error)
                argument_digest_status = (
                    "mismatch" if digest_error == "full_argument_digest_mismatch" else "invalid"
                )

        matching_activity = activity_index.get((str(session_id), str(call_id)), [])
        if _activity_verifies(
            matching_activity,
            tool_name=call.get("tool_name"),
            arguments=call_args,
        ):
            activity_verified = True
        else:
            reasons.append("activity_source_ref_or_arguments_not_verified")

    exact = (
        call is not None
        and session_id == session_log_id
        and row.get("tool_name") == call.get("tool_name")
        and timestamp_verified
        and arguments_verified
        and activity_verified
        and not call_id_error
    )
    result: dict[str, Any] = {
        "audit_line": line_number,
        "parameter_summary": summary_status,
        "call_binding": "exact" if exact else "unknown",
        "full_arguments": "verified" if arguments_verified else "unknown",
        "argument_digest": argument_digest_status,
        "timestamp_relation": "verified_call_lifecycle" if timestamp_verified else "unknown",
        "activity_relation": "verified" if activity_verified else "unknown",
        "reasons": sorted(set(reasons)),
    }
    if exact and call is not None:
        result["call_id"] = call["call_id"]
        result["session_log_line"] = call["session_line"]
    return result


def build_reconstruction_report(
    audit_path: Path,
    session_log_path: Path,
    activity_db_path: Path,
    *,
    pinned_source_lines: int = PINNED_SOURCE_LINES,
) -> dict[str, Any]:
    audit_raw, audit_rows, physical_line_count = _load_jsonl(audit_path)
    session_calls, session_physical_lines, session_record_count = _load_session_calls(
        session_log_path
    )
    audit_session_ids = {
        str(row["session_id"])
        for _, row in audit_rows
        if isinstance(row.get("session_id"), str) and row["session_id"]
    }
    activity_index, activity_counts = _load_activity_index(
        activity_db_path, audit_session_ids
    )
    call_index: dict[str, dict[str, Any]] = {}
    duplicate_call_ids: set[str] = set()
    for call in session_calls:
        call_id = call["call_id"]
        if call_id in call_index:
            duplicate_call_ids.add(call_id)
        call_index[call_id] = call
    for call_id in duplicate_call_ids:
        call_index.pop(call_id, None)

    session_log_id = session_log_path.stem
    source_ref_matches = 0
    source_ref_exact_arguments = 0
    source_ref_mismatches = 0
    source_ref_missing_or_ambiguous = 0
    for call in session_calls:
        related = activity_index.get((session_log_id, call["call_id"]), [])
        if len(related) != 1:
            source_ref_missing_or_ambiguous += 1
            continue
        source_ref_matches += 1
        if (
            related[0].get("name") == call.get("tool_name")
            and related[0].get("arguments") == call.get("arguments")
        ):
            source_ref_exact_arguments += 1
        else:
            source_ref_mismatches += 1

    reconstructed = [
        _reconstruct_row(
            line_number,
            row,
            call_index=call_index,
            activity_index=activity_index,
            session_log_id=session_log_id,
        )
        for line_number, row in audit_rows
    ]
    exact_use_times = {
        (call.get("tool_name"), call.get("start"))
        for call in session_calls
        if call.get("start") is not None
    }
    exact_session_event_times = {
        (call.get("tool_name"), timestamp)
        for call in session_calls
        for timestamp in call.get("event_times", set())
    }
    exact_activity_at_times: set[tuple[str, Any, dt.datetime | None]] = set()
    exact_activity_updated_times: set[tuple[str, Any, dt.datetime | None]] = set()
    for (session_id, _source_ref), related in activity_index.items():
        for activity in related:
            tool_name = activity.get("name")
            if tool_name:
                exact_activity_at_times.add((
                    session_id, tool_name, _parse_timestamp(activity.get("at"))
                ))
                exact_activity_updated_times.add((
                    session_id, tool_name, _parse_timestamp(activity.get("updated_at"))
                ))
    exact_use_lines: list[int] = []
    exact_session_event_lines: list[int] = []
    exact_activity_at_lines: list[int] = []
    exact_activity_updated_lines: list[int] = []
    for line_number, row in audit_rows:
        session_id = row.get("session_id")
        tool_name = row.get("tool_name")
        timestamp = _parse_timestamp(row.get("timestamp"))
        if timestamp is None:
            continue
        if (tool_name, timestamp) in exact_use_times:
            exact_use_lines.append(line_number)
        if (tool_name, timestamp) in exact_session_event_times:
            exact_session_event_lines.append(line_number)
        if (session_id, tool_name, timestamp) in exact_activity_at_times:
            exact_activity_at_lines.append(line_number)
        if (session_id, tool_name, timestamp) in exact_activity_updated_times:
            exact_activity_updated_lines.append(line_number)
    parameter_counts = Counter(item["parameter_summary"] for item in reconstructed)
    binding_counts = Counter(item["call_binding"] for item in reconstructed)
    full_argument_counts = Counter(item["full_arguments"] for item in reconstructed)
    argument_digest_counts = Counter(item["argument_digest"] for item in reconstructed)
    unknown_lines = [
        item["audit_line"] for item in reconstructed
        if item["call_binding"] != "exact"
    ]
    complete_lines = [
        item["audit_line"] for item in reconstructed
        if item["parameter_summary"] == "complete"
    ]
    truncated_lines = [
        item["audit_line"] for item in reconstructed
        if item["parameter_summary"] == "truncated"
    ]
    missing_session_lines = [
        line_number for line_number, row in audit_rows if not row.get("session_id")
    ]
    missing_call_id_lines = [
        item["audit_line"] for item in reconstructed
        if "missing_call_id" in item["reasons"]
    ]

    prefix_count = min(max(pinned_source_lines, 0), len(audit_rows))
    tail_first = pinned_source_lines + 1 if len(audit_rows) > pinned_source_lines else None
    tail_bytes = b"".join(audit_raw.splitlines(keepends=True)[pinned_source_lines:])
    prefix_bytes = b"".join(audit_raw.splitlines(keepends=True)[:prefix_count])
    excess_count = max(len(audit_rows) - pinned_source_lines, 0)
    audit_line_end = audit_rows[-1][0] if audit_rows else 0

    assertions = {
        "observed_source_record_count_matches_pinned_count": len(audit_rows) == pinned_source_lines,
        "every_source_row_has_exact_call_binding": not unknown_lines,
        "every_source_row_has_verified_full_arguments": (
            full_argument_counts.get("verified", 0) == len(audit_rows)
        ),
        "no_truncated_parameter_summaries": not truncated_lines,
    }
    reason_counts = Counter(
        reason for item in reconstructed for reason in item["reasons"]
    )
    return {
        "schema_version": 2,
        "purpose": "read-only permission audit evidence reconstruction; not a policy or authorization source",
        "raw_parameters_persisted": False,
        "sources": {
            "audit": {
                "name": audit_path.name,
                "sha256": _sha256_bytes(audit_raw),
                "physical_line_count": physical_line_count,
                "nonblank_record_count": len(audit_rows),
                "line_range": [audit_rows[0][0], audit_line_end] if audit_rows else None,
            },
            "session_log": {
                "name": session_log_path.name,
                "sha256": _sha256_file(session_log_path),
                "session_id_from_filename": session_log_path.stem,
                "physical_line_count": session_physical_lines,
                "parsed_record_count": session_record_count,
                "tool_use_count": len(session_calls),
            },
            "activity_database": {
                "name": activity_db_path.name,
                "sha256": _sha256_file(activity_db_path),
                "sqlite_mode": "mode=ro&immutable=1",
                **activity_counts,
            },
        },
        "baseline_count_evidence": {
            "pinned_source_line_count": pinned_source_lines,
            "observed_source_record_count": len(audit_rows),
            "delta": len(audit_rows) - pinned_source_lines,
            "current_prefix": {
                "line_range": [1, prefix_count] if prefix_count else None,
                "record_count": prefix_count,
                "sha256": _sha256_bytes(prefix_bytes),
                "historical_prefix_match_proven": False,
            },
            "current_excess_tail": {
                "line_range": [tail_first, audit_line_end] if excess_count else None,
                "record_count": excess_count,
                "sha256": _sha256_bytes(tail_bytes),
                "historical_addition_proven": False,
            },
            "historical_snapshot_available": False,
        },
        "parameter_summary_coverage": {
            "nontruncated_summary_count": len(audit_rows) - len(truncated_lines),
            "nontruncated_summary_line_ranges": _line_ranges(
                [line for line, row in audit_rows
                 if _parameter_summary_status(row) != "truncated"]
            ),
            "parsed_complete_summary_count": parameter_counts.get("complete", 0),
            "parsed_complete_summary_line_ranges": _line_ranges(complete_lines),
            "truncated_summary_count": parameter_counts.get("truncated", 0),
            "truncated_summary_line_ranges": _line_ranges(truncated_lines),
            "invalid_or_missing_summary_count": parameter_counts.get("invalid", 0)
            + parameter_counts.get("missing", 0),
            "existing_sanitizer_complete_count": parameter_counts.get("complete", 0),
            "existing_sanitizer_unknown_count": (
                parameter_counts.get("truncated", 0)
                + parameter_counts.get("invalid", 0)
                + parameter_counts.get("missing", 0)
            ),
            "completeness_is_independent_of_call_binding": True,
        },
        "strict_call_reconstruction": {
            "argument_digest_schema": ARGS_SCHEMA,
            "exact_call_binding_count": binding_counts.get("exact", 0),
            "unknown_call_binding_count": binding_counts.get("unknown", 0),
            "unknown_line_ranges": _line_ranges(unknown_lines),
            "full_arguments_verified_count": full_argument_counts.get("verified", 0),
            "full_arguments_unknown_count": full_argument_counts.get("unknown", 0),
            "argument_digest_missing_count": argument_digest_counts.get("missing", 0),
            "argument_digest_mismatch_count": argument_digest_counts.get("mismatch", 0),
            "argument_digest_invalid_count": argument_digest_counts.get("invalid", 0),
            "argument_digest_present_unverified_count": argument_digest_counts.get(
                "present_unverified", 0
            ),
            "missing_call_id_count": len(missing_call_id_lines),
            "missing_call_id_line_ranges": _line_ranges(missing_call_id_lines),
            "missing_session_id_count": len(missing_session_lines),
            "missing_session_id_line_ranges": _line_ranges(missing_session_lines),
            "reason_counts": dict(sorted(reason_counts.items())),
            "unbound_exact_timestamp_diagnostics": {
                "session_tool_use_event_match_count": len(exact_use_lines),
                "session_tool_use_event_line_ranges": _line_ranges(exact_use_lines),
                "session_any_call_event_match_count": len(exact_session_event_lines),
                "session_any_call_event_line_ranges": _line_ranges(exact_session_event_lines),
                "activity_at_match_count": len(exact_activity_at_lines),
                "activity_at_line_ranges": _line_ranges(exact_activity_at_lines),
                "activity_updated_at_match_count": len(exact_activity_updated_lines),
                "activity_updated_at_line_ranges": _line_ranges(exact_activity_updated_lines),
                "counted_as_call_binding": False,
            },
            "matching_policy": [
                "explicit audit operation_id or consistent legacy call_id/tool_call_id/tool_use_id",
                "session id equals the supplied session log id",
                "audit timestamp is timezone-aware and within that call's recorded lifecycle",
                "args_sha256 uses args_schema json-sort-keys-compact-utf8-v1 and matches the complete source arguments",
                "one activity source_ref row for the same session matches the call id, tool, and full arguments",
                "params_summary and params_summary_truncated describe preview completeness only and never replace the full-argument digest",
            ],
        },
        "session_activity_crosscheck": {
            "transcript_tool_use_ids": len(session_calls),
            "unique_activity_source_ref_count": len({
                ref for (session_id, ref) in activity_index
                if session_id == session_log_id
            }),
            "tool_use_ids_with_one_activity_source_ref": source_ref_matches,
            "source_ref_name_and_full_arguments_verified": source_ref_exact_arguments,
            "source_ref_name_or_arguments_mismatch": source_ref_mismatches,
            "source_ref_missing_or_ambiguous": source_ref_missing_or_ambiguous,
            "this_crosscheck_does_not_bind_audit_rows": True,
        },
        "rows": reconstructed,
        "assertions": assertions,
    }


def assert_reconstruction(report: dict[str, Any]) -> None:
    failed = [name for name, passed in report.get("assertions", {}).items() if not passed]
    if failed:
        raise AssertionError("permission evidence reconstruction failed: " + ", ".join(failed))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--session-log", type=Path, required=True)
    parser.add_argument("--activity-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pinned-source-lines", type=int, default=PINNED_SOURCE_LINES)
    parser.add_argument("--assert", dest="assert_complete", action="store_true",
                        help="fail closed unless source count and every full call are proven")
    args = parser.parse_args(argv)
    try:
        source_paths = {
            args.audit.resolve(), args.session_log.resolve(), args.activity_db.resolve()
        }
        if args.output.resolve() in source_paths:
            raise ValueError("report output must not overwrite a source file")
        result = build_reconstruction_report(
            args.audit, args.session_log, args.activity_db,
            pinned_source_lines=args.pinned_source_lines,
        )
        output_bytes = _canonical_json(result) + b"\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(output_bytes)
        if args.assert_complete:
            assert_reconstruction(result)
        print(json.dumps({
            "report": str(args.output),
            "audit_records": result["sources"]["audit"]["nonblank_record_count"],
            "exact_call_binding_count": result["strict_call_reconstruction"]["exact_call_binding_count"],
            "unknown_call_binding_count": result["strict_call_reconstruction"]["unknown_call_binding_count"],
            "argument_digest_missing_count": result["strict_call_reconstruction"]["argument_digest_missing_count"],
            "argument_digest_mismatch_count": result["strict_call_reconstruction"]["argument_digest_mismatch_count"],
            "nontruncated_summaries": result["parameter_summary_coverage"]["nontruncated_summary_count"],
            "parsed_complete_summaries": result["parameter_summary_coverage"]["parsed_complete_summary_count"],
            "truncated_summaries": result["parameter_summary_coverage"]["truncated_summary_count"],
            "source_count_delta": result["baseline_count_evidence"]["delta"],
        }, sort_keys=True))
        return 0
    except (AssertionError, OSError, sqlite3.Error, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
