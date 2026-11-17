"""Lossless, version-bound review of candidate graphs, including invalid ones.

This is diagnostic data, never an executable graph or a grant to apply changes.
Rows are paged below the normal tool output limit. Large individual rows are
losslessly framed as JSON fragments instead of silently truncating requirements.
"""
from __future__ import annotations

import copy
import json
from collections import deque

from .draft_sources import digest
from .loops import LoopContract, is_loop_edge
from .workflow_draft import check_entry_refs, normalize_legacy_document, project_graph

READER_SERVICE = "orchestration.candidate_graph_reader"
VALIDATOR_SERVICE = "orchestration.candidate_graph_validator"
PAGE_CHARS = 8000
FRAGMENT_CHARS = 1800


def runtime_semantics():
    """Host-owned protocol facts, mirrored by loop-runtime regression tests."""
    from mewcode.kernel.blockers import REGISTRY, Blocker

    return [
        {"rule": "forward_dag", "detail": "普通 always/pass/fail 边在一轮内调度，移除 loop 回边后的图必须无环。fail 不是跨轮 CONTINUE。"},
        {"rule": "input_branch", "detail": "同一个完成节点的两条 pass 边默认都会匹配，不是自动互斥。受参数限制的单条路径只需 input_when={input:已声明参数名,equals:文本值}，不匹配则不调度；仅至少两条互斥路径共享 branch_group，参数未明确或不在组内选项时不能执行下游。条件与 pass/fail 是 AND。"},
        {"rule": "stage_inputs", "detail": "运行参数 collect_at=start 在启动时收集；collect_at=节点ID 在该节点开始前收集。required_when={input:参数名,equals:文本值} 只在条件匹配时必填。参数不是权限或发布批准，已有 gate 仍须独立签认。"},
        {"rule": "task_inputs", "detail": "work_key 与节点类型及父级共同标识同一工作项，修改方案复用该身份；input_bindings 逐项绑定已声明输入到上游输出或运行参数。节点生产者须声明同名 outputs，并沿非 loop 前向路径可达消费者；允许经过中间节点，不要求生产者与消费者直接相连。必须核对整条路径，只有 loop 回边或 parent_id 归属不能建立该依赖。上游产物缺口由生产节点解决，用户偏好才提问。先通过输入到结果读回的最小样本，再扩大批量；样本通过不能替代全量验收。"},
        {"rule": "hierarchy", "detail": "底稿 parent_id 只声明归属，不创建执行依赖；父节点与子节点仍需显式前向边接通输入和下游。不得把归属当作已存在的连接，也不得改用户层级来掩盖断连。"},
        {"rule": "coordination", "detail": "execution_mode=coordinate 的父节点转换为协调职责：入口只核对输入和任务分派，具体制作、写入和独立审计由显式连线的子节点执行，不得重复子节点工作或把协调完成当作内容通过。非审查协调节点按 planner 职责运行；model 绑定保留，子节点仍须单独绑定。"},
        {"rule": "verdict", "detail": "审查节点结构化 EXIT 视为 pass；CONTINUE/BLOCKED 不视为 pass。普通 fail 可匹配非通过结果，但不能借 fail 边制造普通环。"},
        {"rule": "audit_failure_route", "detail": "普通审计返修必须由明确的 audit_target 指向实现节点；若审计另有 fail 边，audit_target 必须是其中目标之一，否则失败策略停止。只有当唯一无条件 fail 边与 audit_target 冲突时，才可建议将该边对齐到已声明目标；不得从 goal、验收或输出文字猜返修目标。循环内审查按循环合同调度。"},
        {"rule": "supervision", "detail": "执行阶段需有上游计划/主控方向交接及成功路径上的独立结果审查，构图就绪检查会报告缺口。主模型职责通过实际节点和绑定承担；优先复用已有计划/审查节点，允许一组相关阶段共用审查。人工 gate、执行自评、纯汇总和仅失败时触发的审查不能替代结果验收；终点为 audit 时也须结构化 PASS。静态连通不证明语义质量或条件分支全部已覆盖，须核对分支与实际产物证据。"},
        {"rule": "after_decision", "detail": "先判定再返修需显式 repair_dispatch=after_decision、requires_repair=true、repair_node_ids。首次跳过这些返修根；最终 check_node_id 在 CONTINUE 的 repair_node_ids 中选择一个或多个已声明返修根，或仅选择 entry_node_id 处理下一项；不可混选。"},
        {"rule": "repair_topology", "detail": "判定→返修应为 loop 调度回边，返修→复核→判定必须是普通前向路径。返修根不得有普通入边、不得指向首次入口。首次入口也必须无需返修就能到判定。编译器将两种回边归属同一声明循环，不新增循环或业务节点。"},
        {"rule": "carry", "detail": "显式判定后调度在 strict/team 均保留无关已完成结果，只重跑所选根的下游并重新执行全部独立审查和判定。旧 in_round 合同不变，strict 默认仍整轮重跑。"},
        {"rule": "gates", "detail": "所有审查者按冻结 criteria 提供证据且全部 PASS 才退出。任意 BLOCKED、strict 的 UNKNOWN、预算/无进展门禁均先停止，不触发返修。先判定再返修不是跳过独立复核/用户签认。"},
        {"rule": "blocker_recovery", "detail": "capabilities.resolvers 显式指定图节点时，该节点必须在图路径上可达；human 表示人工处理。未指定某类阻塞的图内解决者时，使用下列宿主运行范围内的默认角色与清除契约，不要求把这些角色新增为图节点或普通回边。RequestAssistance 是宿主协助入口；任意 BLOCKED 仍先停止，宿主核验对应清除凭证后才允许恢复，协助结束不能替代独立业务审核。策略存在不证明当前运行已有能力、授权、实际协助回执或已恢复；用户明确指定的节点职责与具体恢复要求仍须核验，不得仅删映射或写正文承诺规避。",
         "policies": {kind.value: {"owner": policy.owner, "category": policy.category,
                                   "clearance": policy.clearance}
                      for kind, policy in REGISTRY.items() if kind != Blocker.NONE}},
        {"rule": "repair_method", "detail": "提出 mewcode.graph-repair.v1 方案后调用 InspectStageGraph(action=validate, snapshot_id=所读版本, proposal=方案对象) 只读试修；按具体错误纠正，不重复纳入对话生成节点。校验成功仍须用户预览采用/保存，不能代表业务验收。"},
    ]


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def topology(document, base):
    """Export all active topology even when project_graph cannot validate it."""
    active = [e for e in document["entries"] if e["state"] != "retired"]
    saved = {n["node_id"]: n for n in (base or {}).get("nodes", [])}
    nodes, edges, loops = [], [], []
    for e in active:
        data = e["data"]
        common = {"entry_id": e["id"], "state": e["state"]}
        if e["kind"] == "node":
            node = copy.deepcopy(saved.get(e["id"], {}))
            brief = {**node.get("plan_brief", {}), "summary": e["body"],
                     "known_inputs": data.get("inputs", []), "outputs": data.get("outputs", []),
                     "acceptance": data.get("acceptance", []), "steps": data.get("steps", [])}
            node.update(**common, node_id=e["id"], label=e["title"], goal=e["body"],
                        kind=data.get("node_kind", "implement"), parent_id=data.get("parent_id"), plan_brief=brief)
            node.update({key: copy.deepcopy(data[key]) for key in ("model", "execution_mode", "work_key", "input_bindings", "capabilities") if key in data})
            nodes.append(node)
        elif e["kind"] == "edge":
            edges.append({**common, **copy.deepcopy(data), "condition": data.get("condition", "always")})
        elif e["kind"] == "loop":
            loops.append({**common, **copy.deepcopy(data)})
    return nodes, edges, loops


