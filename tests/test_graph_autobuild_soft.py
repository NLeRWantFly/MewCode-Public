"""The product's autonomous route is driven by results and model decisions."""
import json

import pytest

from mewcode.orchestration.graph_autobuild import new_build_state, run_autobuild
from tests.test_graph_autobuild import FakeModel, graph, proposal, finish_edge, pdf_gap, pdf_proposal


async def build(document, model, reviewer, **kwargs):
    return await run_autobuild(document, {"finalizer_node_id": "finish"},
        [{"id": "u", "role": "user", "text": "最后导出 PDF"}], conversation_id="c", revision=1,
        ask_model=model, ask_review=reviewer, autonomous=True, **kwargs)


def keep_going(step):
    return json.dumps({"control": {"action": "continue", "reason": "已有具体修法", "next_step": step}})


@pytest.mark.asyncio
async def test_construction_can_exceed_old_call_and_repair_limits():
    replies = []
    for index in range(4):
        replies.extend(["incomplete JSON", keep_going(f"第{index + 1}种字段修正方式")])
    model = FakeModel(*replies, proposal(finish_edge()))
    reviewer = FakeModel('{"gaps":[],"notes":"覆盖通过"}')
    initial = new_build_state(graph(), now=0)
    result = await build(graph(), model, reviewer, state=initial, clock=lambda: 10_000)
    assert result["build"]["status"] == "ready"
    assert len(model.calls) + len(reviewer.calls) == result["build"]["call_attempts"] == 10
    assert result["build"]["model_calls"] == 10


@pytest.mark.asyncio
async def test_coverage_can_be_filled_again_after_model_reassesses_same_gap():
    gap = json.dumps({"gaps": [pdf_gap()], "notes": "仍有缺口"})
    model = FakeModel(proposal(), proposal(), keep_going("明确加入 PDF 导出和独立审查，再进入终验"), pdf_proposal())
    reviewer = FakeModel(gap, gap, '{"gaps":[]}')
    result = await build(graph(ready=True), model, reviewer)
    assert result["build"]["status"] == "ready"
    assert len(result["build"]["gap_fill_history"]) == 1
    assert result["build"]["model_calls"] == len(model.calls) + len(reviewer.calls) == 7


@pytest.mark.asyncio
async def test_model_stops_without_claiming_readiness_or_requesting_business_decisions():
    model = FakeModel(proposal(), '{"control":{"action":"stop","reason":"尚无可靠的新修法，保存当前草图。","next_step":""}}')
    reviewer = FakeModel()
    result = await build(graph(), model, reviewer)
    assert result["build"]["status"] == "needs_review"
    assert result["build"]["reason"] == "model_stopped"
    assert result["build"]["remaining"] and not reviewer.calls


@pytest.mark.asyncio
async def test_warning_only_graph_moves_to_coverage_without_stalled_control_call():
    document = graph(ready=True)
    next(e for e in document["entries"] if e["id"] == "audit")["data"]["steps"] = ["写入文件"]
    model = FakeModel(proposal())
    reviewer = FakeModel('{"gaps":[],"notes":"覆盖通过"}')
    result = await build(document, model, reviewer)
    assert result["build"]["status"] == "ready"
    assert len(model.calls) == 1 and len(reviewer.calls) == 1
    assert any(d["code"] == "tools_too_narrow" for d in result["build"]["remaining"])
    assert all(d.get("severity") == "warning" for d in result["build"]["remaining"])
    assert result["document"] == document
