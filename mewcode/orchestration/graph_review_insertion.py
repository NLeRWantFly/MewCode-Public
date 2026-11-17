"""Preview-only insertion of a missing result review before a fixed delivery.

This is not authority for automatic completion to rewrite saved requirements.
The host ticket names eligible edges; adoption still requires the normal UI/CAS
checks. Branch predicates, gates, loops and existing prose remain unchanged.
"""
from __future__ import annotations

import json


def changed_fields(before, edit):
    """Report effective changes; absent keys are not implicit null/defaults."""
    def same(left, right):
        return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False)
    fields = [key for key in edit if key not in {"id", "data", "rationale"}
              and (key not in before or not same(edit[key], before[key]))]
    if isinstance(edit.get("data"), dict):
        fields += ["data." + key for key, value in edit["data"].items()
                   if key not in before["data"] or not same(value, before["data"][key])]
    return fields


def insertion_edges(document, finalizer, diagnostics):
    affected = {key for d in diagnostics if d.get("code") == "missing_result_review"
                for key in d.get("entry_ids", [])}
    active = [e for e in document["entries"] if e["state"] == "active"]
    members = {key for e in active if e["kind"] == "loop" for key in e["data"].get("node_ids", [])}
    return [e["id"] for e in active if e["kind"] == "edge"
            and e["data"].get("from_node") in affected - members
            and e["data"].get("to_node") == finalizer
            and e["data"].get("condition", "always") in {"always", "pass"}]


def insertion_edit(before, edit, allowed):
    return (before["id"] in allowed and before["state"] == "active" and before["kind"] == "edge"
            and {"id", "data", "rationale"}.issubset(edit)
            and not set(edit) - {"id", "kind", "title", "body", "data", "rationale"}
            and isinstance(edit["data"], dict)
            and changed_fields(before, edit) == ["data.to_node"])


def validate_insertions(before, after, edits, allowed):
    old = {e["id"]: e for e in before["entries"]}
    new = {e["id"]: e for e in after["entries"]}
    added_nodes = {key for key, e in new.items() if key not in old and e["kind"] == "node"}
    inserted = [edit for edit in edits if edit["id"] in old
                and insertion_edit(old[edit["id"]], edit, allowed)
                and (edit["data"]["to_node"] in added_nodes
                     or old[edit["id"]].get("reviewed") is True
                     or old[edit["id"]].get("evidence") in {"saved_graph", "manual", "user_source"})]
    if not added_nodes and not inserted:
        return
    used, permitted = set(), set()
    for edit in inserted:
        original = old[edit["id"]]
        audit_id = edit["data"]["to_node"]
        if audit_id not in added_nodes or audit_id in used:
            raise ValueError("交付前插入审查须使用本提案新增的独立 audit 节点，不能改接其它阶段。")
        audit = new[audit_id]
        data = audit["data"]
        if (data.get("node_kind") != "audit" or not audit["body"].strip()
                or not data.get("outputs") or not data.get("acceptance")
                or set(data) - {"node_kind", "inputs", "outputs", "acceptance", "steps", "review_channels"}):
            raise ValueError("新增审查只能填写审查职责、输入、产出、步骤、验收和证据通道；模型绑定留待确认。")
        source = old[original["data"]["from_node"]]["data"]
        if (not source.get("acceptance")
                or not set(source["acceptance"]).issubset(data["acceptance"])
                or not set(source.get("outputs", [])).issubset(data.get("inputs", []))):
            raise ValueError("新增审查必须逐项沿用原阶段验收，并读取原阶段全部产出，不能降低要求。")
        incoming = [e for e in new.values() if e["state"] != "retired" and e["kind"] == "edge"
                    and e["data"].get("to_node") == audit_id]
        outgoing = [e for e in new.values() if e["state"] != "retired" and e["kind"] == "edge"
                    and e["data"].get("from_node") == audit_id]
        if (len(incoming) != 1 or incoming[0]["id"] != original["id"] or len(outgoing) != 1
                or outgoing[0]["id"] in old
                or outgoing[0]["data"] != {"from_node": audit_id,
                    "to_node": original["data"]["to_node"], "condition": "pass"}):
            raise ValueError("审查必须插在原交付连线中，只能以 pass 连回原终点；不能保留旁路或更改原分支。")
        used.add(audit_id)
        permitted.update({original["id"], audit_id, outgoing[0]["id"]})
    if used != added_nodes or {edit["id"] for edit in edits} != permitted:
        raise ValueError("本提案只能插入宿主指定的交付前审查，不能混入其它节点、门禁或连线变更。")
