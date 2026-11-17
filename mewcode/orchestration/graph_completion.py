"""Apply bounded AI graph completions without changing protected user facts."""
from __future__ import annotations

import copy
import hashlib
import json

from .workflow_draft import DraftPatchError, check_entry_refs, entry_contract, validate_entry

_LOCKED_EVIDENCE = {"user_source", "saved_graph", "manual"}
_LIST_FIELDS = {"inputs", "outputs", "acceptance", "steps"}
_LOOP_MEMBERS = {"node_ids", "reviewer_node_ids", "repair_node_ids"}
_LOOP_WIRING = {"entry_node_id", "check_node_id", *_LOOP_MEMBERS,
                "requires_repair", "repair_dispatch", "mode", "items"}
_LOOP_POLICY = {"exit_condition", "criteria", "control_mode"}
_BUDGET = {"max_rounds", "token_budget", "time_budget_seconds", "max_iterations",
           "timeout_seconds", "max_stalled_rounds", "node_max_iterations",
           "reviewer_max_iterations"}
_TOP_LEVEL = {"id", "kind", "state", "title", "body", "sources", "evidence",
              "reviewed", "data", "rationale", "resolve_conflict"}


def _locked(entry):
    return entry.get("evidence") in _LOCKED_EVIDENCE or entry.get("reviewed") is True


def can_resolve_proposed_conflict(entry) -> bool:
    """Only an unconfirmed assistant edge can be clarified automatically."""
    return (entry.get("state") == "conflict" and entry.get("kind") == "edge"
            and entry.get("evidence") == "proposal" and not _locked(entry))


def completion_contract() -> dict:
    """Start from the shared entry schema and apply completion's narrower policy."""
    contract = entry_contract()
    contract.update({
        "upsert_fields": ["id", "kind", "title", "body", "data", "rationale", "resolve_conflict"],
        "new_entry_kinds": ["node", "edge", "loop", "decision", "input", "question"],
        "do_not_write": ["state", "sources", "evidence", "reviewed"],
        "data_fields": {kind: sorted(set(fields) - _BUDGET if kind == "loop" else fields)
                        for kind, fields in contract["data_fields"].items()},
        "new_loop_required": ["loop_id", "entry_node_id", "check_node_id", "node_ids",
                              "reviewer_node_ids", "exit_condition", "criteria", "control_mode"],
        "new_loop_rules": ["control_mode=team，不设置预算、超时或返修轮数",
                           "node_ids包含入口、判定和全部审查节点；reviewer_node_ids包含check_node_id",
                           "移除loop回边后的图无环；循环只从入口进入、从判定的pass/always边退出",
                           "不存在continue_condition、budget、back_edge_ids；edge.data也没有loop_id",
                           "沿用既有验收要求，不虚构通过结论；人工签认仍由用户完成"],
        "conflict_resolution": "仅editable.resolve_conflict允许的未确认建议边可写resolve_conflict=true；必须实质修订body或data，保留来源，不得仅清除状态。用户原话及已复核条目不可自动裁决。",
    })
    types = contract["field_types"]
    types["node"]["node_kind"] = [kind for kind in types["node"]["node_kind"] if kind != "gate"]
    types["loop"]["control_mode"] = ["team"]
    types["loop"]["criteria"] = [{"criterion_id": "非空且唯一的编号",
                                  "acceptance_test": "非空验收方法"}]
    types["decision"]["category"] = ["designed", "defaulted"]
    types["decision"]["resolves_question_id"] = "仅 technical 类型问题的ID；不能替用户裁决 business 问题"
    types["decision"]["value"] = "字符串，最多500字；较长上下文写入body，不能用数组或对象"
    types["question"]["options"] = "2–6 个 {value,label,description,recommended} 方案；只给业务选择，推荐不等于用户同意"
    return contract


def protected_view(entry) -> dict:
    """Return fields that must remain byte-for-byte equal for locked entries."""
    result = {key: copy.deepcopy(entry.get(key)) for key in
              ("id", "kind", "state", "title", "body", "sources", "evidence")}
    result["reviewed"] = bool(entry.get("reviewed"))
    data = entry.get("data") or {}
    kind = entry.get("kind")
    if kind == "node":
        result["data"] = (copy.deepcopy(data) if data.get("node_kind") == "gate" else
                          {key: copy.deepcopy(data[key]) for key in ("node_kind", "parent_id", "execution_mode", "review_channels") if key in data})
    elif kind == "edge":
        result["data"] = {key: copy.deepcopy(data[key]) for key in ("from_node", "to_node") if key in data}
    elif kind == "loop":
        result["data"] = {key: copy.deepcopy(value) for key, value in data.items()
                          if key not in _LOOP_MEMBERS}
    else:
        result["data"] = copy.deepcopy(data)
    return result


