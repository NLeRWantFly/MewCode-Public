"""Graph-first presentation of resumable, source-linked extraction checkpoints.

The index is machine state, not a second prose deliverable or a user sign-off.
Rendering and editing it never invokes a model or starts an orchestration.
"""
from __future__ import annotations

import copy
import time
from datetime import datetime

from .draft_sources import digest
from .draft_store import DraftStore
from .graph_readiness import readiness
from .workflow_draft import SYSTEM, project_graph, seed_document

GRAPH_SYSTEM = SYSTEM.replace("你维护一份有来源的流程底稿", "你直接生成有来源的阶段图结构").replace("底稿", "图结构").replace(
    "decision|question|artifact", "decision|question|input|artifact").replace(
    "顺序不明确时记 question，不能拿时间顺序当执行依赖。",
    "顺序不明确时保留节点并暂不写依赖或 question，由后续补全采用合理顺序并记录 AI 假设。") + """
用户直接在图上查看和修改，不需要独立文字底稿、长篇总结或二次绘图。
node 的 body 是该节点的简明职责，inputs/outputs/acceptance/steps 存在 node.data，避免重复叙述。
全局约束保持全局，节点专属要求关联 node_id；来源只保留短引文。纯历史执行动态列入 ignored，不新增 progress 条目。
document 是已落盘的图和要求；已处理原文不会再次发送。复用稳定节点 ID，只分析本批 sources。
旧来源 ID 仅用于追溯，不可据此编造引文；修改已有节点时只引用本批真实来源，宿主会保留原有来源。
只返回本块改变的结构，不复述未变化节点。不要为了生成成功删改范围、验收或人工签认要求。
用户明确留到开跑才给的主题、路径、数量等值写 input，data 含 name/description/required/default；不写 question。
问题用 question.data.issue_type 区分 business/technical/input/capability；真实业务取舍可 blocking=true，结构问题由 AI 修复。参数用 input，collect_at=节点ID 指定收集阶段，required_when={input:参数名,equals:文本值} 指定条件；关联问题写 input_id，不重复追问。
可选互斥分支在 edge.data 写 input_when={input:已声明参数名,equals:文本值} 和同一 branch_group；仅在正文写启用/禁用不会分流，两条 pass 默认都会匹配。
提取阶段只保留原文事实；原文没有给定的默认值或设计决定留给后续 AI 补全，以免把推断标成用户原话。"""
GRAPH_SYSTEM += """\n需要用户取舍的 business 问题须给 2–6 个简明 options：每项 {value,label,description,recommended}，说明实际影响，最多推荐一项。推荐不是同意，不能预填用户决定。技术问题由你修复，不转成用户选项。input.label 用通俗名称；有限取值的输入也提供同格式 options，默认值须在选项中，收集阶段由 collect_at 声明。"""
GRAPH_SYSTEM += """\n用户指定节点模型时，用 available_model_routes 中实际配置的 provider_key/model_id/effort 写 node.data.model。按原始用户的任务分工和推理档位绑定，不能把不同执行模型统一替换成聊天模型。指定路由不存在时保留 capability 问题，不猜供应商、不静默降档。模型列表只是配置事实，实际视觉/工具能力仍须试点。没有指定模型时可留空在确认界面绑定；已存在模型绑定保持。"""
GRAPH_SYSTEM += """\n父节点若只组织已有独立子阶段，用 execution_mode=coordinate，防止父子重复执行整段业务。两路独立评估或解答共享输入入口，再共同进入汇总；不要给第二路连接第一路的结果依赖，以免泄露答案。需要在循环中反复分发时，共同入口须属于循环体并作为 entry_node_id，下一项回到该入口；验收条件和预算保持。"""


def graph_coverage(state):
    """Count already-materialized selected replies without claiming gap coverage."""
    c = state.get("candidate")
    if c and c.get("mode") == "selected":
        coverage = dict(state["coverage"])
        if c.get("repair_scope"):
            coverage["mode"] = "repair"
        prefix = c.get("base_processed_ids", state["processed_ids"])
        coverage["history_changed"] |= (state["source_ids"][:len(prefix)] != prefix
                                        or not set(state.get("selected_ids", [])).issubset(state["source_ids"]))
        frozen = c.get("history_source_ids", state["source_ids"])
        coverage["new_sources"] = max(0, len(state["source_ids"]) - len(frozen))
        coverage["history_changed"] |= state["source_ids"][:len(frozen)] != frozen
        return coverage
    all_ids = state["source_ids"]
    ids = c.get("history_source_ids", all_ids) if c else all_ids
    processed = c["processed_ids"] if c else state["processed_ids"]
    reused = set(c.get("reused_ids", [])) if c else set(state["processed_ids"] + state.get("selected_ids", []))
    covered = (set(processed) | reused) & set(ids)
    return {"total": len(ids), "processed": len(covered), "pending": len(ids) - len(covered),
            "reused": len(reused & set(ids)),
            "new_sources": max(0, len(all_ids) - len(ids)),
             "history_changed": all_ids[:len(ids)] != ids or ids[:len(processed)] != processed or not reused.issubset(ids)}


