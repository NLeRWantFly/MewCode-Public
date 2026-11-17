"""Immutable pagination and semantic commit bases, using synthetic sessions only."""
import asyncio
import copy
import json

import pytest

from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import TaskDesign
from mewcode.orchestration.workflow_blocks import WorkflowBlocks, freeze_materials
from mewcode.orchestration.workflow_snapshots import WorkflowSnapshotCache
from mewcode.tools.task_design import AdvanceTaskDesign, AdvanceTaskDesignParams
from mewcode.tools.workflow_blocks import ReadWorkflowBlocks, ReadWorkflowBlocksParams
from tests.test_workflow_blocks import make_blocks, node_entry, record, submit, user_sources, write_history


def header(result):
    assert not result.is_error, result.output
    meta, content = result.output.split("\n", 1)
    return json.loads(meta), content


def binding(meta):
    return {"expected_sequence": meta["sequence"], "source_hash": meta["source_hash"],
            "base_graph_hash": meta["base_graph_hash"], "snapshot_id": meta["snapshot_id"],
            "requirement_fingerprint": meta["requirement_fingerprint"]}


@pytest.mark.asyncio
async def test_pages_remain_identical_after_assistant_tool_and_new_user_append(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "原始要求：" + "逐页核对并保留原件。" * 600)])
    tool = ReadWorkflowBlocks(blocks)
    first, content = header(await tool.execute(ReadWorkflowBlocksParams(limit=600)))
    frozen_content = json.dumps(blocks.read(), ensure_ascii=False, sort_keys=True)
    assert not (tmp_path / "project" / ".mewcode" / "workflow-drafts.sqlite3").exists()
    write_history(blocks.source_path, [record("assistant", "正在核对素材。"),
        {"type": "tool_result", "tool_use_id": "ordinary", "content": "环境检查完成"},
        record("user", "新要求：所有结果还要包含日期。")], append=True)
    pieces = [content]
    cursor = first["next_cursor"]
    while cursor:
        result = await tool.execute(ReadWorkflowBlocksParams(cursor=cursor, limit=600))
        meta, chunk = header(result)
        repeated = await tool.execute(ReadWorkflowBlocksParams(snapshot_id=meta["snapshot_id"], offset=meta["offset"], limit=600))
        assert repeated.output == result.output
        assert meta["snapshot_id"] == first["snapshot_id"]
        assert meta["requirement_fingerprint"] == first["requirement_fingerprint"]
        pieces.append(chunk)
        cursor = meta["next_cursor"]
    assert "".join(pieces) == frozen_content
    with pytest.raises(ValueError, match="STALE"):
        blocks.submit(**binding(first), upserts=[], ignored=[])


@pytest.mark.asyncio
async def test_header_exposes_task_revision_before_large_provenance_and_exact_snapshot_sources(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "清晰目标"), record("assistant", "建议" * 1200)])
    design = TaskDesign(blocks.project, blocks.conversation_id)
    design.start("plan")
    design.record_turn("清晰目标")
    state = DraftStore(blocks.project, blocks.conversation_id).get()
    state["task_design"]["specification_provenance"] = {"large": "账本" * 85000}
    with DraftStore(blocks.project, blocks.conversation_id).db() as db:
        DraftStore(blocks.project, blocks.conversation_id)._put(db, state)
    tool = ReadWorkflowBlocks(blocks)
    meta, _ = header(await tool.execute(ReadWorkflowBlocksParams(limit=9000)))
    assert meta["task_design"]["task_id"] == design.read()["task_id"]
    assert meta["task_design"]["revision"] == design.read()["revision"]
    assert meta["task_design"]["status"] == "designing"
    assert meta["sequence"] == 0 and meta["missing_count"] == 0
    assert "specification_provenance" not in meta["task_design"]
    assert len((await tool.execute(ReadWorkflowBlocksParams(snapshot_id=meta["snapshot_id"], limit=9000))).output) < 10000
    source_id = blocks.read()["source_index"][0]["id"]
    source_meta, source_content = header(await tool.execute(ReadWorkflowBlocksParams(
        snapshot_id=meta["snapshot_id"], source_ids=[source_id], limit=9000)))
    # A source projection keeps the same input basis and has its own fixed pages.
    assert source_meta["requirement_fingerprint"] == meta["requirement_fingerprint"]
    projection = [source_content]
    cursor = source_meta["next_cursor"]
    while cursor:
        page, part = header(await tool.execute(ReadWorkflowBlocksParams(cursor=cursor, limit=9000)))
        projection.append(part)
        cursor = page["next_cursor"]
    assert json.loads("".join(projection))["sources"][0]["text"] == "清晰目标"


