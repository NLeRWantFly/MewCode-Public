"""Read-only, redacted scheduler-recovery replay data and pure analysis.

The activity database can contain conversations, arguments, outputs and paths.
Only an exact run is queried; none of those raw fields leave the exporter.
Legacy activity lacks complete span/progress receipts and stays explicitly
partial instead of being promoted to a measured health segment.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mewcode.experts.recovery_actions import plan_recovery_actions
from mewcode.experts.recovery_diagnosis import (
    BUSINESS_ERRORS, CAPACITY_ERRORS, MODEL_ERRORS, PROTOCOL_ERRORS,
    REVIEW_ERRORS, CONTEXT_ERRORS, REASONING_ERRORS, diagnose_segment,
)

EXPORT_VERSION = "recovery-replay-export-v5"
METRIC_SCHEMA_VERSION = 2
EVENT_KINDS = frozenset({"expert_node_started", "expert_node_completed",
                         "expert_tool", "expert_tool_start", "expert_tool_result",
                         "expert_segment_health", "expert_role_wait", "ask_user",
                         "human_oncall", "expert_stage", "expert_flow_state", "expert_usage"})
KNOWN_NON_METRIC_ACTIVITY_KINDS = frozenset({
    "expert_process_file", "expert_step", "expert_tool_state", "expert_tool_wait",
    "permission", "note",
})
KNOWN_ACTIVITY_KINDS = EVENT_KINDS | KNOWN_NON_METRIC_ACTIVITY_KINDS
STATUS_CODES = frozenset({"running", "continuing", "completed", "failed", "paused", "blocked",
                          "budget_exhausted", "stalled", "needs_attention", "queued",
                          "review_pending", "incomplete_audit", "skipped"})
SAFE_ROLES = frozenset({"manager", "planner", "executor", "auditor", "reviewer",
                        "worker", "researcher", "writer", "coder", "verifier", "critic"})
SAFE_PHASES = frozenset({"setup", "preparation", "executing", "review", "debate",
                         "recovery", "rework", "ruling", "verification", "finalizing",
                         "loop", "paused", "blocked", "completed", "hitl_blocked"})
MANUAL_EVENT_TYPES = frozenset({"expert_role_wait", "ask_user", "human_oncall"})
SHUTDOWN_EVENT_TYPES = frozenset({"expert_node_started", "expert_node_completed",
                                  "expert_stage", "expert_flow_state"})
STOP_STATUSES = frozenset({"paused", "blocked", "budget_exhausted", "stalled",
                           "needs_attention", "failed"})
TOKEN_FIELDS = ("input_tokens", "output_tokens", "billable_tokens")
METRIC_SOURCE_KINDS = frozenset((*MANUAL_EVENT_TYPES, *SHUTDOWN_EVENT_TYPES, "expert_usage"))
ERROR_CODES = (BUSINESS_ERRORS | CAPACITY_ERRORS | MODEL_ERRORS | PROTOCOL_ERRORS
               | REVIEW_ERRORS | CONTEXT_ERRORS | REASONING_ERRORS | {
    "state_divergence", "environment_fault", "no_progress", "node_failure",
    "tool_input_type_mismatch", "file_not_found", "path_not_found",
    "output_window_limit", "token_window_no_progress", "token_accounting_overrun",
})
BLOCKER_KINDS = frozenset({"permission", "unknown_effect", "human_question",
                           "safety_review", "legacy_safety_review", "effect_unknown",
                           "permission_denied", "provider_error"})
RESOURCE_FIELDS = ("path", "file_path", "artifact_id", "queue_id", "item_id", "url")
RANGE_FIELDS = ("start", "end", "offset", "limit", "page", "page_idx", "crop")
QUEUE_ACTIONS = frozenset({"evidence", "record", "next", "summary", "read", "initialize"})
QUEUE_READ_ACTIONS = frozenset({"next", "summary", "read"})
SAFE_TOOL_NAMES = frozenset({"ReviewWorkQueue", "ReadReviewQueue", "ReadAuditArtifact",
    "ReadFile", "WriteFile", "EditFile", "Bash", "Glob", "Grep", "report_step",
    "RepairWorkBatch", "SubmitHandoff", "ReadStageResult", "SubmitLoopDecision"})
INT_METRICS = ("effective_calls", "terminal_calls", "path_error_count", "path_call_count",
               "tool_misuse_count", "typed_call_count", "unclassified_tool_errors",
               "read_calls", "keyed_reads", "repeated_reads", "unkeyed_reads")
FLOAT_METRICS = ("first_effective_call_seconds", "effective_call_ratio", "path_error_rate",
                 "tool_misuse_rate")
DELTA_FIELDS = ("review_complete", "review_recorded", "write_receipts", "submitted_items",
                "submitted_revisions", "total_units")
TIMING_FIELDS = ("wall_seconds", "model_seconds", "safety_review_seconds", "tool_seconds",
                 "wait_seconds", "host_seconds", "unattributed_seconds",
                 "business_review_model_seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _key(run_id: str, value: str) -> str:
    return hmac.new(run_id.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if 0 <= value < float("inf") else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def sanitize_health(health: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """Allowlist only aggregate numbers and opaque, run-scoped identifiers."""
    delta = health.get("work_delta") if isinstance(health.get("work_delta"), dict) else {}
    timing = health.get("timing") if isinstance(health.get("timing"), dict) else {}
    eta = health.get("eta") if isinstance(health.get("eta"), dict) else {}
    status = health.get("terminal_status")
    error = health.get("error_kind")
    sampling = health.get("sampling")
    coverage = timing.get("coverage")
    eta_status = eta.get("status")
    clean: dict[str, Any] = {
        "schema_version": 1,
        "segment_key": _key(run_id, str(health.get("segment_id") or "")),
        "node_key": _key(run_id, str(health.get("node_id") or "")),
        "attempt": int(_number(health.get("node_attempt")) or 0),
        "started_at": _number(health.get("started_at")),
        "ended_at": _number(health.get("ended_at")),
        "terminal_status": status if isinstance(status, str) and status in STATUS_CODES else "unknown",
        "error_kind": error if isinstance(error, str) and error in ERROR_CODES else "",
        "checkpoint_present": health.get("checkpoint_present") is True,
        "blocker_kinds": sorted({kind for kind in health.get("blocker_kinds") or []
                                 if isinstance(kind, str) and kind in BLOCKER_KINDS}),
        "sampling": sampling if isinstance(sampling, str) and sampling in {"partial", "tool_complete", "complete"} else "partial",
        "first_effective_call_censored": bool(health.get("first_effective_call_censored")),
        "work_delta": {"known": delta.get("known") is True,
                       **{name: _number(delta.get(name)) for name in DELTA_FIELDS}},
        "timing": {**{name: _number(timing.get(name)) for name in TIMING_FIELDS},
                   "coverage": coverage if isinstance(coverage, str) and coverage in {"partial", "complete"} else "partial"},
        "eta": {"status": eta_status if isinstance(eta_status, str) and eta_status in {"estimated", "unknown"} else "unknown",
                "seconds": _number(eta.get("seconds")), "sample_count": _number(eta.get("sample_count"))},
    }
    clean.update({name: _number(health.get(name)) for name in (*INT_METRICS, *FLOAT_METRICS)})
    return clean


def sanitize_activity(data: dict[str, Any], *, run_id: str, source_seq: int) -> dict[str, Any] | None:
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        return None
    if kind == "expert_segment_health" and isinstance(data.get("health"), dict):
        return {"record_kind": "health", "source_seq": source_seq,
                "health": sanitize_health(data["health"], run_id=run_id)}
    name = str(data.get("name") or "")
    args = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
    action = args.get("action") if name == "ReviewWorkQueue" else None
    action = action if isinstance(action, str) and action in QUEUE_ACTIONS else None
    tool_kind = ("read" if name == "ReviewWorkQueue" and action in QUEUE_READ_ACTIONS else
                 "write" if name == "ReviewWorkQueue" and action in QUEUE_ACTIONS else
                 "read" if name.startswith(("Read", "List", "Search", "Get", "Glob", "Grep")) else
                 "write" if name.startswith(("Write", "Edit", "Submit")) else
                 "execute" if name in {"Bash", "VerifyCandidate"} else "other")
    resource = next((args.get(field) for field in RESOURCE_FIELDS
                     if isinstance(args.get(field), str) and args.get(field)), None)
    if resource is None and name in {"ReviewWorkQueue", "ReadReviewQueue"}:
        resource = "queue:" + str(args.get("reviewer_node_id") or data.get("node_id") or "")
    resource_key = None
    if resource is not None:
        norm = resource.replace("\\", "/").casefold()
        ranges = {field: args[field] for field in (*RANGE_FIELDS, "state_filter", "include_resolved") if field in args}
        resource_key = _key(run_id, _json([name, action, norm, ranges, data.get("graph_version")]))
    status = data.get("status")
    output = str(data.get("text") or "")[:4096].casefold() if kind == "expert_tool_result" else ""
    legacy_path_error = bool(status == "failed" and re.search(
        r"not found|no such file|找不到|不存在|path.*error|路径.*错误", output))
    path_value = str(args.get("file_path") or args.get("path") or "").casefold()
    png_text_request = bool(kind == "expert_tool" and name == "ReadFile" and ".png" in path_value)
    role = data.get("role")
    clean_role = role if isinstance(role, str) and role in SAFE_ROLES else "other"
    safe_name = name if name in SAFE_TOOL_NAMES | {"AskUserQuestion", "human_oncall"} else None
    clean = {"record_kind": "event", "source_seq": source_seq, "event_type": kind,
            "node_key": _key(run_id, str(data.get("node_id") or "")),
            "node_identity_known": isinstance(data.get("node_id"), str) and bool(data.get("node_id")),
            "attempt": int(_number(data.get("attempt")) or 0),
            "at": _timestamp(data.get("at")),
            "status": status if isinstance(status, str) and status in STATUS_CODES else "unknown",
            "tool_kind": tool_kind if kind.startswith("expert_tool") else None,
            "tool_name": safe_name if kind.startswith("expert_tool") else None,
            "tool_action": action,
            "resource_key": resource_key,
            "legacy_path_error": legacy_path_error,
            "png_text_request": png_text_request}
    if kind in MANUAL_EVENT_TYPES:
        interaction_kind = (data.get("interaction_kind") if isinstance(data.get("interaction_kind"), str)
                            else "role_wait" if kind == "expert_role_wait" else kind)
        clean.update(role=clean_role,
                     interaction_kind=interaction_kind if interaction_kind in {
                         "role_wait", "ask_user", "human_oncall"} else "unknown",
                     interaction_key=_key(run_id, str(data.get("wait_id") or data.get("request_id")
                                                       or f"event:{source_seq}")),
                     active=data.get("active") if isinstance(data.get("active"), bool) else None,
                     elapsed_seconds=_number(data.get("elapsed") or data.get("elapsed_seconds")))
    if kind in SHUTDOWN_EVENT_TYPES:
        phase = data.get("phase")
        phase = phase if isinstance(phase, str) and phase in SAFE_PHASES else None
        transition = status if isinstance(status, str) and status in STATUS_CODES else None
        if transition is None:
            possible = data.get("state") or phase
            transition = possible if isinstance(possible, str) and possible in STATUS_CODES else None
        clean.update(role=clean_role, phase=phase, transition_status=transition)
    if kind == "expert_usage":
        raw_member = data.get("member_id")
        member_identity_known = isinstance(raw_member, str) and bool(raw_member)
        attempt_value = _count(data.get("attempt"))
        attempt_identity_known = data.get("attempt_identity_known") is True
        if "attempt_identity_known" not in data:
            # Pure sanitizer callers provide their source attempt directly;
            # stored records carry the explicit marker above because ActivityStore
            # historically fell back to the broader run attempt.
            attempt_identity_known = attempt_value is not None and attempt_value > 0
        node_identity_known = isinstance(data.get("node_id"), str) and bool(data.get("node_id"))
        role_identity_known = clean_role != "other"
        stream_identity_known = (node_identity_known and attempt_identity_known
                                 and attempt_value is not None and attempt_value > 0 and role_identity_known
                                 and member_identity_known)
        clean.update(role=clean_role,
                     member_key=_key(run_id, raw_member) if member_identity_known else None,
                     stream_identity_known=stream_identity_known,
                     usage={field: _count(data.get(field)) for field in TOKEN_FIELDS})
    return clean


def read_activity(db_path: Path, *, run_id: str, conversation_id: str | None = None
                  ) -> tuple[list[dict[str, Any]], str, int, dict[str, Any]]:
    """Query one exact run read-only and return redacted row-coverage diagnostics."""
    if not run_id or not db_path.is_file():
        raise ValueError("an existing activity database and exact run_id are required")
    # ``Path.as_uri()`` yields ``file:///E:/...`` on Windows, which some
    # SQLite builds interpret as a UNC-style path.  Use SQLite's drive-path
    # form while still escaping URI metacharacters and opening strictly RO.
    from urllib.parse import quote
    resolved = db_path.resolve()
    uri_path = quote(resolved.as_posix(), safe="/:\\")
    uri = "file:" + uri_path + "?mode=ro"
    stat_before = (resolved.stat().st_size, resolved.stat().st_mtime_ns)
    sidecars = [Path(str(resolved) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    if any(sidecar.exists() for sidecar in sidecars):
        raise ValueError("activity database has WAL/SHM/journal sidecars; refusing an incomplete or changing snapshot")
    # Some Windows SQLite/VFS combinations cannot read an external-volume
    # file in mode=ro unless immutable is set. Immutable mode is strictly
    # read-only, and is used only for a sidecar-free file whose stat and sidecar
    # state are checked again after the scan. We never checkpoint/modify input.
    db = sqlite3.connect(uri + "&immutable=1", uri=True)
    source_hash = hashlib.sha256()
    result: list[dict[str, Any]] = []
    source_count = 0
    parsed_rows = malformed_json_rows = non_object_rows = invalid_kind_rows = unrecognized_kind_rows = 0
    invalid_source_sequence_rows = invalid_record_id_rows = 0
    duplicate_record_id_rows = duplicate_source_seq_rows = conflicting_duplicate_rows = 0
    metric_event_counts: Counter[str] = Counter()
    seen_record_ids: dict[str, tuple[int, str]] = {}
    seen_source_seqs: dict[int, tuple[str, str]] = {}
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        where = "run_id=?" + (" AND conversation_id=?" if conversation_id is not None else "")
        params = (run_id, conversation_id) if conversation_id is not None else (run_id,)
        for record_id, source_seq, raw in db.execute(
            f"SELECT id,source_seq,data FROM activity WHERE {where} ORDER BY revision,source_seq,id", params
        ):
            source_count += 1
            source_hash.update(_json([record_id, source_seq, raw]).encode("utf-8"))
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                malformed_json_rows += 1
                continue
            parsed_rows += 1
            if not isinstance(data, dict):
                non_object_rows += 1
                continue
            kind = data.get("kind")
            if not isinstance(kind, str) or not kind:
                invalid_kind_rows += 1
            elif kind not in KNOWN_ACTIVITY_KINDS:
                unrecognized_kind_rows += 1

            try:
                seq = int(source_seq) if source_seq is not None else 0
            except (TypeError, ValueError, OverflowError):
                seq = 0
            if seq <= 0:
                invalid_source_sequence_rows += 1
            id_key = str(record_id or "")
            if not id_key:
                invalid_record_id_rows += 1
            prior_id = seen_record_ids.get(id_key) if id_key else None
            prior_seq = seen_source_seqs.get(seq) if seq > 0 else None
            if prior_id is not None or prior_seq is not None:
                duplicate_record_id_rows += prior_id is not None
                duplicate_source_seq_rows += prior_seq is not None
                id_conflict = prior_id is not None and (prior_id[0] != seq or prior_id[1] != raw)
                seq_conflict = prior_seq is not None and (prior_seq[0] != id_key or prior_seq[1] != raw)
                if id_conflict or seq_conflict:
                    conflicting_duplicate_rows += 1
                continue
            if id_key:
                seen_record_ids[id_key] = (seq, raw)
            if seq > 0:
                seen_source_seqs[seq] = (id_key, raw)

            if isinstance(kind, str) and kind:
                if kind in METRIC_SOURCE_KINDS:
                    metric_event_counts[kind] += 1
                clean = sanitize_activity(data, run_id=run_id, source_seq=seq)
                if clean is not None:
                    result.append(clean)
        db.rollback()
    finally:
        db.close()
    stat_after = (resolved.stat().st_size, resolved.stat().st_mtime_ns)
    if any(sidecar.exists() for sidecar in sidecars) or stat_after != stat_before:
        raise RuntimeError("activity database or journal sidecars changed during immutable read-only replay")
    if source_count == 0:
        raise LookupError("the exact run_id has no activity rows")
    complete = not any((malformed_json_rows, non_object_rows, invalid_kind_rows,
                        unrecognized_kind_rows, invalid_source_sequence_rows,
                        invalid_record_id_rows, conflicting_duplicate_rows))
    reasons = []
    if malformed_json_rows:
        reasons.append("malformed_json_rows")
    if non_object_rows:
        reasons.append("non_object_rows")
    if invalid_kind_rows:
        reasons.append("missing_or_invalid_kind_rows")
    if unrecognized_kind_rows:
        reasons.append("unrecognized_activity_kind_rows")
    if invalid_source_sequence_rows:
        reasons.append("missing_or_invalid_source_sequence_rows")
    if invalid_record_id_rows:
        reasons.append("missing_activity_record_id_rows")
    if conflicting_duplicate_rows:
        reasons.append("conflicting_duplicate_identity_rows")
    summary = {
        "schema_version": 1,
        "source_rows": source_count,
        "parsed_rows": parsed_rows,
        "malformed_json_rows": malformed_json_rows,
        "non_object_rows": non_object_rows,
        "missing_or_invalid_kind_rows": invalid_kind_rows,
        "unrecognized_activity_kind_rows": unrecognized_kind_rows,
        "missing_or_invalid_source_sequence_rows": invalid_source_sequence_rows,
        "missing_activity_record_id_rows": invalid_record_id_rows,
        "duplicate_record_id_rows": duplicate_record_id_rows,
        "duplicate_source_seq_rows": duplicate_source_seq_rows,
        "conflicting_duplicate_rows": conflicting_duplicate_rows,
        "retained_rows": len(result),
        "omitted_rows": source_count - len(result),
        "metric_event_counts": {kind: metric_event_counts.get(kind, 0)
                                 for kind in sorted(METRIC_SOURCE_KINDS)},
        "integrity": "complete" if complete else "incomplete",
        "integrity_reasons": reasons,
    }
    return result, source_hash.hexdigest(), source_count, summary


def export_activity(db_path: Path, output_dir: Path, *, run_id: str,
                    conversation_id: str | None = None) -> dict[str, Any]:
    records, source_digest, source_count, source_summary = read_activity(
        db_path, run_id=run_id, conversation_id=conversation_id)
    payload = "".join(_json(record) + "\n" for record in records)
    measurement = analyze_replay(records, mode="measure", source_rows=source_count,
                                 source_summary=source_summary)
    legacy = measurement["historical"]["legacy"]
    manifest = {"schema_version": 2, "exporter_version": EXPORT_VERSION,
                "metric_schema_version": METRIC_SCHEMA_VERSION,
                "run_key": _key(run_id, "run"), "source_rows": source_count,
                "source_coverage": source_summary,
                "source_rows_sha256": source_digest, "records": len(records),
                "health_records": sum(row["record_kind"] == "health" for row in records),
                "legacy_events": sum(row["record_kind"] == "event" for row in records),
                "records_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                "records_file": "events.jsonl",
                "recovery_metrics": measurement["recovery_metrics"],
                "historical_anchor_observations": {
                    "queue_total_declarations": legacy["queue_total_declarations"],
                    "queue_read_or_next_or_summary_declarations": legacy["queue_read_or_next_or_summary_declarations"],
                    "path_error_text_candidates": legacy["path_error_text_candidates"],
                    "png_readfile_declarations": legacy["png_readfile_declarations"],
                    "measured_health_segments": sum(row["record_kind"] == "health" for row in records),
                    "definitions": "total queue calls are not repeated reads; text errors are not machine codes"}}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "events.jsonl").write_text(payload, encoding="utf-8", newline="\n")
    (output_dir / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8", newline="\n")
    return manifest


def load_replay(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    filename = manifest.get("records_file")
    if filename != "events.jsonl":
        raise ValueError("unsupported replay records file")
    raw = (manifest_path.parent / filename).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest.get("records_sha256"):
        raise ValueError("replay records checksum mismatch")
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
    return manifest, records


def _dedupe_source_events(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int, int]:
    """Deduplicate by source sequence; conflicting rows at one sequence are excluded."""
    by_seq: dict[int, list[dict[str, Any]]] = {}
    without_seq: list[dict[str, Any]] = []
    for row in events:
        seq = _count(row.get("source_seq"))
        if seq is None or seq <= 0:
            without_seq.append(row)
        else:
            by_seq.setdefault(seq, []).append(row)
    unique: list[dict[str, Any]] = []
    duplicate_rows = conflict_rows = missing_sequence_rows = 0
    for seq in sorted(by_seq):
        rows = by_seq[seq]
        duplicate_rows += max(0, len(rows) - 1)
        variants = {_json(row) for row in rows}
        if len(variants) != 1:
            conflict_rows += len(rows)
            continue
        unique.append(rows[0])
    for row in without_seq:
        missing_sequence_rows += 1
        unique.append(row)
    return unique, duplicate_rows, conflict_rows, missing_sequence_rows


def _recovery_metrics(records: list[dict[str, Any]], *, pages: int | None = None,
                      source_rows: int | None = None,
                      source_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    source_events = [row for row in records if row.get("record_kind") == "event"]
    events, duplicate_rows, conflicting_rows, missing_sequence_rows = _dedupe_source_events(source_events)
    source_denominator = _count(source_rows)
    summary = source_summary if isinstance(source_summary, dict) else None
    source_integrity = summary is None or summary.get("integrity") == "complete"
    metric_source_counts = summary.get("metric_event_counts") if summary else None
    retained_counts = Counter(row.get("event_type") for row in events)
    count_mismatches: list[str] = []
    if isinstance(metric_source_counts, dict):
        for kind in sorted(METRIC_SOURCE_KINDS):
            expected = metric_source_counts.get(kind)
            expected = expected if isinstance(expected, int) and not isinstance(expected, bool) else None
            if expected is None or expected != retained_counts.get(kind, 0):
                count_mismatches.append(kind)
    elif summary is not None:
        count_mismatches.append("source_event_counts_missing")
    record_identity_complete = not conflicting_rows and not missing_sequence_rows
    source_coverage_complete = (source_denominator is not None and source_denominator > 0
        and source_integrity and record_identity_complete and not count_mismatches
        and (summary is None or summary.get("source_rows") == source_denominator))
    common_coverage_reasons = []
    if summary is not None and not source_integrity:
        common_coverage_reasons.extend(summary.get("integrity_reasons") or ["source_integrity_incomplete"])
    if duplicate_rows:
        common_coverage_reasons.append("duplicate_sanitized_source_sequence_rows_deduplicated")
    if conflicting_rows:
        common_coverage_reasons.append("conflicting_sanitized_source_sequence_rows")
    if missing_sequence_rows:
        common_coverage_reasons.append("sanitized_events_missing_source_sequence")
    if count_mismatches:
        common_coverage_reasons.append("source_metric_event_counts_do_not_match_exported_records")
    if source_denominator is None or source_denominator <= 0:
        common_coverage_reasons.append("exact activity source row denominator is missing")

    manual = [row for row in events if row.get("event_type") in MANUAL_EVENT_TYPES]
    manual_counts = {kind: retained_counts.get(kind, 0) for kind in sorted(MANUAL_EVENT_TYPES)}
    manual_coverage = source_coverage_complete and source_denominator is not None and source_denominator > 0
    manual_metric = {
        "value": len(manual) if manual_coverage else None,
        "numerator": len(manual) if manual_coverage else None,
        "observed_count": len(manual),
        "denominator": source_denominator,
        "coverage": "complete" if manual_coverage else "partial_or_unknown" if source_denominator else "unknown",
        "counts_by_event_type": manual_counts,
        "unknown_reason": None if manual_coverage else "; ".join(dict.fromkeys(common_coverage_reasons)),
        "assumption": "count unique source rows of expert_role_wait, ask_user, and human_oncall once each; question content is discarded",
        "deduplicated_source_rows": duplicate_rows,
    }

    transitions = sorted((row for row in events
                          if row.get("event_type") in SHUTDOWN_EVENT_TYPES),
                         key=lambda row: int(row.get("source_seq") or 0))
    missing_status = sum(not isinstance(row.get("transition_status"), str)
                         or row.get("transition_status") not in STATUS_CODES
                         for row in transitions)
    missing_node_identity = sum(row.get("node_identity_known") is False for row in transitions)
    stop_count = 0
    suppressed_stop_signals = 0
    stop_counts_by_status: Counter[str] = Counter()
    state_by_scope: dict[tuple[str, int], str] = {}
    for row in transitions:
        status = row.get("transition_status")
        if not isinstance(status, str) or status not in STATUS_CODES:
            continue
        scope = (str(row.get("node_key") or ""), int(row.get("attempt") or 0))
        prior = state_by_scope.get(scope)
        current = "stopped" if status in STOP_STATUSES else "active"
        if current == "stopped" and prior != "stopped":
            stop_count += 1
            stop_counts_by_status[status] += 1
        elif current == "stopped":
            suppressed_stop_signals += 1
        state_by_scope[scope] = current
    shutdown_coverage = (source_coverage_complete and bool(transitions)
                         and missing_status == 0 and missing_node_identity == 0)
    shutdown_reasons = list(common_coverage_reasons)
    if missing_status:
        shutdown_reasons.append(f"{missing_status} node/stage/flow rows have no allowlisted transition status")
    if missing_node_identity:
        shutdown_reasons.append(f"{missing_node_identity} node/stage/flow rows lack a node identity")
    if not transitions:
        shutdown_reasons.append("no node/stage/flow transition events are available")
    shutdown_metric = {
        "value": stop_count if shutdown_coverage else None,
        "numerator": stop_count if shutdown_coverage else None,
        "denominator": len(transitions) if transitions else None,
        "coverage": "complete" if shutdown_coverage else "partial_or_unknown" if transitions else "unknown",
        "transition_events": len(transitions), "missing_status_events": missing_status,
        "missing_node_identity_events": missing_node_identity,
        "observed_stop_transitions": stop_count,
        "observed_stop_transitions_by_status": dict(sorted(stop_counts_by_status.items())),
        "same_stopped_state_signals_suppressed": suppressed_stop_signals,
        "deduplicated_source_rows": duplicate_rows,
        "unknown_reason": None if shutdown_coverage else "; ".join(dict.fromkeys(shutdown_reasons)),
        "assumption": "count source-ordered transitions into paused, blocked, budget_exhausted, stalled, needs_attention, or failed; same stopped state per node attempt counts once",
    }

    usage_events = [row for row in events if row.get("event_type") == "expert_usage"]
    ordered_usage = sorted(enumerate(usage_events), key=lambda item: (
        _count(item[1].get("source_seq")) if _count(item[1].get("source_seq")) is not None else -1,
        item[0]))
    missing_usage = 0
    unidentifiable_usage = 0
    reset_streams: set[tuple[str, int, str, str]] = set()
    prior_by_stream: dict[tuple[str, int, str, str], tuple[int, int]] = {}
    last_seq_by_stream: dict[tuple[str, int, str, str], int] = {}
    stream_totals: dict[tuple[str, int, str, str], int] = {}
    invalid_streams: set[tuple[str, int, str, str]] = set()
    observed_tokens = 0
    for _, row in ordered_usage:
        value = _count((row.get("usage") or {}).get("billable_tokens"))
        if value is None:
            missing_usage += 1
            continue
        attempt = _count(row.get("attempt"))
        source_seq = _count(row.get("source_seq"))
        member_key = row.get("member_key")
        role = row.get("role")
        node_key = row.get("node_key")
        identity_known = (
            row.get("stream_identity_known") is True
            and isinstance(node_key, str) and bool(node_key)
            and attempt is not None and attempt > 0
            and isinstance(role, str) and role in SAFE_ROLES
            and isinstance(member_key, str)
            and source_seq is not None and source_seq > 0
        )
        if not identity_known:
            unidentifiable_usage += 1
            continue
        stream = (node_key, attempt, role, member_key)
        previous = prior_by_stream.get(stream)
        if stream in invalid_streams:
            continue
        if source_seq <= last_seq_by_stream.get(stream, 0):
            unidentifiable_usage += 1
            invalid_streams.add(stream)
            stream_totals.pop(stream, None)
            continue
        last_seq_by_stream[stream] = source_seq
        delta = value if previous is None else value - previous[0]
        if delta < 0:
            reset_streams.add(stream)
            invalid_streams.add(stream)
            stream_totals.pop(stream, None)
            continue
        prior_by_stream[stream] = (value, source_seq)
        stream_totals[stream] = stream_totals.get(stream, 0) + delta
        observed_tokens += delta
    total_tokens = sum(stream_totals.values())
    invalid_usage = missing_usage + unidentifiable_usage > 0 or bool(reset_streams)
    token_coverage = bool(usage_events) and not invalid_usage and source_coverage_complete
    page_count = _count(pages)
    token_known = token_coverage and page_count is not None and page_count > 0
    token_metric = {
        "value": round(total_tokens / page_count, 4) if token_known else None,
        "numerator": total_tokens if token_coverage else None,
        "denominator": page_count if page_count and page_count > 0 else None,
        "coverage": "complete" if token_known else "partial_or_unknown" if usage_events else "unknown",
        "total_tokens": total_tokens if token_coverage else None,
        "usage_events": len(usage_events), "missing_token_events": missing_usage,
        "unidentifiable_events": unidentifiable_usage,
        "reset_streams": len(reset_streams),
        "observed_partial_tokens": observed_tokens if invalid_usage else None,
        "unknown_reason": None if token_known else "; ".join(dict.fromkeys(common_coverage_reasons)) if common_coverage_reasons else (
            "one or more cumulative usage counters decreased within a node/attempt/role/member stream" if reset_streams
            else "one or more expert_usage events lack a billable counter" if usage_events and missing_usage
            else "one or more expert_usage events lack a stable node/attempt/role/member/order identity"
            if usage_events and unidentifiable_usage
            else "no expert_usage events contain billable_tokens" if not usage_events
            else "positive page-count denominator is required"),
        "assumption": "billable_tokens are cumulative per exact node/attempt/role/member stream; count the first observed counter and then only monotonic deltas, and divide by caller-declared page count; input/output tokens are not substituted",
    }
    return {"source_coverage": {
                "integrity": summary.get("integrity") if summary else "not_supplied",
                "source_rows": source_denominator,
                "retained_metric_event_rows": {kind: retained_counts.get(kind, 0)
                                                for kind in sorted(METRIC_SOURCE_KINDS)},
                "deduplicated_sanitized_rows": duplicate_rows,
                "conflicting_sanitized_rows": conflicting_rows,
                "missing_source_sequence_rows": missing_sequence_rows,
                "count_mismatches": count_mismatches,
                "complete_for_comparison": source_coverage_complete},
            "human_interventions": manual_metric,
            "shutdown_count": shutdown_metric,
            "billable_tokens_per_page": token_metric}


RECOVERY_METRIC_NAMES = ("human_interventions", "shutdown_count", "billable_tokens_per_page")


def compare_recovery_metrics(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Fail closed unless all three fully covered metrics strictly decrease."""
    comparisons: dict[str, Any] = {}
    errors: list[str] = []
    for name in RECOVERY_METRIC_NAMES:
        before = baseline.get(name) if isinstance(baseline, dict) else None
        after = candidate.get(name) if isinstance(candidate, dict) else None
        before = before if isinstance(before, dict) else {}
        after = after if isinstance(after, dict) else {}
        issues = []
        for label, metric in (("baseline", before), ("candidate", after)):
            denominator = _number(metric.get("denominator"))
            value = _number(metric.get("value"))
            if metric.get("coverage") != "complete":
                issues.append(f"{label} coverage is {metric.get('coverage') or 'missing'}")
            if denominator is None or denominator <= 0:
                issues.append(f"{label} denominator is missing or zero")
            if value is None:
                issues.append(f"{label} value is unknown")
        before_value, after_value = _number(before.get("value")), _number(after.get("value"))
        improved = (after_value < before_value) if not issues else None
        if improved is not True:
            errors.extend(f"{name}: {issue}" for issue in issues)
            if not issues:
                errors.append(f"{name}: candidate metric did not strictly improve")
        comparisons[name] = {"baseline": before, "candidate": after,
                              "improved": improved, "errors": issues}
    passed = all(comparisons[name]["improved"] is True for name in RECOVERY_METRIC_NAMES)
    return {"status": "PASS" if passed else "FAIL",
            "policy": "all_three_metrics_must_strictly_decrease_with_complete_coverage",
            "comparisons": comparisons, "errors": errors}


