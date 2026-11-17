from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from mewcode.decisions.clarification import before_ask
from mewcode.experts.interaction_routing import derive_handling, resolve_handling
from mewcode.tools.ask_user import AskUserParams
from tests.test_decision_runtime import harness


CANDIDATES = [
    {"id": "repair_a", "description": "修复已确认的输入格式问题"},
    {"id": "repair_b", "description": "使用已有的兼容读取方式"},
]


def test_internal_technical_question_stays_with_system():
    route = derive_handling(purpose="decision", question_kind="technical_choice",
        questions=[{"message": "选哪个修复方式？"}], context={})
    assert route["owner"] == "system"
    assert route["handling_state"] == "deciding"
    assert route["reason_kind"] == "technical_question"
    assert route["evidence_refs"] == []


@pytest.mark.parametrize(("kwargs", "reason_kind"), [
    ({"purpose": "decision", "explicit_user_gate": True}, "explicit_user_gate"),
    ({"purpose": "decision", "unmet_authority": ["review-1"]}, "authorization"),
    ({"purpose": "decision", "missing_user_fields": ["invoice number"]}, "user_input"),
    ({"purpose": "decision", "root_cause": "scope", "context": {"evidence_refs": ["E-1"]}}, "scope"),
    ({"purpose": "decision", "root_cause": "cost", "context": {"evidence_refs": ["E-2"]}}, "cost"),
    ({"purpose": "decision", "root_cause": "goal_change", "context": {"evidence_refs": ["E-3"]}}, "goal"),
    ({"purpose": "decision", "root_cause": "model_no_authorized_backup", "context": {"evidence_refs": ["E-4"]}}, "model_fallback"),
    ({"purpose": "user_input"}, "user_input"),
    ({"purpose": "authorization"}, "authorization"),
])
def test_evidenced_user_owned_gates_route_to_human(kwargs, reason_kind):
    route = derive_handling(**kwargs)
    assert route["owner"] == "user"
    assert route["handling_state"] == "needs_user"
    assert route["reason_kind"] == reason_kind


def test_unproven_scope_label_does_not_interrupt_user():
    route = derive_handling(purpose="decision", root_cause="scope", context={})
    assert route["owner"] == "system"
    assert route["handling_state"] == "deciding"


@pytest.mark.asyncio
async def test_user_owned_route_does_not_call_selector():
    selector = AsyncMock()
    route = derive_handling(purpose="decision", explicit_user_gate=True)
    result = await resolve_handling(route, candidates=CANDIDATES,
                                    selector=selector, context={})
    assert result["status"] == "needs_user"
    selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_feasible_technical_candidate_is_system_blocked():
    selector = AsyncMock()
    route = derive_handling(purpose="decision")
    result = await resolve_handling(route, candidates=[
        {"id": "unsafe", "description": "不满足宿主约束", "eligible": False}],
        selector=selector)
    assert result["status"] == "blocked"
    assert result["owner"] == "system"
    assert result["handling_state"] == "blocked"
    selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_candidate_is_advisory_and_stays_system_owned():
    async def select(candidates, context):
        assert context == {"observed": "input is text"}
        return {"status": "selected", "selected_option_id": candidates[0]["id"], "confidence": .9}

    route = derive_handling(purpose="decision")
    result = await resolve_handling(route, candidates=CANDIDATES,
                                    selector=select, context={"observed": "input is text"})
    assert result["status"] == "selected"
    assert result["handling_state"] == "working"
    assert result["selected_option_id"] == "repair_a"
    assert "仍需检查约束" in result["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [
    {"status": "rethink", "reason": "低置信"},
    {"status": "escalate", "reason": "两次拒绝"},
    {"status": "main_model", "reason": "Jev 不可用"},
    {"status": "selected", "selected_option_id": "repair_a", "confidence": .3},
])
async def test_selector_failure_or_uncertainty_returns_to_main_model(selected):
    async def select(_candidates, _context):
        return selected

    route = derive_handling(purpose="decision")
    result = await resolve_handling(route, candidates=CANDIDATES, selector=select)
    assert result["status"] == "main_model"
    assert result["owner"] == "system"
    assert result["handling_state"] == "deciding"
    assert "不构成询问用户的理由" in result["next_action"] or "不询问用户" in result["reason"]


@pytest.mark.asyncio
async def test_selector_exception_returns_to_main_model_without_user_escalation():
    async def select(_candidates, _context):
        raise RuntimeError("provider secret must not escape")

    result = await resolve_handling(derive_handling(purpose="decision"),
                                    candidates=CANDIDATES, selector=select)
    assert result["status"] == "main_model"
    assert result["selector_status"] == "failure"
    assert "provider secret" not in json.dumps(result, ensure_ascii=False)


def _decision_question(**fields):
    return AskUserParams(**{
        "purpose": "decision", "decision_issue": "import-repair",
        "questions": [{"type": "radio", "name": "repair", "message": "选择修复方案？",
                       "options": ["repair A", "repair B"]}],
        **fields,
    })


@pytest.mark.asyncio
async def test_two_rejected_jev_attempts_never_open_user_question(tmp_path):
    runtime, calls, _ = harness(tmp_path, chosen="none", suitability=0)
    first = json.loads((await before_ask(runtime.agent, _decision_question())).output)
    second = json.loads((await before_ask(runtime.agent,
        _decision_question(decision_state={"new_evidence": "两种修复都缺少输入回执"}))).output)
    third = json.loads((await before_ask(runtime.agent, _decision_question())).output)

    assert first["status"] == second["status"] == third["status"] == "main_model"
    assert first["selector_status"] == "rethink"
    assert second["selector_status"] == "escalate"
    assert third["reason_kind"] == "bounded_attempts_exhausted"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_jev_selection_is_advisory_and_human_gates_bypass_it(tmp_path):
    runtime, calls, _ = harness(tmp_path, chosen="option_1")
    decision = json.loads((await before_ask(runtime.agent, _decision_question())).output)
    assert decision["status"] == "selected"
    assert decision["suggested_option"] == "repair B"
    assert decision["source"] == "jev_advisory_not_user_answer"

    scope = _decision_question(root_cause="scope", evidence_refs=["host:scope-check"])
    assert await before_ask(runtime.agent, scope) is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_authorization_requires_existing_host_review_route(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    request = _decision_question(unmet_authority=["review-call-1"])
    routed = json.loads((await before_ask(runtime.agent, request)).output)
    assert routed["status"] == "needs_authorization"
    assert "review_call_ref" in routed["next_action"]
    assert not calls

    generic = _decision_question(purpose="authorization")
    routed = json.loads((await before_ask(runtime.agent, generic)).output)
    assert routed["status"] == "needs_authorization"
    assert not calls


@pytest.mark.asyncio
async def test_jevs_low_confidence_returns_to_main_model_not_user(tmp_path):
    runtime, calls, _ = harness(tmp_path, confidence=.2)
    result = json.loads((await before_ask(runtime.agent, _decision_question())).output)
    assert result["status"] == "main_model"
    assert result["selector_status"] == "rethink"
    assert "用户" in result["next_action"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_auto_classification_nudge_never_turns_into_automatic_user_interrupt(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    params = AskUserParams(purpose="auto", questions=[{
        "type": "text", "name": "question", "message": "缺少什么信息？"}])
    first = json.loads((await before_ask(runtime.agent, params)).output)
    second = json.loads((await before_ask(runtime.agent, params)).output)
    assert first["status"] == "classify_before_asking"
    assert second["status"] == "main_model"
    assert not calls