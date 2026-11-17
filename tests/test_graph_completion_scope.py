"""Large graphs can repair one gap without weakening full-graph acceptance."""
import copy
import json

import pytest

from mewcode.orchestration.graph_autofix import autofix
from mewcode.orchestration.graph_autobuild import new_build_state, restart_build_state, run_autobuild
from mewcode.orchestration.graph_completion_protocol import (
    CompletionRequestTooLarge, build_completion_request,
)
from mewcode.orchestration.graph_completion_scope import scope_rejections
from mewcode.orchestration.graph_readiness import readiness
from tests.test_graph_autofix import BASE, FakeModel, document, edge, entry, node, proposal


def large_graph():
    nodes = [node("plan", "plan"), *(node(f"work{i}", "implement") for i in range(28)),
             node("N12A", "implement"), node("audit", "audit"), node("finish", "reduce")]
    for row in nodes:
        row["body"] = row["id"] + " 原始节点职责与边界。" * 330
    chain = ["plan", *(f"work{i}" for i in range(28)), "audit", "finish"]
    edges = [edge(f"e{i}", start, end) for i, (start, end) in enumerate(zip(chain, chain[1:]))]
    bypass = edge("bypass", "N12A", "finish")
    bypass["evidence"] = "ai_completed"
    bypass["rationale"] = "待修正的建议连线"
    return document(*nodes, *edges, edge("repair_entry", "plan", "N12A"), bypass,
                    entry("constraint", "constraint", body="不得改动原始文件；独立审查后才交付"),
                    entry("goal", "goal", body="依照冻结的输入范围完成处理与验收"))


def diagnostics(doc):
    return readiness(doc, BASE, conversation_id="synthetic", revision=1)["diagnostics"]


def fix():
    return {"id": "bypass", "data": {"to_node": "audit"}, "rationale": "复用独立审查，成功后才交付"}


def test_large_request_preserves_complete_global_facts_and_topology():
    doc = large_graph()
    original = copy.deepcopy(doc)
    issues = diagnostics(doc)
    assert [(d["code"], d["entry_ids"]) for d in issues] == [("missing_result_review", ["N12A"])]
    full = build_completion_request(doc, issues, max_chars=500_000)
    bounded = build_completion_request(doc, issues, max_chars=60000)
    assert len(bounded) <= 60_000 < len(full)
    full_rows = {e["id"]: e for e in json.loads(full)["entries"]}
    payload = json.loads(bounded)
    rows = {e["id"]: e for e in payload["entries"]}
    assert rows.keys() == full_rows.keys()
    assert payload["diagnostics"] == issues
    for key in ("N12A", "audit", "plan", "finish", "constraint", "goal", "bypass"):
        assert rows[key] == full_rows[key]
    for row in doc["entries"]:
        if row["kind"] == "edge":
            assert rows[row["id"]]["data"] == row["data"]
    assert rows["work12"]["context_only"] and "body" not in rows["work12"]
    assert rows["work12"]["editable"] == {}
    assert "work12" not in payload["scope"]["full_node_ids"]
    assert doc == original
    assert bounded == build_completion_request(doc, issues, max_chars=60000)


@pytest.mark.asyncio
async def test_32_node_repair_reuses_audit_and_preserves_original_facts():
    doc = large_graph()
    original = copy.deepcopy(doc)
    model = FakeModel(proposal(fix()))
    result = await autofix(doc, BASE, conversation_id="synthetic", revision=1, ask_model=model)
    assert result["status"] == "ready" and result["remaining"] == []
    assert result["model_calls"] == len(model.calls) == 1
    assert "request_scope" not in result["rounds"][0]
    assert len(model.calls[0][1]) > 60000
    assert result["rounds"][0]["structural"]["adopted"]
    after = {e["id"]: e for e in result["document"]["entries"]}
    for row in original["entries"]:
        if row["id"] != "bypass":
            assert after[row["id"]] == row
    assert len([e for e in after.values() if e["kind"] == "node"]) == 32
    assert doc == original


@pytest.mark.parametrize("edit", [
    {"id": "work12", "data": {"outputs": ["偷改未展示要求"]}},
    {"id": "bypass", "data": {"to_node": "work12"}},
    {"id": "new_edge", "kind": "edge", "data": {"from_node": "work12", "to_node": "audit"}},
    {"id": "e12", "data": {"condition": "loop"}},
    {"id": "new_loop", "kind": "loop", "data": {"node_ids": ["N12A", "work12"]}},
    {"id": "new_node", "kind": "node", "data": {"parent_id": "work12"}},
    {"id": "decision", "kind": "decision", "data": {"node_id": "work12"}},
])
def test_host_rejects_edits_or_references_outside_full_context(edit):
    doc = large_graph()
    scope = json.loads(build_completion_request(doc, diagnostics(doc), max_chars=60000))["scope"]
    assert scope_rejections(doc, [edit], scope)[0]["code"] == "outside_request_scope"