@pytest.mark.asyncio
async def test_unbound_offset_query_change_and_cross_scope_snapshot_rejected(tmp_path):
    blocks = make_blocks(tmp_path / "one", rows=[record("user", "原始要求")])
    tool = ReadWorkflowBlocks(blocks)
    meta, _ = header(await tool.execute(ReadWorkflowBlocksParams(limit=600)))
    result = await tool.execute(ReadWorkflowBlocksParams(offset=600))
    assert result.is_error and "SNAPSHOT_REQUIRED" in result.output
    result = await tool.execute(ReadWorkflowBlocksParams(snapshot_id=meta["snapshot_id"], query="不同查询"))
    assert result.is_error and "SNAPSHOT_QUERY" in result.output
    for other in (WorkflowBlocks(blocks.project, "different", source_path=blocks.source_path),
                  WorkflowBlocks(tmp_path / "two", "chat", source_path=blocks.source_path)):
        result = await ReadWorkflowBlocks(other).execute(ReadWorkflowBlocksParams(snapshot_id=meta["snapshot_id"]))
        assert result.is_error and "SNAPSHOT_SCOPE" in result.output


def test_snapshot_cache_count_bytes_and_expiry_are_bounded(tmp_path, monkeypatch):
    from mewcode.orchestration import workflow_blocks
    clock = [1.0]
    cache = WorkflowSnapshotCache(max_count=2, max_per_scope=1, max_bytes=20000, ttl=10, clock=lambda: clock[0])
    monkeypatch.setattr(workflow_blocks, "WORKFLOW_SNAPSHOTS", cache)
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "原始要求")])
    first, _ = blocks.read_page()
    second, _ = blocks.read_page()
    with pytest.raises(ValueError, match="SNAPSHOT_EXPIRED"):
        blocks.read_page(snapshot_id=first["snapshot_id"])
    assert cache._bytes <= cache.max_bytes and len(cache._snapshots) <= cache.max_count
    clock[0] = 12.0
    with pytest.raises(ValueError, match="SNAPSHOT_EXPIRED"):
        blocks.validate_snapshot(**binding(second))
    assert not cache._snapshots and cache._bytes == 0
    with pytest.raises(ValueError, match="缓存上限"):
        cache.create(("scope",), {"large": "x" * 20001}, [], {})


def test_assistant_observations_keep_semantic_basis_but_material_edits_do_not(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "整理并核对本地材料")])
    meta, _ = blocks.read_page()
    user = user_sources(blocks)[0]
    write_history(blocks.source_path, [record("assistant", "正在检查解释器"),
        {"type": "tool_result", "tool_use_id": "check", "content": "检查成功"}], append=True)
    current = blocks.validate_snapshot(**binding(meta))
    assert current["source_hash"] != meta["source_hash"]
    assert current["requirement_fingerprint"] == meta["requirement_fingerprint"]
    receipt = blocks.submit(**binding(meta), upserts=[node_entry("prepare", "整理材料", "逐项核对", [user])], ignored=[])
    assert receipt["sequence"] == 1
    with pytest.raises(ValueError, match="STALE"):
        blocks.validate_snapshot(**binding(meta))
    fresh = blocks.read()
    submit(blocks, fresh, [node_entry("prepare", "整理材料", "追加输出检查", [user])], turn_id="real-ai-edit")
    assert blocks.current_basis()["requirement_fingerprint"] != current["requirement_fingerprint"]


