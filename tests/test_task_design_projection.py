"""Stable work identity and explicit input bindings in workflow projections."""
from __future__ import annotations

import copy

import pytest

from mewcode.orchestration.draft_dedupe import apply_dedupe, verify_retirements
from mewcode.orchestration.draft_keys import duplicate_diagnostics, duplicate_groups
from mewcode.orchestration.graph_readiness import readiness
from mewcode.orchestration.workflow_draft import (
    DraftPatchError, apply_patch, check_entry_refs, empty_document,
    project_graph, seed_document, validate_entry,
)


def node(entry_id, *, kind="implement", body="做一项工作", title="工作项", data=None, sources=None):
    return {"id": entry_id, "kind": "node", "title": title, "body": body,
            "state": "active", "data": {"node_kind": kind, "inputs": [], "outputs": ["产物"],
                                             "acceptance": ["产物完整"], **(data or {})},
            "sources": copy.deepcopy(sources or []), "evidence": "manual"}


def other(entry_id, kind, *, data=None, body="", sources=None):
    return {"id": entry_id, "kind": kind, "title": entry_id, "body": body,
            "state": "active", "data": copy.deepcopy(data or {}),
            "sources": copy.deepcopy(sources or []), "evidence": "manual"}


def document(*entries):
    return {"schema_version": 1, "entries": list(entries), "ignored": []}


def test_incremental_patch_rejects_duplicate_work_identity_with_stable_diagnostic():
    original = node("first", data={"work_key": "extract-report"})
    doc = document(original)
    source = {"id": "s1", "role": "user", "text": "做一项工作并产出报告"}
    duplicate = node("second", data={"work_key": "extract-report"},
                     body="做一项工作并产出报告")
    duplicate["sources"] = [{"source_id": "s1", "quote": source["text"]}]

    with pytest.raises(DraftPatchError) as caught:
        apply_patch(doc, {"upserts": [duplicate], "ignored": []}, [source])

    assert caught.value.code == "duplicate_work_key"
    assert caught.value.entry_id == "second"
    assert caught.value.path == "data.work_key"
    assert caught.value.diagnostic["entry_ids"] == ["first", "second"]
    assert "复用已有节点 ID" in str(caught.value)


def test_work_key_scope_keeps_audit_roles_and_parent_scopes_distinct():
    doc = document(
        node("parent_a", kind="plan"), node("parent_b", kind="plan"),
        node("implement_a", data={"work_key": "review-document", "parent_id": "parent_a"}),
        node("audit_a", kind="audit", data={"work_key": "review-document", "parent_id": "parent_a"}),
        node("implement_b", data={"work_key": "review-document", "parent_id": "parent_b"}),
    )

    assert duplicate_groups(doc) == []
    assert not [item for item in duplicate_diagnostics(doc) if item["code"] == "duplicate_work_key"]


def test_saved_graph_duplicate_work_identity_is_reported_for_review():
    doc = document(node("one", data={"work_key": "inventory"}),
                   node("two", data={"work_key": "inventory"}))

    diagnostics = duplicate_diagnostics(doc)
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "duplicate_work_key"
    assert diagnostics[0]["entry_ids"] == ["one", "two"]
    assert any(row["code"] == "duplicate_work_key"
               for row in readiness(doc, None, conversation_id="test", revision=1)["diagnostics"])


def test_identical_work_duplicates_merge_losslessly_and_rewire_every_reference():
    body = "整理已确认材料并形成报告"
    business_data = {"work_key": "material-report", "outputs": ["report"],
                     "acceptance": ["报告逐项覆盖原材料"]}
    first = node("a_task", body=body, data=business_data,
                 sources=[{"source_id": "s1", "quote": "来源甲"}])
    duplicate = node("z_task", body=body, data=business_data,
                     sources=[{"source_id": "s2", "quote": "来源乙"}])
    doc = document(
        first, duplicate,
        node("consumer", data={"inputs": ["report"], "input_bindings": {"report": "z_task"}}),
        node("child", data={"parent_id": "z_task"}),
        other("edge_to_consumer", "edge", data={"from_node": "z_task", "to_node": "consumer"}),
        other("repair_loop", "loop", data={"loop_id": "repair", "entry_node_id": "z_task",
            "check_node_id": "consumer", "node_ids": ["a_task", "z_task", "consumer"],
            "reviewer_node_ids": ["consumer"], "exit_condition": "达标",
            "criteria": [{"criterion_id": "C1", "acceptance_test": "报告覆盖材料"}],
            "control_mode": "team"}),
        other("topic_input", "input", data={"name": "topic", "collect_at": "z_task"}),
        other("node_rule", "constraint", data={"node_id": "z_task"}),
    )
    before = copy.deepcopy(doc)

    merged, changes, unresolved = apply_dedupe(doc)

    rows = {item["id"]: item for item in merged["entries"]}
    survivor = rows["a_task"]
    assert not unresolved
    assert survivor["body"] == body
    assert survivor["data"]["acceptance"] == ["报告逐项覆盖原材料"]
    assert {ref["source_id"] for ref in survivor["sources"]} == {"s1", "s2"}
    assert rows["z_task"]["state"] == "retired"
    assert rows["consumer"]["data"]["input_bindings"] == {"report": "a_task"}
    assert rows["child"]["data"]["parent_id"] == "a_task"
    assert rows["edge_to_consumer"]["data"]["from_node"] == "a_task"
    assert rows["repair_loop"]["data"]["entry_node_id"] == "a_task"
    assert rows["repair_loop"]["data"]["node_ids"] == ["a_task", "consumer"]
    assert rows["topic_input"]["data"]["collect_at"] == "a_task"
    assert rows["node_rule"]["data"]["node_id"] == "a_task"
    assert changes and verify_retirements(before, merged)
    assert duplicate_groups(merged) == []


