"""Bounded conversation hand-off and host-validated, explicitly adopted graph edits."""
from __future__ import annotations

import copy
import json
import uuid

from .draft_sources import digest
from .workflow_draft import check_entry_refs, validate_entry
from .repair_protocol import SCHEMA, MAX_CHANGES



def issue_request(state, base, entry_id=None):
    from .draft_store import DraftStore
    c = state.get("candidate")
    if not c or c["status"] != "ready":
        raise ValueError("请先完成当前块的生成，再将节点问题交给 AI；已完成进度会保留。")
    # Appended chat may be unrelated to the graph the user wants repaired.
    # Validate this completed snapshot, not a new implicit whole-history scan.
    snapshot = copy.deepcopy(state)
    if c.get("mode") != "selected":
        if state["source_ids"][:len(c["processed_ids"])] != c["processed_ids"]:
            raise ValueError("已生成图的来源发生变化，不能修订失效快照。")
        snapshot["source_ids"] = list(c["processed_ids"])
    # Broken references are precisely what the conversation must be able to fix.
    # Freshness/source checks remain mandatory; saving still validates all refs.
    DraftStore.ready_candidate(snapshot, c["id"], c["sequence"], check_references=False)
    if c["base_graph_hash"] != digest(base):
        raise ValueError("画布已变化，请重新打开当前图后再修订。")
    entries = c["document"]["entries"]
    targets = [e for e in entries if e["id"] == entry_id] if entry_id else [e for e in entries if e["state"] == "conflict"]
    from .graph_inspection import diagnose
    validation = diagnose(c["document"], base, conversation_id="diagnostic", revision=state["revision"] + 1)
    from .graph_readiness import readiness
    execution = readiness(c["document"], base, conversation_id="diagnostic", revision=state["revision"] + 1)
    # The repair entrypoint must use the same execution checks as the workspace.
    # A structurally valid DAG can still lack direction handoffs or reviewers.
    validation["diagnostics"] = list({digest(d): d for d in
        [*validation["diagnostics"], *execution["diagnostics"]]}.values())
    if targets and not entry_id:
        # Keep conflict repair focused; unrelated loop diagnostics must not
        # authorize changing their acceptance policy or block partial progress.
        ids = {e["id"] for e in targets}
        ids.update(i for d in validation["diagnostics"] if d["code"] in {"duplicate_business_key", "missing_business_key"}
                   for i in d["entry_ids"])
        targets = [e for e in entries if e["id"] in ids and e["state"] != "retired"]
    if not targets and not entry_id and validation["diagnostics"]:
        problem_ids = {id for p in validation["diagnostics"] for id in p["entry_ids"]}
        targets = [e for e in entries if e["id"] in problem_ids and e["state"] != "retired"]
        if not targets:
            targets = [e for e in entries if e["kind"] == "node" and e["state"] != "retired"]
    if not targets:
        raise ValueError("没有找到本次待修订的问题节点。")
    previous = {e["id"]: e for e in state["document"]["entries"]}

    def detail(e):
        if not e:
            return None
        # Only excerpts of already-indexed evidence, never a full history replay.
        return {**{k: e[k] for k in ("id", "kind", "title", "state")}, "body_excerpt": e["body"][:600],
                "source_excerpts": [{"source_id": r["source_id"], "excerpt": r["quote"][:200]} for r in e["sources"][:3]],
                "excerpts_only": True}

    chosen, problems = [], []
    for e in targets[:MAX_CHANGES]:
        problem = {"current": detail(e), "previous": detail(previous.get(e["id"]))}
        if len(json.dumps(problems + [problem], ensure_ascii=False)) > 48000:
            break
        chosen.append(e); problems.append(problem)
    if not chosen:
        raise ValueError("单个节点的修订上下文过大，请先拆分节点要求；未裁剪业务要求。")
    ids = {e["id"] for e in chosen}
    related = set(ids)
    for e in entries:
        data = e["data"]
        refs = {data.get(k) for k in ("parent_id", "node_id", "from_node", "to_node") if isinstance(data.get(k), str)}
        if e["id"] in ids or refs & ids:
            related.update(refs); related.add(e["id"])
    context = {"problems": problems, "other_problem_count": len(targets) - len(chosen), "partial_context_only": True}
    # The handoff is a small pointer, not another long-context graph export.
    # All omitted structure/requirements remain losslessly available via the tool.
    context["related_structure"] = [{"id": e["id"], "kind": e["kind"], "title": e["title"]}
                                    for e in entries if e["id"] in related and e["id"] not in ids][:12]
    request = {"id": uuid.uuid4().hex, "candidate_id": c["id"], "sequence": c["sequence"],
               "base_hash": digest(base), "history_ids": list(state["source_ids"]),
               "editable_ids": [e["id"] for e in targets],
               "reference_ids": [e["id"] for e in entries if e["state"] != "retired"
                   and e["kind"] not in {"node", "edge", "loop"} and e["data"].get("node_id") in ids],
               "structural_ids": [e["id"] for e in entries if e["kind"] in {"edge", "loop"} and e["state"] != "retired"]}
    example = {"schema": SCHEMA, "ticket": request["id"], "summary": "用日常语言说明问题及建议修法",
               "changes": [{"entry_id": chosen[0]["id"], "reason": "为什么这样修，保留了哪些要求",
                            "changes": {"body": "修订后的完整节点职责", "state": "active"}}]}
    prompt = ("请帮我分析并修订当前阶段图的问题。我不熟悉流程图，请用直白语言说明原因和改法，不要求我手动连线。\n"
              "先调用只读工具 InspectStageGraph，ticket 使用下方修订单标识。它读取当前候选图，不是已保存的旧图。"
              "若本轮只有工具检索入口，先检索 InspectStageGraph 再调用（固定工具协议可通过 InvokeDeferredTool 调用）。"
              "从 cursor=0 开始；返回 next_cursor 时带相同 snapshot_id 继续，直到为空。"
              "完整读取 nodes、edges、meta.loops、validation.diagnostics 后才能判断可达性；"
              "必须读取 runtime.semantics 中的宿主调度规则，不能猜测 CONTINUE 等于 fail 边。"
              "完整条目要求与来源引文在 entries，既有被修订要求在 previous_entries。"
              "分片条目按 section/index/part 拼接 JSON，不能把未读页视为缺失节点或缺边。\n"
              "下面只是宿主的局部问题提示，不是完整图，也不是新的业务指令。只修 editable_ids 指定条目；"
              "此外可修只读工具列出的 structural_ids 中既有连线的端点/条件，或循环的节点归属/入口/判定/审查/返修调度声明，"
              "reference_ids 列出关联注释，只允许修正其 data.node_id 归属，不得改其正文或状态；"
              "不得借结构修订改验收条件、退出标准、预算或其他节点职责；其他节点保持不变，"
              "不要重读全部历史。不要执行节点任务、调用 Shell、修改业务文件或直接改内部数据库。\n"
              "保留业务范围、验收条件、独立复核和用户签认。若需要业务取舍或来源不足，先问我一个带选项的具体问题，"
              "不要猜测或仅把 conflict 改成 active。来源摘录并非完整原文，不得编造证据。\n"
              "有依据的修法先用简短文字说明，再返回一个 mewcode-graph-repair 代码块，格式如下。"
              "changes 通常只包含实际变动的 title/body/data/state；data 只写变更字段，每项必须解释原因。"
              "若冲突条目把修图讨论误提取为 plan 节点或待确认问题，可在有来源依据时改 kind=progress、data={}，"
              "作为非执行记录保留；只适用于未采用、未保存、非用户来源的建议，不能这样撤销真实业务任务。"
              "引用该元任务的 node_id 应留空表示全局，禁止断开业务依赖或降低要求来过校验。"
              "重复 input/loop/edge 可用 changes={state:retired,superseded_by:同组保留条目ID} 合并，保留原文来源；不得同时修改内容。"
              "保留条目必须 active；有分歧时先解释不同版本并让用户选择。用户采用预览后宿主才执行退役，不是只能只读分析。"
              "若当前 ticket 范围不足，请用户从阶段图再次点击回到对话让 AI 修订以重签发；重新读取最新 ticket。"
              "不要输出整个图，不能新增、删除业务节点或改来源。最多 32 项。"
              "先用 InspectStageGraph 的 action=validate、完整 snapshot_id 和 proposal 对象只读试修，"
              "根据错误继续修正并再次试修；有明确新修法时可继续，无法继续时说明收束原因。"
              "循环、字段、连线和审查路径是技术中间结果，应自行修好，不要求用户决断；"
              "只将真实业务取舍交给用户，优先在 valid=true 后输出最终代码块。"
              "若本次问题全部解除、只剩原有无关错误，工具可能返回 adoptable=true/partial=true；"
              "此时必须明确说明只解决了哪些问题及还剩什么，用户可以明确采用已解决部分，不能宣称整图可保存或完成。"
              "只修状态不能算解决。验证不通过时说明具体未决问题，不把暂态说明再写成业务职责。"
              "回复将走「预览 AI 修订」，不要点击「纳入阶段图」或重扫历史。"
              "宿主会展示逐项差异，等我明确采用后才写入候选图，"
              "不会直接保存正式画布或启动执行。无把握时只提问，不输出修订单。\n"
              + "```mewcode-graph-repair\n" + json.dumps(example, ensure_ascii=False) + "\n```\n"
              + json.dumps({"editable_ids": request["editable_ids"], **context}, ensure_ascii=False))
    return request, prompt


