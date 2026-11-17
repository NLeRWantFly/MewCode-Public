"""Versioned, host-owned health facts for one expert execution segment.

The projection deliberately keeps unknown measurements unknown. In particular,
the existing role checkpoint has tool timestamps but no complete model/review
spans, so it cannot claim an exclusive wall-clock breakdown yet.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import statistics
from collections import Counter
from typing import Any

from mewcode.experts.recovery_state import NON_PROGRESS_TOOLS, OBSERVATIONS, blockers, progress_marker

SCHEMA_VERSION = 1
POLICY_VERSION = "segment-health-v2"
PATH_ERROR_CODES = frozenset({"file_not_found", "path_not_found", "path_outside_workspace",
                              "path_case_mismatch", "path_mapping_error"})
TYPE_ERROR_CODES = frozenset({"tool_input_type_mismatch", "unsupported_mime_type",
                              "argument_schema_error", "invalid_tool_parameters"})
READ_TOOLS = OBSERVATIONS - {"RequestReplan"}
QUEUE_READ_ACTIONS = frozenset({"next", "read", "summary"})
TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed", "interrupted_read_only"})


def is_read_receipt(tool: dict[str, Any]) -> bool:
    if tool.get("name") in READ_TOOLS:
        return True
    args = tool.get("args") or {}
    return (tool.get("name") == "ReviewWorkQueue" and isinstance(args, dict)
            and args.get("action") in QUEUE_READ_ACTIONS)


def extract_tool_error_code(output: str, content_blocks: list[dict[str, Any]] | None = None) -> str | None:
    """Use explicit machine codes only; prose is not evidence for auto-retry."""
    candidates: list[Any] = list(content_blocks or [])
    if isinstance(output, str) and len(output) <= 4096 and output.lstrip().startswith(("{", "[")):
        try:
            candidates.append(json.loads(output))
        except ValueError:
            pass
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("error_code") or candidate.get("code")
        if isinstance(value, str) and value in PATH_ERROR_CODES | TYPE_ERROR_CODES:
            return value
        nested = candidate.get("error")
        if isinstance(nested, dict):
            value = nested.get("code") or nested.get("error_code")
            if isinstance(value, str) and value in PATH_ERROR_CODES | TYPE_ERROR_CODES:
                return value
    return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 and number < float("inf") else None


def _tool_rows(stage: dict[str, Any], started: float | None, ended: float | None) -> tuple[list[dict[str, Any]], bool]:
    raw = stage.get("tools") or {}
    values = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    rows: list[dict[str, Any]] = []
    partial = False
    for tool in values:
        if not isinstance(tool, dict):
            partial = True
            continue
        timestamp = next((value for value in (_number(tool.get("declared_at")),
                          _number(tool.get("started_at")), _number(tool.get("completed_at")))
                          if value is not None), None)
        if started is not None and timestamp is not None and timestamp < started:
            continue
        if ended is not None and timestamp is not None and timestamp > ended:
            continue
        if started is not None and timestamp is None:
            partial = True  # Old receipts cannot be assigned to a segment with certainty.
            continue
        rows.append(tool)
    return rows, partial


def read_result_fingerprint(output: Any, content_blocks: Any = None) -> str | None:
    """Hash returned read bytes/text without saving them in health telemetry."""
    if not isinstance(output, str):
        output = "" if output is None else str(output)
    blocks = content_blocks if isinstance(content_blocks, list) else []
    if not output and not blocks:
        return None
    digest = hashlib.sha256()
    def feed(tag: str, value: str) -> None:
        encoded = value.encode("utf-8", errors="backslashreplace")
        digest.update(tag.encode("ascii"))
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    feed("output", output)
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for key in ("type", "mimeType", "mime_type", "data", "text"):
            value = block.get(key)
            if isinstance(value, str):
                feed(key, value)
    return digest.hexdigest()


def _resource_key(tool: dict[str, Any], input_revision: str, node_id: str) -> str | None:
    args = tool.get("args")
    if not isinstance(args, dict):
        return None
    result_digest = tool.get("result_sha256")
    if (not isinstance(result_digest, str) or len(result_digest) != 64
            or any(char not in "0123456789abcdef" for char in result_digest)):
        return None
    resource = next((args.get(key) for key in ("path", "file_path", "artifact_id", "queue_id",
                                               "item_id", "node_id", "url", "pattern", "tool_id",
                                               "archive_id", "decision_id", "section")
                     if isinstance(args.get(key), str) and args.get(key)), None)
    if resource is None and tool.get("name") in {"ReviewWorkQueue", "ReadReviewQueue"}:
        resource = "queue:" + str(args.get("reviewer_node_id") or node_id)
    if resource is None and tool.get('name') in {'ReadRunArchive', 'ReadAuthorizations', 'ReadWorkflowContext'}:
        resource = 'index:' + node_id
    if resource is None:
        return None
    normalized = posixpath.normpath(resource.replace("\\", "/")).casefold()
    read_range = {key: args.get(key) for key in (
        "start", "end", "offset", "limit", "page", "page_idx", "crop", "pattern",
        "state_filter", "include_resolved", "source_selector", "source_items_path",
        "items_path", "render_page", "image_member", "criterion_id", "archive_id", "tool_id")
                  if key in args}
    canonical = json.dumps([tool.get("name"), args.get("action"), normalized, read_range,
                            input_revision, result_digest],
                           ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cursor_delta(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(before, dict):
        return {"known": False, "review_complete": None, "review_recorded": None,
                "write_receipts": None, "submitted_items": None, "submitted_revisions": None,
                "total_units": None}
    complete = max(0, int(after.get("review_complete") or 0) - int(before.get("review_complete") or 0))
    recorded = max(0, int(after.get("review_recorded") or 0) - int(before.get("review_recorded") or 0))
    writes = len(set(after.get("write_receipts") or []) - set(before.get("write_receipts") or []))
    items = len(set(after.get("submitted_items") or []) - set(before.get("submitted_items") or []))
    revisions = len(set(after.get("submitted_revisions") or []) - set(before.get("submitted_revisions") or []))
    sources = len(set(after.get("observed_sources") or []) - set(before.get("observed_sources") or []))
    outputs = len(set(after.get("inline_outputs") or []) - set(before.get("inline_outputs") or []))
    # ETA uses finished work items; write receipts remain visible but are not
    # interchangeable with finished pages or accepted review records.
    return {"known": True, "review_complete": complete, "review_recorded": recorded,
            "write_receipts": writes, "submitted_items": items, "submitted_revisions": revisions,
            "observed_sources": sources, "inline_outputs": outputs,
            "total_units": max(complete, items, revisions)}


def _exclusive_timing(stage: dict[str, Any], started: float | None, ended: float | None,
                      rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = _number(stage.get("duration_monotonic_ms"))
    if total is None:
        total = _number(stage.get("duration_ms"))
    total = total / 1000 if total is not None else (ended - started if started is not None and ended is not None and ended >= started else None)
    intervals: list[tuple[float, float, str]] = []
    incomplete = False
    def add(values, category, *, start_key="started_at", end_key="completed_at"):
        nonlocal incomplete
        for row in values:
            if not isinstance(row, dict):
                incomplete = True
                continue
            begin, finish = _number(row.get(start_key)), _number(row.get(end_key))
            duration = _number(row.get("duration_monotonic_ms"))
            if begin is not None and (finish is None or finish < begin) and duration is not None:
                finish = begin + duration / 1000
            if begin is None or finish is None or finish < begin:
                incomplete = True
                continue
            if started is not None:
                begin = max(begin, started)
            if ended is not None:
                finish = min(finish, ended)
            if finish > begin:
                intervals.append((begin, finish, category))

    model_spans = stage.get("model_spans")
    review_spans = stage.get("safety_review_spans")
    add(model_spans if isinstance(model_spans, list) else [], "model")
    add(review_spans if isinstance(review_spans, list) else [], "safety_review")
    add(rows, "tool")
    waits = stage.get("wait_intervals")
    if isinstance(waits, list):
        add(waits, "wait", start_key="start", end_key="end")
    elif _number(stage.get("wait_seconds")):
        incomplete = True
    points = sorted({point for begin, finish, _ in intervals for point in (begin, finish)})
    sums = {key: 0. for key in ("model", "safety_review", "tool", "wait")}
    # One wall-clock slice belongs to one category even if nested spans overlap.
    priority = ("wait", "safety_review", "model", "tool")
    for begin, finish in zip(points, points[1:]):
        active = {category for lo, hi, category in intervals if lo < finish and hi > begin}
        category = next((key for key in priority if key in active), None)
        if category is not None:
            sums[category] += finish - begin
    instrumented = isinstance(model_spans, list) and bool(model_spans)
    known = sum(sums.values())
    complete = (instrumented and not incomplete and not stage.get("request_error")
                and total is not None and known <= total + .02)
    rest = max(0., total - known) if total is not None and known <= total + .02 else None
    return {"wall_seconds": round(total, 3) if total is not None else None,
            "model_seconds": round(sums["model"], 3) if instrumented else None,
            "business_review_model_seconds": round(sums["model"], 3)
                if instrumented and stage.get("role") == "auditor" else None,
            "safety_review_seconds": round(sums["safety_review"], 3) if instrumented else None,
            "tool_seconds": round(sums["tool"], 3) if intervals else None,
            "wait_seconds": round(sums["wait"], 3) if isinstance(waits, list) else _number(stage.get("wait_seconds")),
            "host_seconds": round(rest, 3) if complete and rest is not None else None,
            "unattributed_seconds": 0. if complete else round(rest, 3) if rest is not None else None,
            "coverage": "complete" if complete else "partial"}


def _eta(stage: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    progress = stage.get("review_progress") or {}
    total, complete = progress.get("total"), progress.get("complete")
    if not isinstance(total, int) or not isinstance(complete, int) or total < complete or complete < 0:
        return {"status": "unknown", "reason": "finite_work_total_missing"}
    previous = [row for row in (stage.get("segment_health_history") or [])
                if isinstance(row, dict) and row.get("segment_id") != current.get("segment_id")]
    rates = []
    for row in [*previous, current]:
        delta = row.get("work_delta") or {}
        units = delta.get("total_units")
        seconds = (row.get("timing") or {}).get("wall_seconds")
        if isinstance(units, int) and units > 0 and isinstance(seconds, (int, float)) and seconds > 0:
            rates.append(seconds / units)
    if len(rates) < 3:
        return {"status": "unknown", "reason": "insufficient_productive_segments", "sample_count": len(rates)}
    rates.sort()
    remaining = total - complete
    return {"status": "estimated", "remaining_items": remaining, "sample_count": len(rates),
            "seconds": round(remaining * statistics.median(rates)),
            "range_seconds": [round(remaining * rates[max(0, len(rates) // 4 - 1)]),
                              round(remaining * rates[min(len(rates) - 1, 3 * len(rates) // 4)])],
            "algorithm": "median_productive_seconds_per_item_v1"}


def measure_segment(snapshot: dict[str, Any], node_id: str, *, stage: dict[str, Any] | None = None,
                    terminal_status: str = "", error_kind: str = "") -> dict[str, Any]:
    """Build a deterministic health receipt from one saved role segment."""
    stage = stage or (snapshot.get("node_states") or {}).get(node_id) or {}
    started = _number(stage.get("started_at"))
    ended = _number(stage.get("completed_at"))
    rows, partial_rows = _tool_rows(stage, started, ended)
    run_id = str(snapshot.get("run_id") or "")
    attempt = int(stage.get("node_attempt") or 0)
    identity = json.dumps([run_id, node_id, stage.get("member_id"), attempt, started,
                           stage.get("input_revision")], ensure_ascii=False)
    segment_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    after = progress_marker(snapshot, node_id)
    before = stage.get("slice_progress_baseline")
    work_delta = _cursor_delta(before, after)
    terminal = [row for row in rows if row.get("status") in TERMINAL_TOOL_STATUSES]
    effective, observed_keys = [], set()
    for row in terminal:
        if row.get("status") != "completed" or row.get("is_error"):
            continue
        key = _resource_key(row, str(stage.get("input_revision") or ""), node_id)
        if row.get("name") in {"ReadFile", "ReadAuditArtifact"}:
            if key and key not in observed_keys:
                observed_keys.add(key)
                effective.append(row)
        elif row.get("name") == "SubmitHandoff":
            if (row.get("args") or {}).get("inline_outputs") or (stage.get("handoff") or {}).get("inline_outputs"):
                effective.append(row)
        elif not is_read_receipt(row) and row.get("name") not in OBSERVATIONS | NON_PROGRESS_TOOLS:
            effective.append(row)
    starts = [_number(row.get("declared_at")) or _number(row.get("started_at")) for row in effective]
    first = min((value for value in starts if value is not None), default=None)
    censored = first is None
    first_seconds = (round(max(0., first - started), 3)
                     if first is not None and started is not None and not partial_rows else None)
    path_calls = [row for row in terminal if isinstance(row.get("args"), dict)
                  and any(key in row["args"] for key in ("path", "file_path", "directory"))]
    path_errors = [row for row in path_calls if row.get("error_code") in PATH_ERROR_CODES]
    typed = [row for row in terminal if row.get("error_code") or row.get("input_mime")]
    type_errors = [row for row in typed if row.get("error_code") in TYPE_ERROR_CODES]
    unclassified_errors = sum(row.get("status") == "failed" and not row.get("error_code") for row in terminal)
    read_keys = [_resource_key(row, str(stage.get("input_revision") or ""), node_id) for row in terminal
                 if is_read_receipt(row) and row.get("status") == "completed" and not row.get("is_error")]
    known_keys = [key for key in read_keys if key is not None]
    duplicates = sum(count - 1 for count in Counter(known_keys).values())
    from mewcode.experts.workflow_context import latest_submission
    submission = latest_submission(stage)
    health = {"schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION,
              "run_id": run_id, "node_id": node_id, "member_id": str(stage.get("member_id") or ""),
              "node_attempt": attempt,
              "input_revision": str(stage.get("input_revision") or ""),
              "segment_id": segment_id, "started_at": started, "ended_at": ended,
              "terminal_status": terminal_status or str(stage.get("status") or ""),
              "error_kind": error_kind or str(stage.get("error_kind") or ""),
              "checkpoint_present": bool(stage.get("messages")),
              "blocker_kinds": sorted({str(row.get("kind")) for row in blockers(stage, error_kind=error_kind)}),
              "progress_before": before if isinstance(before, dict) else None,
              "progress_after": after, "work_delta": work_delta,
              "first_effective_call_seconds": first_seconds,
              "first_effective_call_censored": censored or partial_rows,
              "effective_calls": len(effective), "terminal_calls": len(terminal),
              "effective_call_ratio": round(len(effective) / len(terminal), 4) if terminal else None,
              "path_error_count": len(path_errors), "path_call_count": len(path_calls),
              "path_error_rate": round(len(path_errors) / len(path_calls), 4) if path_calls else None,
              "tool_misuse_count": len(type_errors), "typed_call_count": len(typed),
              "tool_misuse_rate": round(len(type_errors) / len(typed), 4) if typed else None,
              "unclassified_tool_errors": unclassified_errors,
              "read_calls": len(read_keys), "keyed_reads": len(known_keys),
              "repeated_reads": duplicates, "unkeyed_reads": len(read_keys) - len(known_keys),
              "evidence_reads": len(set(known_keys)),
              "submission_feedback": submission,
              "timing": _exclusive_timing(stage, started, ended, rows),
              "evidence_refs": [str(row.get("tool_id") or "") for row in terminal if row.get("tool_id")],
              "sampling": "partial" if partial_rows or started is None or ended is None or ended < started else "tool_complete"}
    health["eta"] = _eta(stage, health)
    return health
