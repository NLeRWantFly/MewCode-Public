"""Reduced topology of the live failure; contains no private conversation text."""
import copy


def entry(entry_id, kind, data, *, evidence="manual", state="active"):
    return {"id": entry_id, "kind": kind, "title": entry_id, "body": "保留既有职责与验收",
            "data": data, "state": state, "evidence": evidence, "sources": []}


def failed_document():
    nodes = [("prepare", "plan"), ("node_loop_topology_repair", "plan"),
             ("node_sample_make", "implement"), ("node_sample_audit", "audit"),
             ("node_sample_human_gate", "gate"), ("node_ocr_first_read", "implement"),
             ("node_ocr_second_read", "implement"), ("node_ocr_audit", "audit"),
             ("node_ocr_repair", "implement"), ("node_ocr_decide", "decision"), ("finish", "reduce")]
    entries = [entry(key, "node", {"node_kind": kind, "outputs": ["版本及证据"],
                                   "acceptance": ["保持原始验收要求"]}) for key, kind in nodes]
    links = [("prepare_sample", "prepare", "node_sample_make", "always"),
             ("edge_sample_make_to_audit", "node_sample_make", "node_sample_audit", "always"),
             ("edge_sample_audit_to_gate", "node_sample_audit", "node_sample_human_gate", "always"),
             ("edge_sample_gate_fail_repair", "node_sample_human_gate", "node_sample_make", "fail"),
             ("sample_ocr", "node_sample_human_gate", "node_ocr_first_read", "pass"),
             ("ocr_second", "node_ocr_first_read", "node_ocr_second_read", "always"),
             ("ocr_audit", "node_ocr_second_read", "node_ocr_audit", "always"),
             ("ocr_verdict", "node_ocr_audit", "node_ocr_decide", "always"),
             ("ocr_repair", "node_ocr_decide", "node_ocr_repair", "loop"),
             ("ocr_recheck", "node_ocr_repair", "node_ocr_audit", "always"),
             ("ocr_finish", "node_ocr_decide", "finish", "pass")]
    entries += [entry(key, "edge", {"from_node": start, "to_node": end, "condition": condition})
                for key, start, end, condition in links]
    conflict = entry("edge_ocr_decide_continue_to_first_read", "edge", {
        "from_node": "node_ocr_decide", "to_node": "node_ocr_first_read", "condition": "loop"},
        evidence="proposal", state="conflict")
    conflict["body"] = "首次入口处理未识别原子，返修派发不能混选；这份连线建议尚未确认"
    conflict["sources"] = [{"source_id": "assistant_suggestion", "quote": "建议边定义"}]
    entries.append(conflict)
    entries.append(entry("loop_atomic_ocr_audit_repair", "loop", {
        "loop_id": "loop_atomic_ocr_audit_repair", "entry_node_id": "node_ocr_first_read",
        "check_node_id": "node_ocr_decide", "node_ids": [key for key, _ in nodes if key.startswith("node_ocr_")],
        "reviewer_node_ids": ["node_ocr_audit", "node_ocr_decide"],
        "exit_condition": "原子验收与对应版本独立复核通过，未知不算通过",
        "criteria": [{"criterion_id": "atomic", "acceptance_test": "保留原子复核验收"}],
        "control_mode": "strict", "requires_repair": True,
        "repair_node_ids": ["node_ocr_repair"], "repair_dispatch": "after_decision",
    }))
    return {"schema_version": 1, "entries": entries, "ignored": []}