def _paths(start, adjacency):
    seen, queue = {start}, deque([start])
    while queue:
        for node in adjacency.get(queue.popleft(), []):
            if node not in seen:
                seen.add(node)
                queue.append(node)
    return seen


def _witness(start, target, adjacency):
    parents, queue = {start: None}, deque([start])
    while queue and target not in parents:
        current = queue.popleft()
        for node in adjacency.get(current, []):
            if node not in parents:
                parents[node] = current
                queue.append(node)
    if target not in parents:
        return []
    path = [target]
    while parents[path[-1]] is not None:
        path.append(parents[path[-1]])
    return list(reversed(path))


def _forward_cycles(nodes, edges):
    """Linear DFS witnesses, not exponential enumeration of every cycle."""
    outgoing = {}
    for edge in edges:
        outgoing.setdefault(edge["from_node"], []).append(edge)
    color = {}
    for root in nodes:
        if color.get(root):
            continue
        color[root] = 1
        stack, path, path_edges, active = [(root, iter(outgoing.get(root, [])))], [root], [], {root: 0}
        while stack:
            node, iterator = stack[-1]
            edge = next(iterator, None)
            if edge is None:
                color[node] = 2
                stack.pop(); path.pop(); active.pop(node)
                if path_edges:
                    path_edges.pop()
                continue
            target = edge["to_node"]
            if color.get(target) == 1:
                index = active[target]
                yield path[index:] + [target], path_edges[index:] + [edge["entry_id"]]
            elif not color.get(target):
                color[target] = 1
                active[target] = len(path)
                path.append(target); path_edges.append(edge["entry_id"])
                stack.append((target, iter(outgoing.get(target, []))))


