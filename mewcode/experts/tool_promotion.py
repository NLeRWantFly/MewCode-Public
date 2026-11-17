"""Conservative detection of repeated one-off scripts.

This module creates review proposals only. A command is never registered or
granted a safety-review bypass from similarity alone.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

POLICY_VERSION = "tool-promotion-proposal-v1"
_SCRIPT = re.compile(r"(?i)(?:^|\s)(?:python(?:\.exe)?|uv\s+run\s+python)\s+"
                     r"(?P<path>(?:[^\s\"']+[\\/])?[^\s\"']+?[_-]r(?P<item>[0-9]{2,8})\.py)(?=\s|$)")
_PYTHON_C = re.compile(r"(?i)(?:^|\s)(?:python(?:\.exe)?|uv\s+run\s+python)\s+-c\s+")


def _segment_for(timestamp: Any, history: list[dict[str, Any]]) -> str:
    if not isinstance(timestamp, (int, float)):
        return ""
    for segment in reversed(history):
        start, end = segment.get("started_at"), segment.get("ended_at")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and start <= timestamp <= end:
            return str(segment.get("segment_id") or "")
    return ""


def propose_repeated_scripts(stage: dict[str, Any]) -> list[dict[str, Any]]:
    history = [row for row in stage.get("segment_health_history") or [] if isinstance(row, dict)]
    tools = stage.get("tools") or {}
    rows = tools.values() if isinstance(tools, dict) else tools if isinstance(tools, list) else []
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in list(rows)[-128:]:
        if not isinstance(row, dict) or row.get("name") != "Bash" or row.get("status") != "completed" or row.get("is_error"):
            continue
        command = (row.get("args") or {}).get("command")
        if not isinstance(command, str) or len(command) > 4_096:
            continue
        segment_id = _segment_for(row.get("declared_at") or row.get("started_at"), history)
        if not segment_id:
            continue
        matched = _SCRIPT.search(command)
        if matched:
            raw_path = matched.group("path")
            # Only the filename's exact r<digits> token varies. Every other
            # command byte remains part of the proposal identity.
            template = command[:matched.start("item")] + "{item_id}" + command[matched.end("item"):]
            pattern = "numbered_script_file"
        elif _PYTHON_C.search(command):
            # Exact repeated inline code is useful telemetry; differing code
            # cannot be safely parameterized by a regex.
            template = command
            pattern = "exact_python_inline"
        else:
            continue
        key = hashlib.sha256((pattern + "\0" + template).encode()).hexdigest()
        groups.setdefault(key, []).append({"segment_id": segment_id,
            "tool_id": str(row.get("tool_id") or ""), "item_id": matched.group("item") if matched else None})
    proposals = []
    for key, group in groups.items():
        distinct_segments = {row["segment_id"] for row in group}
        if len(group) < 3 or len(distinct_segments) < 2:
            continue
        pattern = "numbered_script_file" if any(row["item_id"] for row in group) else "exact_python_inline"
        proposals.append({"schema_version": 1, "policy_version": POLICY_VERSION,
            "proposal_id": key, "pattern": pattern, "occurrences": len(group),
            "distinct_segments": len(distinct_segments),
            "example_tool_ids": [row["tool_id"] for row in group[-4:]],
            "suggested_parameters": ({"item_id": {"type": "string", "pattern": "^[0-9]{2,8}$"}}
                                     if pattern == "numbered_script_file" else {}),
            "input_scope": "requires_independent_review", "write_scope": "unknown",
            "side_effect": "unknown", "status": "proposal_only",
            "review_required": True, "eligible_for_execution": False})
    return proposals[:16]
