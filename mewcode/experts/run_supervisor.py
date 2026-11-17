"""Deterministic run-level health supervision; never changes acceptance.

The supervisor observes completed, instrumented segments only. It may request
an existing method-replan capability; the scheduler still owns permission,
budget and idempotency checks. Scope concentration is a user decision signal.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

POLICY_VERSION = "run-supervisor-v3"
WINDOW = 3
MIN_TERMINAL_CALLS = 9
LOW_EFFECTIVE_RATIO = 0.2
HIGH_REPEATED_READ_RATIO = 0.5
MIN_KEYED_READ_CALLS = 6
MAX_HISTORY = 64
MIN_BENEFIT_RATIO_DELTA = 0.05
MIN_BENEFIT_THROUGHPUT_FACTOR = 1.1


def count_mapped_issue_areas(snapshot: dict[str, Any],
                             issue_area_map: dict[str, str] | None) -> dict[str, int] | None:
    """Count defects only when every issue has an explicit frozen area label.

    Guessing a category from prose or an issue ID prefix could manufacture a
    90% concentration. Missing mappings therefore disable this signal.
    """
    if not isinstance(issue_area_map, dict) or not issue_area_map:
        return None
    counts: dict[str, int] = {}
    seen_issue = False
    for queue in (snapshot.get("review_queues") or {}).values():
        if not isinstance(queue, dict):
            continue
        records = queue.get("records") or {}
        if not isinstance(records, dict):
            return None
        for record in records.values():
            if not isinstance(record, dict) or record.get("state") != "needs_repair":
                continue
            ids = record.get("issue_ids") or []
            if not isinstance(ids, list) or any(not isinstance(issue_id, str) for issue_id in ids):
                return None
            for issue_id in set(ids):
                area = issue_area_map.get(issue_id)
                if not isinstance(area, str) or not area.strip():
                    return None
                seen_issue = True
                counts[area] = counts.get(area, 0) + 1
    return counts if seen_issue else None


def arm_supervised_adjustment(receipt: dict[str, Any], *, decision_id: str,
                              input_revision: str) -> dict[str, Any]:
    """Bind one execution-only method trial to a measured baseline.

    The scheduler calls this only after it has selected a permitted method
    replan. The trial is a single next segment, not a persistent business
    contract change. A restart or duplicate dispatch cannot arm it twice.
    """
    value = dict(receipt)
    active = value.get("active_adjustment") or {}
    if isinstance(active, dict) and active.get("decision_id") == decision_id and decision_id:
        return value
    if (value.get("requested_action") != "replan_execution_method"
            or value.get("sampling") != "complete" or not decision_id
            or not input_revision):
        raise ValueError("supervisor has no complete actionable baseline")
    if active:
        raise ValueError("a supervised method trial already owns this input")
    baseline_ids = value.get("window_segment_ids") or []
    if len(baseline_ids) != WINDOW or value.get("last_segment_id") != baseline_ids[-1]:
        raise ValueError("supervisor baseline window is incomplete")
    value["active_adjustment"] = {
        "schema_version": 1, "decision_id": decision_id,
        "input_revision": input_revision,
        "baseline_segment_ids": list(baseline_ids),
        "baseline_effective_call_ratio": value.get("effective_call_ratio"),
        "baseline_throughput_items_per_second": value.get("throughput_items_per_second"),
        "status": "monitoring", "post_segment_ids": [],
        "changes_acceptance": False,
    }
    value["requested_action"] = None
    return value


def _verify_adjustment(active: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate the first three later segments, never a reused baseline row."""
    value = dict(active)
    baseline_ids = value.get("baseline_segment_ids") or []
    if value.get("status") != "monitoring" or not baseline_ids:
        return value
    positions = [index for index, row in enumerate(rows)
                 if row.get("segment_id") == baseline_ids[-1]]
    if not positions:
        value.update(status="inconclusive", reason="baseline_evicted")
        return value
    post = rows[positions[-1] + 1:positions[-1] + 1 + WINDOW]
    value["post_segment_ids"] = [row["segment_id"] for row in post]
    if len(post) < WINDOW:
        return value
    complete = all(row.get("sampling") == "tool_complete"
        and isinstance(row.get("terminal_calls"), int)
        and isinstance(row.get("effective_calls"), int)
        and (row.get("work_delta") or {}).get("known") is True
        and isinstance((row.get("timing") or {}).get("wall_seconds"), (int, float))
        and row["timing"]["wall_seconds"] > 0 for row in post)
    if not complete:
        value.update(status="inconclusive", reason="post_window_partial")
        return value
    calls = sum(row["terminal_calls"] for row in post)
    effective = sum(row["effective_calls"] for row in post)
    wall = sum(row["timing"]["wall_seconds"] for row in post)
    units = sum(max(0, int(row["work_delta"].get("total_units") or 0)) for row in post)
    ratio = round(effective / calls, 4) if calls else None
    throughput = round(units / wall, 6) if wall else None
    value["post_effective_call_ratio"] = ratio
    value["post_throughput_items_per_second"] = throughput
    before_ratio = value.get("baseline_effective_call_ratio")
    before_throughput = value.get("baseline_throughput_items_per_second")
    improved_ratio = (isinstance(ratio, (int, float))
                      and isinstance(before_ratio, (int, float))
                      and ratio >= before_ratio + MIN_BENEFIT_RATIO_DELTA)
    improved_throughput = (isinstance(throughput, (int, float))
                           and isinstance(before_throughput, (int, float))
                           and (throughput > 0 if before_throughput == 0 else
                                throughput >= before_throughput * MIN_BENEFIT_THROUGHPUT_FACTOR))
    value["status"] = "verified_benefit" if improved_ratio or improved_throughput else "rolled_back"
    value["reason"] = ("measured_execution_benefit" if improved_ratio or improved_throughput
                       else "no_measured_execution_benefit")
    value["future_identical_replan_suppressed"] = not (improved_ratio or improved_throughput)
    return value


