"""Finite completion and repair with injected fake-model responses."""
import asyncio
import copy
import json

import pytest

from mewcode.orchestration import graph_autofix
from mewcode.orchestration.graph_completion import protected_violations
from mewcode.orchestration.graph_completion_protocol import CompletionRequestTooLarge
from mewcode.orchestration.graph_readiness import readiness


def entry(entry_id, kind="node", *, data=None, evidence="manual", body="执行"):
    return {"id": entry_id, "kind": kind, "title": entry_id, "body": body,
            "state": "active", "data": data if data is not None else {},
            "sources": [], "evidence": evidence}


def node(node_id, kind):
    return entry(node_id, data={"node_kind": kind, "inputs": [],
                                "outputs": ["产物"], "acceptance": ["验收"]})


def edge(edge_id, start, end):
    return entry(edge_id, "edge", data={"from_node": start, "to_node": end, "condition": "always"})


def document(*entries):
    return {"schema_version": 1, "entries": list(entries), "ignored": []}


def graph(*, complete=False):
    entries = [node("plan", "plan"), node("audit", "audit"), node("finish", "reduce"),
               edge("first", "plan", "audit")]
    if complete:
        entries.append(edge("last", "audit", "finish"))
    return document(*entries)


BASE = {"finalizer_node_id": "finish"}


def proposal(*upserts, notes=""):
    return json.dumps({"upserts": list(upserts), "notes": notes}, ensure_ascii=False)


def finish_edge():
    return {"id": "last", "kind": "edge", "title": "复核后交付", "body": "从复核进入终验",
            "data": {"from_node": "audit", "to_node": "finish", "condition": "always"},
            "rationale": "连接终验以消除断连"}


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, system, user):
        self.calls.append((system, user))
        answer = self.responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


async def run(doc, fake, **kwargs):
    return await graph_autofix.autofix(doc, BASE, conversation_id="c", revision=1,
                                       ask_model=fake, **kwargs)


@pytest.mark.asyncio
async def test_missing_edge_is_fixed_in_first_completion_with_one_call():
    before = graph()
    original = copy.deepcopy(before)
    fake = FakeModel(proposal(finish_edge()))
    result = await run(before, fake)
    assert result["status"] == "ready" and result["reason"] == ""
    assert result["model_calls"] == len(fake.calls) == len(result["rounds"]) == 1
    assert result["rounds"][0]["phase"] == "completion"
    assert result["rounds"][0]["structural"]["adopted"] is True
    assert result["rounds"][0]["structural"]["reason"] == "improved"
    assert result["rounds"][0]["diagnostics_after"] == 0
    assert result["remaining"] == [] and before == original
    assert readiness(result["document"], BASE, conversation_id="c", revision=1)["ready"]
    for old in original["entries"]:
        now = next(e for e in result["document"]["entries"] if e["id"] == old["id"])
        assert protected_violations(old, now) == []
    assert "只根据给定诊断" in fake.calls[0][0]
    assert json.loads(fake.calls[0][1])["diagnostics"]


@pytest.mark.asyncio
async def test_new_bridge_and_its_edges_land_together_even_when_edges_come_first():
    before = document(node("plan", "plan"), node("finish", "reduce"))
    bridge = {"id": "bridge", "kind": "node", "title": "衔接", "body": "连接阶段",
              "data": {"node_kind": "audit", "outputs": ["检查记录"], "acceptance": ["通过"]},
              "rationale": "补充连接节点"}
    first = {"id": "e1", "kind": "edge", "title": "进入复核", "body": "依赖",
             "data": {"from_node": "plan", "to_node": "bridge", "condition": "always"},
             "rationale": "连接阶段"}
    last = {"id": "e2", "kind": "edge", "title": "进入终验", "body": "依赖",
            "data": {"from_node": "bridge", "to_node": "finish", "condition": "always"},
            "rationale": "连接终验"}
    result = await run(before, FakeModel(proposal(first, last, bridge)))
    assert result["status"] == "ready" and result["model_calls"] == 1
    assert {e["id"] for e in result["document"]["entries"]} == {"plan", "bridge", "finish", "e1", "e2"}
    assert [receipt["entry_id"] for receipt in result["rounds"][0]["structural"]["applied"]] == ["e1", "e2", "bridge"]


@pytest.mark.asyncio
async def test_already_ready_graph_still_classifies_ledger_once():
    before = graph(complete=True)
    fake = FakeModel(proposal({"id": "default", "kind": "decision", "title": "默认篇数",
                               "body": "默认八篇", "data": {"category": "defaulted", "value": "8"},
                               "rationale": "有合理默认"}))
    result = await run(before, fake)
    assert result["status"] == "ready" and result["model_calls"] == 1
    assert len(result["rounds"]) == 1
    assert result["rounds"][0]["structural"]["reason"] == "not_proposed"
    assert result["rounds"][0]["ledger"]["adopted"] is True
    assert any(e["id"] == "default" for e in result["document"]["entries"])