def test_legacy_read_submit_accepts_progress_but_unknown_or_changed_basis_rejected(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "保留扫描原图")])
    original = blocks.read()
    user = user_sources(blocks, original)[0]
    write_history(blocks.source_path, [record("assistant", "准备保存已明确素材")], append=True)
    receipt = submit(blocks, original, [node_entry("save", "保存原图", "保留扫描原图", [user])])
    assert receipt["sequence"] == 1
    current = blocks.read()
    with pytest.raises(ValueError, match="旧来源版本未绑定有效快照"):
        blocks.submit(expected_sequence=current["sequence"], source_hash=digest(["unknown"]),
            base_graph_hash=current["base_graph_hash"], upserts=[], ignored=[])
    write_history(blocks.source_path, [record("user", "还要独立核对输出")], append=True)
    with pytest.raises(ValueError, match="STALE"):
        submit(blocks, current, [])


def test_only_whole_host_continue_without_attachments_keeps_semantic_basis(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "建立离线任务")])
    original = blocks.read()
    write_history(blocks.source_path, [record("user", "继续")], append=True)
    same = blocks.current_basis()
    assert same["source_hash"] != original["source_hash"]
    assert same["user_source_hash"] == original["user_source_hash"]
    assert same["requirement_fingerprint"] == original["requirement_fingerprint"]
    write_history(blocks.source_path, [record("user", "继续，新增独立检查")], append=True)
    assert blocks.current_basis()["requirement_fingerprint"] != same["requirement_fingerprint"]
    fresh = blocks.current_basis()
    write_history(blocks.source_path, [{"type": "user", "content": [{"type": "text", "text": "继续"},
        {"type": "file", "name": "new.txt", "attachment_id": "new-input"}]}], append=True)
    assert blocks.current_basis()["requirement_fingerprint"] != fresh["requirement_fingerprint"]


def test_human_answer_and_canvas_changes_invalidate_basis(tmp_path):
    canvas = {"label": "当前画布", "goal": "离线整理", "nodes": [], "edges": [], "meta": {}}
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "离线整理")], base_spec=lambda: copy.deepcopy(canvas))
    meta, _ = blocks.read_page()
    write_history(blocks.source_path, [{"type": "assistant", "content": [{"type": "tool_use", "id": "question",
        "name": "AskUserQuestion", "input": {"question": "输出范围？"}}]},
        {"type": "tool_result", "tool_use_id": "question", "content": json.dumps({"status": "answered", "answer": "全部原图"})}], append=True)
    with pytest.raises(ValueError, match="STALE"):
        blocks.validate_snapshot(**binding(meta))
    current, _ = blocks.read_page()
    canvas["goal"] = "人工修改范围"
    with pytest.raises(ValueError, match="STALE"):
        blocks.validate_snapshot(**binding(current))


@pytest.mark.asyncio
async def test_build_uses_stable_basis_and_preserves_current_full_provenance(tmp_path):
    blocks = make_blocks(tmp_path / "project", rows=[record("user", "整理本地材料")])
    design = TaskDesign(blocks.project, "chat")
    design.start("plan")
    design.record_turn("整理本地材料")
    view = blocks.read()
    submit(blocks, view, [node_entry("prepare", "整理材料", "逐项核对", user_sources(blocks, view))])
    meta, _ = blocks.read_page()
    write_history(blocks.source_path, [record("assistant", "素材已保存，即将提交构图")], append=True)
    params = AdvanceTaskDesignParams(action="build", expected_revision=meta["task_design"]["revision"], **binding(meta))
    result = await AdvanceTaskDesign(blocks).execute(params)
    assert not result.is_error, result.output
    queued = design.read()["build_request"]
    assert queued["source_hash"] == blocks.current_basis()["source_hash"]
    assert queued["requirement_fingerprint"] == meta["requirement_fingerprint"]