def diagnose(document, base, *, conversation_id, revision):
    document, _ = normalize_legacy_document(document)
    from .draft_keys import duplicate_diagnostics
    problems = duplicate_diagnostics(document)
    for e in document["entries"]:
        if e["state"] != "retired" and e["kind"] in {"input", "loop"}:
            field = "name" if e["kind"] == "input" else "loop_id"
            if not isinstance(e["data"].get(field), str) or not e["data"][field].strip():
                problems.append({"code": "missing_business_key", "path": f"entries/{e['id']}/data/{field}",
                                 "entry_ids": [e["id"]], "message": f"条目 {e['id']} 缺少 {field}"})
    for e in document["entries"]:
        if e["state"] == "conflict":
            problems.append({"code": "entry_conflict", "path": f"entries/{e['id']}/state",
                             "entry_ids": [e["id"]], "message": "此条目仍有未解决的要求冲突"})
        try:
            check_entry_refs(document["entries"], focus={e["id"]})
        except ValueError as exc:
            problems.append({"code": "invalid_reference", "path": f"entries/{e['id']}/data",
                             "entry_ids": [e["id"]], "message": str(exc)})
    nodes, edges, loops = topology(document, base)
    node_ids = {node["node_id"] for node in nodes}
    # Report every bad collection target before projection stops at the first
    # error. A stage label/ordinal is not an executable node identity.
    for entry in document["entries"]:
        if entry["kind"] != "input" or entry["state"] == "retired":
            continue
        target = entry["data"].get("collect_at") or "start"
        if target != "start" and target not in node_ids:
            problems.append({"code": "input_collection_target_missing",
                "path": f"entries/{entry['id']}/data/collect_at", "entry_ids": [entry["id"]],
                "actual": target, "expected": "start or an active node ID",
                "message": f"运行参数 {entry['data'].get('name', entry['id'])} 指向不存在的收集节点 {target}；请绑定有效节点 ID。"})
    forward = [e for e in edges if not is_loop_edge(e["condition"])]
    for path, ids in _forward_cycles([n["node_id"] for n in nodes], forward):
        problems.append({"code": "forward_cycle", "path": f"edges/{ids[-1]}", "entry_ids": ids,
                         "node_path": path, "edge_ids": ids,
                         "message": "普通前向边形成未声明环：" + " → ".join(path) + "；需显式循环调度，不能只清除冲突状态。"})
    outgoing, incoming = {}, {}
    for e in forward:
        outgoing.setdefault(e["from_node"], []).append(e["to_node"])
        incoming.setdefault(e["to_node"], []).append(e["from_node"])
    for index, loop in enumerate(loops):
        try:
            contract = LoopContract.from_value(loop)
        except (ValueError, TypeError) as exc:
            problems.append({"code": "invalid_loop", "path": f"meta/loops/{index}",
                             "entry_ids": [loop["entry_id"]], "message": str(exc)})
            continue
        entry, check, body = contract.entry_node_id, contract.check_node_id, contract.node_ids
        reached = _paths(entry, outgoing)
        if contract.repair_dispatch == "after_decision":
            for root in contract.repair_node_ids:
                reached |= _paths(root, outgoing)
        reaches_check = _paths(check, incoming)
        missing_entry, missing_check = sorted(set(body) - reached), sorted(set(body) - reaches_check)
        if not missing_entry and not missing_check:
            continue
        implicated = [e["entry_id"] for e in edges if e["from_node"] in body or e["to_node"] in body]
        problems.append({"code": "loop_unreachable", "path": f"meta/loops/{index}",
            "entry_ids": [loop["entry_id"], *implicated], "loop_id": loop.get("loop_id"),
            "message": "循环存在入口不可达或无法到达判定节点的路径（结构可达性，回边不计入前向路径）",
            "entry_node_id": entry, "check_node_id": check,
            "unreachable_from_entry": missing_entry, "cannot_reach_check": missing_check,
            "failure_paths": [{"node_id": n, "path_from_entry": _witness(entry, n, outgoing),
                               "outgoing_edge_ids": [e["entry_id"] for e in forward if e["from_node"] == n]}
                              for n in sorted(set(missing_entry + missing_check))]})
    # Conflict flags must not hide independent compiler errors. This copy is
    # diagnostic only: it is never returned as a repaired or adopted document.
    structural = copy.deepcopy(document)
    for e in structural["entries"]:
        if e["state"] == "conflict":
            e["state"] = "active"
    # Inspect lower layers after collapsing only equivalent duplicate data in
    # this diagnostic copy. The candidate and its duplicate errors are retained.
    from .draft_dedupe import apply_dedupe
    structural, _, _ = apply_dedupe(structural)
    projected = None
    try:
        projected = project_graph(structural, base, conversation_id=conversation_id, revision=revision)
        projection_valid = True
    except (ValueError, TypeError, KeyError) as exc:
        projection_valid = False
        if not ("收集阶段不存在" in str(exc) and any(
                row["code"] == "input_collection_target_missing" for row in problems)):
            problems.append({"code": "projection_invalid", "path": "$", "entry_ids": [], "message": str(exc)})
    if projected is not None:
        from .graph_lint import design_lint_findings
        try:
            for finding in design_lint_findings(projected):
                problem = {"code": finding.code, "path": finding.path,
                           "entry_ids": list(finding.entry_ids), "message": finding.message}
                if finding.severity == "warning":
                    problem["severity"] = "warning"
                problems.append(problem)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            problems.append({"code": "design_lint_invalid", "path": "nodes",
                             "entry_ids": [], "message": str(exc)})
    blocking = [problem for problem in problems if problem.get("severity") != "warning"]
    return {"valid": projection_valid and not blocking, "diagnostics": problems}


