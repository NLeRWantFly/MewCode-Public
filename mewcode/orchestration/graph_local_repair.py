"""One structural proposal, one complete host check, then explicit adoption."""
from __future__ import annotations

import copy
import json
import uuid

from .draft_sources import digest
from .graph_readiness import readiness
from .graph_repair import require_current
from .workflow_draft import check_entry_refs, infer_draft_finalizer, project_graph, validate_entry
from .graph_review_insertion import changed_fields, insertion_edges, insertion_edit, validate_insertions

SCHEMA = "mewcode.graph-local-repair.v1"
SYSTEM = """你只修当前图的执行结构，不执行业务。只输出JSON，不调用工具，不重读历史，不输出全图。
格式：{\"schema\":\"mewcode.graph-local-repair.v1\",\"ticket\":输入ticket,\"summary\":修复说明,\"upserts\":[变更]}。
upserts每项仅含id、kind、title、body、data、rationale（仅写实际修改字段）。最多32项。
可新增或修订edge/loop；所有已有节点正文、角色、验收、输入输出、来源必须完整保留；不能增删业务节点。
唯一节点结构标记是data.execution_mode=coordinate：仅用于已有子阶段的非gate父节点，表示父阶段只协调/汇总，子阶段独立执行，避免重复制作。
locked=true条目逐字保留。唯一例外是宿主 review_insertions 列出的交付连线：可将该边的 to_node 改为本提案新增 audit，并新增 audit→原终点的 pass 边；原边其它字段和所有原节点保持不变，不得把审查放到固定 finalizer_node_id 之后。
该原边请按 {id:原边ID,data:{to_node:新审查ID},rationale:原因} 提交；不要因改接而改写原边名称、正文、起点、条件或分支。重述已有字段时必须与原值完全一致。
插入审查提案只包含上述3项变更；新audit的inputs逐项包含原执行节点outputs，acceptance逐项包含原执行节点acceptance，说明如何独立核对这些要求并给出非空outputs；可填steps/review_channels，不能填model/parent_id/execution_mode。新审查模型留待用户确认，不沿用执行者默认模型。不得新增implement/gate等其它节点。未确认建议边的conflict必须实质澄清说明，不能只改状态。
重复参数/循环/依赖交由宿主合并；可单独提交仅含id/state=retired/superseded_by/rationale的upserts，指定同组有效保留条目，不能同时改内容。合并后原文和来源保留，用户预览采用才写入。
批处理可用mode=foreach、items=[{item_id,goal}]；每项为有限可核验单元，节点验收、criteria和exit_condition都只看current_item，宿主在当前项EXIT后推进下一项，全部项完成才退出循环。全队列覆盖和最终质量在循环后的汇总/独立验收核对，后续未执行项不能成为当前项返修的理由；未通过和未验证状态如实保留。
parent_id仅分组，无隐式执行连线；人工门禁必须在循环外且收到产物后执行，拒绝会暂停，不能当自动返修判定。
loop判定必须在reviewer_node_ids内；只有entry可进入、只有check可退出；退出pass/always。审查报告须全部覆盖同一criteria。
循环移除loop边后须无环。既有after_decision返修根不能有普通入边，返修不重做首次入口。
循环内部混合入边是所有always与至少一条匹配分支；汇合的两个always才保证两个前置都完成。不要用两条pass假装AND。
新循环沿用原验收，不设置预算，control_mode=team。禁止更改人工确认、绕开复核、把UNKNOWN记为通过、伪造来源或撤销业务要求。
新循环criteria的acceptance_test必须逐字包含check节点已有全部验收条目；宿主会将非reviewer成员列为退出前必需通过阶段。
每次最多32项变更；宿主会保存未采用的修复草案并反馈完整校验结果，你须继续修正技术问题。
本轮entries可能含尚未采用的结构草案；原有locked保护仍以初始图为准。后续只返回本轮改变的条目，不重复已保留的变更。
普通前向边必须无环；返修回边必须condition=loop并声明对应循环，不要将audit→返修计划→执行→audit全部写成普通边。
无法继续时可返回control={action:stop,reason:简短原因,next_step:""}，保存草案并收束；不能把技术错误作为业务问题要求用户判断。
继续时可返回control={action:continue,reason:说明,next_step:具体的新修法}。"""