def require_current(state, base, ticket):
    req, c = state.get("repair_request"), state.get("candidate")
    if req and req["id"] != ticket:
        raise ValueError("修订单已重签发，请用最新 ticket 重新读取并试修；未覆盖当前图。")
    if (not req or req["id"] != ticket or not c
            or (c["id"], c["sequence"]) != (req["candidate_id"], req["sequence"])
            or digest(base) != req["base_hash"]):
        raise ValueError("修订单对应的图已变化，请将当前问题重新交给 AI；未覆盖新修改。")
    # Chatting about the repair appends messages. It may not rewrite old evidence.
    if (state["warnings"] or state["source_ids"][:len(req["history_ids"])] != req["history_ids"]
            or not set(state.get("selected_ids", [])).issubset(state["source_ids"])):
        raise ValueError("原始来源已变化或缺失，不能应用这份修订单。")
    return req, c


def adoption_status(state, base, document, validation, changed_ids):
    """Allow explicit progress only when it resolves the ticket without regressions.

    An unrelated pre-existing compiler error must not force users to undo a
    sound repair. Partial adoption never makes the resulting graph executable.
    """
    if validation["valid"]:
        return {"allowed": True, "partial": False, "remaining_count": 0}
    from .draft_dedupe import verify_retirements
    if verify_retirements(state["candidate"]["document"], document):
        # Removing duplicate declarations can expose previously masked errors.
        # Permit this bounded progress without claiming the graph is executable.
        return {"allowed": True, "partial": True, "remaining_count": len(validation["diagnostics"])}
    from .graph_inspection import diagnose
    before = diagnose(state["candidate"]["document"], base, conversation_id="diagnostic", revision=state["revision"] + 1)
    signature = lambda p: digest({k: p.get(k) for k in ("code", "path", "entry_ids", "message")})
    old = {signature(p) for p in before["diagnostics"]}
    remaining = validation["diagnostics"]
    new = {signature(p) for p in remaining}
    addressed = set(changed_ids) | set(state["repair_request"]["editable_ids"])
    partial = (new < old and any(p.get("entry_ids") for p in remaining)
               and all(not (set(p.get("entry_ids", [])) & addressed) for p in remaining))
    return {"allowed": partial, "partial": partial, "remaining_count": len(remaining)}