def _current_build(state):
    """Return build metadata belonging to the current graph snapshot."""
    if "_graph_build" in state:
        return state.get("_graph_build") or {}
    candidate = state.get("candidate")
    if candidate is not None:
        # A selected/manual candidate deliberately has no independent build;
        # never let the last accepted receipt masquerade as its current state.
        return candidate.get("autobuild") or {}
    return state.get("autobuild_result") or {}


def graph_task_state(state, running, progress=None):
    """Task liveness is distinct from graph readiness and the resume phase."""
    candidate = state.get("candidate") or {}
    has_candidate = bool(state.get("candidate"))
    build = _current_build(state)
    repair = state.get("local_repair_run") or {}
    if (repair.get("candidate_id"), repair.get("sequence")) != (candidate.get("id"), candidate.get("sequence")):
        repair = {}
    kind = (progress or {}).get("request_kind") or (progress or {}).get("phase") or ("autobuild" if build else "generation")
    repair_meta = {}
    if repair:
        work = repair.get("work") or {}
        control = work.get("control") or {}
        repair_meta = {"round_count": len(work.get("rounds", [])),
                       "saved_changes": len(work.get("upserts", [])),
                       "soft_boundary": control.get("active_seconds", 0) >= control.get("soft_seconds", 1800)}
    if running:
        return {"status": "running", "kind": kind, "error": "",
                **(repair_meta if kind == "local_repair" else {})}
    if repair:
        repair = {k: v for k, v in repair.items() if k != "work"}
        status = repair["status"] if repair["status"] != "running" else "interrupted"
        return {**repair, **repair_meta, "status": status, "kind": "local_repair", "error": repair.get("error") or
                ("修复已中断，当前图保留；可重新修复。" if status == "interrupted" else "")}
    status, error = candidate.get("status"), candidate.get("error", "")
    if status in {"failed", "paused"}:
        return {"status": status, "kind": kind, "error": error}
    if build.get("phase") == "waiting_user":
        return {"status": "waiting_user", "kind": kind, "error": ""}
    if status == "working" or (build and build.get("phase", "done") != "done"):
        return {"status": "interrupted", "kind": kind, "error": "构图已中断，已保存进度；可继续当前构建。"}
    from .graph_autobuild import TIME_LIMIT_REASON, CALL_LIMIT_REASON
    failed = build.get("reason") in {"model_error", "review_error", "request_too_large", "parse_failed", "time_limit", TIME_LIMIT_REASON, CALL_LIMIT_REASON}
    return {"status": "failed" if failed else build.get("status", "completed" if candidate else "idle"),
            "kind": kind, "error": build.get("reason", "") if failed else ""}