@pytest.mark.asyncio
async def test_scope_escape_does_not_apply_even_a_valid_partial_fix(monkeypatch):
    monkeypatch.setattr("mewcode.orchestration.graph_autofix.build_completion_request",
                        lambda *args, **kwargs: build_completion_request(*args, **kwargs, max_chars=60000))
    doc = large_graph()
    # Mark the hidden node as AI-owned so the normal protected-fact guard alone
    # would allow its change. The new host scope guard must reject the batch.
    next(e for e in doc["entries"] if e["id"] == "work12")["evidence"] = "ai_completed"
    edit = {"id": "work12", "data": {"outputs": ["越界产出"]}, "rationale": "不应采用"}
    result = await autofix(doc, BASE, conversation_id="synthetic", revision=1,
                           ask_model=FakeModel(proposal(fix(), edit)), max_repair_rounds=0)
    assert result["document"] == doc and result["status"] == "needs_review"
    assert result["rounds"][0]["structural"]["rejected"][0]["code"] == "outside_request_scope"


@pytest.mark.asyncio
async def test_omitting_review_still_fails_full_graph_validation():
    doc = large_graph()
    # This points to another fully visible node but introduces a cycle and does
    # not provide independent result review. Scope is not an acceptance waiver.
    edit = {"id": "bypass", "data": {"to_node": "plan"}, "rationale": "无效绕行"}
    result = await autofix(doc, BASE, conversation_id="synthetic", revision=1,
                           ask_model=FakeModel(proposal(edit)), max_repair_rounds=0)
    assert result["document"] == doc and result["status"] == "needs_review"
    assert any(d["code"] == "missing_result_review" for d in result["remaining"])


def test_required_facts_are_never_truncated_to_fit():
    doc = large_graph()
    for i in range(12):
        doc["entries"].append(entry(f"global{i}", "constraint", body="全局要求不可省略" * 700))
    with pytest.raises(CompletionRequestTooLarge):
        build_completion_request(doc, diagnostics(doc), max_chars=60000)


def test_focus_keeps_loop_members_and_their_requirements_together():
    doc = large_graph()
    doc["entries"].append(entry("loop", "loop", data={
        "node_ids": ["N12A", "work12"], "reviewer_node_ids": ["N12A"],
        "entry_node_id": "work12", "check_node_id": "N12A", "exit_condition": "原验收"}))
    payload = json.loads(build_completion_request(doc, [{"code": "loop_gap", "entry_ids": ["N12A"]}], max_chars=60000))
    rows = {e["id"]: e for e in payload["entries"]}
    assert rows["work12"]["body"] == next(e for e in doc["entries"] if e["id"] == "work12")["body"]
    assert rows["loop"]["data"]["exit_condition"] == "原验收"


@pytest.mark.asyncio
async def test_retry_after_old_capacity_stop_completes_repair_and_independent_review():
    doc = large_graph()
    for row in doc["entries"]:
        if row["kind"] == "node":
            row["body"] = row["body"][:1350]
    assert len(build_completion_request(doc, diagnostics(doc), max_chars=500_000)) > 60_000
    previous = new_build_state(doc)
    previous.update(phase="done", status="needs_review", reason="request_too_large")
    previous["autofix"]["rounds"] = [{"phase": "completion", "error": "CompletionRequestTooLarge"}]
    state = restart_build_state(doc, previous)
    generate, review = FakeModel(proposal(fix())), FakeModel('{"gaps":[],"notes":"已核对"}')
    result = await run_autobuild(doc, BASE, [{"id": "u", "role": "user", "text": "独立审查后才交付"}],
        conversation_id="synthetic", revision=1, state=state, ask_model=generate, ask_review=review)
    build = result["build"]
    assert build["phase"] == "done" and build["status"] == "ready"
    assert build["model_calls"] == build["call_attempts"] == 2
    assert len(generate.calls) == len(review.calls) == 1
    assert build["previous_attempts"][0]["reason"] == "request_too_large"
    assert len(generate.calls[0][1]) > 60_000
    assert not build["remaining"] and not build["review"]["error"]
    # Independent review still receives every original node requirement.
    reviewed = json.loads(review.calls[0][1])["graph"]["nodes"]
    assert len(reviewed) == 32
    assert {n["id"]: n["goal"] for n in reviewed} == {
        e["id"]: e["body"] for e in doc["entries"] if e["kind"] == "node"}


@pytest.mark.asyncio
async def test_large_repair_and_independent_review_both_receive_complete_requests():
    doc = large_graph()
    generate, review = FakeModel(proposal(fix())), FakeModel('{"gaps":[],"notes":"全部检查"}')
    result = await run_autobuild(doc, BASE, [{"id": "u", "role": "user", "text": "独立审查后才交付"}],
        conversation_id="synthetic", revision=1, ask_model=generate, ask_review=review)
    assert result["build"]["status"] == "ready"
    assert result["build"]["review"]["error"] == ""
    assert len(review.calls) == 1 and len(review.calls[0][1]) > 60000
    assert {n["id"]: n["goal"] for n in json.loads(review.calls[0][1])["graph"]["nodes"]} == {
        e["id"]: e["body"] for e in doc["entries"] if e["kind"] == "node"}