def material_refresh_fixture():
    old = {"id": "n_scope", "kind": "node", "title": "调查适配", "body": "调查 OCR 和看图能力",
        "state": "active", "evidence": "proposal", "sources": [{"source_id": "old-material", "quote": "old"}],
        "data": {"node_kind": "implement", "model": "old-model", "capabilities": {"possible_blockers": [
            "OCR与embedding无专用注册工具；需调查已有适配与凭据引用", "模型配置存在不证明看图能力"]}}}
    current = copy.deepcopy(old)
    current.update(evidence="user_source", sources=[{"source_id": "actual-user", "quote": "保留范围"}])
    fresh = copy.deepcopy(old)
    fresh["data"]["capabilities"]["possible_blockers"] = ["capability_missing", "external_dependency", "environment_fault", "provider_error"]
    fresh["sources"] = [{"source_id": "new-material", "quote": "new"}]
    candidate = {"sequence": 44, "document": {"entries": [current], "ignored": []},
        "materials_snapshot": {"sequence": 5, "document": {"entries": [old], "ignored": []}},
        "history_basis": {"document": {"entries": [current], "ignored": []},
                          "entry_field_sources": {"n_scope": {"data.model": [{"source_id": "actual-user", "quote": "old-model"}]}}}}
    state = {"materials": {"sequence": 6, "document": {"entries": [fresh], "ignored": []},
        "sources": [{"id": "new-material", "role": "assistant", "text": "new", "kind": "workflow_material"}]}}
    return candidate, state


def test_section_offsets_match_exact_cached_json_with_unicode_and_escapes():
    text = '范围😀 "引号"\\路径\n换行'
    value = {"document": {"entries": [{"body": text}], "ignored": []},
        "material_refresh": {"pending": [{"previous": text, "current": text + "候选", "proposed": text + "素材"}],
            "projection_repairs": [], "projection_repair_history": [{"before": text}]},
        "source_index": [{"id": "真实来源"}], "sources": [{"text": text}]}
    cache = WorkflowSnapshotCache()
    identity = cache.create(("scope",), value, [], {})
    row = cache.get(("scope",), identity)
    assert row["content"] == json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert next(iter(row["section_ranges"])) == "material_refresh.pending"
    assert "material_refresh.projection_repairs" not in row["section_ranges"]
    for path, bounds in row["section_ranges"].items():
        expected = value
        for key in path.split("."):
            expected = expected[key]
        assert json.loads(row["content"][bounds["offset"]:bounds["end"]]) == expected


@pytest.mark.asyncio
async def test_jump_to_three_way_conflicts_keeps_old_ranges_and_complete_sources(tmp_path):
    original_requirement = "完整来源与要求不可删减。" * 800
    blocks = make_blocks(tmp_path / "project", rows=[record("user", original_requirement)])
    candidate, state = material_refresh_fixture()
    previous = candidate["materials_snapshot"]["document"]["entries"][0]
    current = candidate["document"]["entries"][0]
    proposed = state["materials"]["document"]["entries"][0]
    current["body"] = previous["body"] + '；候选 "独立" 修订😀'
    proposed["body"] = previous["body"] + "；素材后续修订\\路径\n换行"
    previous["data"]["capabilities"]["possible_blockers"] = copy.deepcopy(proposed["data"]["capabilities"]["possible_blockers"])
    current["sources"][0]["quote"] = "完整审计引用。" * 3000
    candidate["history_basis"]["material_projection_repairs"] = [{"entry_id": "n_scope",
        "path": "data.capabilities.possible_blockers", "before": ["旧投影"], "after": ["provider_error"]}]
    store = DraftStore(blocks.project, blocks.conversation_id)
    with store.db() as db:
        store._put(db, {**store._get(db), **state, "candidate": candidate})
    tool = ReadWorkflowBlocks(blocks)
    first, _ = header(await tool.execute(ReadWorkflowBlocksParams(entry_ids=["n_scope"], limit=100)))
    section = first["section_ranges"]["material_refresh.pending"]
    assert section["offset"] > 9000  # Reach the repair without paging through entry copies.
    assert first["section_ranges"]["material_refresh.projection_repairs"]
    assert first["section_ranges"]["material_refresh.projection_repair_history"]
    result = await tool.execute(ReadWorkflowBlocksParams(cursor=section["cursor"],
        limit=section["end"] - section["offset"]))
    meta, chunk = header(result)
    conflicts = json.loads(chunk)
    body = next(issue for issue in conflicts if issue["path"] == "body")
    assert (body["previous"], body["current"], body["proposed"]) == (
        previous["body"], current["body"], proposed["body"])
    assert meta["next_offset"] == section["end"]
    assert meta["section_ranges"] == first["section_ranges"]
    with store.db() as db:
        updated = store._get(db)
        updated["materials"]["sequence"] += 1
        updated["candidate"]["document"]["entries"][0]["body"] = "更新后的候选"
        store._put(db, updated)
    repeated = await tool.execute(ReadWorkflowBlocksParams(cursor=section["cursor"],
        limit=section["end"] - section["offset"]))
    assert repeated.output == result.output
    with pytest.raises(ValueError, match="STALE"):
        blocks.validate_snapshot(**binding(first))

    # Select every frozen user-source part, then jump directly to the full texts.
    original = blocks.read_page(snapshot_id=first["snapshot_id"], limit=100)[0]
    from mewcode.orchestration.workflow_snapshots import WORKFLOW_SNAPSHOTS
    frozen = WORKFLOW_SNAPSHOTS.get(blocks.snapshot_scope, original["snapshot_id"])
    source_ids = [source["id"] for source in frozen["sources"] if source["role"] == "user"]
    source_meta, _ = header(await tool.execute(ReadWorkflowBlocksParams(
        snapshot_id=first["snapshot_id"], source_ids=source_ids, limit=100)))
    source_section = source_meta["section_ranges"]["sources"]
    offset, pieces = source_section["offset"], []
    while offset < source_section["end"]:
        page, part = header(await tool.execute(ReadWorkflowBlocksParams(snapshot_id=source_meta["snapshot_id"],
            offset=offset, limit=min(9000, source_section["end"] - offset))))
        pieces.append(part)
        offset = page["next_offset"]
    assert "".join(source["text"] for source in json.loads("".join(pieces))) == original_requirement
    assert source_meta["requirement_fingerprint"] == first["requirement_fingerprint"]


