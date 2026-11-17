"""Batch-one ledger and contract compatibility tests."""
import copy

import pytest

from mewcode.orchestration.spec import GoalContract
from mewcode.orchestration.workflow_draft import (
    DraftPatchError, apply_patch, empty_document, project_graph, render_markdown,
    seed_document, validate_entry,
)


def item(entry_id, kind, *, data=None, body="说明", evidence="manual", rationale=None):
    entry = {"id": entry_id, "kind": kind, "title": entry_id, "body": body,
             "state": "active", "data": data or {}, "sources": [], "evidence": evidence}
    if rationale is not None:
        entry["rationale"] = rationale
    return entry


def document(*entries):
    return {"schema_version": 1, "entries": list(entries), "ignored": []}


def test_ai_completed_requires_bounded_rationale_but_can_omit_sources():
    entry = item("ai", "decision", data={"category": "designed"},
                 evidence="ai_completed", rationale="为图补齐顺序")
    assert validate_entry(entry)["rationale"] == "为图补齐顺序"
    assert validate_entry(entry)["sources"] == []
    assert "推断理由：为图补齐顺序" in render_markdown(document(entry))
    for rationale in (None, "  ", "x" * 1001):
        with pytest.raises(DraftPatchError, match="rationale"):
            validate_entry({**entry, **({} if rationale is None else {"rationale": rationale}),
                            **({"rationale": ""} if rationale is None else {})})
    assert validate_entry(item("legacy", "decision"))["kind"] == "decision"


def test_input_validation_and_markdown():
    entry = item("topic", "input", data={"name": "主题", "description": "研究主题",
                                         "required": True, "default": None})
    assert validate_entry(entry)["data"] == entry["data"]
    assert validate_entry(item("implicit", "input", data={"name": "文件路径"}))["data"]["required"] is True
    assert "## 运行参数" in render_markdown(document(entry))
    for data in ({}, {"name": "x" * 81}, {"name": " " * 80 + "x"}, {"name": "主题", "required": "yes"},
                 {"name": "主题", "default": 5}, {"name": "主题", "description": "x" * 501}):
        with pytest.raises(DraftPatchError):
            validate_entry(item("bad", "input", data=data))


def test_decision_question_extensions_preserve_legacy_forms():
    assert validate_entry(item("d0", "decision"))["data"] == {}
    assert validate_entry(item("q0", "question"))["data"] == {}
    assert validate_entry(item("d1", "decision", data={"category": "defaulted", "value": "8"}))["data"]["value"] == "8"
    assert validate_entry(item("q1", "question", data={"blocking": False}))["data"]["blocking"] is False
    for kind, data in (("decision", {"category": "unknown"}), ("decision", {"value": "x" * 501}),
                       ("question", {"blocking": 0})):
        with pytest.raises(DraftPatchError):
            validate_entry(item("bad", kind, data=data))


def test_contract_old_dict_is_identical_and_new_fields_roundtrip():
    old = {"objective": "交付报告", "acceptance": ["可读"], "constraints": ["不删源文件"],
           "open_assumptions": ["需确认格式"]}
    assert GoalContract.from_value(old).as_dict() == old
    value = {**old, "assumptions": ["默认八篇"],
             "run_inputs": [{"name": "主题", "description": "研究主题", "required": True, "default": None}]}
    assert GoalContract.from_value(value).as_dict() == value
    assert GoalContract.from_value({"run_inputs": None}) is None