def protected_violations(before, after) -> list[dict]:
    """Report forbidden changes to a previously locked entry."""
    if not _locked(before):
        return []
    entry_id = before.get("id", "")
    failures = []

    def add(field, detail):
        failures.append({"entry_id": entry_id, "field": field, "code": "locked_content",
                         "message": detail})

    old_view, new_view = protected_view(before), protected_view(after)
    for key in old_view:
        if key == "data":
            for field in set(old_view["data"]) | set(new_view["data"]):
                if ((field in old_view["data"]) != (field in new_view["data"]) or
                        old_view["data"].get(field) != new_view["data"].get(field)):
                    add("data." + field, f"锁定条目 {entry_id} 的 {field} 不可改动")
        elif old_view[key] != new_view.get(key):
            add(key, f"锁定条目 {entry_id} 的 {key} 不可改动")
    old_data, new_data = before.get("data") or {}, after.get("data") or {}
    if before.get("kind") == "node" and old_data.get("node_kind") != "gate":
        if old_data.get("model") and old_data["model"] != new_data.get("model"):
            add("data.model", f"锁定节点 {entry_id} 的已有模型绑定不可改动")
        for field in sorted(_LIST_FIELDS):
            if old_data.get(field) and old_data.get(field) != new_data.get(field):
                add("data." + field, f"锁定节点 {entry_id} 的非空 {field} 不可改动")
    elif before.get("kind") == "edge":
        if new_data.get("condition", "always") not in {old_data.get("condition", "always"), "loop"}:
            add("data.condition", f"锁定依赖 {entry_id} 只能把条件改为 loop")
    elif before.get("kind") == "loop":
        for field in sorted(_LOOP_MEMBERS):
            if not set(old_data.get(field) or []).issubset(set(new_data.get(field) or [])):
                add("data." + field, f"锁定循环 {entry_id} 的成员只能增加")
    return failures


def _reject(entry_id, code, message):
    return {"entry_id": entry_id, "code": code, "message": message}