def test_force_refresh_merges_legal_blocker_revision_under_user_source_node_field_by_field():
    candidate, state = material_refresh_fixture()
    candidate["document"]["entries"][0]["data"]["model"] = "user-selected-model"
    original = copy.deepcopy(candidate)
    refreshed = freeze_materials(candidate, state, force=True)
    entry = refreshed["document"]["entries"][0]
    from mewcode.orchestration.workflow_draft import entry_contract
    allowed = entry_contract()["field_types"]["node"]["capabilities"]["possible_blockers"]["values"]
    assert set(entry["data"]["capabilities"]["possible_blockers"]).issubset(allowed)
    assert candidate == original
    assert refreshed["materials_snapshot"]["sequence"] == 6
    assert entry["data"]["model"] == "user-selected-model"
    assert entry["data"]["capabilities"]["possible_blockers"] == state["materials"]["document"]["entries"][0]["data"]["capabilities"]["possible_blockers"]
    assert entry["evidence"] == "user_source"
    basis = refreshed["history_basis"]
    assert basis["entry_field_evidence"]["n_scope"]["data.capabilities.possible_blockers"] == "proposal"
    assert basis["entry_field_sources"]["n_scope"]["data.model"] == original["history_basis"]["entry_field_sources"]["n_scope"]["data.model"]
    assert basis["entry_field_sources"]["n_scope"]["data.capabilities.possible_blockers"] == [{"source_id": "new-material", "quote": "new"}]


def test_force_refresh_conflict_returns_real_repair_diagnostic_without_overwrite():
    candidate, state = material_refresh_fixture()
    candidate["document"]["entries"][0]["data"]["capabilities"]["possible_blockers"] = ["manual-current-blocker"]
    original = copy.deepcopy(candidate)
    with pytest.raises(ValueError, match="material_refresh_conflict") as error:
        freeze_materials(candidate, state, force=True)
    diagnostic = json.loads(str(error.value).split("：", 1)[1])["pending"][0]
    assert diagnostic["entry_id"] == "n_scope" and diagnostic["path"] == "data.capabilities.possible_blockers"
    assert diagnostic["current"] == ["manual-current-blocker"]
    assert candidate == original