def snapshot(state, base, *, conversation_id, draft_id="main"):
    c = (state or {}).get("candidate")
    if not c:
        raise ValueError("当前对话没有候选图；未以已保存画布或其他对话的图替代。请先生成或打开候选图。")
    if c["base_graph_hash"] != digest(base):
        raise ValueError("画布基线已变化，候选图需要刷新；未混用旧候选与新的画布设置。")
    document, migrations = normalize_legacy_document(c["document"])
    nodes, edges, loops = topology(document, base)
    identity = {"conversation_id": conversation_id, "draft_id": draft_id, "candidate_id": c["id"],
                "sequence": c["sequence"], "revision": state["revision"], "base_hash": digest(base),
                "document_hash": digest(document), "status": c["status"]}
    from .graph_readiness import readiness
    check = readiness(document, base, conversation_id=conversation_id, revision=state["revision"] + 1)
    validation = {"valid": check["ready"], "diagnostics": check["diagnostics"]}
    # The data is the candidate, not the saved graph. The latter contributes only
    # unchanged bindings/policy, exactly as it does during graph projection.
    goals = [e["body"] or e["title"] for e in document["entries"] if e["kind"] == "goal" and e["state"] != "retired"]
    header = {"origin": "candidate", "not_executable": True,
              "goal": "\n".join(goals) or (base or {}).get("goal", ""),
              "finalizer_node_id": (base or {}).get("finalizer_node_id"),
              "saved_base_hash_matches": c["base_graph_hash"] == digest(base),
              "source_validation": "Indexed sources only; preview/adoption rechecks live history.",
              "source_warnings": state.get("warnings", []), "normalization_notes": migrations,
              "processed_count": len(c["processed_ids"]),
              "validation_valid": validation["valid"],
              "validation_scope": "Layered identity, reference, topology and projection diagnostics; not business sign-off.",
              "validation_skipped": check.get("skipped", []), "validation_skipped_reason": check.get("skipped_reason", ""),
              "repair": {k: state.get("repair_request", {}).get(k) for k in ("id", "editable_ids", "structural_ids", "reference_ids")}}
    previous_ids = set((state.get("repair_request") or {}).get("editable_ids", []))
    sections = {"graph": [header], "runtime.semantics": runtime_semantics(), "nodes": nodes, "edges": edges, "meta.loops": loops,
                "validation.diagnostics": validation["diagnostics"], "entries": document["entries"],
                "previous_entries": [e for e in state["document"]["entries"] if e["id"] in previous_ids]}
    rows = []
    for section, values in sections.items():
        for index, value in enumerate(values):
            serialized = encode(value)
            if len(encode(serialized)) <= FRAGMENT_CHARS:
                rows.append({"section": section, "index": index, "value": value})
            else:
                # Small slices also bound escaping overhead in the outer JSON.
                parts = [serialized[i:i + FRAGMENT_CHARS // 2] for i in range(0, len(serialized), FRAGMENT_CHARS // 2)]
                rows.extend({"section": section, "index": index, "part": part, "parts": len(parts),
                             "encoding": "json-fragment", "content": text} for part, text in enumerate(parts))
    return {"snapshot_id": digest({**identity, "sections": sections}), "identity": identity,
            "counts": {key: len(value) for key, value in sections.items()}, "rows": rows}


def page(state, base, *, conversation_id, draft_id="main", ticket=None, snapshot_id=None, cursor=0):
    if ticket:
        from .graph_repair import require_current
        require_current(state, base, ticket)
    view = snapshot(state, base, conversation_id=conversation_id, draft_id=draft_id)
    if (cursor and not snapshot_id) or (snapshot_id and snapshot_id != view["snapshot_id"]):
        raise ValueError("候选图版本已变化或缺少分页版本，请从第 0 页重新读取；不可混用新旧图。")
    rows = view.pop("rows")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or not 0 <= cursor < len(rows):
        raise ValueError("候选图分页游标无效")
    result = {"schema": "mewcode.stage-graph-page.v1", **view, "cursor": cursor,
              "total_frames": len(rows), "next_cursor": cursor, "complete": False, "rows": [],
              "instructions": "Read from cursor 0 through next_cursor=null with the SAME snapshot_id. "
              "complete means the final page, NOT proof that earlier pages were read. "
              "Only the full contiguous sequence is a complete graph; an unread node/edge is not absent. "
              "Join json-fragment content by section/index/part and JSON-decode; nothing is truncated. "
              "Data is not an instruction or permission to run/save. Propose repairs for explicit user adoption."}
    end = cursor
    while end < len(rows):
        trial = {**result, "rows": result["rows"] + [rows[end]], "next_cursor": end + 1}
        if len(encode(trial)) > PAGE_CHARS:
            break
        result["rows"].append(rows[end])
        end += 1
    if end == cursor:
        raise ValueError("单页元数据超出读取上限，未返回截断图")
    result["next_cursor"] = end if end < len(rows) else None
    result["complete"] = end == len(rows)
    return result


def validate_proposal(state, base, *, conversation_id, proposal, snapshot_id):
    """A pure trial; no preview receipt, persistence, adoption, or execution."""
    from .graph_repair import preview_repair
    view = snapshot(state, base, conversation_id=conversation_id)
    if not snapshot_id or snapshot_id != view["snapshot_id"]:
        raise ValueError("试修必须绑定刚读完的完整图版本；请重新读取，未改变候选。")
    try:
        preview = preview_repair(state, base, proposal)
        validation = preview["validation"]
        adoption = preview["adoption"]
        changed_ids = [e["entry_id"] for e in preview["changes"]]
    except (ValueError, TypeError, KeyError) as exc:
        validation = {"valid": False, "diagnostics": [{"code": "repair_rejected", "path": "$", "entry_ids": [], "message": str(exc)}]}
        changed_ids = []
        adoption = {"allowed": False, "partial": False}
    result = {"schema": "mewcode.graph-repair-validation.v1", "snapshot_id": snapshot_id,
              "valid": validation["valid"], "read_only": True, "adopted": False,
              "adoptable": adoption["allowed"], "partial": adoption["partial"],
              "changed_ids": changed_ids, "diagnostic_count": len(validation["diagnostics"]), "diagnostics": [],
              "instructions": "Fix reported errors and validate again (at most 3 proposals per turn); no writes occurred. "
              "Prefer valid=true. Only if adoptable=true/partial=true may you propose explicit partial adoption, "
              "clearly stating remaining unrelated errors. It cannot save/run the graph; no business acceptance implied."}
    for problem in validation["diagnostics"]:
        short = {k: problem[k] for k in ("code", "path", "message", "entry_ids")}
        short["message"] = short["message"][:600]
        short["entry_ids"] = short["entry_ids"][:8]
        if len(encode({**result, "diagnostics": result["diagnostics"] + [short]})) > PAGE_CHARS - 150:
            break
        result["diagnostics"].append(short)
    result["omitted_diagnostics"] = len(validation["diagnostics"]) - len(result["diagnostics"])
    return result
