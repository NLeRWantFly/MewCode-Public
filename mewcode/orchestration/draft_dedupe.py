"""Deterministic, lossless duplicate retirement; no model or task execution."""
from __future__ import annotations

import copy
from .draft_keys import duplicate_groups
from .draft_sources import digest


def priority(entry):
    return (not bool(entry.get("reviewed")),
            {"user_source": 0, "saved_graph": 1, "manual": 2, "proposal": 3, "ai_completed": 4, "reported": 5}.get(entry.get("evidence"), 6),
            -len(entry.get("sources", [])), entry["id"])


def merged_sources(entries, survivor, retired_ids):
    return list({digest(ref): copy.deepcopy(ref) for entry_id in [survivor, *sorted(retired_ids)]
                 for ref in entries[entry_id].get("sources", [])}.values())


def rewrite_references(entry, replacements):
    """Return an entry with every host-owned cross-entry ID rewritten."""
    result = copy.deepcopy(entry)
    data = result.get("data") or {}

    def rewrite(value):
        return replacements.get(value, value) if isinstance(value, str) else value

    for field in ("node_id", "parent_id", "from_node", "to_node", "from", "to",
                  "entry_node_id", "check_node_id", "collect_at", "input_id",
                  "resolves_question_id"):
        if field in data:
            data[field] = rewrite(data[field])
    for field in ("node_ids", "reviewer_node_ids", "repair_node_ids", "required_node_ids"):
        values = data.get(field)
        if isinstance(values, list):
            rows = []
            for value in values:
                replacement = rewrite(value)
                if replacement not in rows:
                    rows.append(replacement)
            data[field] = rows
    bindings = data.get("input_bindings")
    if isinstance(bindings, dict):
        data["input_bindings"] = {name: rewrite(value) for name, value in bindings.items()}
    result["data"] = data
    return result


def apply_dedupe(document, choices=None):
    choices = choices or {}
    if not isinstance(choices, dict):
        raise ValueError("合并选择必须是组编号到保留条目编号的对象")
    groups = duplicate_groups(document)
    if set(choices) - {g["id"] for g in groups}:
        raise ValueError("重复组已变化，请重新预览")
    result = copy.deepcopy(document)
    entries = {e["id"]: e for e in result["entries"]}
    changes, unresolved, replacements = [], [], {}
    for group in groups:
        chosen = choices.get(group["id"])
        if chosen is not None and chosen not in group["entry_ids"]:
            raise ValueError("保留条目必须属于当前重复组")
        if chosen is None and (not group["identical"] or any(entries[i]["state"] != "active" for i in group["entry_ids"])):
            unresolved.append({**group, "entries": [copy.deepcopy(entries[i]) for i in group["entry_ids"]]})
            continue
        survivor = entries[chosen] if chosen else min((entries[i] for i in group["entry_ids"]), key=priority)
        if survivor["state"] != "active":
            raise ValueError("请先解决保留条目的冲突，再合并重复项")
        for entry_id in group["entry_ids"]:
            if entry_id == survivor["id"]:
                continue
            entry = entries[entry_id]
            before = copy.deepcopy(entry)
            entry.update(state="retired", superseded_by=survivor["id"])
            replacements[entry_id] = survivor["id"]
            changes.append({"entry_id": entry_id, "title": entry["title"],
                            "reason": f"合并重复 {group['kind']}，保留 {survivor['id']}；原内容和来源留存。",
                            "before": before, "after": copy.deepcopy(entry)})
    for survivor in dict.fromkeys(replacements.values()):
        entry = entries[survivor]
        refs = merged_sources(entries, survivor, [i for i, target in replacements.items() if target == survivor])
        if refs != entry["sources"]:
            before = copy.deepcopy(entry)
            entry["sources"] = refs
            changes.append({"entry_id": survivor, "title": entry["title"], "reason": "保留所有重复副本的来源引文",
                            "before": before, "after": copy.deepcopy(entry)})
    # Rewire active references only. Retired rows retain their original
    # evidence and data as an audit trail of the merged copy.
    rewired_ids = set()
    for entry in entries.values():
        if entry["state"] == "retired":
            continue
        before = copy.deepcopy(entry)
        rewritten = rewrite_references(entry, replacements)
        if rewritten != entry:
            entry.update(rewritten)
            rewired_ids.add(entry["id"])
            changes.append({"entry_id": entry["id"], "title": entry["title"],
                            "reason": "引用跟随合并后的保留条目",
                            "before": before, "after": copy.deepcopy(entry)})

    work_replacements = {entry_id: target for entry_id, target in replacements.items()
                         if entries[entry_id]["kind"] == "node"}
    if work_replacements:
        old_entries = {e["id"]: e for e in document.get("entries", [])}
        for entry in entries.values():
            if entry["state"] == "retired":
                continue
            data = entry.get("data") or {}
            old = old_entries.get(entry["id"], {})
            old_data = old.get("data") or {}
            if (entry["kind"] == "edge" and data.get("from_node") == data.get("to_node")
                    and old_data.get("from_node") != old_data.get("to_node")):
                raise ValueError(f"合并工作身份会使依赖 {entry['id']} 成为自环；未执行合并")
            if (entry["kind"] == "node" and data.get("parent_id") == entry["id"]
                    and old_data.get("parent_id") != entry["id"]):
                raise ValueError(f"合并工作身份会使节点 {entry['id']} 成为自己的父节点；未执行合并")
            bindings = data.get("input_bindings") or {}
            old_bindings = old_data.get("input_bindings") or {}
            if any(target == entry["id"] and old_bindings.get(name) != entry["id"]
                   for name, target in bindings.items()):
                raise ValueError(f"合并工作身份会使节点 {entry['id']} 的输入绑定到自身；未执行合并")
        if rewired_ids:
            from .workflow_draft import check_entry_refs
            try:
                check_entry_refs(entries.values(), focus=rewired_ids | set(work_replacements.values()))
            except ValueError as exc:
                raise ValueError(f"合并工作身份会留下无效引用，未执行合并：{exc}") from exc
    return result, changes, unresolved


def verify_retirements(before, after):
    """Recognize only exact duplicate retirements, never arbitrary deletion."""
    groups = duplicate_groups(before)
    old = {e["id"]: e for e in before["entries"]}
    new = {e["id"]: e for e in after["entries"]}
    if old.keys() != new.keys():
        return False
    replacements = {}
    group_ids = [set(group["entry_ids"]) for group in groups]
    for entry_id, entry in old.items():
        updated = new[entry_id]
        target = updated.get("superseded_by")
        if (entry.get("state") != "retired"
                and updated == {**entry, "state": "retired", "superseded_by": target}
                and new.get(target, {}).get("state") == "active"
                and any({entry_id, target}.issubset(group) for group in group_ids)):
            replacements[entry_id] = target
    if not replacements:
        return False

    expected = copy.deepcopy(old)
    for entry_id, target in replacements.items():
        expected[entry_id].update(state="retired", superseded_by=target)
    for survivor in dict.fromkeys(replacements.values()):
        retired_ids = [entry_id for entry_id, target in replacements.items() if target == survivor]
        expected[survivor]["sources"] = merged_sources(old, survivor, retired_ids)
    for entry in expected.values():
        if entry["state"] != "retired":
            rewritten = rewrite_references(entry, replacements)
            entry.update(rewritten)
    return expected == new