def correct_proposal():
    return {"upserts": [
        {"id": "loop_sample_quality", "kind": "loop", "title": "小样返修循环", "body": "保留独立审计与人工门禁",
         "data": {"loop_id": "loop_sample_quality", "entry_node_id": "node_sample_make",
                  "check_node_id": "node_sample_human_gate",
                  "node_ids": ["node_sample_make", "node_sample_audit", "node_sample_human_gate"],
                  "reviewer_node_ids": ["node_sample_audit", "node_sample_human_gate"],
                  "exit_condition": "既有小样验收通过且用户完成既有签认",
                  "criteria": [{"criterion_id": "sample", "acceptance_test": "核对既有小样验收与签认"}],
                  "control_mode": "team"}, "rationale": "为既有反馈声明循环，不添加预算或改变人工门禁"},
        {"id": "edge_sample_gate_fail_repair", "data": {"condition": "loop"},
         "rationale": "这条既有失败反馈属于跨轮回边，不能作为普通前向依赖"},
        {"id": "prepare_topology", "kind": "edge", "title": "准备后核验拓扑", "body": "使用真实原子清单",
         "data": {"from_node": "prepare", "to_node": "node_loop_topology_repair", "condition": "always"},
         "rationale": "补齐孤立核验节点的输入"},
        {"id": "topology_sample", "kind": "edge", "title": "拓扑通过后进入小样", "body": "保留后续审计与签认",
         "data": {"from_node": "node_loop_topology_repair", "to_node": "node_sample_make", "condition": "pass"},
         "rationale": "补齐核验节点的下游"},
        {"id": "edge_ocr_decide_continue_to_first_read", "resolve_conflict": True,
         "body": "判定仅选择首次入口处理新的未识别原子；已识别原子的返修仅选择返修根，两类派发不能混选",
         "rationale": "按现有after_decision合同区分下一项与返修，不改变五节点拓扑或用户验收"},
    ], "notes": "只修现图的反馈与连接，未执行任务或签认"}


def rejected_proposal():
    value = copy.deepcopy(correct_proposal())
    value["upserts"][0]["data"].update(continue_condition="不通过则继续", budget={"input_id": "sample_budget"})
    value["upserts"][1]["data"]["loop_id"] = "loop_sample_quality"
    value["upserts"] = value["upserts"][:2]
    value["upserts"].append({"id": "order", "kind": "decision", "title": "核验顺序", "body": "先核验再小样",
                              "data": {"category": "defaulted", "value": ["prepare", "sample"]},
                              "rationale": "故意复现旧提案错误"})
    return value


def hierarchical_failed_document():
    """Keep the observed 24-node hierarchy and feedback edges, with generic text."""
    value = failed_document()
    remap = {"prepare": "node_source_atomic_partition", "finish": "node_final_gate_delivery"}
    for row in value["entries"]:
        row["id"] = remap.get(row["id"], row["id"])
        for key in ("from_node", "to_node"):
            if key in row["data"]:
                row["data"][key] = remap.get(row["data"][key], row["data"][key])
    nodes = [("node_material_capability", "plan", None), ("node_sample_gate", "gate", None),
             ("node_atomic_ocr", "implement", None), ("node_assemble_store", "implement", None),
             ("node_rag_retrieval", "implement", None), ("node_independent_audit", "audit", None),
             ("node_chapter_book_completeness_audit", "audit", None),
             ("node_assemble_cross_page_question", "implement", "node_assemble_store"),
             ("node_organize_knowledge_point", "implement", "node_assemble_store"),
             ("node_link_question_knowledge", "audit", "node_assemble_store"),
             ("node_write_sqlite_index", "implement", "node_assemble_store"),
             ("node_data_audit", "audit", "node_independent_audit"),
             ("node_rag_audit", "audit", "node_independent_audit")]
    for key, kind, parent in nodes:
        value["entries"].append(entry(key, "node", {"node_kind": kind, "outputs": ["版本及证据"],
            "acceptance": ["保持原始验收要求"], **({"parent_id": parent} if parent else {})}))
    for row in value["entries"]:
        if row["kind"] != "node":
            continue
        if row["id"] == "node_final_gate_delivery":
            row["data"]["node_kind"] = "gate"
        if row["id"].startswith("node_sample_") and row["id"] != "node_sample_gate":
            row["data"]["parent_id"] = "node_sample_gate"
        if row["id"].startswith("node_ocr_"):
            row["data"]["parent_id"] = "node_atomic_ocr"
    redirects = {"prepare_sample": ("node_source_atomic_partition", "node_sample_gate"),
                 "sample_ocr": ("node_sample_human_gate", "node_atomic_ocr"),
                 "ocr_finish": ("node_ocr_decide", "node_assemble_store")}
    for row in value["entries"]:
        if row["id"] in redirects:
            row["data"].update(zip(("from_node", "to_node"), redirects[row["id"]]))
    links = [("material_partition", "node_material_capability", "node_source_atomic_partition", "always"),
             ("assemble_link", "node_assemble_cross_page_question", "node_link_question_knowledge", "always"),
             ("knowledge_link", "node_organize_knowledge_point", "node_link_question_knowledge", "always"),
             ("link_write", "node_link_question_knowledge", "node_write_sqlite_index", "pass"),
             ("write_rag", "node_write_sqlite_index", "node_rag_retrieval", "always"),
             ("rag_independent", "node_rag_retrieval", "node_independent_audit", "always"),
             ("data_fail", "node_data_audit", "node_assemble_store", "fail"),
             ("rag_fail", "node_rag_audit", "node_rag_retrieval", "fail"),
             ("independent_chapter", "node_independent_audit", "node_chapter_book_completeness_audit", "pass"),
             ("chapter_delivery", "node_chapter_book_completeness_audit", "node_final_gate_delivery", "pass")]
    value["entries"] += [entry(key, "edge", {"from_node": start, "to_node": end, "condition": condition})
                         for key, start, end, condition in links]
    next(row for row in value["entries"] if row["id"] == "rag_independent")["evidence"] = "proposal"
    return value


