"""Read-only, content-free metrics for a frozen paused-dialogue interval."""
from __future__ import annotations

import asyncio
import collections
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_classify import hard_review_hold
from mewcode.orchestration.paused_handoff_compression import aggregate_repeated_reads
from mewcode.orchestration.paused_handoff_review import (
    ReviewLedger, classify_with_receipts, preflight,
)


async def replay_paused_handoff(
    *, log_path: str | Path, run_id: str, conversation_id: str,
    contract_fingerprint: str, start: int, end: int,
    critical_record_ordinals: list[int] | None = None,
    fragment_bytes: int = 8192,
) -> dict[str, Any]:
    """Use a disposable private index; never mutate the source or real run."""
    with tempfile.TemporaryDirectory(prefix="mewcode-handoff-replay-") as root:
        started = time.perf_counter()
        index = PausedHandoffIndex.build(
            log_path=log_path, root=root, run_id=run_id,
            conversation_id=conversation_id,
            contract_fingerprint=contract_fingerprint,
            start=start, end=end, fragment_bytes=fragment_bytes,
        )
        coverage = index.verify(original=True)
        index_seconds = time.perf_counter() - started
        ledger = ReviewLedger(index)
        classified_started = time.perf_counter()
        result = await classify_with_receipts(index, ledger, None,
                                              task_contract="", remaining_tokens=None)
        classification_seconds = time.perf_counter() - classified_started
        classifications = {event["fragment_id"]: event["classification"]
                           for event in ledger.events if event["phase"] == "classified"}
        levels = collections.Counter(row["level"] for row in classifications.values())
        records = index.manifest["records"]
        critical = critical_record_ordinals or []
        invalid_labels = [ordinal for ordinal in critical
                          if not isinstance(ordinal, int) or ordinal < 0 or ordinal >= len(records)]
        if invalid_labels:
            raise ValueError("critical event label is outside frozen source")
        missed = [ordinal for ordinal in critical
                  if any(classifications[fragment["id"]]["level"]
                         not in {"core", "uncertain"}
                         for fragment in records[ordinal]["fragments"])]
        budget = preflight(index, remaining_tokens=None, remaining_seconds=None)
        orphan_results = sum(record["record_type"] == "tool_result" and not record.get("tool")
                             for record in records)
        prior_linked = sum(record.get("call_provenance") == "matched_prior_session_record"
                           for record in records)
        groups = aggregate_repeated_reads(index, list(classifications.values()))
        repeated_members = sum(max(0, group["count"] - 1) for group in groups)
        potential_view_bytes = sum(
            member["end"] - member["start"]
            for group in groups for member in group["member_refs"][1:]
        )
        hold_tool_actions: collections.Counter[str] = collections.Counter()
        for record in records:
            if not hard_review_hold(record):
                continue
            calls = (record.get("calls") or []) if record.get("record_type") == "assistant" else [record]
            for call in calls:
                tool = str(call.get("tool") or "unknown")
                action = str(call.get("action") or "-")
                # Reports are content-free. Never copy arbitrary command or
                # path text from a malformed action field into public output.
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", tool):
                    tool = "unknown"
                if action != "-" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", action):
                    action = "other"
                hold_tool_actions[f"{tool}:{action}"] += 1
        return {
            "mode": "historical_read_only", "source_sha256": index.manifest["source"]["sha256"],
            "raw_bytes": coverage["bytes"], "records": coverage["records"],
            "fragments": coverage["fragments"], "mechanical_coverage_complete": coverage["complete"],
            "classification_status": result["status"], "levels": dict(levels),
            "critical_events_checked": len(critical), "critical_events_missed": len(missed),
            "critical_recall": ((len(critical) - len(missed)) / len(critical)
                                if critical else None),
            "orphan_tool_results": orphan_results,
            "prior_call_linked_results": prior_linked,
            "repeated_read_groups": len(groups),
            "repeated_read_members_beyond_first": repeated_members,
            "potential_view_bytes_avoided": potential_view_bytes,
            "index_wall_seconds": round(index_seconds, 3),
            "classification_wall_seconds": round(classification_seconds, 3),
            "estimated_semantic_tokens_upper": budget["estimated_semantic_tokens_upper"],
            "deterministic_hold_records": budget["deterministic_hold_records"],
            "deterministic_hold_reasons": budget["deterministic_hold_reasons"],
            "hard_hold_tool_actions": dict(sorted(hold_tool_actions.items())),
            "model_tokens_spent": 0, "real_resume_attempted": False,
            "real_completion_time_seconds": None, "counterfactual_savings": None,
            "actual_token_savings": None, "actual_time_savings": None,
            "actual_cost_savings": None,
        }


def replay_paused_handoff_sync(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(replay_paused_handoff(**kwargs))