@pytest.mark.asyncio
async def test_new_diagnostic_discards_entire_structural_trial():
    before = graph()
    fake = FakeModel(proposal({"id": "orphan", "kind": "node", "title": "孤点", "body": "辅助",
                               "data": {"node_kind": "implement", "outputs": [], "acceptance": []},
                               "rationale": "尝试增加节点"}))
    result = await run(before, fake, max_repair_rounds=0)
    assert result["document"] == before
    assert result["status"] == "needs_review" and result["reason"] == "rounds_exhausted"
    assert result["rounds"][0]["structural"]["reason"] == "new_errors"
    assert result["rounds"][0]["structural"]["adopted"] is False


@pytest.mark.asyncio
async def test_no_improvement_discards_structure_and_its_design_explanation():
    before = graph()
    fake = FakeModel(proposal(
        {"id": "plan", "data": {"inputs": ["主题"]}, "rationale": "补齐输入"},
        {"id": "assumption", "kind": "decision", "title": "顺序", "body": "先拟纲要",
         "data": {"category": "designed"}, "rationale": "顺序设计"}))
    result = await run(before, fake, max_repair_rounds=0)
    assert result["rounds"][0]["structural"]["reason"] == "no_improvement"
    assert result["rounds"][0]["ledger"]["reason"] == "structure_not_adopted"
    assert result["rounds"][0]["ledger"]["rejected"][0]["entry_id"] == "assumption"
    ids = {e["id"] for e in result["document"]["entries"]}
    assert "assumption" not in ids and not any(e["id"].startswith("audit_") for e in result["document"]["entries"])
    assert next(e for e in result["document"]["entries"] if e["id"] == "plan")["data"]["inputs"] == []


@pytest.mark.asyncio
async def test_partial_structural_acceptance_cannot_update_existing_design():
    before = graph()
    decision = entry("assumption", "decision", data={"category": "designed", "value": "旧顺序"}, evidence="ai_completed")
    before["entries"].append(decision)
    fake = FakeModel(proposal(finish_edge(),
        {"id": "plan", "body": "越权改职责", "rationale": "不得采用"},
        {"id": "assumption", "body": "假称已变更", "rationale": "新说明"}))
    result = await run(before, fake)
    assert result["rounds"][0]["structural"]["adopted"]
    assert result["rounds"][0]["structural"]["rejected"]
    assert result["rounds"][0]["ledger"]["rejected"][0]["code"] == "structure_not_adopted"
    assert next(e for e in result["document"]["entries"] if e["id"] == "assumption") == decision


@pytest.mark.asyncio
async def test_nonconverging_graph_stops_after_one_completion_and_three_repairs():
    fake = FakeModel(*(proposal() for _ in range(5)))
    result = await run(graph(), fake)
    assert result["status"] == "needs_review" and result["reason"] == "rounds_exhausted"
    assert result["model_calls"] == len(fake.calls) == len(result["rounds"]) == 4
    assert [row["phase"] for row in result["rounds"]] == ["completion", "repair", "repair", "repair"]
    assert len(fake.responses) == 1


@pytest.mark.asyncio
async def test_forbidden_delete_acceptance_and_gate_are_rejected_without_write():
    before = graph()
    fake = FakeModel(proposal(
        {"id": "plan", "state": "retired", "rationale": "尝试撤回"},
        {"id": "audit", "data": {"acceptance": ["放宽验收"]}, "rationale": "尝试改验收"},
        {"id": "gate_new", "kind": "node", "title": "新确认", "body": "人工签认",
         "data": {"node_kind": "gate"}, "rationale": "尝试新增确认"}))
    result = await run(before, fake, max_repair_rounds=0)
    assert result["document"] == before
    assert {row["code"] for row in result["rounds"][0]["structural"]["rejected"]} == {
        "delete_forbidden", "locked_content", "gate_forbidden"}


@pytest.mark.asyncio
async def test_cumulative_guard_rolls_back_a_bypassed_locked_change(monkeypatch):
    real_apply = graph_autofix.apply_completion

    def bypass(document, proposal):
        updated, applied, rejected = real_apply(document, proposal)
        if applied and any(item["id"] == "last" for item in proposal["upserts"]):
            next(e for e in updated["entries"] if e["id"] == "plan")["body"] = "被越权改写"
        return updated, applied, rejected

    monkeypatch.setattr(graph_autofix, "apply_completion", bypass)
    before = graph()
    decision = {"id": "assumption", "kind": "decision", "title": "默认顺序", "body": "先复核后终验",
                "data": {"category": "designed"}, "rationale": "记录设计判断"}
    result = await run(before, FakeModel(proposal(finish_edge(), decision)), max_repair_rounds=0)
    assert result["document"] == before
    assert result["rounds"][0]["structural"]["reason"] == "guard_failed"
    assert result["rounds"][0]["structural"]["adopted"] is False
    assert result["rounds"][0]["ledger"]["adopted"] is False
    assert all(e["id"] != "assumption" and not e["id"].startswith("audit_")
               for e in result["document"]["entries"])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"id": [], "rationale": "畸形 ID"},
    {"id": "bad", "kind": ["question"], "rationale": "畸形类型"},
])
async def test_malformed_model_fields_become_rejections_not_exceptions(bad):
    before = graph()
    result = await run(before, FakeModel(proposal(bad)), max_repair_rounds=0)
    assert result["document"] == before
    assert result["rounds"][0]["structural"]["rejected"][0]["code"] == "not_whitelisted"
    assert result["rounds"][0]["structural"]["reason"] == "nothing_applied"