@pytest.mark.parametrize("source_state", ["present", "missing", "wrong_quote", "history_only"])
def test_force_refresh_checks_retained_history_and_new_material_sources(source_state):
    candidate, state = material_refresh_fixture()
    historical = {"id": "actual-user", "role": "user", "text": "保留范围"}
    reference = {"source_id": historical["id"], "quote": historical["text"]}
    entry = state["materials"]["document"]["entries"][0]
    entry["sources"].insert(0, reference)
    sources = [historical]
    if source_state == "missing":
        sources = []
    elif source_state == "wrong_quote":
        reference["quote"] = "来源中没有这句话"
    elif source_state == "history_only":
        entry["sources"] = [reference]
    original = copy.deepcopy(candidate)
    if source_state != "present":
        with pytest.raises(ValueError, match="material_refresh_invalid_source"):
            freeze_materials(candidate, state, sources=sources, force=True)
    else:
        refreshed = freeze_materials(candidate, state, sources=sources, force=True)
        assert refreshed["document"]["entries"][0]["sources"] == entry["sources"]
        assert refreshed["history_basis"]["entry_field_sources"]["n_scope"][
            "data.capabilities.possible_blockers"] == entry["sources"]
        assert refreshed["history_basis"]["entry_field_evidence"]["n_scope"][
            "data.capabilities.possible_blockers"] == "proposal"
    assert candidate == original


def test_candidate_conflict_view_supports_explicit_merge_then_next_refresh(tmp_path):
    from mewcode.orchestration.workflow_blocks import material_refresh_view
    candidate, state = material_refresh_fixture()
    candidate.update(id="synthetic-candidate")
    candidate["document"]["entries"][0]["body"] = "候选已改写的处理说明"
    state["materials"]["document"]["entries"][0]["body"] = "新素材同时改写的处理说明"
    state["candidate"] = candidate
    original = copy.deepcopy(state)
    view = material_refresh_view(state)
    assert view["pending"][0]["path"] == "body"
    assert view["pending"][0]["current"] == "候选已改写的处理说明"
    assert view["candidate_entries"][0]["body"] == "候选已改写的处理说明"
    assert view["previous_material_entries"][0]["body"] == "调查 OCR 和看图能力"
    assert state == original
    # The model can explicitly preserve the candidate field, save it in a new
    # material revision, and still refresh the independently corrected blockers.
    state["materials"]["document"]["entries"][0]["body"] = view["pending"][0]["current"]
    state["materials"]["sequence"] = 7
    assert not material_refresh_view(state)["pending"]
    refreshed = freeze_materials(candidate, state, force=True)
    assert refreshed["document"]["entries"][0]["body"] == "候选已改写的处理说明"
    assert refreshed["materials_snapshot"]["sequence"] == 7
    assert refreshed["document"]["entries"][0]["data"]["capabilities"]["possible_blockers"] == state["materials"]["document"]["entries"][0]["data"]["capabilities"]["possible_blockers"]


def test_same_legal_snapshot_and_materials_repair_invalid_candidate_enum_with_audit():
    from mewcode.orchestration.workflow_blocks import material_refresh_view
    candidate, state = material_refresh_fixture()
    fresh = state["materials"]["document"]["entries"][0]
    candidate["materials_snapshot"]["document"]["entries"][0]["data"]["capabilities"] = copy.deepcopy(fresh["data"]["capabilities"])
    state["candidate"] = candidate
    view = material_refresh_view(state)
    assert not view["pending"]
    assert view["projection_repairs"][0]["code"] == "projection_invalid_enum"
    original = copy.deepcopy(candidate)
    refreshed = freeze_materials(candidate, state, force=True)
    assert candidate == original
    assert refreshed["document"]["entries"][0]["data"]["capabilities"] == fresh["data"]["capabilities"]
    audit = refreshed["history_basis"]["material_projection_repairs"][0]
    assert audit["before"] == original["document"]["entries"][0]["data"]["capabilities"]["possible_blockers"]
    assert audit["after"] == fresh["data"]["capabilities"]["possible_blockers"]
    assert audit["original_sources"] == original["document"]["entries"][0]["sources"]
    assert audit["evidence"] == "proposal"
    # This narrow exception never replaces a different valid manual selection.
    candidate["document"]["entries"][0]["data"]["capabilities"]["possible_blockers"] = ["user_input"]
    kept = freeze_materials(candidate, state, force=True)
    assert kept["document"]["entries"][0]["data"]["capabilities"]["possible_blockers"] == ["user_input"]
