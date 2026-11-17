"""Business identities shared by extraction, diagnostics and host repair."""
from __future__ import annotations

import copy
from .draft_sources import digest


def business_key(entry):
    if entry.get("state") == "retired":
        return None
    kind, data = entry.get("kind"), entry.get("data") or {}
    if kind in {"input", "loop"}:
        value = data.get("name" if kind == "input" else "loop_id")
        return (kind, value) if isinstance(value, str) and value.strip() else None
    if kind == "node":
        value = data.get("work_key")
        node_kind, parent_id = data.get("node_kind", "implement"), data.get("parent_id")
        if (isinstance(value, str) and value.strip() and isinstance(node_kind, str)
                and (parent_id is None or isinstance(parent_id, str))):
            # Work identity is intentionally scope-bound: a reviewer and its
            # implementation, or same-key work under different parents, are
            # separate responsibilities.
            return ("work", value, node_kind, parent_id)
    if kind == "edge":
        return (kind, digest([data.get("from_node"), data.get("to_node"),
                             data.get("condition", "always"), data.get("input_when"),
                             data.get("branch_group", "")]))
    return None


def semantic_data(entry):
    data = copy.deepcopy(entry["data"])
    if entry["kind"] == "edge":
        data.setdefault("condition", "always")
        data.setdefault("input_when", None)
        data.setdefault("branch_group", "")
    elif entry["kind"] == "input":
        data.setdefault("required", True)
    elif entry["kind"] == "loop":
        from .workflow_draft import normalize_loop_data
        data = normalize_loop_data(data)
    elif entry["kind"] == "node" and data.get("work_key"):
        # A same-key merge is safe only when all business content survives
        # unchanged. Sources/evidence are merged separately by the host.
        return {"title": entry.get("title", ""), "body": entry.get("body", ""), "data": data}
    return data


def duplicate_groups(document):
    groups = {}
    for entry in document.get("entries", []):
        key = business_key(entry)
        if key:
            groups.setdefault(key, []).append(entry)
    return [{"id": digest(key), "kind": key[0], "key": (
                f"{entries[0]['data'].get('from_node')} → {entries[0]['data'].get('to_node')} ({entries[0]['data'].get('condition', 'always')})"
                if key[0] == "edge" else
                (f"{key[1]} (node_kind={key[2]}, parent_id={key[3] or 'top-level'})"
                 if key[0] == "work" else key[1])),
             "entry_ids": [e["id"] for e in entries],
             "identical": len({digest(semantic_data(e)) for e in entries}) == 1}
            for key, entries in groups.items() if len(entries) > 1]


def duplicate_diagnostics(document):
    labels = {"input": "运行参数", "loop": "循环", "edge": "依赖", "work": "工作身份"}
    return [{"code": "duplicate_work_key" if g["kind"] == "work" else "duplicate_business_key",
             "path": "entries", "entry_ids": g["entry_ids"],
             "group_id": g["id"], "message":
             (f"duplicate_work_key：{labels[g['kind']]} {g['key']} 重复定义："
              f"{'、'.join(g['entry_ids'])}；"
              + ("正文与业务字段完全相同，可合并" if g["identical"] else "内容有分歧，请先明确保留版本"))
             if g["kind"] == "work" else
             (f"{labels[g['kind']]}重复定义 {g['key']}：{'、'.join(g['entry_ids'])}；"
              + ("可合并相同定义" if g["identical"] else "内容有分歧，请选择保留版本"))}
            for g in duplicate_groups(document)]


def reject_new_duplicates(before, after, changed_ids):
    """Old damaged groups may survive, but no patch may grow or create one."""
    old = {g["id"]: set(g["entry_ids"]) for g in duplicate_groups(before)}
    for group in duplicate_groups(after):
        ids = set(group["entry_ids"])
        if ids & set(changed_ids) and not ids.issubset(old.get(group["id"], set())):
            from .workflow_draft import DraftPatchError
            if group["kind"] == "work":
                raise DraftPatchError(f"duplicate_work_key：工作身份 {group['key']} 已存在于 "
                                      + "、".join(group["entry_ids"])
                                      + "；请复用已有节点 ID，或先合并完全相同的副本",
                                      entry_id=next(iter(sorted(ids & set(changed_ids)))),
                                      path="data.work_key", code="duplicate_work_key",
                                      details={"group_id": group["id"], "entry_ids": group["entry_ids"],
                                               "work_key": group["key"]})
            raise DraftPatchError(f"duplicate_business_key：{group['kind']} {group['key']} 已存在于 "
                                  + "、".join(group["entry_ids"]) + "；请复用已有 ID，不要另建重复条目",
                                  code="duplicate_business_key",
                                  details={"group_id": group["id"], "entry_ids": group["entry_ids"]})


def append_seed_entries(entries, seeded):
    """A saved canvas may use different IDs for the same accepted definition."""
    result = copy.deepcopy(entries)
    ids = {e["id"] for e in entries}
    keys = {business_key(e): e for e in result if business_key(e)}
    has_goal = any(e["kind"] == "goal" for e in entries)
    replacements = {}
    for entry in seeded:
        entry = copy.deepcopy(entry)
        if entry["id"] in ids or has_goal and entry["kind"] == "goal":
            continue
        key = business_key(entry)
        if key in keys:
            if semantic_data(keys[key]) != semantic_data(entry):
                if key[0] == "work":
                    # Preserve both conflicting work rows for the duplicate
                    # review surface instead of silently choosing one.
                    result.append(entry)
                    ids.add(entry["id"])
                    continue
                from .workflow_draft import DraftPatchError
                raise DraftPatchError(f"画布与候选的 {key[0]} {key[1]} 定义不同，请核对后按新图重新生成；未写入重复定义。")
            if key[0] == "work":
                survivor = keys[key]
                replacements[entry["id"]] = survivor["id"]
                entry.update(state="retired", superseded_by=survivor["id"])
                result.append(entry)
                ids.add(entry["id"])
                survivor["sources"] = list({digest(ref): copy.deepcopy(ref)
                                            for ref in survivor.get("sources", [])
                                            + entry.get("sources", [])}.values())
            continue
        result.append(entry)
        ids.add(entry["id"])
        if key:
            keys[key] = entry
    if replacements:
        from .draft_dedupe import rewrite_references
        from .workflow_draft import DraftPatchError, check_entry_refs
        affected = set()
        before = {entry["id"]: copy.deepcopy(entry) for entry in result}
        for entry in result:
            if entry.get("state") == "retired":
                continue
            rewritten = rewrite_references(entry, replacements)
            if rewritten != entry:
                entry.update(rewritten)
                affected.add(entry["id"])
        for entry_id in affected:
            entry = next(item for item in result if item["id"] == entry_id)
            old_entry = before[entry_id]
            data, old_data = entry.get("data") or {}, old_entry.get("data") or {}
            if (entry.get("kind") == "edge" and data.get("from_node") == data.get("to_node")
                    and old_data.get("from_node") != old_data.get("to_node")):
                raise DraftPatchError(f"相同工作身份的画布/候选合并会使依赖 {entry_id} 成为自环；未合并",
                                      entry_id=entry_id, path="data", code="invalid_reference")
        if affected:
            try:
                check_entry_refs(result, focus=affected | set(replacements.values()),
                                 check_binding_reachability=False)
            except ValueError as exc:
                raise DraftPatchError(f"相同工作身份的画布/候选合并会留下无效引用；未合并：{exc}",
                                      path="entries", code="invalid_reference") from exc
    return result