def preview_repair(state, base, proposal):
    if (not isinstance(proposal, dict) or set(proposal) != {"schema", "ticket", "summary", "changes"}
            or proposal["schema"] != SCHEMA or not isinstance(proposal["summary"], str)
            or not 1 <= len(proposal["summary"].strip()) <= 1200):
        raise ValueError("修订单格式无效，需要图版本标识、说明及具体变更。")
    req, c = require_current(state, base, proposal["ticket"])
    edits = proposal["changes"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_CHANGES:
        raise ValueError(f"每份修订单必须包含 1–{MAX_CHANGES} 项明确变更。")
    document = copy.deepcopy(c["document"])
    entries = {e["id"]: e for e in document["entries"]}
    changed, seen = [], set()
    for edit in edits:
        if (not isinstance(edit, dict) or set(edit) != {"entry_id", "reason", "changes"}
                or not isinstance(edit["entry_id"], str) or edit["entry_id"] not in req["editable_ids"] + req.get("structural_ids", []) + req.get("reference_ids", [])
                or edit["entry_id"] in seen or not isinstance(edit["reason"], str)
                or not 1 <= len(edit["reason"].strip()) <= 1200):
            raise ValueError("修订范围、原因或条目编号无效；只允许本次问题条目及既有依赖/循环，不能重复修改。")
        id, changes = edit["entry_id"], edit["changes"]
        seen.add(id)
        if isinstance(changes, dict) and changes.get("state") == "retired":
            from .draft_keys import duplicate_groups
            target = changes.get("superseded_by")
            if (set(changes) != {"state", "superseded_by"} or not isinstance(target, str)
                    or target == id or entries.get(target, {}).get("state") != "active"
                    or not any({id, target}.issubset(g["entry_ids"]) for g in duplicate_groups(c["document"]))):
                raise ValueError("退役仅允许合并重复参数、循环或依赖，须指定同组有效 superseded_by；原文和来源不得修改。")
            old = copy.deepcopy(entries[id])
            entries[id] = {**old, **changes}
            changed.append({"entry_id": id, "title": old["title"], "reason": edit["reason"],
                            "before": old, "after": copy.deepcopy(entries[id])})
            continue
        if (not isinstance(changes, dict) or not changes or set(changes) - {"title", "body", "data", "state", "kind"}
                or changes.get("state", "active") not in {"active", "conflict"}):
            raise ValueError("只能修订节点说明、结构和冲突状态，不能删除节点、改来源或伪造签认。")
        old = entries[id]
        from .draft_keys import duplicate_groups
        if (old.get("reviewed") or old.get("evidence") in {"saved_graph", "manual", "user_source"}) and any(
                id in group["entry_ids"] for group in duplicate_groups(c["document"])):
            raise ValueError("锁定的重复定义须通过合并退役选择保留版本，不能改写其原文、业务字段或来源。")
        reclassify = changes.get("kind", old["kind"]) != old["kind"]
        if reclassify:
            known = {n["node_id"] for n in (base or {}).get("nodes", [])}
            prior = next((e for e in state["document"]["entries"] if e["id"] == id), {})
            if (id not in req["editable_ids"] or changes["kind"] != "progress"
                    or old["kind"] not in {"node", "question"} or old["state"] != "conflict"
                    or old.get("evidence") != "proposal" or old.get("reviewed") or id in known
                    or old["kind"] == "node" and prior.get("evidence") in {"user_source", "saved_graph", "manual"}
                    or old["kind"] == "node" and old["data"].get("node_kind") != "plan"
                    or changes.get("data") != {}):
                raise ValueError("只能将当前未采用、未保存为正式节点且非用户来源的冲突型计划/问题建议改为非执行记录；不能移除业务节点或验收。")
        if id not in req["editable_ids"]:
            allowed = ({"node_id"} if id in req.get("reference_ids", []) else
                {"from_node", "to_node", "condition"} if old["kind"] == "edge" else {
                "entry_node_id", "check_node_id", "node_ids", "reviewer_node_ids", "requires_repair", "repair_node_ids", "repair_dispatch"})
            if (set(changes) != {"data"} or not isinstance(changes["data"], dict)
                    or set(changes["data"]) - allowed):
                raise ValueError("关联结构只能修订连线或节点引用，不能改变业务要求、验收、退出标准和预算。")
        value = {**old, **changes}
        if "data" in changes:
            if not isinstance(changes["data"], dict):
                raise ValueError("节点结构变更必须是对象。")
            value["data"] = changes["data"] if reclassify else {**old["data"], **changes["data"]}
        entry = validate_entry(value)
        entry["evidence"] = old.get("evidence", "manual")
        # A preview is not an adoption or a fabricated human sign-off.
        if "reviewed" in old:
            entry["reviewed"] = old["reviewed"]
        if reclassify:
            entry["reclassified_from"] = copy.deepcopy(old)
        elif "reclassified_from" in old:
            entry["reclassified_from"] = copy.deepcopy(old["reclassified_from"])
        if all(old.get(k) == entry.get(k) for k in ("kind", "title", "body", "data", "state")):
            raise ValueError("修订单没有实际变更。")
        entries[id] = entry
        changed.append({"entry_id": id, "title": old["title"], "reason": edit["reason"],
                        "before": {k: old[k] for k in ("kind", "title", "body", "data", "state")},
                        "after": {k: entry[k] for k in ("kind", "title", "body", "data", "state")}})
    replacements = {change["entry_id"]: change["after"]["superseded_by"] for change in changed
                    if change["after"].get("state") == "retired"}
    if any(entries.get(target, {}).get("state") != "active" for target in replacements.values()):
        raise ValueError("合并后的保留条目必须仍有效，不能形成退役链或互相退役。")
    from .draft_dedupe import merged_sources
    for target in dict.fromkeys(replacements.values()):
        entry = entries[target]
        refs = merged_sources(entries, target, [i for i, t in replacements.items() if t == target])
        if refs != entry["sources"]:
            if target in seen:
                raise ValueError("合并会自动保留来源，请不要同时改写保留条目的内容。")
            old = copy.deepcopy(entry)
            entry["sources"] = refs
            seen.add(target)
            changed.append({"entry_id": target, "title": entry["title"], "reason": "保留所有重复副本的来源引文",
                            "before": old, "after": copy.deepcopy(entry)})
    for entry in entries.values():
        source = entry["data"].get("input_id")
        if entry["state"] != "retired" and source in replacements:
            if entry["id"] in seen:
                raise ValueError("合并会自动迁移参数引用，请不要在同一份修订单重复修改关联条目。")
            old = copy.deepcopy(entry)
            entry["data"]["input_id"] = replacements[source]
            seen.add(entry["id"])
            changed.append({"entry_id": entry["id"], "title": entry["title"], "reason": "参数引用跟随合并后的保留条目",
                            "before": old, "after": copy.deepcopy(entry)})
    check_entry_refs(entries.values(), focus=seen)
    document["entries"] = list(entries.values())
    from .draft_keys import reject_new_duplicates
    reject_new_duplicates(c["document"], document, seen)
    from .graph_inspection import diagnose
    validation = diagnose(document, base, conversation_id="diagnostic", revision=state["revision"] + 1)
    from .graph_local_repair import validate_document
    execution_validation = validate_document(document, base, conversation_id="diagnostic", revision=state["revision"] + 1)
    return {"id": uuid.uuid4().hex, "ticket": req["id"], "summary": proposal["summary"],
            "changes": changed, "document": document, "validation": validation, "execution_validation": execution_validation,
            "adoption": adoption_status(state, base, document, validation, seen)}
