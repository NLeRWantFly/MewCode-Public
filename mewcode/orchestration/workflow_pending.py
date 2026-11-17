"""Addressable planning diagnostics and validation after proposal withdrawal."""
from __future__ import annotations

import copy

from .draft_sources import digest


def pending_records(issues, scope):
    """Give legacy anonymous issues stable IDs and coalesce repeated failures."""
    rows = {}
    for issue in issues:
        row = copy.deepcopy(issue)
        identity = {key: value for key, value in row.items()
                    if key not in {"diagnostic_id", "occurrences"}}
        key = "pd_" + digest([scope, identity])[:24]
        row["diagnostic_id"] = key
        row["occurrences"] = max(1, row.get("occurrences", 1))
        if key in rows:
            rows[key]["occurrences"] += row["occurrences"]
        else:
            rows[key] = row
    return list(rows.values())


def proposal_key(raw):
    entry_id = raw.get("id")
    return entry_id if isinstance(entry_id, str) and entry_id else "anonymous:" + digest(raw)


def entry_identity(value):
    return value if isinstance(value, str) and value else None


def validate_current_document(document, sources, orchestration_mode):
    """Withdrawal cannot hide a defect in the accepted document."""
    from .draft_keys import duplicate_diagnostics
    from .workflow_draft import DraftPatchError, check_entry_refs, quote_matches, validate_entry

    issues, entries = [], []
    source_map = {row["id"]: row for row in sources}
    for raw in document.get("entries", []):
        if raw.get("state") == "retired":
            continue
        try:
            unsupported = sorted(set(raw) - {"id", "kind", "title", "body", "data", "state", "sources",
                "evidence", "rationale", "reviewed", "reclassified_from", "superseded_by"})
            if unsupported:
                raise DraftPatchError("当前有效条目含不支持的顶层字段：" + "、".join(unsupported),
                    entry_id=raw.get("id", ""), path="entry", code="unsupported_entry_field")
            entry = validate_entry(raw, orchestration_mode=orchestration_mode)
        except DraftPatchError as exc:
            issues.append(exc.diagnostic)
            continue
        entries.append(entry)
        refs = entry["sources"]
        if (not refs and raw.get("evidence") == "proposal") or any(
                not isinstance(ref, dict) or ref.get("source_id") not in source_map
                or not isinstance(ref.get("quote"), str)
                or not quote_matches(ref["quote"], source_map[ref["source_id"]]["text"]) for ref in refs):
            issues.append({"entry_id": entry["id"], "path": "sources", "code": "invalid_source",
                           "message": "当前有效条目缺少真实来源及逐字引文，撤回诊断不能替代修正。"})
    for entry in entries:
        try:
            check_entry_refs(entries, focus={entry["id"]})
        except DraftPatchError as exc:
            issues.append(exc.diagnostic)
    issues.extend(duplicate_diagnostics({"entries": entries}))
    return [{**issue, "origin": "document"} for issue in issues]
