"""Bound a repair view while retaining topology and every global requirement.

Only unrelated node prose is left out. Those nodes become read-only references;
the host rejects edits and new references to them before applying a proposal to
the complete document. Limits, protected facts and full-graph checks still apply.
"""
from __future__ import annotations

import copy


_NODE_REFS = {"from_node", "to_node", "parent_id", "entry_node_id", "check_node_id",
              "node_id", "collect_at", "audit_target"}
_NODE_LISTS = {"node_ids", "reviewer_node_ids", "repair_node_ids", "required_node_ids"}


def _references(data):
    refs = {value for key, value in data.items() if key in _NODE_REFS and isinstance(value, str)}
    for key in _NODE_LISTS:
        values = data.get(key)
        if isinstance(values, (list, tuple)):
            refs.update(value for value in values if isinstance(value, str))
    return refs


def focused_payloads(payload):
    """Yield deterministic diagnostic views, without truncating selected facts."""
    entries = payload["entries"]
    by_id = {entry["id"]: entry for entry in entries}
    nodes = {key: row for key, row in by_id.items() if row["kind"] == "node"}
    # Keep existing reviewers, planning, delivery and human gates available in
    # full, so a fix can reuse them and cannot guess their acceptance criteria.
    checkpoints = {key for key, row in nodes.items()
                   if row["data"].get("node_kind", "implement") != "implement"}
    seen = set()
    for index, diagnostic in enumerate(payload["diagnostics"]):
        focus = set(diagnostic.get("entry_ids") or []) & by_id.keys()
        if not focus:
            continue
        selected = (focus & nodes.keys()) | checkpoints
        for key in focus:
            selected.update(_references(by_id[key]["data"]) & nodes.keys())
        # Include direct dependencies of the affected nodes, not every node on
        # a long chain. The complete edge list below still shows every path.
        affected = (selected - checkpoints) | (focus & nodes.keys())
        for row in entries:
            if row["kind"] == "edge" and _references(row["data"]) & affected:
                selected.update(_references(row["data"]) & nodes.keys())
        # A selected loop must be understood as a whole; hierarchy references
        # also need their original requirements. Closure is monotonic/bounded.
        while True:
            expanded = set(selected)
            for key in selected:
                expanded.update(_references(nodes[key]["data"]) & nodes.keys())
            for row in entries:
                if row["kind"] == "loop" and _references(row["data"]) & selected:
                    expanded.update(_references(row["data"]) & nodes.keys())
            if expanded == selected:
                break
            selected = expanded
        omitted = nodes.keys() - selected
        signature = frozenset(selected)
        if not omitted or signature in seen:
            continue
        seen.add(signature)
        view = copy.deepcopy(payload)
        editable = []
        for row in view["entries"]:
            if row["id"] in omitted:
                row.pop("body", None)
                row["data"] = {key: value for key, value in row["data"].items()
                               if key in {"node_kind", "parent_id", "execution_mode"}}
                row["context_only"] = True
                row["editable"] = {}
            elif _references(row["data"]) & omitted:
                row["editable"] = {}
            elif row["editable"]:
                editable.append(row["id"])
        view["scope"] = {"mode": "focused", "diagnostic_index": index,
                         "focus_entry_ids": sorted(focus), "full_node_ids": sorted(selected),
                         "editable_entry_ids": sorted(editable)}
        yield view


def scope_rejections(document, upserts, scope):
    """Enforce the host-selected view, never a scope supplied by the model."""
    if not scope or scope.get("mode") != "focused":
        return []
    existing = {row["id"]: row for row in document.get("entries", [])}
    hidden_nodes = {key for key, row in existing.items() if row["kind"] == "node"}
    hidden_nodes -= set(scope["full_node_ids"])
    editable = set(scope["editable_entry_ids"])
    rejected = []
    for item in upserts:
        key = item.get("id")
        if not isinstance(key, str):
            continue  # The normal completion validator handles malformed IDs.
        old = existing.get(key)
        data = item.get("data", {})
        if not isinstance(data, dict):
            continue
        merged = {**(old or {}).get("data", {}), **data}
        if (old is not None and key not in editable) or _references(merged) & hidden_nodes:
            rejected.append({"entry_id": key, "code": "outside_request_scope",
                             "message": "条目或引用节点未在本轮完整展示；保留原图，不能依据拓扑索引修改其要求或依赖。"})
    return rejected