def supervise_segments(history: list[dict[str, Any]], *,
                       previous: dict[str, Any] | None = None,
                       target_eta_seconds: float | None = None,
                       issue_counts: dict[str, int] | None = None,
                       scope_context: dict[str, Any] | None = None,
                       input_revision: str = "") -> dict[str, Any]:
    """Return a restart-stable supervision receipt for one node.

    Incomplete telemetry never becomes a zero. Two distinct bad windows are
    required before method replanning is requested, preventing one slow slice
    from changing strategy. No model judgment or business result is inferred.
    """
    previous = previous if isinstance(previous, dict) else {}
    rows = [row for row in history[-MAX_HISTORY:] if isinstance(row, dict)
            and (not input_revision or row.get("input_revision") == input_revision)]
    distinct: dict[str, dict[str, Any]] = {}
    for row in rows:
        segment_id = row.get("segment_id")
        if isinstance(segment_id, str) and segment_id:
            distinct[segment_id] = row
    window = list(distinct.values())[-WINDOW:]
    all_rows = list(distinct.values())
    previous_revision = str(previous.get("input_revision") or "")
    same_input = not previous_revision or previous_revision == input_revision
    active = (previous.get("active_adjustment") or {}) if same_input else {}
    active = active if isinstance(active, dict) else {}
    if active:
        active = _verify_adjustment(active, all_rows)
    complete = len(window) == WINDOW and all(row.get("sampling") == "tool_complete"
        and (row.get("timing") or {}).get("wall_seconds") is not None
        and (row.get("work_delta") or {}).get("known") is True for row in window)
    last_segment_id = window[-1]["segment_id"] if window else ""
    terminal = sum(int(row.get("terminal_calls") or 0) for row in window) if complete else 0
    effective = sum(int(row.get("effective_calls") or 0) for row in window) if complete else 0
    wall = sum(float(row["timing"]["wall_seconds"]) for row in window) if complete else 0.
    units = sum(int(row["work_delta"].get("total_units") or 0) for row in window) if complete else 0
    ratio = round(effective / terminal, 4) if complete and terminal else None
    throughput = round(units / wall, 6) if complete and wall > 0 else None
    readable = (complete and all(isinstance(row.get("read_calls"), int)
        and isinstance(row.get("keyed_reads"), int)
        and isinstance(row.get("repeated_reads"), int)
        and isinstance(row.get("unkeyed_reads"), int) for row in window))
    read_calls = sum(row["read_calls"] for row in window) if readable else None
    keyed_reads = sum(row["keyed_reads"] for row in window) if readable else None
    unkeyed_reads = sum(row["unkeyed_reads"] for row in window) if readable else None
    repeated_reads = sum(row["repeated_reads"] for row in window) if readable else None
    repeated_ratio = (round(repeated_reads / keyed_reads, 4)
                      if readable and keyed_reads else None)
    eta = (window[-1].get("eta") or {}) if window else {}
    eta_seconds = (eta.get("seconds") if complete and eta.get("status") == "estimated"
                   and isinstance(eta.get("seconds"), (int, float)) else None)
    signals = []
    if ratio is not None and terminal >= MIN_TERMINAL_CALLS and ratio < LOW_EFFECTIVE_RATIO:
        signals.append("low_effective_call_ratio")
    if (repeated_ratio is not None and unkeyed_reads == 0
            and keyed_reads >= MIN_KEYED_READ_CALLS
            and repeated_ratio >= HIGH_REPEATED_READ_RATIO):
        signals.append("high_repeated_read_ratio")
    if (eta_seconds is not None and isinstance(target_eta_seconds, (int, float))
            and target_eta_seconds > 0 and eta_seconds > target_eta_seconds):
        signals.append("eta_over_target")
    previous_bad = int(previous.get("consecutive_bad_windows") or 0) if same_input else 0
    if last_segment_id and previous.get("last_segment_id") == last_segment_id:
        consecutive = previous_bad
    else:
        consecutive = min(3, previous_bad + 1) if signals else 0
    # An unverified trial is never repeated. A failed or inconclusive trial is
    # not silently retried against the same input generation.
    requested = bool(signals and consecutive >= 2 and not active)
    scope = None
    if isinstance(issue_counts, dict):
        valid = {str(key): value for key, value in issue_counts.items()
                 if type(value) is int and value >= 0}
        total = sum(valid.values())
        if total >= 10 and valid:
            area, count = max(valid.items(), key=lambda item: item[1])
            if count / total >= .9:
                context = scope_context if isinstance(scope_context, dict) else {}
                decision_key = hashlib.sha256(json.dumps(
                    [input_revision, context.get("contract_hash"), area],
                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
                scope = {"kind": "possible_scope_concentration", "area": area,
                         "count": count, "total": total, "decision_required": True,
                         "automatic_scope_change": False,
                         "decision_id": "scope-" + decision_key,
                         "count_basis": "recorded_needs_repair_issue_occurrences",
                         "original_scope": {"contract_hash": context.get("contract_hash"),
                                            "item_count": context.get("item_count"),
                                            "recorded_item_count": context.get("recorded_item_count"),
                                            "status": "unchanged"},
                         "candidate_scope": {"focus_area": area, "status": "unapproved_proposal_only"},
                         "evidence_impact": {"outside_focus_issue_count": total - count,
                                             "unreviewed_item_count": context.get("unreviewed_item_count"),
                                             "must_recheck_all_affected_evidence": True,
                                             "independent_review_preserved": True},
                         "time_estimate": {"status": "unknown_for_revised_scope",
                                           "original_eta_seconds": eta_seconds,
                                           "revised_eta_seconds": None},
                         "options": [
                             {"id": "keep_original", "effect": "保持原范围与验收继续执行"},
                             {"id": "request_impact_analysis", "effect": "只做证据和工期影响分析，不改范围"},
                             {"id": "request_formal_revision", "effect": "需用户明确批准新范围及验收修订"},
                         ]}
    return {"schema_version": 1, "policy_version": POLICY_VERSION,
            "last_segment_id": last_segment_id,
            "window_segment_ids": [row["segment_id"] for row in window],
            "sampling": "complete" if complete else "partial",
            "terminal_calls": terminal if complete else None,
            "effective_calls": effective if complete else None,
            "effective_call_ratio": ratio,
            "throughput_items_per_second": throughput,
            "read_calls": read_calls, "keyed_reads": keyed_reads,
            "unkeyed_reads": unkeyed_reads, "repeated_reads": repeated_reads,
            "repeated_read_ratio": repeated_ratio,
            "eta_seconds": eta_seconds,
            "target_eta_seconds": target_eta_seconds,
            "signals": signals,
            "consecutive_bad_windows": consecutive,
            "requested_action": "replan_execution_method" if requested else None,
            "input_revision": input_revision,
            "active_adjustment": active or None,
            "scope_decision": scope,
            "changes_acceptance": False}
