"""Narrow regressions for saved-canvas repair provenance and trial locks."""
import copy
import json

import pytest

from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_generation import graph_state, ready_graph_document
from mewcode.orchestration.graph_local_repair import SCHEMA
from mewcode.orchestration.graph_repair_build import build_repair
from mewcode.orchestration.workflow_draft import seed_document
from tests.test_generic_loops import spec_for


def _base():
    base = spec_for().as_dict()
    for node in base["nodes"]:
        node["plan_brief"] = {
            "steps": ["核对当前结构"],
            "outputs": ["结构核对清单"],
            "acceptance": ["清单可核验"],
        }
    return base


def _saved_scope(tmp_path):
    base = _base()
    store = DraftStore(tmp_path, "paper")
    document = seed_document(base)
    with store.db() as db:
        state = store._get(db)
        state.update(revision=2, document=document, source_ids=["old", "new"],
                     processed_ids=["old"], selected_ids=[], warnings=[], snapshot={})
        store._put(db, state)
    candidate = store.prepare_local_repair(base, expected_revision=2,
                                           expected_base_graph_hash=digest(base))
    with store.db() as db:
        state = store._get(db)
        entries = state["candidate"]["document"]["entries"]
        manual = next(e for e in entries if e["kind"] == "edge" and e["data"].get("condition") == "always")
        reviewed = next(e for e in entries if e["kind"] == "edge" and e["id"] != manual["id"])
        manual["evidence"] = "manual"
        reviewed["reviewed"] = True
        store._put(db, state)
    packet = store.request_local_graph_repair(base)
    state = store.get()
    loop_edge = next(e for e in state["candidate"]["document"]["entries"]
                     if e["kind"] == "edge" and e["data"].get("condition") == "loop")
    seed = store.preview_graph_repair(base, {
        "schema": SCHEMA, "ticket": packet["ticket"], "summary": "试修后续再校正",
        "upserts": [{"id": loop_edge["id"], "data": {"condition": "always"},
                     "rationale": "制造一次前向环试修以验证后续锁映射"}],
    })
    assert not seed["validation"]["valid"]
    request = store.get()["repair_request"]
    return store, base, packet, seed, request, loop_edge["id"], manual["id"], reviewed["id"]


@pytest.mark.asyncio
async def test_saved_structural_lock_stays_editable_after_invalid_trial(tmp_path):
    store, base, packet, seed, request, saved_id, manual_id, reviewed_id = _saved_scope(tmp_path)
    payloads = []

    async def ask_model(_system, user):
        payload = json.loads(user)
        payloads.append(payload)
        return json.dumps({
            "schema": SCHEMA,
            "ticket": payload["ticket"],
            "summary": "恢复原有返修调度",
            "upserts": [{"id": saved_id, "data": {"condition": "loop"},
                         "rationale": "恢复原有循环边"}],
        }, ensure_ascii=False)

    async def checkpoint(_work):
        return None

    result = await build_repair(
        store.get(), base, {"ticket": request["id"], "prompt": packet["prompt"]},
        ask_model=ask_model, checkpoint=checkpoint, report=lambda _event: None,
        guard=lambda: None, seed=seed,
    )
    assert result["validation"]["valid"] and len(payloads) == 1
    rows = {entry["id"]: entry for entry in payloads[0]["entries"]}
    assert rows[saved_id]["locked"] is False
    assert rows[manual_id]["locked"] is True
    assert rows[reviewed_id]["locked"] is True


def test_saved_canvas_prepare_keeps_private_source_progress(tmp_path):
    base = _base()
    store = DraftStore(tmp_path, "paper")
    document = seed_document(base)
    with store.db() as db:
        state = store._get(db)
        state.update(revision=2, document=document, source_ids=["old", "new"],
                     processed_ids=["old"], selected_ids=["old"], warnings=[], snapshot={})
        store._put(db, state)
    candidate = store.prepare_local_repair(base, expected_revision=2,
                                           expected_base_graph_hash=digest(base))
    assert candidate["source_ids"] == candidate["processed_ids"] == []
    assert candidate["base_processed_ids"] == ["old"]
    assert candidate["history_source_ids"] == ["old", "new"]
    current = store.get()
    assert current["revision"] == 2 and current["processed_ids"] == ["old"]
    assert current["selected_ids"] == ["old"]


def test_candidate_parameter_edit_keeps_plan_saveable_but_not_execution_ready():
    base = _base()
    document = seed_document(base)
    state = {
        "project": ".", "conversation_id": "paper", "draft_id": "main", "revision": 1,
        "document": document, "source_ids": [], "processed_ids": [], "selected_ids": [],
        "warnings": [], "history_basis": {}, "coverage": {
            "total": 0, "processed": 0, "pending": 0, "history_changed": False,
        }, "running": False, "progress": None, "basis": {},
        "candidate": {
            "id": "candidate", "sequence": 1, "base_revision": 1,
            "base_graph_hash": digest(base), "document": document, "status": "ready",
            "processed_ids": [], "source_ids": [], "mode": "selected",
            "autobuild": {"phase": "done", "status": "needs_review",
                          "reason": "manual_edits_after_review", "review_stale": True},
        },
    }
    result = graph_state(state, base)
    assert result["can_preview"] and not result["execution_ready"]
    assert result["actions"]["save"]["enabled"]
    assert result["delivery"]["review_stale"]
    assert result["process"]["outcome"] == "needs_work"


def test_ready_graph_document_rejects_manual_full_read_plan():
    base = {
        "spec_id": "manual-lint", "label": "手工图", "goal": "完成全量读取",
        "nodes": [{"node_id": "plan", "kind": "plan", "label": "计划", "goal": "读取",
                   "plan_brief": {"steps": ["逐页读取全部文件"], "outputs": ["清单"],
                                  "acceptance": ["清单可核对"]}}],
        "edges": [], "finalizer_node_id": "plan", "meta": {},
    }
    document = seed_document(base)
    state = {"revision": 0, "document": {"schema_version": 1, "entries": [], "ignored": []},
             "source_ids": [], "processed_ids": [], "warnings": [], "history_basis": {},
             "candidate": {"id": "candidate", "sequence": 0, "base_revision": 0,
                           "base_graph_hash": digest(base), "document": document, "status": "ready",
                           "processed_ids": [], "source_ids": [], "mode": "selected"}}
    with pytest.raises(ValueError, match="逐项全量处理"):
        ready_graph_document(state, state["candidate"], base, conversation_id="paper")
