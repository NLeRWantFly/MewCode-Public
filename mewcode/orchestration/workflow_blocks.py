"""Conversation-scoped planning materials; never edits a canvas or a run."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .draft_sources import digest, read_sources
from .draft_store import DraftStore, now
from .effective_history import references
from .workflow_draft import empty_document, quote_matches, seed_document
from .workflow_snapshots import WORKFLOW_SNAPSHOTS
from .workflow_pending import entry_identity, pending_records, proposal_key, validate_current_document


WORKFLOW_BLOCK_GUIDANCE = """
编排执行采用新版 Core：普通节点正常交付后按依赖推进；不要自动添加验收证书、隐式质量检查、最终强制验收或返修轮数。
需要判断、分支、循环时在图上明确表示。循环默认不设轮次、时间、Token 或收敛上限；只有用户明确要求才填写上限。
node.data.node_kind 使用 work（执行）、judge（判断）、branch（分支）、loop（循环）；组件参数放在 data.config。
循环体保存在 loop.config.graph 的 nodes/edges 中；branch 的出口由 config.cases/config.default 和 edge.condition 对应。
计划可以包含并行出口与汇合，不要求额外添加最终汇总节点。执行过程中用户可以编辑图、跳过、局部重跑和复用已有文件。
编排讨论每轮可用 ReadWorkflowBlocks 查询节点、来源和完整字段协议，用 SubmitWorkflowBlocks 保存新增或修订的素材。
先读再改，用户指定节点 ID 时用 entry_ids 精确读取，修改同一节点复用 ID，不因改名或移动重新编号。
设计初期先保存明确的节点和依赖，再针对未知点探索；缺项可在后续完善，不强制每轮新增节点。
已有探索证据在适用条件未变时复用，不把完整用例或反复回归当成构建草稿的前置条件。
保存的是规划素材，不改已保存画布、不启动业务、不代表用户授权。引用真实来源；你的设计标为 AI 建议。
先用主模型解决格式、技术结构和服务配置缺口。Jev 的复用/归属判断仅作建议，不可用时自行继续。
GLM-OCR 等工具服务与节点聊天模型路由分别核对；未出现在模型列表不代表服务不存在。
缺 OCR 接入参数、硬件信息或具体页批次时保留运行准备事项，不把技术缺口变成用户业务选择，不编造 foreach.items。
遇到字段错误，依据返回的 path/code/contract 修订对应条目；独立已保存素材无需重复提交。
pending 中每条都有 diagnostic_id。放弃错误的未保存提案时，单独提交 withdraw_pending=[{diagnostic_id,reason}]；
不需要创造临时节点或改用 milestone 承接。宿主保留历史并重新校验有效规格，真正的规格错误仍须修正。
ignored 仅表示排除来源内容，格式为 {source_id,reason}，不能清除 pending。
有真实来源依据时，可将未被复核的 AI 补全条目退役；保留来源及版本记录，不代表撤回用户要求或执行授权。
只有真实业务取舍或用户独有的信息才询问用户，先说明背景并给出具体可比较选项。
"""


def material_sources(state):
    return copy.deepcopy((state.get("materials") or {}).get("sources", []))


def with_material_sources(sources, state):
    rows = {s["id"]: s for s in list(sources) + material_sources(state)}
    order = list(dict.fromkeys(state.get("source_ids", []) +
        (state.get("materials") or {}).get("source_order", []) + list(rows)))
    return [rows[source_id] for source_id in order if source_id in rows]


def block_context_sources(sources, state):
    """Panel conversations are citable for planning, never swept into extraction."""
    rows = with_material_sources(sources, state)
    known = {s["id"] for s in rows}
    discussion = ((state.get("candidate") or {}).get("autobuild") or {}).get("discussion") or {}
    for message in discussion.get("messages", []):
        if message.get("id") not in known and message.get("role") in {"user", "assistant"}:
            rows.append({"id": message["id"], "role": message["role"], "text": message["text"],
                         "kind": "planning_discussion", "line": "discussion:" + message["id"],
                         "part": 1, "parts": 1, "at": str(message.get("at", ""))})
    return rows


def _baseline(state, base):
    materials = state.get("materials") or {}
    document = copy.deepcopy(materials.get("document") or state.get("document") or empty_document())
    candidate = state.get("candidate") or {}
    if candidate.get("status") == "ready":
        rows = {e["id"]: e for e in document["entries"]}
        for entry in (candidate.get("document") or {}).get("entries", []):
            rows.setdefault(entry["id"], copy.deepcopy(entry))
        document["entries"] = list(rows.values())
    if base:
        previous_canvas = {e["id"]: e for e in seed_document(materials.get("base_spec"))["entries"]}
        current_canvas = seed_document(base, previous=state.get("document"))["entries"]
        rows = {e["id"]: e for e in document["entries"]}
        # Manual canvas changes own their fields. Independent unsaved materials remain.
        for entry in current_canvas:
            old = previous_canvas.get(entry["id"])
            if old is None or any(entry.get(k) != old.get(k) for k in ("title", "body", "data", "state")):
                rows[entry["id"]] = entry
        for entry_id in previous_canvas.keys() - {e["id"] for e in current_canvas}:
            if entry_id in rows:
                rows[entry_id] = {**rows[entry_id], "state": "retired"}
        document["entries"] = list(rows.values())
    return document


def freeze_materials(candidate, state, *, force=False, sources=()):
    """Include the exact material snapshot once; later submissions cannot leak in."""
    if "materials_snapshot" in candidate and not force:
        return candidate
    result = copy.deepcopy(candidate)
    materials = state.get("materials") or {}
    material_sources_by_id = {source["id"]: source for source in materials.get("sources", [])}
    source_map = {source["id"]: source for source in sources}
    source_map.update(material_sources_by_id)
    previous = {e["id"]: e for e in ((candidate.get("materials_snapshot") or {}).get("document") or {}).get("entries", [])}
    result["materials_snapshot"] = copy.deepcopy(materials)
    existing = {e["id"]: e for e in result["document"]["entries"]}
    conflicts = []
    for entry in (materials.get("document") or {}).get("entries", []):
        old = existing.get(entry["id"])
        if force and old and entry["id"] in previous:
            merged, changed, issues = _refresh_material_entry(old, previous[entry["id"]], entry)
            conflicts.extend(issues)
            if changed:
                refs = entry.get("sources", [])
                # Entries retain historical citations alongside new material
                # payloads. Verify both against the host's frozen source scope;
                # an old quote alone is not evidence of a material revision.
                if (not any(ref.get("source_id") in material_sources_by_id for ref in refs)
                        or any(ref.get("source_id") not in source_map or not quote_matches(
                        ref.get("quote", ""), source_map[ref["source_id"]]["text"]) for ref in refs)):
                    conflicts.append({"entry_id": entry["id"], "path": "sources", "code": "material_refresh_invalid_source",
                                      "message": "素材修订缺少实际已保存来源，未更新候选字段。"})
                    continue
                merged["sources"] = list({(ref["source_id"], ref["quote"]): ref
                    for ref in old.get("sources", []) + refs}.values())
                basis = result.setdefault("history_basis", {})
                field_sources = basis.setdefault("entry_field_sources", {}).setdefault(entry["id"], {})
                field_evidence = basis.setdefault("entry_field_evidence", {}).setdefault(entry["id"], {})
                for path in changed:
                    field_sources[path] = copy.deepcopy(refs)
                    field_evidence[path] = "proposal"
                if _invalid_blocker_projection(old, previous[entry["id"]], entry):
                    basis.setdefault("material_projection_repairs", []).append({
                        "entry_id": entry["id"], "path": "data.capabilities.possible_blockers",
                        "code": "projection_invalid_enum", "before": copy.deepcopy(old["data"]["capabilities"]["possible_blockers"]),
                        "after": copy.deepcopy(entry["data"]["capabilities"]["possible_blockers"]),
                        "original_sources": copy.deepcopy(old.get("sources", [])), "sources": copy.deepcopy(refs),
                        "evidence": "proposal"})
            existing[entry["id"]] = merged
            continue
        if old and old.get("evidence") in {"user_source", "saved_graph", "manual"}:
            continue
        existing[entry["id"]] = copy.deepcopy(entry)
    if conflicts:
        # A failed refresh keeps the previous candidate intact. The existing
        # update/build error path returns these repair diagnostics to the chat.
        raise ValueError("素材与候选字段发生并发冲突，未覆盖候选；请修订后重新提交：" +
                         json.dumps({"pending": conflicts}, ensure_ascii=False))
    result["document"]["entries"] = list(existing.values())
    if result.get("history_basis"):
        result["history_basis"]["document"] = copy.deepcopy(result["document"])
    return result


def _refresh_material_entry(current, previous, proposed):
    """Three-way refresh at document fields and nested data keys."""
    missing = object()
    changed, conflicts = [], []
    repair_blockers = _invalid_blocker_projection(current, previous, proposed)

    def merge(old, live, new, path):
        if path == "data.capabilities.possible_blockers" and repair_blockers:
            changed.append(path)
            return copy.deepcopy(new)
        if old == new:
            # Descend through unchanged containers when the graph projection
            # contains an invalid machine enum. All other live values survive.
            if repair_blockers and path in {"data", "data.capabilities"} and all(
                    isinstance(value, dict) for value in (old, live, new)):
                value = {}
                for key in dict.fromkeys([*old, *live, *new]):
                    item = merge(old.get(key, missing), live.get(key, missing), new.get(key, missing), path + "." + key)
                    if item is not missing:
                        value[key] = item
                return value
            return copy.deepcopy(live) if live is not missing else missing
        if live == new:
            return copy.deepcopy(live) if live is not missing else missing
        if isinstance(old, dict) and isinstance(live, dict) and isinstance(new, dict):
            value = {}
            for key in dict.fromkeys([*old, *live, *new]):
                item = merge(old.get(key, missing), live.get(key, missing), new.get(key, missing), path + "." + key)
                if item is not missing:
                    value[key] = item
            return value
        if live == old:
            changed.append(path)
            return copy.deepcopy(new) if new is not missing else missing
        def detail(value):
            return None if value is missing else copy.deepcopy(value)
        conflicts.append({"entry_id": current["id"], "path": path, "code": "material_refresh_conflict",
            "message": "候选字段已独立修改，保留当前值；请明确合并后重新提交素材。",
            "previous": detail(old), "current": detail(live), "proposed": detail(new)})
        return copy.deepcopy(live) if live is not missing else missing

    result = copy.deepcopy(current)
    for key in ("kind", "title", "body", "data", "state"):
        value = merge(previous.get(key, missing), current.get(key, missing), proposed.get(key, missing), key)
        if value is missing:
            result.pop(key, None)
        else:
            result[key] = value
    return result, changed, conflicts


def _invalid_blocker_projection(current, previous, proposed):
    """Only repair a malformed host enum backed by unchanged valid materials."""
    from mewcode.kernel.blockers import REGISTRY
    values = {blocker.value for blocker in REGISTRY}
    def blockers(entry):
        return ((entry.get("data") or {}).get("capabilities") or {}).get("possible_blockers")
    old, live, new = blockers(previous), blockers(current), blockers(proposed)
    return (isinstance(old, list) and bool(old) and old == new and all(isinstance(value, str) and value in values for value in old)
            and isinstance(live, list) and any(not isinstance(value, str) or value not in values for value in live))


def material_refresh_view(state, *, entry_ids=()):
    """Expose the three-way inputs so the main model can repair real conflicts."""
    candidate, materials = state.get("candidate") or {}, state.get("materials") or {}
    snapshot = candidate.get("materials_snapshot") or {}
    recorded = (candidate.get("history_basis") or {}).get("material_projection_repairs", [])
    if not snapshot or snapshot.get("sequence", 0) == materials.get("sequence", 0):
        if recorded:
            return {"candidate_id": candidate.get("id"), "candidate_sequence": candidate.get("sequence"),
                "previous_material_sequence": snapshot.get("sequence", 0), "material_sequence": materials.get("sequence", 0),
                "changed_entry_ids": [], "pending": [], "projection_repairs": [],
                "projection_repair_history": copy.deepcopy(recorded), "candidate_entries": [],
                "previous_material_entries": [], "proposed_material_entries": [], "next_action": "materials_current"}
        return None
    current = {e["id"]: e for e in (candidate.get("document") or {}).get("entries", [])}
    previous = {e["id"]: e for e in (snapshot.get("document") or {}).get("entries", [])}
    proposed = {e["id"]: e for e in (materials.get("document") or {}).get("entries", [])}
    conflicts, changed_ids, repairs = [], [], []
    wanted = set(entry_ids)
    for entry_id in proposed:
        if entry_id not in current or entry_id not in previous:
            continue
        if wanted and entry_id not in wanted:
            continue
        _, changed, issues = _refresh_material_entry(current[entry_id], previous[entry_id], proposed[entry_id])
        conflicts.extend(issues)
        if changed:
            changed_ids.append(entry_id)
        if _invalid_blocker_projection(current[entry_id], previous[entry_id], proposed[entry_id]):
            repairs.append({"entry_id": entry_id, "path": "data.capabilities.possible_blockers", "code": "projection_invalid_enum",
                "before": copy.deepcopy(current[entry_id]["data"]["capabilities"]["possible_blockers"]),
                "after": copy.deepcopy(proposed[entry_id]["data"]["capabilities"]["possible_blockers"]),
                "original_sources": copy.deepcopy(current[entry_id].get("sources", [])),
                "sources": copy.deepcopy(proposed[entry_id].get("sources", [])), "evidence": "proposal"})
    conflict_ids = list(dict.fromkeys(issue["entry_id"] for issue in conflicts))
    return {"candidate_id": candidate.get("id"), "candidate_sequence": candidate.get("sequence"),
        "previous_material_sequence": snapshot.get("sequence", 0), "material_sequence": materials.get("sequence", 0),
        "changed_entry_ids": sorted(changed_ids), "pending": conflicts, "projection_repairs": repairs,
        "projection_repair_history": copy.deepcopy(recorded),
        "candidate_entries": [copy.deepcopy(current[key]) for key in conflict_ids],
        "previous_material_entries": [copy.deepcopy(previous[key]) for key in conflict_ids],
        "proposed_material_entries": [copy.deepcopy(proposed[key]) for key in conflict_ids],
        "next_action": "merge_conflicting_fields_and_submit_materials" if conflicts else "materials_can_refresh",
        "repair_instruction": "逐字段合并 previous/current/proposed。保留候选当前字段时，将 current 值写入下一次素材修订；"
                              "引用真实来源并保留用户取舍，修订后重新读取版本并提交构图。"}


def workflow_basis(state, sources, base):
    """Semantic input identity, separate from the complete provenance ledger.

    Generated candidates are outputs of a build, not new requirements. Saved
    materials and their actual cited payloads remain part of the input identity.
    """
    from .task_design import host_task_control
    users = [s for s in sources if s.get("role") == "user" and not (
        s.get("kind") == "message" and host_task_control(s["text"], has_attachments=bool(s.get("attachments"))))]
    materials = state.get("materials") or {}
    document = _baseline({**state, "candidate": None}, base)
    cited_ids = {ref.get("source_id") for entry in document.get("entries", []) for ref in entry.get("sources", [])}
    cited_ids.update(ref.get("source_id") for refs in materials.get("requirement_sources", {}).values() for ref in refs)
    cited = [{key: source.get(key) for key in ("id", "role", "kind", "text", "attachments")}
             for source in sources if source["id"] in cited_ids]
    return {"sequence": materials.get("sequence", 0), "source_hash": digest([s["id"] for s in sources]),
        "user_source_hash": digest([(s["id"], s["text"]) for s in users]), "base_graph_hash": digest(base),
        "requirement_fingerprint": digest({"users": [(s["id"], s["text"], s.get("attachments", [])) for s in users],
            "document": document, "cited_sources": cited, "pending": materials.get("pending", []),
            "pending_upserts": materials.get("pending_upserts", []),
            "requirement_sources": materials.get("requirement_sources", {})})}


def compact_task_design(value):
    if not value:
        return None
    result = {key: value.get(key) for key in ("task_id", "revision", "intent", "status")}
    result["reason"] = str(value.get("reason") or "")[:240]
    if value.get("build_diagnostics"):
        result["build_diagnostics"] = copy.deepcopy(value["build_diagnostics"])
    if value.get("health"):
        from .design_health import compact_health
        result["health"] = compact_health(value["health"])
    evidence = ((value.get("build_request") or {}).get("failure_evidence") or {})
    failure = evidence.get("failure") if isinstance(evidence, dict) else None
    if isinstance(failure, dict) and failure:
        result["failure"] = {key: copy.deepcopy(failure[key]) for key in
            ("phase", "stage", "batch", "category", "exception_type", "http_status", "retryable", "route")
            if key in failure}
        result["failure"]["summary"] = str(failure.get("summary") or "")[:1200]
    spec = value.get("specification") or {}
    result["specification"] = {key: spec.get(key) for key in ("material_sequence", "node_count")}
    return result


class WorkflowBlocks:
    def __init__(self, project, conversation_id, *, source_path=None, base_spec=None, guard=None,
                 model_routes=None, tool_services=None, run_store=None):
        self.project = str(Path(project).resolve())
        self.conversation_id = conversation_id
        self.source_path = Path(source_path or Path(project) / ".mewcode" / "sessions" / f"{conversation_id}.jsonl")
        self.base_spec = base_spec or (lambda: None)
        self.guard = guard or (lambda: True)
        self.model_routes = model_routes or (lambda: [])
        self.tool_services = tool_services or (lambda: [])
        self.run_store = run_store

    @property
    def snapshot_scope(self):
        return (os.path.normcase(self.project), self.conversation_id, os.path.normcase(str(self.source_path.resolve())))

    def _check(self):
        if not self.guard():
            raise ValueError("项目或对话已切换，请重新查询当前节点素材。")

    def _context_sources(self, sources, state):
        # Adopted choices survive build restarts in the scoped source ledger.
        # They remain planning evidence and must not enter source_ids/extraction.
        persisted = DraftStore.read_planning_sources(self.project, self.conversation_id)
        return block_context_sources([*sources, *persisted], state)

    def _capture(self, *, entry_ids=(), query="", source_ids=()):
        from .workflow_draft import entry_contract
        from .task_design import design_view
        self._check()
        state = DraftStore.read_state(self.project, self.conversation_id) or {}
        base = self.base_spec()
        document = _baseline(state, base)
        if self.run_store:
            from .collab_service import bound_record
            run = bound_record(self.run_store, state.get("task_design"), self.conversation_id)
            if run:
                document = {"entries": copy.deepcopy(list(run["collaboration"]["entries"].values())), "ignored": []}
        sources, warnings, _ = read_sources(self.source_path)
        sources = self._context_sources(sources, state)
        rows = document["entries"]
        wanted = set(entry_ids)
        if query:
            wanted.update(e["id"] for e in rows if query.casefold() in (e["title"] + " " + e["body"]).casefold())
        if entry_ids or query:
            adjacent = [e for e in rows if e["id"] in wanted or references(e) & wanted]
            wanted.update(e["id"] for e in adjacent)
            wanted.update(ref for e in adjacent for ref in references(e))
            rows = [e for e in rows if e["id"] in wanted or e["kind"] in {"goal", "constraint", "input"}]
        materials = state.get("materials") or {}
        missing = [{"entry_id": e["id"], "fields": [k for k in ("inputs", "outputs")
                     if not e["data"].get(k)]} for e in rows if e["kind"] == "node"]
        missing = [item for item in missing if item["fields"]]
        basis = workflow_basis(state, sources, base)
        value = {**basis, "document": {"entries": rows, "ignored": document["ignored"]},
                "sources": [s for s in sources if s["id"] in source_ids] if source_ids else [],
                "source_index": [{"id": s["id"], "role": s["role"], "excerpt": s["text"][:220]} for s in sources],
                "missing": missing, "pending": pending_records(materials.get("pending", []), self.snapshot_scope),
                "pending_upserts": materials.get("pending_upserts", []),
                "pending_history": materials.get("pending_history", []),
                "pending_contract": {"withdraw_pending": [{"diagnostic_id": "pending 中的编号", "reason": "非空撤回理由"}],
                    "instruction": "单独撤回未保存的错误提案；不修改有效规格，重新校验后剩余错误仍阻止构图。",
                    "ignored": {"source_id": "真实来源编号", "reason": "排除此来源内容的理由；不是诊断清理"}},
                "requirement_sources": materials.get("requirement_sources", {}),
                "versions": materials.get("versions", []), "contract": entry_contract((state.get("task_design") or {}).get("orchestration_mode", "classic")), "warnings": warnings,
                "available_model_routes": self.model_routes(), "available_tool_services": self.tool_services(),
                "task_design": design_view(state),
                "material_refresh": material_refresh_view(state, entry_ids=[e["id"] for e in rows] if entry_ids or query else ()),
                "planning_only": True, "execution_authorization": False}
        WORKFLOW_SNAPSHOTS.remember_basis(self.snapshot_scope, basis)
        self._check()
        return value, sources

    def read(self, *, entry_ids=(), query="", source_ids=()):
        return self._capture(entry_ids=entry_ids, query=query, source_ids=source_ids)[0]

    def current_basis(self):
        """Compact current commit basis; does not create or alter conversation state."""
        from .task_design import design_view
        self._check()
        state = DraftStore.read_state(self.project, self.conversation_id) or {}
        sources, warnings, _ = read_sources(self.source_path)
        current = {**workflow_basis(state, self._context_sources(sources, state), self.base_spec()),
                   "task_design": compact_task_design(design_view(state)), "warnings": warnings}
        self._check()
        return current

    def read_page(self, *, entry_ids=(), query="", source_ids=(), snapshot_id="", cursor="", offset=0, limit=8000):
        self._check()
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 9000:
            raise ValueError("SNAPSHOT_PAGE：offset/limit 无效，请使用返回的分页参数。")
        if cursor:
            try:
                cursor_snapshot, cursor_offset = cursor.rsplit(":", 1)
                cursor_offset = int(cursor_offset)
            except (ValueError, AttributeError) as exc:
                raise ValueError("SNAPSHOT_CURSOR：分页游标无效，请重新读取。") from exc
            if (snapshot_id and snapshot_id != cursor_snapshot) or offset not in (0, cursor_offset) or cursor_offset < 0:
                raise ValueError("SNAPSHOT_CURSOR：游标与快照/offset 不匹配，请重新读取。")
            snapshot_id, offset = cursor_snapshot, cursor_offset
        selection = {"entry_ids": list(entry_ids), "query": query, "source_ids": list(source_ids)}
        if snapshot_id:
            row = WORKFLOW_SNAPSHOTS.get(self.snapshot_scope, snapshot_id)
            previous = row["selection"]
            if any(selection[key] and selection[key] != previous[key] for key in ("entry_ids", "query")):
                raise ValueError("SNAPSHOT_QUERY：分页期间不能改变 entry_ids/query，请重新读取。")
            if source_ids and list(source_ids) != previous["source_ids"]:
                if offset:
                    raise ValueError("SNAPSHOT_QUERY：分页期间不能改变 source_ids，请从 offset=0 读取来源。")
                value = row["value"]
                source_map = {s["id"]: s for s in row["sources"]}
                if not set(source_ids).issubset(source_map):
                    raise ValueError("SNAPSHOT_SOURCE：来源不在此快照中，请重新读取当前来源。")
                value["sources"] = [source_map[sid] for sid in source_ids]
                snapshot_id = WORKFLOW_SNAPSHOTS.create(self.snapshot_scope, value, row["sources"],
                    {**previous, "source_ids": list(source_ids)})
                row = WORKFLOW_SNAPSHOTS.get(self.snapshot_scope, snapshot_id)
        else:
            if offset:
                raise ValueError("SNAPSHOT_REQUIRED：后续分页必须传首屏 snapshot_id 或 next_cursor；请从 offset=0 重新读取。")
            value, sources = self._capture(entry_ids=entry_ids, query=query, source_ids=source_ids)
            snapshot_id = WORKFLOW_SNAPSHOTS.create(self.snapshot_scope, value, sources, selection)
            row = WORKFLOW_SNAPSHOTS.get(self.snapshot_scope, snapshot_id)
        content, value = row["content"], row["value"]
        if offset > len(content):
            raise ValueError("SNAPSHOT_OFFSET：offset 超过此快照长度，请使用返回的 next_cursor。")
        end = min(len(content), offset + limit)
        pending = [{key: issue.get(key) for key in ("diagnostic_id", "entry_id", "path", "code", "occurrences")} for issue in value["pending"][:8]]
        header = {key: value[key] for key in ("sequence", "source_hash", "user_source_hash", "requirement_fingerprint", "base_graph_hash")}
        header.update(snapshot_id=snapshot_id, task_design=compact_task_design(value["task_design"]),
            missing=value["missing"][:12], missing_count=len(value["missing"]), pending=pending, pending_count=len(value["pending"]),
            offset=offset, next_offset=end, total_chars=len(content), more=end < len(content),
            next_cursor=f"{snapshot_id}:{end}" if end < len(content) else "")
        header["section_ranges"] = {key: {**bounds, "cursor": f"{snapshot_id}:{bounds['offset']}"}
                                    for key, bounds in row["section_ranges"].items()}
        refresh = value.get("material_refresh")
        if refresh:
            header["material_refresh"] = {key: refresh[key] for key in ("candidate_id", "candidate_sequence",
                "previous_material_sequence", "material_sequence", "next_action")}
            header["material_refresh"]["pending_count"] = len(refresh["pending"])
            header["material_refresh"]["projection_repair_count"] = len(refresh["projection_repairs"])
            header["material_refresh"]["projection_repair_history_count"] = len(refresh["projection_repair_history"])
            header["material_refresh"]["pending"] = [{key: issue[key] for key in ("entry_id", "path", "code")}
                                                      for issue in refresh["pending"][:8]]
        # The normal tool result is capped at 10k characters. Reserve room for
        # the compact header so truncation cannot silently drop a page suffix.
        budget = max(1, 9700 - len(json.dumps(header, ensure_ascii=False)) - 1)
        end = min(len(content), offset + min(limit, budget))
        header.update(next_offset=end, more=end < len(content), next_cursor=f"{snapshot_id}:{end}" if end < len(content) else "")
        self._check()
        return header, content[offset:end]

    def _validate_basis(self, current, *, expected_sequence, source_hash, base_graph_hash,
                        snapshot_id="", requirement_fingerprint=""):
        previous = None
        if snapshot_id:
            previous = WORKFLOW_SNAPSHOTS.get(self.snapshot_scope, snapshot_id)["value"]
            if any(previous[key] != value for key, value in (("sequence", expected_sequence),
                    ("source_hash", source_hash), ("base_graph_hash", base_graph_hash))):
                raise ValueError("STALE：提交版本与读取快照不一致，请重新读取；本次未覆盖新内容。")
            if requirement_fingerprint and previous["requirement_fingerprint"] != requirement_fingerprint:
                raise ValueError("STALE：语义指纹与快照不一致，请重新读取；本次未覆盖新内容。")
        elif requirement_fingerprint:
            previous = {"requirement_fingerprint": requirement_fingerprint}
        elif source_hash != current["source_hash"]:
            previous = WORKFLOW_SNAPSHOTS.legacy_basis(self.snapshot_scope, sequence=expected_sequence,
                source_hash=source_hash, base_graph_hash=base_graph_hash)
            if previous is None:
                raise ValueError("STALE：旧来源版本未绑定有效快照，请重新读取并传 snapshot_id/requirement_fingerprint；本次未覆盖新内容。")
        if (expected_sequence != current["sequence"] or base_graph_hash != current["base_graph_hash"]
                or previous is not None and previous["requirement_fingerprint"] != current["requirement_fingerprint"]):
            raise ValueError("STALE：素材、来源或画布版本已变化，请重新读取；本次未覆盖新内容。")
        return current

    def validate_snapshot(self, *, expected_sequence, source_hash, base_graph_hash, snapshot_id="", requirement_fingerprint=""):
        current = self.current_basis()
        if current["warnings"]:
            raise ValueError("来源尚未完整保存，请稍后重新查询；未修改素材。")
        return self._validate_basis(current, expected_sequence=expected_sequence, source_hash=source_hash,
            base_graph_hash=base_graph_hash, snapshot_id=snapshot_id, requirement_fingerprint=requirement_fingerprint)

    def submit(self, *, expected_sequence, source_hash, base_graph_hash, upserts, ignored, turn_id="",
               snapshot_id="", requirement_fingerprint="", withdraw_pending=()):
        from .workflow_draft import apply_patch_fragments, entry_contract
        self._check()
        store = DraftStore(self.project, self.conversation_id)
        fingerprint = digest([upserts, ignored, source_hash, base_graph_hash, withdraw_pending])
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = store._get(db)
            task = state.get("task_design") or {}
            if task.get("orchestration_mode") == "collaborative" and task.get("run_id"):
                raise ValueError("该任务已进入协作运行；有效规格由运行账本持有，请在任务详情提交规格修订。")
            base = self.base_spec()
            original_sources, warnings, _ = read_sources(self.source_path)
            if warnings:
                raise ValueError("来源尚未完整保存，请稍后重新查询；未修改素材。")
            materials = state.get("materials") or {}
            sources = self._context_sources(original_sources, state)
            current = workflow_basis(state, sources, base)
            if materials.get("last_submission") == fingerprint:
                result_basis = materials.get("last_result_basis")
                if result_basis and all(result_basis[key] == current[key] for key in
                        ("sequence", "base_graph_hash", "requirement_fingerprint")):
                    if snapshot_id:
                        WORKFLOW_SNAPSHOTS.get(self.snapshot_scope, snapshot_id)
                    return {**copy.deepcopy(materials["last_receipt"]), "cached": True}
                raise ValueError("STALE：素材、来源或画布版本已变化，请重新读取；本次未覆盖新内容。")
            self._validate_basis(current, expected_sequence=expected_sequence, source_hash=source_hash,
                base_graph_hash=base_graph_hash, snapshot_id=snapshot_id, requirement_fingerprint=requirement_fingerprint)
            old_pending = pending_records(materials.get("pending", []), self.snapshot_scope)
            if not isinstance(withdraw_pending, (list, tuple)) or len(withdraw_pending) > 200:
                raise ValueError("withdraw_pending 必须为最多200条的撤回数组。")
            withdrawals = {}
            for item in withdraw_pending:
                if (not isinstance(item, dict) or set(item) != {"diagnostic_id", "reason"}
                        or not isinstance(item.get("diagnostic_id"), str)
                        or not isinstance(item.get("reason"), str) or not item["reason"].strip()
                        or len(item["reason"]) > 1000):
                    raise ValueError("撤回须提供 pending 中的 diagnostic_id 和非空 reason（最多1000字）。")
                if item["diagnostic_id"] in withdrawals:
                    raise ValueError("同一诊断编号不能重复撤回。")
                withdrawals[item["diagnostic_id"]] = item["reason"]
            if withdrawals and (upserts or ignored):
                raise ValueError("请将 withdraw_pending 作为独立提交，不与 upserts 或 ignored 混用。")
            if set(withdrawals) - {row["diagnostic_id"] for row in old_pending}:
                raise ValueError("STALE：诊断已变化或不属于当前对话，请重新读取后定向撤回。")
            if not upserts and not ignored and not withdrawals:
                return {"sequence": materials.get("sequence", 0), "saved_entry_ids": [], "changed_entry_ids": [],
                        "changes": [], "pending": old_pending, "contract": None,
                        "planning_only": True, "execution_authorization": False, "cached": False}
            document = _baseline(state, base)
            before = {e["id"]: e for e in document["entries"]}
            source_map = {s["id"]: s for s in sources}
            prepared, generated, provenance, pre_errors = [], [], {}, []
            for raw in upserts:
                entry = copy.deepcopy(raw)
                refs = entry.get("sources", [])
                if (not refs or any(not isinstance(r, dict) or r.get("source_id") not in source_map
                                    or not isinstance(r.get("quote"), str)
                                    or not quote_matches(r["quote"], source_map[r["source_id"]]["text"]) for r in refs)):
                    pre_errors.append({"entry_id": entry.get("id"), "path": "sources", "code": "invalid_source",
                                       "message": "请引用查询返回的真实来源及逐字引文。"})
                    entry["sources"] = []
                    prepared.append(entry)
                    continue
                if (entry.get("kind") == "decision" and isinstance(entry.get("data"), dict)
                        and entry["data"].get("category") == "confirmed"):
                    pre_errors.append({"entry_id": entry.get("id"), "path": "data.category", "code": "proposal_not_confirmation",
                                       "message": "助手提交的是设计素材；category 使用 designed，不能记录用户执行授权。"})
                    entry["sources"] = []
                    prepared.append(entry)
                    continue
                if entry_identity(entry.get("id")) is None:
                    # The schema validator reports malformed/anonymous IDs;
                    # never use them as provenance dictionary keys.
                    prepared.append(entry)
                    continue
                text = json.dumps({k: entry.get(k) for k in ("id", "kind", "title", "body", "data", "state")},
                                  ensure_ascii=False, sort_keys=True)
                source = {"id": "s_material_" + digest([self.conversation_id, turn_id, text, refs])[:24],
                          "role": "assistant", "kind": "workflow_material", "text": text,
                          "line": "material:" + str(entry.get("id") or ""), "part": 1, "parts": 1, "at": now(),
                          "requirement_sources": refs, "turn_id": turn_id}
                generated.append(source)
                provenance[entry.get("id")] = refs
                # AI-authored material is cited to its own actual host-saved payload.
                # User quotations remain separate supporting requirements, never grants.
                entry["sources"] = [{"source_id": source["id"], "quote": text[:1000]}]
                prepared.append(entry)
            known_generated = {s["id"] for s in sources}
            generated = [s for s in generated if s["id"] not in known_generated]
            patch = {"upserts": prepared, "ignored": ignored}
            batch_sources = generated + [source_map[r["source_id"]] for r in ignored
                if isinstance(r, dict) and r.get("source_id") in source_map]
            result = apply_patch_fragments(document, patch, batch_sources, context=sources,
                                           allow_ai_retirement=True, orchestration_mode=task.get("orchestration_mode", "classic"))
            for entry in result.document["entries"]:
                if entry["id"] in result.accepted_entry_ids:
                    entry["evidence"] = "proposal"
                    old = before.get(entry["id"])
                    if old and old.get("evidence") in {"user_source", "saved_graph", "manual"} and any(
                            entry.get(k) != old.get(k) for k in ("title", "body", "data", "state")):
                        entry["state"] = "conflict"
            sequence = materials.get("sequence", 0) + 1
            changes = [{"entry_id": entry_id, "before": before.get(entry_id),
                        "after": next(e for e in result.document["entries"] if e["id"] == entry_id)}
                       for entry_id in result.accepted_entry_ids]
            changed_ids = [c["entry_id"] for c in changes if any(
                (c["before"] or {}).get(k) != c["after"].get(k) for k in ("kind", "title", "body", "data", "state"))]
            attempted_ids = {e["id"] for e in upserts if isinstance(e.get("id"), str) and e["id"]}
            removed = [p for p in old_pending if p["diagnostic_id"] in withdrawals or entry_identity(p.get("entry_id")) in attempted_ids]
            pending = [p for p in old_pending if p not in removed]
            pending += pre_errors + result.pending_issues
            if withdrawals or any(p.get("origin") == "document" for p in old_pending):
                pending = [p for p in pending if p.get("origin") != "document"]
                pending += validate_current_document(result.document, sources + generated,
                    task.get("orchestration_mode", "classic"))
            pending = pending_records(pending, self.snapshot_scope)
            pending_entries = {proposal_key(e): e for e in materials.get("pending_upserts", [])}
            if withdrawals:
                withdrawn_entry_ids = {entry_identity(p.get("entry_id")) for p in removed}
                remaining_entry_ids = {entry_identity(p.get("entry_id")) for p in pending}
                for key, raw in list(pending_entries.items()):
                    entry_id = entry_identity(raw.get("id"))
                    if (entry_id in withdrawn_entry_ids and entry_id not in remaining_entry_ids
                            or not entry_id and not any(not value for value in remaining_entry_ids)):
                        pending_entries.pop(key)
            for raw in upserts:
                if raw.get("id") in result.accepted_entry_ids:
                    pending_entries.pop(proposal_key(raw), None)
                else:
                    pending_entries[proposal_key(raw)] = copy.deepcopy(raw)
            history = copy.deepcopy(materials.get("pending_history", []))
            history.append({"sequence": sequence, "at": now(), "turn_id": turn_id,
                "action": "withdraw_pending" if withdrawals else "submit",
                "removed": [{**p, "reason": withdrawals.get(p["diagnostic_id"], "由同 ID 提案修订替代")} for p in removed],
                "reported": copy.deepcopy(pre_errors + result.pending_issues),
                "rejected_upserts": [copy.deepcopy(raw) for raw in upserts if raw.get("id") not in result.accepted_entry_ids],
                "withdrawn_upserts": [copy.deepcopy(raw) for raw in materials.get("pending_upserts", [])
                    if proposal_key(raw) not in pending_entries] if withdrawals else []})
            receipt = {"sequence": sequence, "saved_entry_ids": result.accepted_entry_ids,
                       "changed_entry_ids": changed_ids, "changes": changes, "pending": pending,
                       "withdrawn_pending_ids": sorted(withdrawals),
                       "contract": entry_contract((state.get("task_design") or {}).get("orchestration_mode", "classic")) if pending else None,
                       "planning_only": True, "execution_authorization": False, "cached": False}
            versions = copy.deepcopy(materials.get("versions", []))
            if changed_ids:
                versions.append({"sequence": sequence, "at": now(), "turn_id": turn_id, "changes": changes})
            state["materials"] = {"sequence": sequence, "document": result.document, "pending": pending,
                                  "pending_upserts": list(pending_entries.values()),
                                  "pending_history": history,
                                  "sources": material_sources(state) + generated, "base_spec": copy.deepcopy(base),
                                  "source_order": [s["id"] for s in sources + generated if s.get("kind") != "planning_discussion"],
                                  "base_graph_hash": digest(base), "versions": versions,
                                  "requirement_sources": {**materials.get("requirement_sources", {}), **provenance},
                                  "last_submission": fingerprint, "last_receipt": receipt}
            # Check again immediately before the write. Assistant observations
            # may append during validation; genuine requirements/canvas edits may not.
            latest_sources, latest_warnings, _ = read_sources(self.source_path)
            latest = workflow_basis({**state, "materials": materials},
                self._context_sources(latest_sources, {**state, "materials": materials}), self.base_spec())
            if latest_warnings or any(latest[key] != current[key] for key in
                    ("sequence", "base_graph_hash", "requirement_fingerprint")):
                raise ValueError("STALE：素材、来源或画布版本已变化，请重新读取；本次未覆盖新内容。")
            state["materials"]["last_result_basis"] = workflow_basis(state,
                self._context_sources(latest_sources, state), base)
            self._check()
            store._put(db, state)
        return receipt
