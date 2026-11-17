"""Recover the observed conflict/schema/feedback failure without weakening guards."""
import copy
import json

import pytest

from mewcode.orchestration.graph_autofix import _atoms, autofix
from mewcode.orchestration.graph_completion import apply_completion, protected_violations
from mewcode.orchestration.graph_completion_protocol import build_completion_request
from mewcode.orchestration.graph_readiness import readiness
from mewcode.orchestration.workflow_draft import project_graph
from mewcode.orchestration.convert import to_expert_flow
from tests.graph_repair_recovery_fixtures import (correct_proposal, failed_document, rejected_proposal,
                                                hierarchical_failed_document, hierarchical_proposals)
from tests.test_graph_readiness import doc, edge, node


@pytest.mark.asyncio
async def test_hierarchical_repair_rejects_exposed_review_gaps_then_adopts_complete_fix():
    original = hierarchical_failed_document()
    before = copy.deepcopy(original)
    proposals = list(hierarchical_proposals())
    calls = []
    async def model(_system, request):
        inputs = json.loads(request)
        calls.append(inputs)
        assert any(d["code"] == "unreachable_to_finalizer" for d in inputs["diagnostics"])
        assert any(row["rule"] == "hierarchy" for row in inputs["runtime_semantics"])
        if len(calls) == 1:
            return json.dumps(proposals[0])
        return json.dumps({'upserts': proposals[0]['upserts'] + proposals[1]['upserts']})
    result = await autofix(original, None, conversation_id="c", revision=1, ask_model=model)
    assert result["status"] == "ready" and len(calls) == 2, result['remaining']
    assert result['rounds'][0]['structural']['reason'] == 'new_errors'
    assert any(d['code'] == 'missing_result_review'
               for d in result['rounds'][0]['structural']['diagnostics'])
    assert result['rounds'][1]['structural']['adopted']
    assert original == before
    current = {row["id"]: row for row in result["document"]["entries"]}
    assert len([row for row in result["document"]["entries"] if row["kind"] == "node"]) == 24
    spec = project_graph(result["document"], None, conversation_id="c", revision=1)
    flow = to_expert_flow(spec)
    assert spec.finalizer_node_id == flow.finalizer_node_id == "node_final_gate_delivery"
    assert len(flow.nodes) == 24
    ocr_loop = next(loop for loop in flow.meta["loops"] if loop["loop_id"] == "loop_atomic_ocr_audit_repair")
    assert ocr_loop.get("control_mode", "strict") == "strict"
    assert ocr_loop["repair_dispatch"] == "after_decision" and len(ocr_loop["node_ids"]) == 5
    for old in before["entries"]:
        assert protected_violations(old, current[old["id"]]) == []
        if old["kind"] in {"node", "loop"}:
            assert current[old["id"]] == old


@pytest.mark.asyncio
async def test_partial_component_join_is_adopted_without_hiding_remaining_old_disconnection():
    document = doc(node("start", "plan"), node("finish", "reduce"), node("a"), node("b"), node("orphan"),
                   edge("e1", "start", "finish"), edge("e2", "a", "b"))
    before = readiness(document, None, conversation_id="c", revision=1)
    async def model(*_):
        return json.dumps({"upserts": [{"id": "join", "kind": "edge", "title": "接通孤点", "body": "输入依赖",
            "data": {"from_node": "orphan", "to_node": "start"}, "rationale": "逐步连接既有分量"}]})
    result = await autofix(document, None, conversation_id="c", revision=1, ask_model=model, max_repair_rounds=0)
    assert result["rounds"][0]["structural"]["adopted"]
    assert result["status"] == "needs_review"
    assert _atoms(result["remaining"]) < _atoms(before["diagnostics"])
    assert any(d["code"] == "disconnected" and d["isolated_node_ids"] == [] for d in result["remaining"])