def graph_process_state(state, running, progress=None):
    """Readable milestones; model activity never stands in for saved results."""
    task = graph_task_state(state, running, progress)
    candidate = state.get("candidate") or {}
    has_candidate = bool(state.get("candidate"))
    build = _current_build(state)
    coverage = state.get("coverage") or {}
    phase = (progress or {}).get("phase") or build.get("phase") or "extracting"
    outcome = task["status"]
    current_without_build = has_candidate and not build
    saved_review_stale = (not has_candidate and bool(build) and
                          (build.get("review_stale") or build.get("reason") == "manual_edits_after_review"))
    # A local repair owns the process display while it is running or awaiting
    # adoption.  Check this before the manual/selected no-build branch so a
    # completed repair cannot be flattened into the candidate's host status.
    if task["kind"] == "local_repair":
        phase = "repair"
        if outcome == "completed":
            if "local_repair_preview" in state:
                repair_preview = state.get("local_repair_preview") or {}
            else:
                repair_preview = state.get("repair_preview") or {}
            if not repair_preview:
                outcome = "needs_work"
            elif not repair_preview.get("changes") and repair_preview.get("validation", {}).get("valid"):
                outcome = "checked"
            elif repair_preview.get("validation", {}).get("valid"):
                outcome = "ready" if task.get("adopted") else "waiting_adoption"
            else:
                outcome = "needs_work"
            phase = "done"
    elif current_without_build:
        # Selected/manual candidates are checked by the host projection.  Do
        # not inherit the last accepted model review as their current build.
        phase = "done" if (candidate.get("readiness") or {}).get("ready") else "review"
        complete = (candidate.get("status") == "ready" and not coverage.get("pending")
                    and not coverage.get("history_changed") and not state.get("warnings"))
        outcome = "ready" if complete and phase == "done" else "needs_work"
    elif saved_review_stale:
        phase = "review"
        outcome = "needs_work"
    elif phase == "done" and build.get("status") != "ready":
        phase = ("review" if build.get("reason") == "manual_edits_after_review" else
                 "extracting" if coverage.get("pending") else "autofix")
        if outcome not in {"failed", "paused", "interrupted"}:
            outcome = "needs_work"
    elif build.get("phase") == "waiting_user":
        outcome = "waiting_user"
    elif build.get("status") == "ready" and not coverage.get("pending"):
        outcome = "ready"
    if outcome == "needs_review":
        outcome = "model_stopped"
    if build.get("reason") in {"model_stopped", "invalid_control", "repeated_control"}:
        outcome = "model_stopped"
    if running:
        outcome = "running"
    if state.get("_structure_checked") and state.get("_graph_ready") and not has_candidate and not running:
        phase, outcome = "done", "checked"
    # graph_state supplies the authoritative structural/CAS result when it is
    # available.  A stale canvas must not be described as save-ready merely
    # because an older task receipt reached its done phase.
    if state.get("_graph_ready") is False and not running and outcome in {"ready", "completed", "checked"}:
        outcome = "needs_work"
        if phase == "done":
            phase = "review"
    stages = {"extracting": 0, "preparing": 0, "model": 0, "applying": 0,
              "autofix": 1, "repair": 1, "review": 2, "gap_fill": 3, "final_review": 3, "done": 4}
    labels = {"running": "AI 正在完善修订" if task["kind"] == "local_repair" else "正在构图",
               "waiting_user": "等待你决定业务取舍", "waiting_adoption": "修订已通过校验，等待你采用",
               "checked": "当前结构校验通过，无需修改",
              "ready": "阶段图已就绪", "needs_work": "草图已保留，执行结构仍需完善",
              "model_stopped": "AI 已收束，当前修复进度已保留", "failed": "请求未完成，当前图已保留",
               "paused": "已停止，当前进度已保留", "interrupted": "任务已中断，当前进度已保留"}
    if current_without_build and outcome == "needs_work":
        labels["needs_work"] = "当前图需校验后再保存"
    elif saved_review_stale:
        labels["needs_work"] = "当前图已保存，需重新校验"
    already_saved = (state.get("_canvas_saved") and not has_candidate and not running
                     and not saved_review_stale and outcome in {"ready", "checked"})
    if already_saved:
        labels[outcome] = "阶段图已保存"
    last = "尚未保存新的构图成果。"
    if task["kind"] == "local_repair":
        last = ("当前结构校验通过，无需采用结构修改。" if outcome == "checked" else
                "已保存可采用的最终修订。" if outcome == "waiting_adoption" else
                "修订已采用到当前候选图。" if task.get("adopted") else
                f"已保存 {task.get('saved_changes', 0)} 项修复草案；校验通过后才展示最终差异。")
    else:
        rounds = ((build.get("autofix") or {}).get("rounds", []) +
                  (build.get("gap_fill") or {}).get("rounds", []))
        if saved_review_stale:
            last = "当前图已保存；此前审查结论仅供参考，需要重新校验。"
        elif rounds:
            row = rounds[-1]
            parts = [row.get("structural") or {}, row.get("ledger") or {}]
            count = sum(len(p.get("applied", [])) for p in parts if p.get("adopted"))
            last = (f"最近一轮已采用并保存 {count} 项变更。" if count else
                    "本轮无需结构改动，当前图已通过核验。" if outcome == "ready" else
                    "最近一轮提案尚未采用，仍需由 AI 完善。")
        elif coverage.get("processed"):
            last = f"已整理并保存 {coverage['processed']} / {coverage.get('total', 0)} 个历史片段。"
        else:
            count = sum(e.get("kind") == "node" and e.get("state") != "retired"
                        for e in candidate.get("document", {}).get("entries", []))
            if count:
                last = f"已保存 {count} 个阶段的草图，历史整理仍在继续。"
    next_step = ("可保存当前图。" if outcome == "checked" else
                 "核对最终差异并采用修订。" if outcome == "waiting_adoption" else
                 "在待决事项中明确业务选择。" if outcome == "waiting_user" else
                 "先校验当前图并核对修复差异。" if saved_review_stale or current_without_build and outcome != "ready" else
                 "可保存阶段图。" if outcome == "ready" else
                 "校验通过后核对最终修订差异。" if task["kind"] == "local_repair" else
                 "完成历史整理后补全执行结构。" if stages.get(phase, 1) == 0 else
                 "修复与校验通过后进行独立审查。" if stages.get(phase, 1) == 1 else
                 "由 AI 回补审查缺口，再次核验。")
    if already_saved:
        last = "当前阶段图已通过核验并保存，无需重复保存。"
        next_step = "可返回对话，或在完整画布中继续编辑。"
    control = build.get("control") or {}
    local = task["kind"] == "local_repair"
    if local:
        control = ((state.get("local_repair_run") or {}).get("work") or {}).get("control") or {}
    active = float(control.get("active_seconds") or 0)
    if running and ((progress or {}).get("model") or {}).get("stage") in {"requesting", "thinking", "reasoning", "streaming"}:
        try:
            active += max(0, time.time() - datetime.fromisoformat(progress["request_started_at"]).timestamp())
        except (KeyError, ValueError, TypeError):
            pass
    index = 2 if local and ((progress or {}).get("model") or {}).get("stage") == "validating" else stages.get(phase, 1)
    stage_labels = None
    if current_without_build:
        stage_labels = ["纳入对话", "结构校验", "待保存"]
        index = 2 if outcome == "ready" else 1
    entries = {e["id"]: e for e in candidate.get("document", {}).get("entries", [])}
    def related_nodes(ids):
        result = set()
        for entry_id in ids:
            e = entries.get(entry_id, {})
            data = e.get("data") or {}
            if e.get("kind") == "node":
                result.add(entry_id)
            result.update(v for k, v in data.items() if k in {"from_node", "to_node", "node_id", "entry_node_id", "check_node_id"} and isinstance(v, str))
        return sorted(result)
    changed = []
    if local and task.get("adopted"):
        changed = (candidate.get("log") or [{}])[-1].get("entry_ids", [])
    elif not local:
        rounds = (build.get("autofix") or {}).get("rounds", []) + (build.get("gap_fill") or {}).get("rounds", [])
        if rounds:
            changed = [r["entry_id"] for part in ("structural", "ledger")
                       for r in (rounds[-1].get(part) or {}).get("applied", [])
                       if (rounds[-1].get(part) or {}).get("adopted")]
    return {"phase": phase, "stage_index": index, "outcome": outcome,
            "focus_node_ids": related_nodes((progress or {}).get("focus_entry_ids", [])) if running else [],
            "changed_node_ids": related_nodes(changed),
             "stages": stage_labels or (["读取当前图", "完善结构", "整图校验", "整理最终差异", "修订已采用" if task.get("adopted") else "等待采用"] if local else
                       ["整理历史", "补全结构", "独立审查", "回补与复审", "交付"]),
            "headline": labels.get(outcome, "等待构图"), "last_result": last, "next_step": next_step,
            "soft_boundary": active >= (control.get("soft_seconds") or 1800)}