def _audit_entry(before, after, code, rationale):
    change = json.dumps([before["id"], code, after], ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"))
    audit_id = "audit_" + hashlib.sha256(change.encode("utf-8")).hexdigest()[:16]
    target = (before["id"] if before["kind"] == "node" else
              after["data"].get("to_node") if before["kind"] == "edge" else
              after["data"].get("entry_node_id"))
    old_data, new_data = before["data"], after["data"]
    details = []
    for key in ("state", "title", "body"):
        if before.get(key) != after.get(key):
            details.append(f"{key}: {str(before.get(key, ''))[:70]} → {str(after.get(key, ''))[:70]}")
    for key in new_data:
        old_value, new_value = old_data.get(key), new_data.get(key)
        if old_value == new_value:
            continue
        if isinstance(old_value, list) and isinstance(new_value, list):
            added = [value for value in new_value if value not in old_value]
            detail = f"{key}: {len(old_value)}项 → {len(new_value)}项；新增 {json.dumps(added[:2], ensure_ascii=False)[:60]}"
        else:
            old_text = json.dumps(old_value, ensure_ascii=False)[:70]
            new_text = json.dumps(new_value, ensure_ascii=False)[:70]
            detail = f"{key}: {old_text} → {new_text}"
        details.append(detail)
    summary = "；".join(details)
    if len(summary) > 500:
        summary = summary[:497] + "..."
    return {"id": audit_id, "kind": "decision", "title": "AI 补全：" + before["title"][:165],
            "body": f"{before['id']} · {code}：{summary}", "state": "active",
            "data": {"category": "designed", "value": summary, "node_id": target},
            "sources": [], "evidence": "ai_completed", "rationale": rationale}


def _new_entry(entry, existing, nodes, loop_ids):
    kind, data = entry["kind"], entry["data"]
    if entry["state"] != "active":
        return "delete_forbidden", "新条目必须处于 active 状态"
    if kind == "node":
        if data.get("node_kind") == "gate":
            return "gate_forbidden", "AI 补全不得新增人工确认节点"
        return "node_added", ""
    if kind == "edge":
        if (not isinstance(data.get("from_node"), str) or not isinstance(data.get("to_node"), str) or
                data["from_node"] not in nodes or data["to_node"] not in nodes):
            return "bad_reference", "新增依赖的端点必须是已存在的 active 节点"
        if data["from_node"] == data["to_node"]:
            return "bad_reference", "新增依赖不能连接节点自身"
        return "edge_added", ""
    if kind == "loop":
        loop_id = data.get("loop_id")
        if not isinstance(loop_id, str) or not loop_id.strip() or len(loop_id) > 80:
            return "not_whitelisted", "新增循环必须有 1–80 字的 loop_id"
        if loop_id in loop_ids:
            return "duplicate_id", "新增循环的 loop_id 已存在"
        if set(data) & _BUDGET:
            return "budget_forbidden", "新增循环不得包含预算或超时字段"
        if data.get("control_mode") != "team":
            return "loop_policy_forbidden", "新增循环的 control_mode 必须为 team"
        if (not isinstance(data.get("entry_node_id"), str) or not isinstance(data.get("check_node_id"), str) or
                data["entry_node_id"] not in nodes or data["check_node_id"] not in nodes):
            return "bad_reference", "新增循环的入口与判定必须是已存在的 active 节点"
        members = data.get("node_ids") or []
        reviewers = data.get("reviewer_node_ids") or []
        if (not isinstance(members, list) or not isinstance(reviewers, list) or
                not {data["entry_node_id"], data["check_node_id"], *reviewers}.issubset(set(members))):
            return "bad_reference", "新增循环体必须包含入口、判定及全部复核节点"
        if not isinstance(data.get("exit_condition"), str) or not data["exit_condition"].strip():
            return "loop_policy_forbidden", "新增循环必须有非空退出条件"
        criteria = data.get("criteria")
        if (not isinstance(criteria, list) or not criteria or any(
                not isinstance(item, dict) or set(item) != {"criterion_id", "acceptance_test"}
                or not isinstance(item.get("criterion_id"), str) or not item["criterion_id"].strip()
                or not isinstance(item.get("acceptance_test"), str) or not item["acceptance_test"].strip()
                for item in criteria)):
            return "loop_policy_forbidden", "新增循环必须有非空验收标准对象数组"
        return "loop_added", ""
    if kind == "decision":
        if data.get("category") not in {"designed", "defaulted"}:
            return "not_whitelisted", "新增裁决必须给出 category"
        if data.get("resolves_question_id"):
            question = existing.get(data["resolves_question_id"], {})
            if question.get("kind") != "question" or question.get("data", {}).get("issue_type") != "technical":
                return "business_decision_forbidden", "AI 只能收束已分类的技术问题，不能替用户确认业务选择"
        return "decision_added", ""
    if kind == "input":
        name = data.get("name")
        taken = {str(e["data"].get("name") or "").strip().casefold()
                 for e in existing.values() if e["kind"] == "input" and e["state"] == "active"}
        if isinstance(name, str) and name.strip().casefold() in taken:
            return "duplicate_id", "运行参数名称已存在"
        return "input_added", ""
    if kind == "question":
        return "question_added", ""
    return "not_whitelisted", "AI 补全不得新增此类条目"


def _modified_entry(before, after, *, resolve_conflict=False):
    kind, old, new = before["kind"], before["data"], after["data"]
    if kind == "question" and old.get("issue_type") == "business" and new.get("issue_type") != "business":
        return "business_decision_forbidden", "不能通过改分类绕过待决业务选择"
    if kind == "node" and old.get("node_kind") == "gate":
        return "gate_forbidden", "人工确认节点不可由 AI 修改"
    if kind == "node" and old.get("model") and old["model"] != new.get("model"):
        return "locked_content", "已有模型绑定不可由自动补全替换；请在画布明确调整"
    if _locked(before):
        violations = protected_violations(before, after)
        if violations:
            return "locked_content", violations[0]["message"]
        if kind == "node":
            return "list_filled", ""
        if kind == "edge":
            return "condition_to_loop", ""
        if kind == "loop":
            return "loop_members_extended", ""
        return "locked_content", "此类锁定条目不可修改"
    changed_data = {key for key in set(old) | set(new)
                    if (key in old) != (key in new) or old.get(key) != new.get(key)}
    changed_text = before["title"] != after["title"] or before["body"] != after["body"]
    if kind == "node":
        allowed = _LIST_FIELDS | {"model"} | ({"node_kind", "parent_id"} if before.get("evidence") == "ai_completed" else set())
        if changed_data - allowed or (changed_text and before.get("evidence") != "ai_completed"):
            return "not_whitelisted", "此节点的改动不在自动补全白名单内"
        if new.get("node_kind") == "gate":
            return "gate_forbidden", "AI 补全不得把节点改为 gate"
        if before.get("evidence") != "ai_completed" and any(old.get(key) and old.get(key) != new.get(key)
                                                           for key in _LIST_FIELDS):
            return "not_whitelisted", "已有的非空节点列表不可改动"
        return "own_entry_updated" if before.get("evidence") == "ai_completed" else "list_filled", ""
    if kind == "edge":
        if resolve_conflict and can_resolve_proposed_conflict(before):
            return ("proposed_conflict_resolved", "") if (
                before["title"] == after["title"] and changed_data <= {"from_node", "to_node", "condition"}
                and (before["body"] != after["body"] or changed_data)) else (
                "conflict_resolution_forbidden", "建议边的冲突必须实质修订说明或端点/条件，不能只清除状态或更名")
        return ("own_entry_updated", "") if not changed_text and changed_data <= {"from_node", "to_node", "condition", "input_when", "branch_group"} else (
            "not_whitelisted", "此依赖只能修改端点与声明条件")
    if kind == "loop":
        if changed_text:
            return "not_whitelisted", "循环名称和说明不可改动"
        if before.get("evidence") == "ai_completed":
            if set(new) & _BUDGET:
                return "budget_forbidden", "AI 循环不得包含预算或超时字段"
            if new.get("control_mode") != "team":
                return "loop_policy_forbidden", "AI 循环的 control_mode 必须为 team"
            allowed = _LOOP_WIRING | _LOOP_POLICY
        else:
            allowed = _LOOP_WIRING
        if changed_data - allowed:
            return "loop_policy_forbidden", "此循环的退出标准、控制模式或预算不可修改"
        return "own_entry_updated", ""
    if kind in {"decision", "question", "input"} and before.get("evidence") == "ai_completed":
        return "own_entry_updated", ""
    return "not_whitelisted", "此类条目不可由 AI 修改"


def apply_completion(document, proposal):
    """Apply each permitted upsert in memory and return applied/rejected receipts."""
    result = copy.deepcopy(document)
    entries = {entry["id"]: entry for entry in result.get("entries", [])}
    original_ids = set(entries)
    loop_ids = {e["data"].get("loop_id") for e in entries.values()
                if e["kind"] == "loop" and isinstance(e["data"].get("loop_id"), str)}
    applied, rejected, seen = [], [], set()
    if not isinstance(proposal, dict) or not isinstance(proposal.get("upserts"), list):
        return result, applied, [_reject("", "not_whitelisted", "提案必须包含 upserts 数组")]
    for deletion in proposal.get("deletes", []):
        entry_id = deletion if isinstance(deletion, str) else deletion.get("id", "") if isinstance(deletion, dict) else ""
        rejected.append(_reject(entry_id, "delete_forbidden", "自动补全不得删除条目"))
    upserts = proposal["upserts"]
    # New nodes are settled first so edges and loops in the same proposal may
    # reference them; receipts are still reported in the proposal's own order.
    ordered = sorted(upserts, key=lambda raw: 0 if (
        isinstance(raw, dict) and raw.get("kind") == "node" and isinstance(raw.get("id"), str)
        and raw["id"] not in entries) else 1)
    for raw in ordered:
        entry_id = raw.get("id", "") if isinstance(raw, dict) else ""
        if not isinstance(raw, dict) or not isinstance(entry_id, str):
            rejected.append(_reject(str(entry_id), "not_whitelisted", "补全条目必须是对象并带 ID"))
            continue
        if entry_id in seen:
            rejected.append(_reject(entry_id, "duplicate_id", "同一提案重复修改条目"))
            continue
        seen.add(entry_id)
        old = entries.get(entry_id)
        if raw.get("op") == "delete" or raw.get("state") == "retired":
            rejected.append(_reject(entry_id, "delete_forbidden", "自动补全不得删除或撤回条目"))
            continue
        rationale = raw.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 1000:
            rejected.append(_reject(entry_id, "missing_rationale", "每项 AI 补全必须带 1–1000 字的理由"))
            continue
        if set(raw) - _TOP_LEVEL:
            rejected.append(_reject(entry_id, "not_whitelisted", "提案包含白名单之外的条目字段"))
            continue
        resolve_conflict = raw.get("resolve_conflict", False)
        if not isinstance(resolve_conflict, bool) or resolve_conflict and (
                old is None or not can_resolve_proposed_conflict(old)):
            rejected.append(_reject(entry_id, "conflict_resolution_forbidden",
                                    "只能修订未确认、未复核的建议边冲突；用户要求和已确认条目保持锁定"))
            continue
        if old:
            if any(key in raw and raw[key] != old.get(key) for key in
                   ("kind", "state", "sources", "evidence", "reviewed")):
                rejected.append(_reject(entry_id, "not_whitelisted", "既有条目的类型、来源、状态和复核标记不可改"))
                continue
            if "data" in raw and not isinstance(raw["data"], dict):
                rejected.append(_reject(entry_id, "not_whitelisted", "data 必须是对象"))
                continue
            candidate = copy.deepcopy(old)
            candidate.update({key: copy.deepcopy(raw[key]) for key in ("title", "body") if key in raw})
            candidate["data"] = {**old["data"], **copy.deepcopy(raw.get("data", {}))}
            if resolve_conflict:
                candidate["state"] = "active"
            if not _locked(old):
                candidate["rationale"] = rationale
        else:
            if entry_id in original_ids:
                rejected.append(_reject(entry_id, "duplicate_id", "条目 ID 已存在"))
                continue
            if raw.get("reviewed") or raw.get("sources"):
                rejected.append(_reject(entry_id, "not_whitelisted", "AI 补全条目不得自带复核标记或来源引用"))
                continue
            candidate = copy.deepcopy(raw)
            candidate["evidence"] = "ai_completed"
            candidate.setdefault("state", "active")
            candidate.pop("resolve_conflict", None)
        try:
            validated = validate_entry(candidate)
            candidate.update(validated)
        except (DraftPatchError, ValueError, TypeError) as exc:
            rejected.append(_reject(entry_id, "not_whitelisted", str(exc)))
            continue
        if old:
            if all(old.get(key) == candidate.get(key) for key in ("kind", "title", "body", "data", "state")):
                rejected.append(_reject(entry_id, "not_whitelisted", "提案没有实际变更"))
                continue
            code, error = _modified_entry(old, candidate, resolve_conflict=resolve_conflict)
        else:
            active_nodes = {i for i, e in entries.items() if e["kind"] == "node" and e["state"] == "active"}
            code, error = _new_entry(candidate, entries, active_nodes, loop_ids)
        if error:
            rejected.append(_reject(entry_id, code, error))
            continue
        audit = _audit_entry(old, candidate, code, rationale) if old and (_locked(old) or resolve_conflict) else None
        if audit:
            try:
                audit.update(validate_entry(audit))
            except DraftPatchError as exc:
                rejected.append(_reject(entry_id, "not_whitelisted", str(exc)))
                continue
            if audit["id"] in entries and entries[audit["id"]] != audit:
                rejected.append(_reject(entry_id, "duplicate_id", "审计记录 ID 与已有条目冲突"))
                continue
        trial = dict(entries)
        trial[entry_id] = candidate
        from .draft_keys import reject_new_duplicates
        try:
            reject_new_duplicates({"entries": list(entries.values())}, {"entries": list(trial.values())}, {entry_id})
        except DraftPatchError as exc:
            rejected.append(_reject(entry_id, "duplicate_business_key", str(exc)))
            continue
        if audit:
            trial[audit["id"]] = audit
        try:
            check_entry_refs(trial.values(), focus={entry_id, audit["id"]} if audit else {entry_id})
        except (DraftPatchError, ValueError, TypeError) as exc:
            rejected.append(_reject(entry_id, "bad_reference", str(exc)))
            continue
        if old and _locked(old) and protected_violations(old, candidate):
            rejected.append(_reject(entry_id, "locked_content", "后置守卫发现锁定内容被修改"))
            continue
        entries = trial
        if not old and candidate["kind"] == "loop":
            loop_ids.add(candidate["data"]["loop_id"])
        applied.append({"entry_id": entry_id, "op": "modify" if old else "add", "code": code,
                        "before": copy.deepcopy(old), "after": copy.deepcopy(candidate),
                        "audit_entry_id": audit["id"] if audit else None})
    position = {}
    for index, raw in enumerate(upserts):
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            position.setdefault(raw["id"], index)
    applied.sort(key=lambda receipt: position.get(receipt["entry_id"], -1))
    rejected.sort(key=lambda receipt: position.get(receipt["entry_id"], -1))
    result["entries"] = list(entries.values())
    return result, applied, rejected