def hierarchical_proposals():
    first = correct_proposal()
    first["upserts"][2]["data"]["from_node"] = "node_source_atomic_partition"
    first["upserts"][3]["data"]["to_node"] = "node_sample_gate"
    links = [("sample_entry", "node_sample_gate", "node_sample_make", "always"),
             ("atomic_entry", "node_atomic_ocr", "node_ocr_first_read", "always"),
             ("store_assemble", "node_assemble_store", "node_assemble_cross_page_question", "always"),
             ("store_knowledge", "node_assemble_store", "node_organize_knowledge_point", "always"),
             ("rag_review", "node_rag_retrieval", "node_rag_audit", "always"),
             ("rag_review_data", "node_rag_audit", "node_data_audit", "pass")]
    second = {"upserts": [{"id": key, "kind": "edge", "title": key, "body": "接通既有阶段与复核",
        "data": {"from_node": start, "to_node": end, "condition": condition},
        "rationale": "归属不产生依赖；明确连接既有输入与审查路径"} for key, start, end, condition in links], "notes": "补齐既有层级的执行依赖"}
    second["upserts"] += [{"id": key, "data": {"condition": "loop"}, "rationale": "失败反馈属于跨轮调度"}
                           for key in ("data_fail", "rag_fail")]
    second["upserts"].append({"id": "rag_independent", "data": {
        "from_node": "node_data_audit", "condition": "pass"},
        "rationale": "修订尚未确认的直达建议，让既有两项审查通过后进入汇总审查"})
    outer = ["node_assemble_store", "node_assemble_cross_page_question", "node_organize_knowledge_point",
             "node_link_question_knowledge", "node_write_sqlite_index", "node_rag_retrieval", "node_rag_audit", "node_data_audit"]
    for key, start, check, members, reviewers in [
        ("data_quality", "node_assemble_store", "node_data_audit", outer, ["node_rag_audit", "node_data_audit"]),
        ("rag_quality", "node_rag_retrieval", "node_rag_audit", ["node_rag_retrieval", "node_rag_audit"], ["node_rag_audit"]),
    ]:
        second["upserts"].append({"id": key, "kind": "loop", "title": key, "body": "沿用既有复核，未知不算通过",
            "data": {"loop_id": key, "entry_node_id": start, "check_node_id": check, "node_ids": members,
                     "reviewer_node_ids": reviewers, "exit_condition": "既有验收要求与独立复核全部通过",
                     "criteria": [{"criterion_id": key, "acceptance_test": "保持原始验收要求"}], "control_mode": "team"},
            "rationale": "声明已存在的失败反馈，保留原始验收与门禁"})
    return first, second