def saved_graph_document(state, base):
    if not base:
        return state["document"]
    return seed_document(base, previous=state["document"])


def delivery_view(document, build):
    """Expose source-linked assumptions, inputs, and unresolved work."""
    if not build:
        return None
    assumptions, inputs, pending = [], [], []
    from .graph_design_provenance import unsupported_designs
    unsupported = {e["id"] for e in unsupported_designs(document, build)}
    from .run_inputs import resolved_question_ids
    resolved = resolved_question_ids(document)
    for entry in document.get("entries", []):
        if entry.get("state") != "active":
            continue
        data = entry.get("data") or {}
        common = {"id": entry["id"], "title": entry.get("title", ""),
                  "body": entry.get("body", ""), "evidence": entry.get("evidence", ""),
                  "sources": copy.deepcopy(entry.get("sources", [])),
                  "rationale": entry.get("rationale", "")}
        if entry.get("kind") == "decision" and data.get("category") in {"designed", "defaulted"}:
            assumptions.append({**common, "category": data["category"], "value": data.get("value", ""),
                                "unsupported": entry["id"] in unsupported})
        elif entry.get("kind") == "input":
            inputs.append({**common, "name": data.get("name", ""),
                           "description": data.get("description", ""),
                           "required": data.get("required", True),
                           "default": data.get("default", ""), "collect_at": data.get("collect_at", "start"),
                           **{key: copy.deepcopy(data[key]) for key in ("label", "options") if key in data},
                           "required_when": copy.deepcopy(data.get("required_when"))})
        elif entry.get("kind") == "question" and entry["id"] not in resolved:
            pending.append({**common, "kind": "question", "blocking": data.get("blocking", False)})
    for gap in build.get("pending_gaps", []):
        pending.append({"kind": "coverage_gap", "source_id": gap["source_id"],
                        "quote": gap["quote"], "body": gap["summary"],
                        "suggested_fix": gap.get("suggested_fix", ""), "evidence": "user_source"})
    for diagnostic in build.get("remaining", []):
        pending.append({"kind": "diagnostic", "code": diagnostic.get("code", ""),
                        "body": diagnostic.get("message", ""),
                        "entry_ids": diagnostic.get("entry_ids", []), "evidence": "host"})
    from .graph_discussion import issue_rows, public_discussion
    return {"status": build.get("status", "working"), "phase": build.get("phase", "done"),
            "issue_groups": issue_rows(document, build), "discussion": public_discussion(build),
            "reason": build.get("reason", ""), "model_calls": build.get("model_calls", 0),
            "call_attempts": build.get("call_attempts", build.get("model_calls", 0)),
            "assumptions": assumptions, "run_inputs": inputs, "pending": pending,
            "review": copy.deepcopy(build.get("final_review") or build.get("review")),
            "review_stale": build.get("review_stale", False),
            "previous_attempts": copy.deepcopy(build.get("previous_attempts", [])),
            "diagnostics": copy.deepcopy(build.get("remaining", [])),
             "attempts": copy.deepcopy((build.get("autofix") or {}).get("rounds", []) +
                                       [{**row, "phase": "gap_fill"} for row in (build.get("gap_fill") or {}).get("rounds", [])])}