def issue_request(state, base):
    from .draft_store import DraftStore
    c = state.get("candidate")
    if not c or c.get("status") != "ready":
        raise ValueError("请先完成或停止生成，保留当前完整候选图后再修复。")
    snapshot = copy.deepcopy(state)
    if c.get("mode") != "selected":
        snapshot["source_ids"] = list(c["processed_ids"])
    DraftStore.ready_candidate(snapshot, c["id"], c["sequence"], check_references=False)
    if digest(base) != c["base_graph_hash"]:
        raise ValueError("画布已变化，请先重新打开当前图。")
    saved_canvas_scope = ((c.get("source_override") or {}).get("kind") == "saved_canvas")
    saved_structural_ids = [
        e["id"] for e in c["document"].get("entries", [])
        if saved_canvas_scope and e.get("state") != "retired" and e.get("kind") in {"edge", "loop"}
        and e.get("evidence") == "saved_graph" and e.get("reviewed") is not True
    ]
    req = {"id": uuid.uuid4().hex, "candidate_id": c["id"], "sequence": c["sequence"],
           "base_hash": digest(base), "history_ids": list(state["source_ids"]), "local": True,
           "editable_ids": [], "saved_structural_ids": saved_structural_ids}
    diagnostics = readiness(c["document"], base, conversation_id="local", revision=state["revision"] + 1)["diagnostics"]
    finalizer = (base or {}).get("finalizer_node_id") or infer_draft_finalizer(c["document"])
    req["review_insertions"] = insertion_edges(c["document"], finalizer, diagnostics)
    entries = [{**{k: e[k] for k in ("id", "kind", "title", "body", "data", "state")},
                "locked": _locked(e) and e["id"] not in saved_structural_ids} for e in c["document"]["entries"] if e["state"] != "retired"]
    user = json.dumps({"ticket": req["id"], "entries": entries, "finalizer_node_id": finalizer,
                       "review_insertions": req["review_insertions"], "diagnostics": diagnostics},
                      ensure_ascii=False, separators=(",", ":"))
    return req, user


def _locked(e):
    return e.get("reviewed") is True or e.get("evidence") in {"user_source", "manual", "saved_graph"}


def unchanged_current_check(state, base):
    """A completed host check applies only to the unchanged ticketed candidate."""
    run = state.get("local_repair_run") or {}
    preview = state.get("repair_preview") or {}
    candidate = state.get("candidate") or {}
    if (run.get("status") != "completed" or not preview.get("local")
            or preview.get("dedupe") or preview.get("changes")
            or not preview.get("validation", {}).get("valid")
            or (run.get("candidate_id"), run.get("sequence")) !=
               (candidate.get("id"), candidate.get("sequence"))
            or candidate.get("base_graph_hash") != digest(base)
            or "document" not in preview
            or digest(preview["document"]) != digest(candidate.get("document"))
            or ("local_repair_preview" in state and state["local_repair_preview"] is None)):
        return False
    try:
        request, _ = require_current(state, base, preview.get("ticket"))
        return bool(request.get("local"))
    except (ValueError, KeyError, TypeError):
        return False


def validate_document(document, base, *, conversation_id, revision):
    from .convert import to_expert_flow
    from .control_plan import build_control_plan
    from .loops import compile_loops
    result = readiness(document, base, conversation_id=conversation_id, revision=revision)
    if not result["ready"]:
        return {"valid": False, "diagnostics": result["diagnostics"]}
    try:
        spec = project_graph(document, base, conversation_id=conversation_id, revision=revision)
        flow = to_expert_flow(spec)
        _, loops = compile_loops(spec.nodes, spec.edges, spec.meta.get("loops"))
        gates = {n.node_id for n in spec.nodes if n.kind == "gate"}
        if any(gates & set(loop.node_ids) for loop in loops):
            raise ValueError("人工门禁必须位于返修循环外，不能自动替用户确认。")
        parents = spec.meta["workflow_draft"]["parents"]
        for node_id in spec.meta["workflow_draft"].get("coordination_nodes", []):
            if node_id not in parents.values():
                raise ValueError("协调汇总节点必须保留独立执行的子阶段。")
        build_control_plan(spec)
        if {n.node_id for n in flow.nodes} != {n.node_id for n in spec.nodes}:
            raise ValueError("修订不能丢失实际执行节点。")
    except (ValueError, TypeError, KeyError) as exc:
        return {"valid": False, "diagnostics": [{"code": "execution_invalid", "path": "$", "entry_ids": [], "message": str(exc)}]}
    return {"valid": True, "diagnostics": [], "node_count": len(flow.nodes), "loop_count": len(loops),
            "human_gates": sorted(gates)}


