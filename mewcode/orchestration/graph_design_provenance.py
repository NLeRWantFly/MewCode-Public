"""Identify unchanged AI explanations from wholly unaccepted design history.

This only supplies candidates for an explicit withdrawal preview. Reads never
mutate a document and user-sourced/reviewed decisions are never eligible.
"""
from __future__ import annotations

import copy
import json

from .workflow_draft import validate_entry


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _rounds(build):
    if not isinstance(build, dict):
        return
    for previous in _rows(build.get("previous_attempts")):
        yield from _rounds(previous)
    yield from _rows((build.get("autofix") or {}).get("rounds"))
    for previous in _rows(build.get("gap_fill_history")):
        yield from _rows(previous.get("rounds"))
    yield from _rows((build.get("gap_fill") or {}).get("rounds"))


def _ambiguous_ids(build):
    """Legacy phase buckets lack a global sequence after discussion resumes."""
    if not isinstance(build, dict):
        return set()
    def ids(rows):
        return {item.get("id") for row in rows
                for item in _rows((row.get("proposal") or {}).get("upserts"))
                if isinstance(item.get("id"), str)}
    automatic = _rows((build.get("autofix") or {}).get("rounds"))
    gaps = _rows((build.get("gap_fill") or {}).get("rounds"))
    for previous in _rows(build.get("gap_fill_history")):
        gaps += _rows(previous.get("rounds"))
    result = ids(automatic) & ids(gaps)
    for previous in _rows(build.get("previous_attempts")):
        result |= _ambiguous_ids(previous)
    return result


def unsupported_designs(document, build):
    """Require an unbroken add/update receipt chain and full JSON equality."""
    tracked = {}
    ambiguous = _ambiguous_ids(build)
    for row in _rounds(build or {}):
        structural, ledger = row.get("structural") or {}, row.get("ledger") or {}
        unsupported = bool(structural.get("applied") or structural.get("rejected")) and (
            not structural.get("adopted") or bool(structural.get("rejected")))
        proposal = row.get("proposal")
        raw_rows = _rows(proposal.get("upserts")) if isinstance(proposal, dict) else []
        receipts = _rows(ledger.get("applied")) if ledger.get("adopted") else []
        touched = {e.get("id") for e in raw_rows if isinstance(e.get("id"), str)} | {
            r.get("entry_id") for part in (structural, ledger)
            for key in ("applied", "rejected") for r in _rows(part.get(key)) if isinstance(r.get("entry_id"), str)}
        for entry_id in touched:
            previous = tracked.pop(entry_id, None)
            matches = [e for e in raw_rows if e.get("id") == entry_id]
            applied = [r for r in receipts if r.get("entry_id") == entry_id]
            if not unsupported or len(matches) != 1 or len(applied) != 1:
                continue
            raw, code = matches[0], applied[0].get("code")
            if set(raw) - {"id", "kind", "title", "body", "data", "rationale", "state"}:
                continue
            if code == "decision_added" and raw.get("kind") == "decision":
                rebuilt = copy.deepcopy(raw)
                rebuilt.update(evidence="ai_completed")
                rebuilt.setdefault("state", "active")
            elif code == "own_entry_updated" and previous and raw.get("kind", "decision") == "decision":
                rebuilt = copy.deepcopy(previous)
                rebuilt.update({k: copy.deepcopy(raw[k]) for k in ("title", "body", "rationale") if k in raw})
                if not isinstance(raw.get("data", {}), dict):
                    continue
                rebuilt["data"].update(copy.deepcopy(raw.get("data", {})))
            else:
                continue
            try:
                rebuilt.update(validate_entry(rebuilt))
            except (ValueError, TypeError, KeyError):
                continue
            tracked[entry_id] = rebuilt
    result = []
    for entry in document.get("entries", []):
        expected = tracked.get(entry.get("id"))
        if (expected and entry.get("id") not in ambiguous and entry.get("kind") == "decision" and entry.get("state") == "active"
                and entry.get("evidence") == "ai_completed" and not entry.get("sources")
                and not entry.get("reviewed")
                and json.dumps(entry, sort_keys=True) == json.dumps(expected, sort_keys=True)):
            result.append(copy.deepcopy(entry))
    return result