def _canvas_state(base):
    """Describe the persisted canvas independently from a draft candidate.

    A failed or abandoned candidate is an attempt at changing the canvas.  It
    must never turn an already saved graph into an ``empty`` or ``failed``
    canvas in the read model.
    """
    nodes = base.get("nodes", []) if isinstance(base, dict) else []
    if not isinstance(nodes, list):
        nodes = []
    return {"exists": bool(nodes), "node_count": len(nodes),
            "status": "saved" if nodes else "empty"}


def _graph_actions(state, base, document, *, ready, issues):
    """Return the single action contract used by all graph surfaces.

    The action map is deliberately derived from the same candidate/base CAS
    state used by the mutating endpoints.  In particular, a stale candidate
    exposes only the explicit rebase operation; every other write is refused
    until that operation succeeds.
    """
    candidate = state.get("candidate") or {}
    has_candidate = bool(candidate)
    running = bool(state.get("running"))
    current_hash = digest(base)
    stale = has_candidate and candidate.get("base_graph_hash") != current_hash
    status = candidate.get("status", "")
    candidate_ready = has_candidate and status == "ready"
    build = candidate.get("autobuild") or {}
    build_phase = build.get("phase")
    # A ready candidate can still be inside the automatic completion/review
    # pipeline.  The client maps this continuation to the bounded retry path
    # when a finished build is needs_review, so expose it as resume rather than
    # leaving the primary action in the misleading completed state.
    build_resumable = candidate_ready and bool(build) and (
        build_phase not in {None, "done", "waiting_user"}
        or build_phase == "done" and build.get("status") != "ready"
    )
    discussion = build.get("discussion") or {}
    # A normal question can pause an otherwise ready graph.  The discussion
    # continue endpoint is safe exactly when no proposal/message/problem still
    # needs a user decision; expose that same predicate as resume.
    continueable = (candidate_ready and build_phase == "waiting_user"
                    and not discussion.get("pending") and not discussion.get("proposal"))
    if continueable:
        from .graph_discussion import waiting_questions
        continueable = not bool(waiting_questions(document))
    build_resumable = build_resumable or continueable
    coverage = state.get("coverage") or {}
    basis = state.get("basis") or {}
    source_available = bool(state.get("source_ids")) or bool(coverage.get("total"))
    saved_canvas = bool(isinstance(base, dict) and base.get("nodes"))
    source_consistent = not coverage.get("history_changed") and not state.get("warnings")
    source_blocked_reason = (
        "原始来源已变化或存在读取警告，请先重新读取全部历史；当前进度不能继续复用"
        if not source_consistent else ""
    )
    rebuildable = bool(source_available and not running and
                       (not source_consistent or has_candidate or state.get("revision")
                        or coverage.get("history_changed") or coverage.get("new_sources")))
    rebuild_reason = ("可显式重新读取全部历史并建立新的候选基线"
                      if rebuildable else
                      ("当前构图正在处理中" if running else
                       source_blocked_reason if not source_consistent else
                       "尚无已保存图或候选图可重读"))
    business = False
    if candidate_ready and bool(build) and not stale and source_consistent:
        from .graph_discussion import waiting_questions
        business = bool(waiting_questions(document))

    def item(enabled, reason):
        return {"enabled": bool(enabled), "reason": str(reason)}

    if stale:
        blocked = "候选基线落后于当前画布，请显式 rebase_current_canvas；其他写入已拒绝"
        if not source_consistent:
            blocked = source_blocked_reason
        return {**{key: item(False, blocked) for key in
                   ("update", "resume", "discuss", "repair", "save")},
                "rebuild": item(rebuildable, rebuild_reason),
                "rebase_current_canvas": item(not running and source_consistent,
                    ("可将候选基线显式更新为当前画布" if not running else "当前构图正在处理中")
                    if source_consistent else source_blocked_reason)}
    actions = {
        "rebuild": item(rebuildable, rebuild_reason),
        "rebase_current_canvas": item(False, source_blocked_reason if not source_consistent else
                                        ("候选基线已与当前画布一致" if has_candidate
                                         else "当前没有候选图可 rebase"))}
    if running:
        actions.update({key: item(False, "当前构图正在处理中") for key in
                        ("update", "resume", "discuss", "repair", "save")})
        return actions

    if not has_candidate:
        actions.update({
            "update": item(source_available and source_consistent,
                            "可以从当前画布继续整理" if source_available and source_consistent else
                            (source_blocked_reason if not source_consistent else "尚无可整理的原始来源")),
            "resume": item(False, "当前没有未完成候选图"),
            "discuss": item(False, "当前没有可讨论的候选图"),
            "repair": item(bool(state.get("revision")) and saved_canvas and source_consistent,
                            "可检查当前已保存画布" if state.get("revision") and saved_canvas and source_consistent else
                            (source_blocked_reason if not source_consistent else "当前没有可修订的已保存画布")),
            "save": item(False, "当前没有待保存候选图"),
        })
        return actions

    resumable = status in {"failed", "paused", "working", "interrupted"} or build_resumable
    actions["resume"] = item(resumable and source_consistent,
                              ("可继续当前候选片段，并纳入新增来源" if resumable and coverage.get("new_sources")
                               else "可继续当前候选片段" if resumable else
                               source_blocked_reason if not source_consistent else "当前候选无需继续"))
    updateable = (status == "ready" and source_consistent and
                  (bool(coverage.get("pending")) or bool(coverage.get("new_sources")) or
                   bool(basis.get("needs_update"))))
    actions["update"] = item(updateable,
                              "有新的来源或构图依据可整理" if updateable else
                              (source_blocked_reason if not source_consistent else
                               "候选未完成，请使用 resume" if resumable else "当前候选已完成"))
    # The discussion endpoint applies the same autobuild guard as
    # graph_discussion._candidate; a ready legacy candidate without a build
    # record is not a discussion target.
    discussable = candidate_ready and bool(build) and source_consistent
    actions["discuss"] = item(discussable,
                               ("可继续讨论当前图；仅真实 business 问题进入业务选择"
                                if discussable and not business else
                                ("存在已分类的真实业务取舍" if business and discussable else
                                  ("候选尚未就绪，不能开始讨论" if not candidate_ready or not build else
                                   "原始来源已变化，请先核对来源后讨论"))))
    # Repair is also an explicit whole-graph check.  It remains actionable
    # when the host found no diagnostic yet, including a manual candidate with
    # no autobuild record.
    repairable = candidate_ready and not coverage.get("pending") and source_consistent
    actions["repair"] = item(repairable,
                              "存在可交给 AI 修订的候选问题" if repairable else
                              ("原始来源已变化或存在读取警告，请先重新读取全部历史" if not source_consistent else
                               "候选尚未就绪，不能修订" if not candidate_ready else
                               "仍有来源未处理，不能修订" if coverage.get("pending") else
                               "当前候选没有待修订问题"))
    actions["save"] = item(bool(ready),
                            "候选已通过检查，可以保存" if ready else "候选尚未通过完整校验")
    return actions