@pytest.mark.asyncio
async def test_later_guard_failure_preserves_prior_safe_round(monkeypatch):
    before = graph()
    next(e for e in before["entries"] if e["id"] == "plan")["data"]["outputs"] = []
    real_apply = graph_autofix.apply_completion

    def bypass(document, proposal):
        updated, applied, rejected = real_apply(document, proposal)
        if applied and any(item.get("id") == "last" for item in proposal["upserts"]):
            next(e for e in updated["entries"] if e["id"] == "plan")["data"]["acceptance"] = ["越权标准"]
        return updated, applied, rejected

    monkeypatch.setattr(graph_autofix, "apply_completion", bypass)
    fake = FakeModel(
        proposal({"id": "plan", "data": {"outputs": ["纲要"]}, "rationale": "补齐空产出"}),
        proposal(finish_edge()),
    )
    result = await run(before, fake, max_repair_rounds=1)
    assert result["model_calls"] == 2
    assert result["rounds"][0]["structural"]["reason"] == "improved"
    assert result["rounds"][1]["structural"]["reason"] == "guard_failed"
    final = {e["id"]: e for e in result["document"]["entries"]}
    assert final["plan"]["data"]["outputs"] == ["纲要"]
    assert final["plan"]["data"]["acceptance"] == ["验收"]
    assert "last" not in final and any(entry_id.startswith("audit_") for entry_id in final)


@pytest.mark.asyncio
async def test_model_error_redacts_message_and_stops_without_retry():
    fake = FakeModel(RuntimeError("credential=do-not-log"), proposal(finish_edge()))
    result = await run(graph(), fake)
    assert result["status"] == "needs_review" and result["reason"] == "model_error"
    assert result["model_calls"] == len(fake.calls) == 1 and len(result["rounds"]) == 1
    assert result["rounds"][0]["error"] == "RuntimeError"
    assert result["rounds"][0]["structural"]["reason"] == "model_error"
    assert "do-not-log" not in str(result)


@pytest.mark.asyncio
async def test_cancelled_model_call_propagates():
    fake = FakeModel(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run(graph(), fake)
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_parse_failure_uses_round_then_later_repair_succeeds():
    fake = FakeModel("not json", proposal(finish_edge()))
    result = await run(graph(), fake, max_repair_rounds=1)
    assert result["status"] == "ready" and result["model_calls"] == 2
    assert [row["structural"]["reason"] for row in result["rounds"]] == ["parse_failed", "improved"]
    assert [row["phase"] for row in result["rounds"]] == ["completion", "repair"]


@pytest.mark.asyncio
async def test_blocking_downgrade_and_final_blocking_ids():
    question = {"id": "q", "kind": "question", "title": "待确认", "body": "是否删除",
                "data": {"blocking": True}, "rationale": "人工决定"}
    result = await run(graph(complete=True), FakeModel(proposal(question)))
    assert result["blocking_question_ids"] == []
    assert result["rounds"][0]["normalize_notes"] == [{"code": "blocking_downgraded", "entry_id": "q"}]
    assert next(e for e in result["document"]["entries"] if e["id"] == "q")["data"]["blocking"] is False
    empty = document()
    result = await graph_autofix.autofix(empty, None, conversation_id="c", revision=1,
                                        ask_model=FakeModel(proposal(question)), max_repair_rounds=0)
    assert result["blocking_question_ids"] == ["q"]


@pytest.mark.asyncio
async def test_request_too_large_ends_without_model_call(monkeypatch):
    def too_large(*args, **kwargs):
        raise CompletionRequestTooLarge("长度 100 超过上限 10")

    monkeypatch.setattr(graph_autofix, "build_completion_request", too_large)
    fake = FakeModel(proposal(finish_edge()))
    result = await run(graph(), fake)
    assert result["status"] == "needs_review" and result["reason"] == "request_too_large"
    assert result["model_calls"] == len(fake.calls) == 0
    assert result["rounds"][0]["error"] == "CompletionRequestTooLarge"
