"""Preserve source, field adoption, strength and scope as separate host facts."""
from __future__ import annotations

import copy

from mewcode.kernel.events import digest


def annotate(entry, source_snapshot):
    item = copy.deepcopy(entry)
    fields = source_snapshot.get("field_sources", {}).get(entry["id"], {})
    sources = {row["id"]: row for row in source_snapshot.get("sources", [])}
    main_refs = fields.get("body", entry.get("sources", []))
    roles = {sources.get(ref.get("source_id"), {}).get("role", "unknown") for ref in main_refs}
    adopted_fields = sorted(field for field, refs in fields.items()
                            if refs and all(sources.get(ref.get("source_id"), {}).get("role") == "user" for ref in refs))
    origin = "legacy_unknown"
    if roles == {"user"}:
        origin = "user"
    elif roles == {"assistant"}:
        origin = "model"
    elif roles == {"tool"}:
        origin = "observation"
    # Templates are marked only in host-saved source metadata, never a model's
    # origin field. A policy template does not become a revisable method.
    if main_refs and all(sources.get(ref.get("source_id"), {}).get("kind") == "host_policy" for ref in main_refs):
        origin = "host"
    elif main_refs and all(sources.get(ref.get("source_id"), {}).get("kind") == "template" for ref in main_refs):
        origin = "template"
    data = entry.get("data") or {}
    scope = (copy.deepcopy(data.get("scope")) or data.get("node_id")
             or ({"milestone_id": data["milestone_id"]} if data.get("milestone_id") else "global"))
    item.update(version=1, origin=origin, adopted_fields=adopted_fields, field_sources=copy.deepcopy(fields),
                scope=scope, strength=data.get("strength", "hard" if origin == "host" else "soft"))
    return item


def challenge_key(constraint, evidence):
    return digest([constraint["id"], constraint["version"], evidence])


def record_challenge(value, constraint_id, evidence, *, reason):
    entry = value["entries"][constraint_id]
    if entry["kind"] != "constraint" or not evidence or not reason:
        raise ValueError("约束挑战须有理由和可核对证据")
    key = challenge_key(entry, evidence)
    previous = value.setdefault("challenges", {}).get(key)
    if previous:
        return {**copy.deepcopy(previous), "cached": True}
    row = {"id": key, "constraint_id": constraint_id, "version": entry["version"],
           "evidence": copy.deepcopy(evidence), "reason": reason, "status": "proposed"}
    value["challenges"][key] = row
    return copy.deepcopy(row)
