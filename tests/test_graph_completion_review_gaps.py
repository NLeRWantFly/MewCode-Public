"""Regression tests for gaps found in independent review of the first batch.

They pin behavior the specification requires but the first implementation
missed: a connective node and its edges must land together, and the model must
not be able to mark its own entries as human-reviewed or attach fabricated
source quotations.
"""
import pytest

from mewcode.orchestration.graph_completion import apply_completion
from mewcode.orchestration.graph_readiness import readiness
from mewcode.orchestration.workflow_draft import empty_document


def entry(entry_id, kind="node", *, data=None, evidence="user_source", **more):
    return {"id": entry_id, "kind": kind, "title": entry_id, "body": "用户要求",
            "state": "active", "data": data if data is not None else {}, "sources": [],
            "evidence": evidence, **more}


def step(entry_id, node_kind="implement"):
    return entry(entry_id, data={"node_kind": node_kind, "outputs": ["产出" + entry_id],
                                 "acceptance": ["验收" + entry_id]})


def doc():
    return {"schema_version": 1, "ignored": [], "entries": [
        step("a"), step("b", "reduce"),
        entry("g", data={"node_kind": "gate"}),
        entry("in1", "input", data={"name": "主题", "required": True},
              evidence="ai_completed", rationale="运行时需要主题"),
    ]}


def item(entry_id, kind, title="条目", body="", data=None, **more):
    return {"id": entry_id, "kind": kind, "title": title, "body": body,
            "data": data or {}, "rationale": "补齐结构", **more}


def test_connective_node_and_its_edges_land_in_one_proposal_in_any_order():
    proposal = {"upserts": [
        item("wire_out", "edge", data={"from_node": "bridge", "to_node": "b", "condition": "always"}),
        item("bridge", "node", "衔接步骤", "补齐 A 到 B 的衔接",
             {"node_kind": "implement", "outputs": ["衔接产物"], "acceptance": ["可核对"]}),
        item("wire_in", "edge", data={"from_node": "a", "to_node": "bridge", "condition": "always"}),
    ]}
    after, applied, rejected = apply_completion(doc(), proposal)
    assert rejected == []
    assert [a["entry_id"] for a in applied] == ["wire_out", "bridge", "wire_in"]
    assert [a["code"] for a in applied] == ["edge_added", "node_added", "edge_added"]
    assert {"bridge", "wire_in", "wire_out"} <= {e["id"] for e in after["entries"]}


def test_edge_to_a_rejected_new_node_is_rejected_too():
    proposal = {"upserts": [
        item("new_gate", "node", "签认", data={"node_kind": "gate"}),
        item("to_gate", "edge", data={"from_node": "a", "to_node": "new_gate", "condition": "always"}),
    ]}
    after, applied, rejected = apply_completion(doc(), proposal)
    assert applied == [] and after == doc()
    assert [(r["entry_id"], r["code"]) for r in rejected] == [
        ("new_gate", "gate_forbidden"), ("to_gate", "bad_reference")]


@pytest.mark.parametrize("extra", [
    {"reviewed": True},
    {"sources": [{"source_id": "s_1", "quote": "用户说过必须有这一步"}]},
])
def test_ai_cannot_self_certify_or_forge_sources_on_new_entries(extra):
    after, applied, rejected = apply_completion(doc(), {"upserts": [
        item("n", "node", data={"node_kind": "implement"}, **extra)]})
    assert applied == [] and after == doc()
    assert rejected[0]["code"] == "not_whitelisted"


def test_neutral_provenance_fields_are_accepted_and_normalised():
    after, applied, rejected = apply_completion(doc(), {"upserts": [
        item("n", "node", data={"node_kind": "implement"}, reviewed=False, sources=[])]})
    assert rejected == [] and applied[0]["code"] == "node_added"
    stored = next(e for e in after["entries"] if e["id"] == "n")
    assert stored["evidence"] == "ai_completed" and not stored.get("reviewed") and stored["sources"] == []


def test_duplicate_run_input_names_and_self_edges_are_rejected():
    after, applied, rejected = apply_completion(doc(), {"upserts": [
        item("in2", "input", data={"name": " 主题 ", "required": False}),
        item("self_edge", "edge", data={"from_node": "a", "to_node": "a", "condition": "always"}),
    ]})
    assert applied == [] and after == doc()
    assert [(r["entry_id"], r["code"]) for r in rejected] == [
        ("in2", "duplicate_id"), ("self_edge", "bad_reference")]


def test_completion_turns_a_disconnected_graph_into_a_ready_one():
    graph = empty_document()
    graph["entries"] = [step("a", "plan"), step("b", "audit"), step("c", "reduce")]
    before = readiness(graph, None, conversation_id="c1", revision=1)
    assert not before["ready"]
    assert {"disconnected", "unreachable_to_finalizer"} <= {d["code"] for d in before["diagnostics"]}
    completed, applied, rejected = apply_completion(graph, {"upserts": [
        item("e_bc", "edge", data={"from_node": "b", "to_node": "c", "condition": "always"}),
        item("bridge", "node", "衔接", "衔接 A 与 B",
             {"node_kind": "implement", "outputs": ["x"], "acceptance": ["y"]}),
        item("e_a_bridge", "edge", data={"from_node": "a", "to_node": "bridge", "condition": "always"}),
        item("e_bridge_b", "edge", data={"from_node": "bridge", "to_node": "b", "condition": "always"}),
    ]})
    assert rejected == [] and len(applied) == 4
    after = readiness(completed, None, conversation_id="c1", revision=2)
    assert after["ready"] and after["diagnostics"] == []