def analyze_replay(records: list[dict[str, Any]], *, mode: str = "all",
                   pages: int | None = None, source_rows: int | None = None,
                   source_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Historical facts and counterfactual policy suggestions remain separate."""
    if mode not in {"all", "measure", "classify", "simulate"}:
        raise ValueError("unsupported replay mode")
    health = [row["health"] for row in records if row.get("record_kind") == "health"]
    events = [row for row in records if row.get("record_kind") == "event"]
    declarations = [row for row in events if row.get("event_type") == "expert_tool"]
    results = [row for row in events if row.get("event_type") == "expert_tool_result"]
    queue_actions = Counter(row.get("tool_action") or "unknown" for row in declarations
                            if row.get("tool_name") == "ReviewWorkQueue")
    tool_names = Counter(row.get("tool_name") or "unclassified" for row in declarations)
    declared_effectful = sum(row.get("tool_kind") in {"write", "execute"} for row in declarations)
    legacy_path_candidates = sum(row.get("legacy_path_error") is True for row in results)
    png_text_requests = sum(row.get("png_text_request") is True for row in declarations)
    queue_reads = sum(queue_actions[action] for action in QUEUE_READ_ACTIONS)
    started = [row for row in events if row.get("event_type") == "expert_node_started"]
    completed = [row for row in events if row.get("event_type") == "expert_node_completed"]
    legacy = {
        "coverage": "partial_event_metadata_only",
        "node_starts": len(started), "node_completions": len(completed),
        "tool_declarations": len(declarations), "tool_results": len(results),
        "tool_declarations_by_name": dict(sorted(tool_names.items())),
        "declared_effectful_calls": declared_effectful,
        "declared_effectful_ratio": round(declared_effectful / len(declarations), 4) if declarations else None,
        "effectful_ratio_is_success_metric": False,
        "queue_actions": dict(sorted(queue_actions.items())),
        "queue_read_or_next_or_summary_declarations": queue_reads,
        "queue_total_declarations": sum(queue_actions.values()),
        "path_error_text_candidates": legacy_path_candidates,
        "path_error_text_candidates_are_machine_codes": False,
        "png_readfile_declarations": png_text_requests,
        "input_versions_available": False,
    }
    complete = [row for row in health if row.get("sampling") in {"tool_complete", "complete"}]
    first_calls = [row["first_effective_call_seconds"] for row in health
                   if isinstance(row.get("first_effective_call_seconds"), (int, float))
                   and row.get("first_effective_call_censored") is False]
    etas = [row["eta"]["seconds"] for row in health
            if isinstance(row.get("eta"), dict) and row["eta"].get("status") == "estimated"
            and isinstance(row["eta"].get("seconds"), (int, float))]
    count_metrics = ("path_error_count", "tool_misuse_count", "repeated_reads")
    observed_counts = {name: sum(row.get(name) or 0 for row in complete) for name in count_metrics}
    diagnoses = [diagnose_segment(row, error_kind=row.get("error_kind") or "",
        blockers=[{"kind": kind, "ref": "redacted"} for kind in row.get("blocker_kinds") or []])
        for row in health]
    report: dict[str, Any] = {"schema_version": 1, "mode": mode,
        "recovery_metrics": _recovery_metrics(records, pages=pages, source_rows=source_rows,
                                                source_summary=source_summary),
        "historical": {"measured_segments": len(health), "legacy_partial_events": len(events),
                       "legacy": legacy,
                       "classification_coverage": "health_only" if health else "unavailable",
                       "effective_call_ratio": None,
                       "metric_coverage": {"complete_segments": len(complete),
                           "partial_segments": len(health) - len(complete)},
                       "first_effective_call_seconds": {"sample_count": len(first_calls),
                           "min": min(first_calls) if first_calls else None,
                           "max": max(first_calls) if first_calls else None,
                           "median": statistics.median(first_calls) if first_calls else None},
                       "counts": {name: {"observed_complete_segments": observed_counts[name],
                           "coverage": "complete" if health and len(complete) == len(health) else "partial_or_unknown"}
                           for name in count_metrics},
                       "eta_seconds": {"sample_count": len(etas),
                           "median": statistics.median(etas) if etas else None}}}
    ratios = [(row["effective_calls"], row["terminal_calls"]) for row in health
              if isinstance(row.get("effective_calls"), int)
              and isinstance(row.get("terminal_calls"), int) and row["terminal_calls"] > 0]
    if ratios:
        report["historical"]["effective_call_ratio"] = round(
            sum(pair[0] for pair in ratios) / sum(pair[1] for pair in ratios), 4)
    if mode in {"all", "measure"}:
        report["anchor_comparison"] = {
            "first_effective_call_sample_seconds": {"expected_range": [95, 147],
                "observed_range": [min(first_calls), max(first_calls)] if first_calls else None,
                "status": "sample_definition_unverified" if first_calls else "missing"},
            "path_errors": {"expected": 36, "observed_complete_segments": observed_counts["path_error_count"],
                "legacy_text_candidates": legacy_path_candidates,
                "status": "machine_codes_only" if health and len(complete) == len(health)
                    and not any(row.get("unclassified_tool_errors") for row in complete)
                    else "incomplete_coverage_or_unclassified_errors"},
            "png_type_misuse": {"expected": 18, "observed_all_type_errors": observed_counts["tool_misuse_count"],
                "legacy_readfile_png_declarations": png_text_requests,
                "status": "different_definition_png_subset_not_identified"},
            "queue_repeated_reads": {"expected": 1240, "observed_all_repeated_reads": observed_counts["repeated_reads"],
                "legacy_queue_total_declarations": legacy["queue_total_declarations"],
                "legacy_queue_read_declarations": queue_reads,
                "status": "1240_is_total_queue_calls_not_repeated_reads"
                    if legacy["queue_total_declarations"] == 1240 else
                    "different_definition_queue_subset_not_identified"},
            "eta_weeks": {"expected_range": [3, 4],
                "observed_median": round(statistics.median(etas) / 604800, 2) if etas else None,
                "status": "algorithm_or_worklist_unverified" if etas else "missing"},
        }
    if mode in {"all", "classify"}:
        report["classification"] = dict(sorted(Counter(d["primary_kind"] for d in diagnoses).items()))
    if mode in {"all", "simulate"}:
        suggestions = Counter()
        unavailable = Counter()
        for row, diagnosis in zip(health, diagnoses):
            plan = plan_recovery_actions(diagnosis=diagnosis,
                allowed=["continue_checkpoint", "diagnose_path"],
                checkpoint_present=row.get("checkpoint_present") is True,
                hard_boundary=bool(diagnosis["hard_boundary"] or row.get("blocker_kinds")),
                review_route_present=False, marker=row.get("work_delta") or {})
            first = next((action for action in plan["actions"] if action["status"] == "available"), None)
            if first:
                suggestions[first["action"]] += 1
            unavailable.update(action["action"] for action in plan["actions"]
                               if action["status"] == "unavailable")
        report["simulation"] = {"status": "counterfactual_only", "suggested_actions": dict(sorted(suggestions.items())),
                                "unavailable_actions": dict(sorted(unavailable.items())),
                                "verified_successes": None}
    return report
