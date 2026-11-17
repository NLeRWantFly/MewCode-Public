"""Read-only, privacy-minimal statistics for one explicit expert-run snapshot.

This is a structural probe, not a historical replay: it reports only known
field names, allowlisted status buckets, and token-counter availability. It
never emits snapshot values, identifiers, paths, prompts, or counter values.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


KNOWN_TOP_LEVEL_FIELDS = frozenset({
    "version", "run_id", "conversation_id", "task", "supplemental_requirements",
    "attachments", "status", "stage_cursor", "attempt", "strategy",
    "strategy_source", "locked_profile", "locked_flow", "flow_id", "flow_version",
    "flow_fingerprint", "saved_plan_fingerprint", "finalizer_node_id", "created_at",
    "updated_at", "started_at", "completed_at", "paused_intervals", "attempts",
    "stages", "node_states", "node_attempts", "members", "pipeline_runs",
    "node_expansions", "handoffs", "active_node_ids", "tools", "children", "usage",
    "events", "display_index", "last_seq", "legacy", "flow_state", "audit_protocols",
    "test_records", "process_files", "process_file_preview_refs", "review_queue_refs",
    "review_queues", "review_queue_archives", "locked_models", "locked_flow",
    "pending_recovery_revision", "recovery_decision", "authorized_revision",
})

SAFE_STATUSES = frozenset({
    "running", "completed", "failed", "paused", "blocked", "budget_exhausted",
    "stalled", "needs_attention", "terminated", "queued", "review_pending",
    "incomplete_audit", "skipped", "stopped_for_stable_takeover", "resume_pending",
    "waiting", "not_started", "unknown",
})

SAFE_EVENT_TYPES = frozenset({
    "expert_stage", "expert_step", "expert_run_state", "expert_run_done",
    "expert_children_stopped", "expert_lessons_proposed", "expert_lessons_unavailable",
    "expert_flow_defined", "expert_node_started", "expert_node_completed",
    "expert_member_started", "expert_member_completed", "expert_join_started",
    "expert_join_completed", "expert_handoff", "expert_flow_state", "plan_revision",
    "expert_pipeline_started", "expert_pipeline_completed",
    "expert_pipeline_step_started", "expert_pipeline_step_completed",
    "expert_pipeline_step_output", "expert_pipeline_step_tool",
    "expert_pipeline_step_usage", "expert_pipeline_warning", "expert_usage",
    "expert_role_wait", "ask_user", "human_oncall", "expert_tool", "expert_tool_start",
    "expert_tool_result", "expert_tool_wait", "expert_tool_state", "expert_child",
})

COUNTER_FIELDS = ("input_tokens", "output_tokens", "billable_tokens")
STAGE_FIELDS = frozenset({
    "node_id", "member_id", "role", "status", "attempt", "node_attempt", "usage",
    "usage_by_attempt", "billable_tokens", "input_tokens", "output_tokens", "started_at",
    "completed_at", "duration_ms", "wait_seconds", "active_seconds", "error_kind",
    "messages", "partial_text", "partial_reasoning", "text", "tools", "events",
})
CHILD_FIELDS = frozenset({
    "agent_id", "node_id", "role", "parent_id", "name", "type", "status", "mode",
    "task_id", "started_at", "completed_at", "worktree", "result", "messages", "usage",
})
ATTEMPT_FIELDS = frozenset({"attempt", "strategy", "started_at", "ended_at", "status"})
EVENT_FIELDS = frozenset({
    "type", "seq", "source_seq", "status", "node_id", "member_id", "role", "attempt",
    "node_attempt", "phase", "usage", "input_tokens", "output_tokens", "billable_tokens",
    "snapshot_aggregate", "server_time", "at",
})
USAGE_FIELDS = frozenset({
    *COUNTER_FIELDS, "cache_read_tokens", "cache_creation_tokens", "cache_miss_tokens",
    "cache_usage_available", "cache_usage_partial", "source_count",
})


def _valid_counter(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _safe_status(value: Any) -> str:
    return value if isinstance(value, str) and value in SAFE_STATUSES else "unknown"


def _mapping_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return value
    return []


def _known_fields(records: list[Any], allowlist: frozenset[str]) -> list[str]:
    found: set[str] = set()
    for record in records:
        if isinstance(record, dict):
            found.update(key for key in record if isinstance(key, str) and key in allowlist)
    return sorted(found)


def _status_summary(records: list[Any]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for record in records:
        counts[_safe_status(record.get("status") if isinstance(record, dict) else None)] += 1
    return {"records": len(records), "counts": dict(sorted(counts.items()))}


def _counter_summary(records: list[Any], *, direct: bool = True) -> dict[str, Any]:
    present: Counter[str] = Counter()
    for record in records:
        if not isinstance(record, dict):
            continue
        nested = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        for field in COUNTER_FIELDS:
            if ((direct and _valid_counter(record.get(field)))
                    or _valid_counter(nested.get(field))):
                present[field] += 1
    return {
        field: {"records_with_counter": present[field],
                "availability": "available" if present[field] else "unknown"}
        for field in COUNTER_FIELDS
    }


def _counter_field_structure(records: list[Any]) -> list[str]:
    found: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        usage = record.get("usage")
        if isinstance(usage, dict):
            found.update(key for key in usage if isinstance(key, str) and key in USAGE_FIELDS)
        found.update(key for key in record if isinstance(key, str) and key in COUNTER_FIELDS)
    return sorted(found)


def _event_counter_summary(events: list[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    usage_events = [event for event in events
                    if isinstance(event, dict) and event.get("type") == "expert_usage"]
    derived = [event for event in usage_events if event.get("snapshot_aggregate") is True]
    source = [event for event in usage_events if event.get("snapshot_aggregate") is not True]
    identity_complete = 0
    seqs: set[int] = set()
    duplicate_seq_rows = 0
    for event in source:
        attempt = event.get("node_attempt", event.get("attempt"))
        sequence = event.get("source_seq", event.get("seq"))
        has_identity = (
            isinstance(event.get("node_id"), str) and bool(event.get("node_id"))
            and _valid_counter(attempt) and attempt > 0
            and isinstance(event.get("role"), str) and bool(event.get("role"))
            and isinstance(event.get("member_id"), str) and bool(event.get("member_id"))
            and _valid_counter(sequence) and sequence > 0
        )
        identity_complete += int(has_identity)
        if _valid_counter(sequence) and sequence > 0:
            if sequence in seqs:
                duplicate_seq_rows += 1
            seqs.add(sequence)
    return _counter_summary(source), {
        "usage_event_records": len(usage_events),
        "source_usage_event_records": len(source),
        "snapshot_aggregate_event_records_excluded": len(derived),
        "records_with_complete_stream_identity": identity_complete,
        "duplicate_source_sequence_rows": duplicate_seq_rows,
        "identity_fields": ["node_id", "node_attempt_or_attempt", "role", "member_id", "source_seq_or_seq"],
    }


def analyze_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return an allowlisted structural report without copying snapshot values."""
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot root must be an object")

    # The on-disk *.public.json is a cache envelope. Its summary is the
    # projection of the run; the envelope itself contains only source stamps.
    cache_fields = frozenset({
        "source_mtime_ns", "source_size", "event_log_size", "snapshot_version",
        "projection_version", "conversation_id", "summary",
    })
    is_public_cache = isinstance(snapshot.get("summary"), dict)
    envelope = snapshot if is_public_cache else None
    snapshot = snapshot["summary"] if is_public_cache else snapshot

    present_top = sorted(field for field in snapshot if isinstance(field, str)
                         and field in KNOWN_TOP_LEVEL_FIELDS)
    node_states = snapshot.get("node_states")
    stages = snapshot.get("stages")
    node_records = _mapping_values(node_states)
    stage_records = _mapping_values(stages)
    exact_stage_alias = isinstance(node_states, (dict, list)) and node_states == stages

    collections: dict[str, Any] = {}
    for name, allowlist in (("node_states", STAGE_FIELDS), ("members", STAGE_FIELDS),
                            ("children", CHILD_FIELDS), ("pipeline_runs", STAGE_FIELDS),
                            ("attempts", ATTEMPT_FIELDS)):
        records = _mapping_values(snapshot.get(name))
        collections[name] = {
            "present": name in snapshot,
            "fields": _known_fields(records, allowlist),
            **_status_summary(records),
        }
    if "stages" in snapshot:
        collections["stages"] = {
            "present": True,
            "fields": _known_fields(stage_records, STAGE_FIELDS),
            "deduplicated_to": "node_states" if exact_stage_alias else None,
            **(_status_summary([]) if exact_stage_alias else _status_summary(stage_records)),
        }

    events = _mapping_values(snapshot.get("events"))
    known_event_counts: Counter[str] = Counter()
    other_event_count = 0
    for event in events:
        event_type = event.get("type") if isinstance(event, dict) else None
        if isinstance(event_type, str) and event_type in SAFE_EVENT_TYPES:
            known_event_counts[event_type] += 1
        else:
            other_event_count += 1
    collections["events"] = {
        "present": "events" in snapshot,
        "fields": _known_fields(events, EVENT_FIELDS),
        "records": len(events),
        "known_type_counts": dict(sorted(known_event_counts.items())),
        "other_or_unknown_type_records": other_event_count,
        "coverage": "retained_snapshot_records_only",
    }

    # The stored top-level total is produced from node/stage, member and child
    # counters by aggregate_snapshot_usage. It is not an independent source.
    usage = snapshot.get("usage") if isinstance(snapshot.get("usage"), dict) else {}
    aggregate = usage.get("total") if isinstance(usage.get("total"), dict) else {}
    event_counters, event_dedup = _event_counter_summary(events)
    stage_source_name = "node_states" if node_states else "stages"
    source_collections = {
        stage_source_name: node_records if node_states else stage_records,
        "members": _mapping_values(snapshot.get("members")),
        "children": _mapping_values(snapshot.get("children")),
    }
    counters_by_source = {
        name: {
            "fields": _counter_field_structure(records),
            "availability": _counter_summary(records),
        }
        for name, records in source_collections.items()
    }
    if exact_stage_alias:
        counters_by_source["stages"] = {
            "fields": _counter_field_structure(stage_records),
            "deduplicated_to": "node_states",
        }
    counters_by_source["expert_usage_events"] = {
        "fields": _counter_field_structure([
            event for event in events if isinstance(event, dict)
            and event.get("type") == "expert_usage"
            and event.get("snapshot_aggregate") is not True
        ]),
        "availability": event_counters,
    }
    if aggregate:
        counters_by_source["usage.total"] = {
            "fields": sorted(key for key in aggregate if isinstance(key, str)
                              and key in USAGE_FIELDS),
            "availability": _counter_summary([aggregate]),
            "deduplicated_as": "derived_aggregate_excluded_from_source_counters",
        }

    reported_source_count = aggregate.get("source_count")
    visible_node_usage_count = sum(
        isinstance(record, dict) and isinstance(record.get("usage"), dict)
        for record in (node_records if node_states else stage_records)
    )

    return {
        "schema_version": 1,
        "source_format": "public_summary_cache" if is_public_cache else "run_snapshot",
        "cache_envelope": {
            "known_fields": sorted(key for key in envelope if key in cache_fields),
            "source_binding_validated": False,
        } if envelope is not None else None,
        "snapshot_version": snapshot.get("version") if isinstance(snapshot.get("version"), int)
        and not isinstance(snapshot.get("version"), bool) else None,
        "field_structure": {
            "known_top_level_fields": present_top,
            "collections": collections,
            "usage_total_fields": sorted(key for key in aggregate if isinstance(key, str)
                                         and key in USAGE_FIELDS),
        },
        "run_status": _safe_status(snapshot.get("status")),
        "token_counter_availability": {
            "sources": counters_by_source,
            "billable_token_total": None,
            "total_coverage": "unknown",
            "unknown_reason": "snapshot counters may overlap across node, member, child, aggregate, and retained event sources; the snapshot does not prove a complete exact-once history",
        },
        "deduplication": {
            "stages_alias": "node_states" if exact_stage_alias else "not_proven",
            "usage_total": "derived_aggregate_excluded",
            "reported_aggregate_source_count": reported_source_count
            if _valid_counter(reported_source_count) else None,
            "visible_node_usage_records": visible_node_usage_count,
            **event_dedup,
            "policy": "do not sum overlapping snapshot collections or snapshot_aggregate events",
        },
    }


def inspect_path(path: Path) -> dict[str, Any]:
    """Read exactly one caller-supplied JSON file; omit filesystem details on error."""
    try:
        with path.open("r", encoding="utf-8") as source:
            snapshot = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "ERROR", "error": "input_unreadable_or_invalid_json"}
    if not isinstance(snapshot, dict):
        return {"status": "ERROR", "error": "snapshot_root_must_be_object"}
    return {"status": "OK", "report": analyze_snapshot(snapshot)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="explicit path to one expert-run snapshot JSON")
    args = parser.parse_args(argv)
    result = inspect_path(args.snapshot)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