def graph_state(state, base):
    """A lightweight graph view remains editable even if full projection fails."""
    candidate = state.get("candidate")
    document = candidate["document"] if candidate else saved_graph_document(state, base)
    elements = [copy.deepcopy(e) for e in document["entries"] if e["kind"] != "progress"]
    live = [e for e in elements if e["state"] != "retired"]
    view = {
        "nodes": [{"node_id": e["id"], "label": e["title"], "goal": e["body"], "state": e["state"]}
                  for e in live if e["kind"] == "node"],
        "edges": [{"entry_id": e["id"], **e["data"]} for e in live if e["kind"] == "edge"],
    }
    issues = list(state.get("warnings") or [])
    if state["coverage"].get("history_changed"):
        issues.append("已处理的原始来源发生变化或缺失，旧进度不可继续复用；请核对来源后选择重新读取全部历史")
    for e in live:
        if e["state"] == "conflict":
            issues.append(f"{e['title']}：要求存在冲突，可交给 AI 分析并修订，不必手动调整流程")
    spec = None
    build = _current_build(state)
    build = build or None
    if build and candidate and candidate["status"] in {"failed", "paused"}:
        build = {**build, "status": candidate["status"], "reason": candidate.get("error", "")}
    elif build and not state["running"] and build.get("status") == "working" and build.get("phase") != "waiting_user":
        build = {**build, "status": "interrupted"}
    structural = None
    if candidate or build:
        if build:
            structural = readiness(document, base, conversation_id=state["conversation_id"],
                                   revision=state["revision"] + bool(candidate))
        else:
            from .graph_inspection import diagnose
            check = diagnose(document, base, conversation_id=state["conversation_id"], revision=state["revision"] + bool(candidate))
            structural = {"ready": check["valid"], "diagnostics": check["diagnostics"]}
        if candidate:
            candidate["readiness"] = structural
        issues.extend(d["message"] for d in structural["diagnostics"]
                      if d.get("severity") != "warning")
    if build:
        build = {**build, "remaining": structural["diagnostics"]}
        if not state["running"] and not structural["ready"] and build.get("status") not in {"failed", "paused", "interrupted"}:
            build["status"] = "needs_review"
        if not candidate and build.get("graph_hash") and build["graph_hash"] != digest(base):
            build.update(review_stale=True, status="needs_review", reason="manual_edits_after_review")
        issues.extend(d["message"] for d in structural["diagnostics"]
                      if d.get("severity") != "warning")
    if candidate and digest(base) != candidate["base_graph_hash"]:
        issues.append("画布已被修改，当前生成结果不会覆盖；请按新图重新生成")
    elif not any(e["state"] == "conflict" for e in live):
        try:
            spec = project_graph(document, base, conversation_id=state["conversation_id"],
                                 revision=state["revision"] + bool(candidate)).as_dict()
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            from .graph_inspection import diagnose
            try:
                check = diagnose(document, base, conversation_id=state["conversation_id"],
                                 revision=state["revision"] + bool(candidate))
                projection_diagnostics = check["diagnostics"]
            except (ValueError, TypeError, KeyError, AttributeError) as diagnostic_exc:
                projection_diagnostics = [{"code": "projection_invalid", "path": "$", "entry_ids": [],
                                           "message": str(diagnostic_exc)}]
            if structural is None:
                structural = {"ready": False, "diagnostics": copy.deepcopy(projection_diagnostics)}
            else:
                existing_diagnostics = structural.setdefault("diagnostics", [])
                for diagnostic in projection_diagnostics:
                    if diagnostic not in existing_diagnostics:
                        existing_diagnostics.append(copy.deepcopy(diagnostic))
                structural["ready"] = False
            if candidate:
                candidate["readiness"] = structural
            concrete = [p["message"] for p in structural["diagnostics"]
                        if p.get("code") in {"forward_cycle", "loop_unreachable", "invalid_reference",
                                              "duplicate_business_key", "missing_business_key",
                                              "projection_invalid"}]
            issues.extend(concrete or [str(exc)])
    complete = not state["coverage"].get("pending") and not state["coverage"].get("history_changed")
    ready = bool(spec and complete and not issues and not state["running"]
                 and (not candidate or candidate["status"] == "ready"))
    if (state.get("basis") or {}).get("needs_update"):
        ready = False
    if structural is not None:
        ready = ready and structural["ready"]
    # A review result belongs to the snapshot that produced it.  Once the
    # canvas was edited, or a build stopped at review, the current graph needs
    # an explicit check before it can be presented as save-ready.  A local
    # repair that has passed host validation and was explicitly adopted is
    # that check; its historical review receipt remains visible separately.
    local_repair = state.get("local_repair_run") or {}
    local_repair_adopted = bool(candidate and local_repair.get("candidate_id") == candidate.get("id")
                                and local_repair.get("sequence") == candidate.get("sequence")
                                and local_repair.get("adopted"))
    from .graph_local_repair import unchanged_current_check
    local_repair_checked = unchanged_current_check(state, base)
    saved_check = (build or {}).get("structure_check") or {}
    saved_structure_checked = bool(not candidate and saved_check.get("graph_hash") == digest(base)
                                   and saved_check.get("valid") is True)
    host_structure_checked = local_repair_checked or saved_structure_checked
    # A saved graph whose only invalidation is a post-review parameter/layout
    # edit remains saveable as a plan after deterministic host checks pass.
    # Keep the old review receipt visibly stale and do not call it executable,
    # but do not strand a harmless plan edit behind the automatic review gate.
    review_only_stale = bool(
        (build or {}).get("review_stale")
        and (build or {}).get("reason") == "manual_edits_after_review"
        and structural is not None and structural.get("ready")
    )
    if (build and (build.get("review_stale") or build.get("status") in {"needs_review", "working", "interrupted"})
            and not local_repair_adopted and not review_only_stale and not host_structure_checked):
        ready = False
    if build and candidate:
        from .graph_discussion import waiting_questions
        if (build.get("phase", "done") != "done" and not local_repair_checked) or waiting_questions(document):
            ready = False
    preview = state.get("graph_candidate")
    if preview and candidate and (preview.get("candidate_id"), preview.get("sequence")) != (candidate["id"], candidate["sequence"]):
        preview = None
    if preview and state.get("history_hash") and preview.get("source_hash") != state["history_hash"]:
        preview = None
    repair_preview = state.get("local_repair_preview")
    canvas_state = _canvas_state(base)
    actions = _graph_actions(state, base, document, ready=ready, issues=issues)
    return {**{k: state[k] for k in ("project", "conversation_id", "draft_id", "revision", "coverage", "running", "progress")},
            "candidate": {k: v for k, v in candidate.items() if k in {"id", "sequence", "status", "error", "log", "mode", "readiness"}} if candidate else None,
            "elements": elements, "view": view, "issues": list(dict.fromkeys(issues)), "can_preview": ready,
            # ``can_preview`` means the deterministic plan can be previewed
            # and saved.  A stale automatic review receipt is still not an
            # execution-ready claim.
            "execution_ready": bool(ready and not review_only_stale and not host_structure_checked),
            "can_open_canvas": bool(base and base.get("nodes")),
            "preview": preview, "base_graph_hash": digest(base), "ignored_count": len(document.get("ignored", [])),
            "delivery": delivery_view(document, build), "readiness": structural, "local_repair_preview": repair_preview,
            "basis": state.get("basis"),
            "canvas_state": canvas_state, "actions": actions,
            "task": graph_task_state(state, state["running"], state.get("progress")),
            "process": graph_process_state({**state, "_graph_ready": ready, "_graph_build": build,
                                            "_canvas_saved": canvas_state["exists"],
                                            "_structure_checked": host_structure_checked},
                                            state["running"], state.get("progress"))}