@pytest.mark.asyncio
async def test_rejected_real_field_shapes_are_fed_back_and_three_failures_converge():
    original = failed_document()
    before = copy.deepcopy(original)
    calls = []

    async def model(_system, request):
        payload = json.loads(request)
        calls.append(payload)
        conflict = next(row for row in payload["entries"] if row["id"] == "edge_ocr_decide_continue_to_first_read")
        assert conflict["state"] == "conflict" and conflict["data"]["condition"] == "loop"
        assert conflict["editable"]["resolve_conflict"] == ["body", "data"]
        contract = payload["completion_contract"]
        assert "continue_condition" not in contract["data_fields"]["loop"]
        assert "budget" not in contract["data_fields"]["loop"]
        assert "loop_id" not in contract["data_fields"]["edge"]
        assert contract["field_types"]["loop"]["control_mode"] == ["team"]
        assert {row["rule"] for row in payload["runtime_semantics"]} >= {"after_decision", "forward_dag"}
        if len(calls) == 1:
            assert payload["previous_feedback"] == []
            return json.dumps(rejected_proposal(), ensure_ascii=False)
        feedback = payload["previous_feedback"][-1]
        assert feedback["structural"]["adopted"] is False
        assert {row["entry_id"] for row in feedback["structural"]["rejected"]} == {
            "loop_sample_quality", "edge_sample_gate_fail_repair"}
        assert feedback["ledger"]["rejected"][0]["code"] == "structure_not_adopted", "design prose from a refused structure must remain an unadopted proposal"
        return json.dumps(correct_proposal(), ensure_ascii=False)

    result = await autofix(original, None, conversation_id="c", revision=1, ask_model=model)
    assert result["status"] == "ready" and result["remaining"] == []
    assert len(calls) == result["model_calls"] == 2
    assert result["rounds"][0]["diagnostics_before"] == 5
    assert result["rounds"][1]["diagnostics_after"] == 0
    assert original == before
    for old in before["entries"]:
        current = next(row for row in result["document"]["entries"] if row["id"] == old["id"])
        assert protected_violations(old, current) == []
        if old["kind"] == "node" or old["kind"] == "loop":
            assert current == old
    conflict = next(row for row in result["document"]["entries"] if row["id"] == "edge_ocr_decide_continue_to_first_read")
    assert conflict["state"] == "active" and conflict["sources"] == before["entries"][-2]["sources"]
    audit = next(row for row in result["document"]["entries"] if "proposed_conflict_resolved" in row["body"])
    assert audit["evidence"] == "ai_completed" and "state: conflict → active" in audit["data"]["value"]


@pytest.mark.parametrize("evidence,reviewed", [("user_source", False), ("manual", False), ("saved_graph", False), ("proposal", True)])
def test_conflict_resolution_never_clears_user_or_confirmed_facts(evidence, reviewed):
    document = failed_document()
    target = next(row for row in document["entries"] if row["state"] == "conflict")
    target.update(evidence=evidence, reviewed=reviewed)
    before = copy.deepcopy(document)
    raw = correct_proposal()["upserts"][-1]
    after, applied, rejected = apply_completion(document, {"upserts": [raw]})
    assert after == before and applied == []
    assert rejected[0]["code"] == "conflict_resolution_forbidden"
    hint = next(row for row in json.loads(build_completion_request(document, []))["entries"] if row["id"] == target["id"])
    assert hint["state"] == "conflict" and hint["editable"] == {}


@pytest.mark.parametrize("extra", [{}, {"state": "active"}, {"title": "只改名字"}, {"resolve_conflict": "true"}])
def test_state_only_or_malformed_resolution_is_rejected(extra):
    document = failed_document()
    raw = {"id": "edge_ocr_decide_continue_to_first_read", "resolve_conflict": True,
           "rationale": "尝试只清除冲突", **extra}
    after, applied, rejected = apply_completion(document, {"upserts": [raw]})
    assert after == document and not applied and rejected


@pytest.mark.asyncio
async def test_feedback_survives_interruption_and_includes_failed_trial_diagnostics():
    document = failed_document()
    orphan = {"id": "orphan", "kind": "node", "title": "孤点", "body": "辅助",
              "data": {"node_kind": "implement"}, "rationale": "故意引入新错误"}

    async def first(*_):
        return json.dumps({"upserts": [orphan]})

    failed = await autofix(document, None, conversation_id="c", revision=1, ask_model=first, max_repair_rounds=0)
    assert failed["rounds"][0]["structural"]["reason"] == "new_errors"
    calls = []

    async def second(_system, request):
        feedback = json.loads(request)["previous_feedback"]
        calls.append(feedback)
        assert feedback[0]["structural"]["reason"] == "new_errors"
        assert any("orphan" in d["entry_ids"] for d in feedback[0]["structural"]["diagnostics"])
        return json.dumps(correct_proposal())

    result = await autofix(failed["document"], None, conversation_id="c", revision=1, ask_model=second,
                          resume={"rounds": failed["rounds"], "model_calls": failed["model_calls"]})
    assert len(calls) == 1 and result["status"] == "ready" and result["model_calls"] == 2


@pytest.mark.asyncio
async def test_unresolvable_user_conflict_stops_repeated_requests_once_other_structure_is_ready():
    document, _, rejected = apply_completion(failed_document(), correct_proposal())
    assert rejected == [] and readiness(document, None, conversation_id="c", revision=1)["ready"]
    target = next(row for row in document["entries"] if row["id"] == "prepare_sample")
    target.update(state="conflict", evidence="user_source")
    before = copy.deepcopy(document)
    calls = []

    async def model(*_):
        calls.append(1)
        return '{"upserts":[],"notes":"用户要求冲突保留"}'

    result = await autofix(document, None, conversation_id="c", revision=1, ask_model=model)
    assert result["document"] == before and result["status"] == "needs_review"
    assert result["reason"] == "conflict_requires_review" and len(calls) == 1
