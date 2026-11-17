"""Conversation-scoped workflow material persistence and tool boundaries."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.workflow_blocks import WorkflowBlocks, freeze_materials
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.tools.base import Tool
from mewcode.tools.workflow_blocks import (
    ReadWorkflowBlocks,
    ReadWorkflowBlocksParams,
    SubmitWorkflowBlocks,
    SubmitWorkflowBlocksParams,
)
from mewcode.orchestration.workflow_draft import empty_document


def record(kind: str, content: str, *, timestamp: str = "2026-10-01T10:00:00Z"):
    return {"type": kind, "content": content, "timestamp": timestamp}


def write_history(path: Path, rows, *, append=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def make_blocks(project: Path, *, rows=None, base_spec=None, guard=None):
    project.mkdir(parents=True, exist_ok=True)
    source_path = project / ".mewcode" / "sessions" / "chat.jsonl"
    if rows is not None:
        write_history(source_path, rows)
    return WorkflowBlocks(project, "chat", source_path=source_path,
                          base_spec=base_spec or (lambda: None), guard=guard)


def get_sources(blocks: WorkflowBlocks, source_ids):
    return blocks.read(source_ids=list(source_ids))["sources"]


def user_sources(blocks: WorkflowBlocks, snapshot=None):
    snapshot = snapshot or blocks.read()
    ids = [row["id"] for row in snapshot["source_index"]
           if row["role"] == "user"]
    return get_sources(blocks, ids)


def all_other_sources_ignored(snapshot, used_ids):
    return [{"source_id": row["id"], "reason": "本轮未使用的历史或助手素材保留在原位置"}
            for row in snapshot["source_index"] if row["id"] not in set(used_ids)]


def citation(source):
    return {"source_id": source["id"], "quote": source["text"][:120]}


def node_entry(entry_id, title, body, sources):
    return {"id": entry_id, "kind": "node", "title": title, "body": body,
            "data": {"node_kind": "implement", "inputs": ["原始材料"], "outputs": ["处理结果"],
                     "acceptance": ["保留原始要求"], "steps": ["逐项处理"]},
            "sources": [citation(source) for source in sources]}


def submit(blocks, snapshot, upserts, *, ignored=None, turn_id="turn-1"):
    return blocks.submit(expected_sequence=snapshot["sequence"], source_hash=snapshot["source_hash"],
                         base_graph_hash=snapshot["base_graph_hash"], upserts=upserts,
                         ignored=ignored if ignored is not None else all_other_sources_ignored(
                             snapshot, {ref["source_id"] for item in upserts for ref in item.get("sources", [])}),
                         turn_id=turn_id)


def test_same_node_id_is_reused_across_three_submits_and_versions_are_recorded(tmp_path):
    source_texts = ["用户要求：把票据逐页录入并保留原图。",
                    "用户补充：录入后逐页核对金额和日期。",
                    "用户确认：异常页要交给人工复核。"]
    blocks = make_blocks(tmp_path / "project", rows=[record("user", source_texts[0])])
    receipts = []
    source_refs = []

    for index, text in enumerate(source_texts):
        if index:
            write_history(blocks.source_path, [record("user", text, timestamp=f"2026-10-01T10:0{index}:00Z")], append=True)
        snapshot = blocks.read()
        current_users = user_sources(blocks, snapshot)
        assert len(current_users) == index + 1
        source_refs = current_users
        item = node_entry("capture_receipts", "逐页录入票据", f"第 {index + 1} 轮补全：{text}", source_refs)
        receipt = submit(blocks, snapshot, [item], turn_id=f"turn-{index + 1}")
        assert receipt["saved_entry_ids"] == ["capture_receipts"]
        assert receipt["sequence"] == index + 1
        assert not receipt["pending"]
        receipts.append(receipt)

    state = DraftStore(tmp_path / "project", "chat").get()
    materials = state["materials"]
    assert [row["sequence"] for row in materials["versions"]] == [1, 2, 3]
    assert [row["turn_id"] for row in materials["versions"]] == ["turn-1", "turn-2", "turn-3"]
    entries = [row for row in materials["document"]["entries"] if row["id"] == "capture_receipts"]
    assert len(entries) == 1
    assert entries[0]["body"].startswith("第 3 轮补全")
    assert entries[0]["evidence"] == "proposal"
    assert materials["requirement_sources"]["capture_receipts"] == [citation(source) for source in source_refs]
    assert all(ref["source_id"].startswith("s_material_") for ref in entries[0]["sources"])
    assert [receipt["sequence"] for receipt in receipts] == [1, 2, 3]


def test_sources_must_be_real_queried_refs_with_verbatim_quotes(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "用户要求：OCR结果必须保留原页编号。")])
    initial = blocks.read()
    source = user_sources(blocks, initial)[0]
    queried = blocks.read(source_ids=[source["id"]])["sources"]
    assert queried == [source]
    assert source["role"] == "user"
    assert source["text"] == "用户要求：OCR结果必须保留原页编号。"

    accepted = submit(blocks, initial, [node_entry("keep_page_number", "保留原页编号", source["text"], [source])])
    assert accepted["saved_entry_ids"] == ["keep_page_number"]
    saved_materials = DraftStore(tmp_path / "project", "chat").get()["materials"]
    stored = next(item for item in saved_materials["document"]["entries"] if item["id"] == "keep_page_number")
    assert saved_materials["requirement_sources"]["keep_page_number"] == [citation(source)]
    assert stored["sources"][0]["source_id"].startswith("s_material_")
    assert stored["evidence"] == "proposal"

    fresh = blocks.read()
    omitted_quote = node_entry("missing_quote", "没有引文", "不得省略来源引文", [])
    omitted_quote["sources"] = [{"source_id": source["id"], "quote": ""}]
    rejected = submit(blocks, fresh, [omitted_quote], turn_id="missing-quote")
    assert "missing_quote" not in rejected["saved_entry_ids"]
    assert any(issue["code"] in {"invalid_source", "quote_not_found", "quote_size"}
               for issue in rejected["pending"])

    fabricated = node_entry("fabricated_quote", "改写引文", "引文必须逐字对应", [source])
    fabricated["sources"] = [{"source_id": source["id"], "quote": "OCR结果保证原页编号"}]
    rejected_quote = submit(blocks, blocks.read(), [fabricated], turn_id="fabricated-quote")
    assert "fabricated_quote" not in rejected_quote["saved_entry_ids"]
    assert any(issue["code"] == "invalid_source" and issue["entry_id"] == "fabricated_quote"
               for issue in rejected_quote["pending"])


def test_independent_valid_entry_is_saved_while_confirmed_ai_decision_is_rejected(tmp_path):
    user_text = "用户要求：扫描归档材料并逐页保留来源。"
    ai_text = "AI建议：先做硬件核验，再检查GLM-OCR接入。"
    blocks = make_blocks(tmp_path / "project", rows=[record("user", user_text), record("assistant", ai_text)])
    snapshot = blocks.read()
    users = user_sources(blocks, snapshot)
    assistant = next(source for source in get_sources(
        blocks, [row["id"] for row in snapshot["source_index"] if row["role"] == "assistant"]))
    good = node_entry("archive_pages", "逐页归档材料", "扫描材料并逐页保留来源。", users)
    bad = {"id": "decision_local_ocr_hardware", "kind": "decision", "title": "AI代为决定OCR硬件",
           "body": "未向用户确认就记录为已确认。", "data": {"category": "confirmed", "value": "采用某型号"},
           "sources": [citation(assistant)]}

    receipt = submit(blocks, snapshot, [good, bad], turn_id="mixed-valid-and-invalid")
    assert "archive_pages" in receipt["saved_entry_ids"]
    assert "decision_local_ocr_hardware" not in receipt["saved_entry_ids"]
    assert any(issue["code"] == "proposal_not_confirmation"
               and issue["entry_id"] == "decision_local_ocr_hardware" for issue in receipt["pending"])
    document = DraftStore(tmp_path / "project", "chat").get()["materials"]["document"]
    assert any(item["id"] == "archive_pages" for item in document["entries"])
    assert not any(item["id"] == "decision_local_ocr_hardware" for item in document["entries"])

    # An ordinary AI-authored design is retained as a proposal with assistant provenance.
    fresh = blocks.read()
    design = {"id": "ocr_readiness", "kind": "decision", "title": "待评估OCR服务",
              "body": "GLM-OCR可作为待评估服务。", "data": {"category": "designed", "value": "待评估"},
              "sources": [citation(assistant)]}
    design_receipt = submit(blocks, fresh, [design], turn_id="ai-design")
    assert "ocr_readiness" in design_receipt["saved_entry_ids"]
    saved_design = next(item for item in DraftStore(tmp_path / "project", "chat").get()["materials"]["document"]["entries"]
                        if item["id"] == "ocr_readiness")
    assert saved_design["data"]["category"] == "designed"
    assert saved_design["evidence"] == "proposal"


def test_pending_entry_survives_unrelated_submission_and_clears_after_correction(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[
        record("user", "用户要求：整理本地OCR硬件评估记录。"),
        record("assistant", "建议评估本地OCR硬件，但需要用户确认后才能作为已决定事项。"),
    ])
    initial = blocks.read()
    user = user_sources(blocks, initial)[0]
    assistant = get_sources(
        blocks, [row["id"] for row in initial["source_index"] if row["role"] == "assistant"]
    )[0]
    bad_decision = {
        "id": "decision_local_ocr_hardware", "kind": "decision", "title": "本地OCR硬件方案",
        "body": "将AI建议作为用户已经确认的决定。",
        "data": {"category": "confirmed", "value": "采用本地硬件"},
        "sources": [citation(assistant)],
    }

    first = submit(blocks, initial, [bad_decision], turn_id="unconfirmed-decision")
    assert "decision_local_ocr_hardware" not in first["saved_entry_ids"]
    assert any(issue["entry_id"] == "decision_local_ocr_hardware"
               and issue["code"] == "proposal_not_confirmation" for issue in first["pending"])
    state = DraftStore(tmp_path / "project", "chat").get()["materials"]
    assert any(item["id"] == "decision_local_ocr_hardware" for item in state["pending_upserts"])

    unrelated_snapshot = blocks.read()
    current_user = user_sources(blocks, unrelated_snapshot)
    unrelated = node_entry("archive_ocr_notes", "归档OCR评估记录", "归档本地OCR硬件评估记录。", current_user)
    second = submit(blocks, unrelated_snapshot, [unrelated], turn_id="unrelated-update")
    assert "archive_ocr_notes" in second["saved_entry_ids"]
    assert any(issue["entry_id"] == "decision_local_ocr_hardware"
               and issue["code"] == "proposal_not_confirmation" for issue in second["pending"])
    visible = blocks.read()
    assert any(issue["entry_id"] == "decision_local_ocr_hardware"
               and issue["code"] == "proposal_not_confirmation" for issue in visible["pending"])
    assert any(item["id"] == "decision_local_ocr_hardware" for item in visible["pending_upserts"])

    corrected = copy.deepcopy(bad_decision)
    corrected["title"] = "待评估的本地OCR硬件方案"
    corrected["body"] = "将本地硬件方案保留为待评估设计，尚未记录用户确认。"
    corrected["data"] = {"category": "designed", "value": "待评估本地硬件"}
    final = submit(blocks, visible, [corrected], turn_id="corrected-proposal")
    assert "decision_local_ocr_hardware" in final["saved_entry_ids"]
    assert not any(issue["entry_id"] == "decision_local_ocr_hardware" for issue in final["pending"])
    settled = blocks.read()
    assert not any(issue["entry_id"] == "decision_local_ocr_hardware" for issue in settled["pending"])
    assert not any(item["id"] == "decision_local_ocr_hardware" for item in settled["pending_upserts"])


def test_stale_sequence_source_and_canvas_snapshots_are_rejected(tmp_path):
    blocks = make_blocks(tmp_path / "sequence-project", rows=[record("user", "用户要求：建立清晰的归档流程。")])
    stale_sequence = blocks.read()
    user = user_sources(blocks, stale_sequence)[0]
    submit(blocks, stale_sequence, [node_entry("stage_one", "归档文件", "归档文件", [user])], turn_id="first")
    current = blocks.read()
    later = user_sources(blocks, current)
    with pytest.raises(ValueError, match="素材、来源或画布版本已变化"):
        blocks.submit(expected_sequence=current["sequence"] - 1, source_hash=current["source_hash"],
                      base_graph_hash=current["base_graph_hash"],
                      upserts=[node_entry("stage_two", "校验归档", "校验归档", later)], ignored=[], turn_id="stale-sequence")

    source_blocks = make_blocks(tmp_path / "source-project", rows=[record("user", "首轮用户要求：保留扫描原图。")])
    stale_source = source_blocks.read()
    stale_users = user_sources(source_blocks, stale_source)
    write_history(source_blocks.source_path, [record("user", "后来补充：文件名包含日期。",
                                                      timestamp="2026-10-02T11:00:00Z")], append=True)
    with pytest.raises(ValueError, match="素材、来源或画布版本已变化"):
        source_blocks.submit(expected_sequence=stale_source["sequence"], source_hash=stale_source["source_hash"],
                             base_graph_hash=stale_source["base_graph_hash"],
                             upserts=[node_entry("stale_source", "保留原图", "保留扫描原图", stale_users)],
                             ignored=[], turn_id="stale-source")

    canvas = {"label": "初始画布", "goal": "整理归档材料", "nodes": [
        {"node_id": "manual_stage", "label": "手工维护节点", "kind": "implement", "goal": "读取材料"}],
        "edges": [], "meta": {}}
    canvas_blocks = make_blocks(tmp_path / "canvas-project", rows=[
        record("user", "用户要求：后续人工维护的阶段内容要保留。"),
        record("assistant", "AI建议：把此阶段改名为自动识别。")], base_spec=lambda: copy.deepcopy(canvas))
    stale_canvas = canvas_blocks.read()
    canvas["nodes"][0]["goal"] = "用户手工修改后的内容"
    source_for_canvas = user_sources(canvas_blocks, stale_canvas)
    with pytest.raises(ValueError, match="素材、来源或画布版本已变化"):
        canvas_blocks.submit(expected_sequence=stale_canvas["sequence"], source_hash=stale_canvas["source_hash"],
                             base_graph_hash=stale_canvas["base_graph_hash"],
                             upserts=[node_entry("manual_stage", "手工维护节点", "旧画布内容", source_for_canvas)],
                             ignored=[], turn_id="stale-canvas")


def test_current_manual_canvas_fields_remain_protected_from_ai_material_changes(tmp_path):
    canvas = {"label": "现有画布", "goal": "归档资料", "nodes": [
        {"node_id": "manual_stage", "label": "人工维护阶段", "kind": "implement", "goal": "人工设定职责"}],
        "edges": [], "meta": {}}
    original = copy.deepcopy(canvas)
    blocks = make_blocks(tmp_path / "project", rows=[
        record("user", "用户要求：保留手工维护的阶段。"),
        record("assistant", "建议改写这个阶段的职责并自动识别。")], base_spec=lambda: copy.deepcopy(canvas))
    snapshot = blocks.read()
    assistant_ids = [row["id"] for row in snapshot["source_index"] if row["role"] == "assistant"]
    assistant = get_sources(blocks, assistant_ids)[0]
    proposed = node_entry("manual_stage", "AI建议的新阶段名", "自动识别并替换人工定义。", [assistant])
    receipt = submit(blocks, snapshot, [proposed], turn_id="ai-canvas-proposal")

    assert canvas == original, "planning submissions cannot mutate the caller-owned canvas"
    materials = DraftStore(tmp_path / "project", "chat").get()["materials"]
    entry = next(item for item in materials["document"]["entries"] if item["id"] == "manual_stage")
    assert entry["state"] == "conflict"
    assert entry["evidence"] == "proposal"
    assert materials["base_spec"] == original
    assert "manual_stage" in receipt["saved_entry_ids"]


def test_submission_retry_is_idempotent(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "用户要求：扫描后保存文件名和日期。")])
    snapshot = blocks.read()
    source = user_sources(blocks, snapshot)[0]
    upserts = [node_entry("save_filename", "保存文件名", "保存文件名和日期", [source])]
    ignored = all_other_sources_ignored(snapshot, {source["id"]})
    first = blocks.submit(expected_sequence=snapshot["sequence"], source_hash=snapshot["source_hash"],
                         base_graph_hash=snapshot["base_graph_hash"], upserts=upserts, ignored=ignored,
                         turn_id="retryable-turn")
    after_first = DraftStore(tmp_path / "project", "chat").get()
    retry = blocks.submit(expected_sequence=snapshot["sequence"], source_hash=snapshot["source_hash"],
                          base_graph_hash=snapshot["base_graph_hash"], upserts=upserts, ignored=ignored,
                          turn_id="retryable-turn")
    after_retry = DraftStore(tmp_path / "project", "chat").get()

    assert first["cached"] is False
    assert retry["cached"] is True
    assert retry["sequence"] == first["sequence"] == 1
    assert after_retry["materials"] == after_first["materials"]
    assert len(after_retry["materials"]["versions"]) == 1


@pytest.mark.parametrize("evidence,reviewed,expected_state", [
    ("ai_completed", False, "retired"),
    ("ai_completed", True, "active"),
    ("user_source", False, "conflict"),
    ("saved_graph", False, "conflict"),
    ("manual", False, "conflict"),
])
def test_main_material_retirement_keeps_authorship_and_review_protection(
        tmp_path, evidence, reviewed, expected_state):
    blocks = make_blocks(tmp_path / "project", rows=[
        record("user", "保留完整交付要求，只在真正需要用户决定时询问。"),
        record("assistant", "先前补充的偏好没有业务依赖，拟撤回这个额外要求。"),
    ])
    snapshot = blocks.read()
    assistant = get_sources(blocks, [row["id"] for row in snapshot["source_index"]
                                     if row["role"] == "assistant"])[0]
    old = {"id": "extra_preference", "kind": "constraint", "title": "额外偏好要求",
           "body": "必须先选择展示侧重点。", "state": "active", "data": {},
           "sources": [citation(assistant)], "evidence": evidence,
           "rationale": "先前的补充建议", "reviewed": reviewed}
    store = DraftStore(blocks.project, "chat")
    with store.db() as db:
        state = store._get(db)
        state["document"]["entries"] = [copy.deepcopy(old)]
        store._put(db, state)
    proposal = {key: copy.deepcopy(old[key])
                for key in ("id", "kind", "title", "body", "data", "sources")}
    proposal["state"] = "retired"
    receipt = submit(blocks, blocks.read(), [proposal])
    materials = store.get()["materials"]
    entry = materials["document"]["entries"][0]
    assert entry["state"] == expected_state
    assert receipt["planning_only"] and not receipt["execution_authorization"]
    if reviewed:
        assert receipt["saved_entry_ids"] == []
        assert any(issue["code"] == "retirement_forbidden" for issue in receipt["pending"])
        assert entry == old
    else:
        assert receipt["saved_entry_ids"] == [old["id"]]
        assert not receipt["pending"]
        assert entry["evidence"] == "proposal"
        assert materials["versions"][-1]["changes"][0]["before"] == old
        assert materials["requirement_sources"][old["id"]] == [citation(assistant)]


def test_source_extraction_still_cannot_retire_an_ai_completion():
    from mewcode.orchestration.workflow_draft import apply_patch_fragments

    source = {"id": "s_ai", "role": "assistant", "text": "拟撤回一个补充建议。"}
    old = {"id": "extra_preference", "kind": "constraint", "title": "额外偏好",
           "body": source["text"], "state": "active", "data": {},
           "sources": [citation(source)], "evidence": "ai_completed", "rationale": "补充建议"}
    document = empty_document()
    document["entries"] = [old]
    retired = {key: copy.deepcopy(old[key]) for key in
               ("id", "kind", "title", "body", "data", "sources")}
    retired["state"] = "retired"
    result = apply_patch_fragments(document, {"upserts": [retired], "ignored": []}, [source])
    assert not result.accepted_entry_ids
    assert result.document == document
    assert result.pending_issues[0]["code"] == "retirement_forbidden"


def test_freezing_materials_once_excludes_later_messages_and_submissions(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "用户要求：先建立资料索引。")])
    first_snapshot = blocks.read()
    first_user = user_sources(blocks, first_snapshot)[0]
    submit(blocks, first_snapshot, [node_entry("build_index", "建立索引", "建立资料索引。", [first_user])],
           turn_id="first-material")
    first_state = DraftStore(tmp_path / "project", "chat").get()
    candidate = {"document": empty_document(), "history_basis": {"document": empty_document()}}
    frozen = freeze_materials(candidate, first_state)
    frozen_copy = copy.deepcopy(frozen)
    assert "materials_snapshot" in frozen
    assert any(item["id"] == "build_index" for item in frozen["document"]["entries"])

    write_history(blocks.source_path, [record("user", "新消息：再加入OCR结果复核。",
                                               timestamp="2026-10-02T10:00:00Z")], append=True)
    later_snapshot = blocks.read()
    users = user_sources(blocks, later_snapshot)
    submit(blocks, later_snapshot, [node_entry("review_ocr", "复核OCR结果", "加入OCR结果复核。", users)],
           turn_id="later-material")
    later_state = DraftStore(tmp_path / "project", "chat").get()

    refrozen = freeze_materials(frozen, later_state)
    assert refrozen == frozen_copy
    assert "review_ocr" not in {item["id"] for item in refrozen["document"]["entries"]}
    assert "review_ocr" not in {item["id"] for item in refrozen["materials_snapshot"]["document"]["entries"]}


@pytest.mark.asyncio
async def test_read_tool_paginates_without_gaps_or_reordering(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[
        record("user", "用户要求：" + "逐页核对票据并保留原件。" * 30),
        record("assistant", "建议分为扫描、识别、复核三个阶段。" * 20),
    ])
    tool = ReadWorkflowBlocks(blocks)
    chunks = []
    offset = 0
    snapshot_id = ""
    while True:
        result = await tool.execute(ReadWorkflowBlocksParams(offset=offset, limit=700, snapshot_id=snapshot_id))
        assert not result.is_error, result.output
        header, content = result.output.split("\n", 1)
        meta = json.loads(header)
        snapshot_id = meta["snapshot_id"]
        assert meta["offset"] == offset
        assert meta["next_offset"] == offset + len(content)
        assert meta["total_chars"] >= meta["next_offset"]
        chunks.append(content)
        if not meta["more"]:
            assert meta["next_offset"] == meta["total_chars"]
            break
        assert meta["next_offset"] > offset
        offset = meta["next_offset"]

    expected = json.dumps(blocks.read(), ensure_ascii=False, sort_keys=True)
    assert "".join(chunks) == expected


def test_plan_mode_allows_only_real_current_project_workflow_material_tool(tmp_path):
    project = tmp_path / "project"
    other_project = tmp_path / "other-project"
    blocks = make_blocks(project)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(project)), RuleEngine(), PermissionMode.PLAN)
    scoped = SubmitWorkflowBlocks(blocks)
    view = blocks.read()
    arguments = SubmitWorkflowBlocksParams(expected_sequence=view["sequence"], source_hash=view["source_hash"],
        base_graph_hash=view["base_graph_hash"], use_jev=False).model_dump()
    allowed = checker.check(scoped, arguments)
    assert allowed.effect == "allow"
    assert allowed.source == "plan_mode"

    read = checker.check(ReadWorkflowBlocks(blocks), {})
    assert read.effect == "allow"

    other = SubmitWorkflowBlocks(WorkflowBlocks(other_project, "chat"))
    denied = checker.check(other, arguments)
    assert denied.effect == "deny"
    assert denied.source == "host_tool_scope"

    class Pretender(Tool):
        name = "SubmitWorkflowBlocks"
        category = "write"
        params_model = SubmitWorkflowBlocksParams

        async def execute(self, params):  # pragma: no cover - permission test never executes tools
            raise AssertionError("checker must not execute a fake tool")

    fake = checker.check(Pretender(), arguments)
    assert fake.effect == "deny"
    assert fake.source == "host_tool_scope"