def ready_graph_document(state, candidate, base, *, conversation_id):
    def check_manual_structure(document, revision):
        # Manual/selected candidates do not inherit the strict automatic-build
        # completeness gate, but every save path still shares the deterministic
        # topology and design lint check.  Warnings remain visible and do not
        # block a host-validated saved graph.
        from .graph_inspection import diagnose
        result = diagnose(document, base, conversation_id=conversation_id, revision=revision)
        blocking = [d for d in result["diagnostics"] if d.get("severity") != "warning"]
        if blocking:
            raise ValueError("结构尚未就绪，不能保存为可执行版本：" +
                             "；".join(d["message"] for d in blocking[:3]))
        return document

    if candidate:
        document = DraftStore.ready_candidate(state, candidate["id"], candidate["sequence"])
        if candidate.get("autobuild"):
            from .graph_discussion import waiting_questions
            from .graph_local_repair import unchanged_current_check
            if (candidate["autobuild"].get("phase", "done") != "done" and not unchanged_current_check(state, base)) or waiting_questions(document):
                raise ValueError("当前图仍在讨论或修改后待校验，请继续当前构建后再保存。")
            result = readiness(document, base, conversation_id=conversation_id,
                               revision=state["revision"] + 1)
            if not result["ready"]:
                # Existing hard validation failures keep their original text;
                # the stronger readiness refusal covers additional gaps.
                project_graph(document, base, conversation_id=conversation_id,
                              revision=state["revision"] + 1)
                raise ValueError("结构尚未就绪，不能保存为可执行版本：" +
                                 "；".join(d["message"] for d in result["diagnostics"][:3]))
            return document
        return check_manual_structure(document, state["revision"] + 1)
    coverage = graph_coverage(state)
    if not state["revision"] or state["warnings"] or coverage.get("pending") or coverage.get("history_changed"):
        raise ValueError("来源尚未完整处理，不能把未完成的图保存为完整版本")
    document = saved_graph_document(state, base)
    if state.get("autobuild_result"):
        result = readiness(document, base, conversation_id=conversation_id, revision=state["revision"])
        if not result["ready"]:
            project_graph(document, base, conversation_id=conversation_id, revision=state["revision"])
            raise ValueError("结构尚未就绪，不能保存为可执行版本：" +
                             "；".join(d["message"] for d in result["diagnostics"][:3]))
        return document
    return check_manual_structure(document, state["revision"])