def preview_repair(state, base, proposal, *, max_changes=32):
    if (not isinstance(proposal, dict) or set(proposal) - {"schema", "ticket", "summary", "upserts", "control"}
            or not {"schema", "ticket", "summary", "upserts"}.issubset(proposal)
            or proposal["schema"] != SCHEMA or not isinstance(proposal["summary"], str)
            or not 1 <= len(proposal["summary"].strip()) <= 1200):
        raise ValueError("一次修复提案格式无效。")
    req, c = require_current(state, base, proposal["ticket"])
    if not req.get("local"):
        raise ValueError("修订单类型不匹配。")
    edits = proposal["upserts"]
    if not isinstance(edits, list) or max_changes is not None and len(edits) > max_changes:
        raise ValueError("每批最多32项明确结构变更；当前图保持不变。")
    if any(isinstance(e, dict) and e.get("state") == "retired" for e in edits):
        if any(not isinstance(e, dict) or set(e) != {"id", "state", "superseded_by", "rationale"}
               or e["state"] != "retired" for e in edits):
            raise ValueError("合并退役须单独预览，不能混入内容或其它结构修改。")
        from .draft_keys import duplicate_groups
        from .graph_repair import preview_repair as preview_retirements
        from .repair_protocol import SCHEMA as REPAIR_SCHEMA
        trial = copy.deepcopy(state)
        trial["repair_request"]["editable_ids"] = list({i for g in duplicate_groups(c["document"]) for i in g["entry_ids"]})
        result = preview_retirements(trial, base, {"schema": REPAIR_SCHEMA, "ticket": proposal["ticket"],
            "summary": proposal["summary"], "changes": [{"entry_id": e["id"], "reason": e["rationale"],
                "changes": {"state": "retired", "superseded_by": e["superseded_by"]}} for e in edits]})
        result.update(local=True, dedupe=True)
        return result
    document = copy.deepcopy(c["document"])
    entries = {e["id"]: e for e in document["entries"]}
    changes, seen = [], set()
    for edit in edits:
        if (not isinstance(edit, dict) or set(edit) - {"id", "kind", "title", "body", "data", "rationale"}
                or not isinstance(edit.get("rationale"), str) or not 1 <= len(edit["rationale"].strip()) <= 1000):
            raise ValueError("每项结构变更必须有依据，不能修改来源、状态或复核标记。")
        entry_id = edit.get("id")
        if not isinstance(entry_id, str) or entry_id in seen:
            raise ValueError("变更ID无效或重复。")
        seen.add(entry_id)
        before = entries.get(entry_id)
        insertion = before is not None and insertion_edit(before, edit, req.get("review_insertions", []))
        saved_structural = (before is not None and before["id"] in req.get("saved_structural_ids", [])
                            and before.get("kind") in {"edge", "loop"}
                            and before.get("evidence") == "saved_graph"
                            and before.get("reviewed") is not True)
        if before and (before["state"] == "retired" or (_locked(before) and not insertion and not saved_structural)):
            fields = "、".join(changed_fields(before, edit)) or "没有实际改动"
            permitted = "；此交付连线只允许改 to_node 并插入独立审查" if entry_id in req.get("review_insertions", []) else ""
            raise ValueError(f"受保护条目 {entry_id}（{before['title']}）不能按本提案修改：{fields}{permitted}。原图已保留。")
        if before:
            raw = {**before, **edit, "data": {**before["data"], **edit.get("data", {})}}
            if before["kind"] == "node":
                if (set(edit) - {"id", "data", "rationale"} or edit.get("data") != {"execution_mode": "coordinate"}
                        or before["data"].get("node_kind") == "gate"):
                    raise ValueError("节点仅允许添加协调汇总标记，职责与验收不可改动。")
                # Keep original node provenance and prose byte-for-byte.
                raw.pop("rationale", None)
                if "rationale" in before:
                    raw["rationale"] = before["rationale"]
            elif before["kind"] not in {"edge", "loop"} or raw["kind"] != before["kind"]:
                raise ValueError("只允许修订执行依赖和循环。")
            elif before["kind"] == "loop":
                wiring = {"entry_node_id", "check_node_id", "node_ids", "reviewer_node_ids", "repair_node_ids", "repair_dispatch", "requires_repair", "mode", "items"}
                if (set(edit.get("data", {})) - wiring or set(edit) & {"title", "body"}):
                    raise ValueError("既有循环只允许修订调度归属，验收、退出标准、必需阶段和预算必须保留。")
            if before["state"] == "conflict":
                if before["kind"] != "edge" or (raw["body"] == before["body"] and raw["data"] == before["data"]):
                    raise ValueError("冲突必须实质澄清，不能只清除状态。")
                raw["state"] = "active"
        else:
            if edit.get("kind") not in {"edge", "loop", "node"}:
                raise ValueError("不能新增业务节点或新要求。")
            raw = {**edit, "state": "active", "sources": [], "evidence": "ai_completed"}
            if edit["kind"] == "loop" and (edit.get("data", {}).get("control_mode") != "team"
                    or set(edit.get("data", {})) & {"max_rounds", "token_budget", "time_budget_seconds", "max_iterations", "timeout_seconds", "max_stalled_rounds", "node_max_iterations", "reviewer_max_iterations"}):
                raise ValueError("新增循环只能沿用team规则，不能设定预算。")
            if edit["kind"] == "loop":
                data = copy.deepcopy(raw.get("data") or {})
                check = entries.get(data.get("check_node_id"), {})
                required_acceptance = set(check.get("data", {}).get("acceptance") or [])
                tests = {v.get("acceptance_test") for v in data.get("criteria", []) if isinstance(v, dict)}
                if not required_acceptance or not required_acceptance.issubset(tests):
                    raise ValueError("新增循环必须逐字沿用判定节点的全部已有验收条目。")
                data["required_node_ids"] = [n for n in data.get("node_ids", []) if n not in data.get("reviewer_node_ids", [])]
                raw["data"] = data
        after = validate_entry(raw)
        if before is None:
            after["evidence"] = "ai_completed"
        for key in ("evidence", "reviewed", "reclassified_from"):
            if before and key in before:
                after[key] = copy.deepcopy(before[key])
        entries[entry_id] = after
        changes.append({"entry_id": entry_id, "title": after["title"], "reason": edit["rationale"],
                        "before": copy.deepcopy(before), "after": copy.deepcopy(after)})
    document["entries"] = list(entries.values())
    validate_insertions(c["document"], document, edits, req.get("review_insertions", []))
    # Old versions adopted design prose even when its structure was rejected.
    # Include only proven, unchanged AI entries in this explicit CAS-bound
    # preview. The model cannot nominate arbitrary requirements for withdrawal.
    from .graph_design_provenance import unsupported_designs
    stale = unsupported_designs(c["document"], c.get("autobuild") or {})
    for before in stale:
        after = {**copy.deepcopy(before), "state": "retired"}
        entries[before["id"]] = after
        changes.append({"entry_id": before["id"], "title": before["title"],
                        "reason": "这条 AI 说明来自未完整采用的结构提案，撤回过期说明，原文保留在历史中。",
                        "before": before, "after": copy.deepcopy(after)})
    document["entries"] = list(entries.values())
    from .draft_keys import reject_new_duplicates
    reject_new_duplicates(c["document"], document, seen)
    check_entry_refs(document["entries"])
    validation = validate_document(document, base, conversation_id="local", revision=state["revision"] + 1)
    summary = proposal["summary"] + (f" 同时预览撤回 {len(stale)} 条未采用结构提案留下的 AI 说明。" if stale else "")
    return {"id": uuid.uuid4().hex, "ticket": proposal["ticket"], "summary": summary,
            "changes": changes, "document": document, "validation": validation, "local": True,
            "adoption": {"allowed": bool(changes) and validation["valid"], "partial": False}}