def test_conflicting_work_duplicates_remain_explicit_and_self_edges_block_merge():
    first = node("a_task", data={"work_key": "convert", "acceptance": ["正文保留"]})
    conflicting = node("b_task", data={"work_key": "convert", "acceptance": ["正文和附录保留"]})
    doc = document(first, conflicting)

    unchanged, _, unresolved = apply_dedupe(doc)
    assert len(unresolved) == 1 and not unresolved[0]["identical"]
    assert all(entry["state"] == "active" for entry in unchanged["entries"])

    self_edge = other("cross_copy", "edge", data={"from_node": "a_task", "to_node": "b_task"})
    with pytest.raises(ValueError, match="自环"):
        apply_dedupe(document(node("a_task", data={"work_key": "convert"}),
                              node("b_task", data={"work_key": "convert"}), self_edge))


def test_input_bindings_validate_producer_kind_outputs_and_forward_reachability():
    producer = node("produce", kind="implement", data={"outputs": ["report"]})
    consumer = node("consume", data={"inputs": ["report"], "input_bindings": {"report": "produce"}})

    with pytest.raises(DraftPatchError) as caught:
        check_entry_refs([producer, consumer])
    assert caught.value.code == "unreachable_input_producer"
    assert "非 loop 前向边" in str(caught.value)
    readiness_result = readiness(document(producer, consumer), None,
                                 conversation_id="test", revision=1)
    assert any(item["code"] == "unreachable_input_producer"
               for item in readiness_result["diagnostics"])

    loop_only = other("back", "edge", data={"from_node": "produce", "to_node": "consume",
                                             "condition": "loop"})
    with pytest.raises(DraftPatchError, match="非 loop 前向边"):
        check_entry_refs([producer, consumer, loop_only])

    forward = other("forward", "edge", data={"from_node": "produce", "to_node": "consume"})
    check_entry_refs([producer, consumer, forward])

    wrong_output = node("produce", data={"outputs": ["another-report"]})
    with pytest.raises(DraftPatchError, match="未在 outputs 中声明"):
        check_entry_refs([wrong_output, consumer, forward])


def test_input_entry_binding_obeys_collect_at_availability():
    producer = node("early", kind="plan")
    later = node("late", kind="implement")
    consumer = node("consumer", data={"inputs": ["choice"], "input_bindings": {"choice": "choice_input"}})
    choice = other("choice_input", "input", data={"name": "choice", "collect_at": "late"})
    edge = other("early_to_late", "edge", data={"from_node": "early", "to_node": "late"})

    with pytest.raises(DraftPatchError) as caught:
        check_entry_refs([producer, later, consumer, choice, edge])
    assert caught.value.code == "unreachable_input_binding"


def test_work_key_and_input_bindings_survive_projection_and_reseeding():
    graph = document(
        node("consume", kind="plan", body="按用户选择整理结果", data={
            "work_key": "prepare-selected-output", "inputs": ["topic"],
            "input_bindings": {"topic": "topic_input"},
            "outputs": ["结果"], "acceptance": ["输出与选择一致"]}),
        other("topic_input", "input", data={"name": "topic", "label": "主题", "required": True,
                                               "collect_at": "start", "description": "处理主题"}),
    )

    projected = project_graph(graph, None, conversation_id="test", revision=1).as_dict()
    metadata = projected["meta"]["workflow_draft"]
    assert metadata["node_work_keys"] == {"consume": "prepare-selected-output"}
    assert metadata["node_input_bindings"] == {"consume": {"topic": "topic_input"}}

    reseeded = seed_document(projected)
    restored = {entry["id"]: entry for entry in reseeded["entries"]}
    assert restored["consume"]["data"]["work_key"] == "prepare-selected-output"
    assert restored["consume"]["data"]["input_bindings"] == {"topic": "topic_input"}
    assert restored["topic_input"]["data"]["name"] == "topic"


@pytest.mark.parametrize("data", [
    {"work_key": ""}, {"work_key": " "}, {"work_key": "x" * 101}, {"work_key": 9},
])
def test_work_key_is_optional_but_bounded_when_declared(data):
    with pytest.raises(DraftPatchError):
        validate_entry(node("work", data=data))