def test_project_and_seed_decisions_inputs_without_losing_ledger_text():
    node = item("write", "node", data={"node_kind": "plan", "outputs": ["纲要"],
                                        "acceptance": ["完整"]}, body="写纲要")
    original = document(
        item("goal", "goal", body="写报告"), node,
        item("chosen", "decision", data={"category": "designed", "node_id": "write"}, body="先拟纲要"),
        item("default", "decision", data={"category": "defaulted", "value": "8"}, body="默认八篇"),
        item("old_decision", "decision", body="沿用旧约束"),
        item("question", "question", data={"blocking": False}, body="确认封面"),
        item("topic", "input", data={"name": "主题", "description": "研究主题", "required": True,
                                       "default": None}, body="运行时提供"))
    before = copy.deepcopy(original)
    spec = project_graph(original, None, conversation_id="c", revision=1).as_dict()
    assert original == before
    contract = spec["goal_contract"]
    assert contract["assumptions"] == ["[write] 先拟纲要", "默认八篇"]
    assert contract["run_inputs"] == [original["entries"][-1]["data"]]
    assert "沿用旧约束" in contract["constraints"]
    assert "确认封面" in contract["open_assumptions"]
    assert all(note.get("workflow_draft_entry") != "chosen" for note in spec["meta"].get("annotations", []))
    seeded = seed_document(spec)
    assumptions = {(entry["kind"], entry["body"]) for entry in seeded["entries"]
                   if entry["kind"] == "decision"}
    assert assumptions == {("decision", "[write] 先拟纲要"), ("decision", "默认八篇")}
    assert all(entry["evidence"] == "saved_graph" and entry["data"]["category"] == "designed"
               for entry in seeded["entries"] if entry["kind"] == "decision")
    recovered = next(entry for entry in seeded["entries"] if entry["kind"] == "input")
    assert recovered["data"] == original["entries"][-1]["data"]
    assert project_graph(seeded, spec, conversation_id="c", revision=2).goal_contract.as_dict()["run_inputs"] == contract["run_inputs"]


def test_ai_entry_cannot_be_retired_by_model_batch_but_user_can_withdraw():
    ai = item("ai", "decision", evidence="ai_completed", rationale="设计判断")
    doc = document(ai)
    model = {"id": "s_model", "role": "assistant", "text": "建议撤回"}
    user = {"id": "s_user", "role": "user", "text": "明确撤回"}
    ref = lambda source: {"source_id": source["id"], "quote": source["text"]}
    patch = lambda source: {"upserts": [{**ai, "state": "retired", "sources": [ref(source)]}], "ignored": []}
    with pytest.raises(DraftPatchError, match="ai"):
        apply_patch(doc, patch(model), [model])
    changed = apply_patch(doc, patch(user), [user])
    assert changed["entries"][0]["state"] == "retired"
    with pytest.raises(DraftPatchError, match="ai"):
        apply_patch(doc, {"upserts": [{**ai, "kind": "question", "sources": [ref(model)]}], "ignored": []}, [model])


def test_saved_graph_keeps_ledger_ids_origin_defaults_and_scoped_assumptions():
    node = item("write", "node", data={"node_kind": "plan", "outputs": ["报告"], "acceptance": ["完整"]}, body="写报告")
    node["sources"] = [{"source_id": "user", "quote": "写报告"}]
    original = document(item("goal", "goal", body="完成报告"), node,
        item("order", "decision", data={"category": "designed", "node_id": "write"}, body="先拟纲要", evidence="ai_completed", rationale="补齐设计"),
        item("count", "decision", data={"category": "defaulted", "value": "8"}, body="默认八篇", evidence="ai_completed", rationale="合理默认"),
        item("topic", "input", data={"name": "主题", "description": "研究主题", "required": True, "default": None},
             body="运行时提供", evidence="ai_completed", rationale="只有用户知道本次主题"))
    spec = project_graph(original, None, conversation_id="c", revision=1).as_dict()
    recovered = seed_document(spec, previous=original)
    assert {e["id"] for e in recovered["entries"]} >= {"write", "order", "count", "topic"}
    for entry_id in ("order", "count", "topic"):
        actual = next(e for e in recovered["entries"] if e["id"] == entry_id)
        expected = next(e for e in original["entries"] if e["id"] == entry_id)
        assert actual["data"] == expected["data"]
        assert actual["evidence"] == "ai_completed" and actual["rationale"] == expected["rationale"]
    assert project_graph(recovered, spec, conversation_id="c", revision=2).goal_contract.as_dict() == spec["goal_contract"]
    changed = copy.deepcopy(spec)
    changed["nodes"][0]["goal"] = "用户在画布修改的职责"
    manual = next(e for e in seed_document(changed, previous=original)["entries"] if e["id"] == "write")
    assert manual["body"] == "用户在画布修改的职责" and manual["evidence"] == "saved_graph"
    assert manual["sources"] == node["sources"]
