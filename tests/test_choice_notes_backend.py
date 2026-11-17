from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from frontend.server import _human_oncall_response_result
from mewcode.conversation import ConversationManager, Message, ToolResultBlock, ToolUseBlock
from mewcode.experts.case_runtime import answer_case
from mewcode.experts.decision_context import decision_background
from mewcode.experts.gate_dialogue import confirmation_valid, digest
from mewcode.experts.problem_dossier import build_dossier, dossier_summary
from mewcode.experts.recovery_case import apply_event, open_case
from mewcode.tools.ask_user import AskUserParams, format_ask_answers, recorded_user_answers
from mewcode.tools.human_oncall import (
    HumanOnCallParams,
    HumanOnCallTool,
    validate_answer_notes,
)


QUESTIONS = [{
    "name": "choice",
    "question": "选择处理方式",
    "options": [{"value": "draft"}, {"value": "publish"}],
    "allowOther": False,
    "required": True,
}]


def test_notes_are_bound_to_nonempty_displayed_answers() -> None:
    assert validate_answer_notes(QUESTIONS, {"choice": "draft"}, {"choice": "补充背景"}) == {
        "choice": "补充背景"
    }
    with pytest.raises(ValueError, match="not displayed"):
        validate_answer_notes(QUESTIONS, {"choice": "draft"}, {"other": "越界"})
    with pytest.raises(ValueError, match="non-empty"):
        validate_answer_notes(QUESTIONS, {"choice": ""}, {"choice": "不能单独生效"})
    with pytest.raises(ValueError, match="too long"):
        validate_answer_notes(QUESTIONS, {"choice": "draft"}, {"choice": "x" * 2001})
    with pytest.raises(ValueError, match="NUL"):
        validate_answer_notes(QUESTIONS, {"choice": "draft"}, {"choice": "坏\x00备注"})


def test_human_oncall_notes_do_not_change_canonical_choice_or_cancel_shape() -> None:
    params = HumanOnCallParams.model_validate({
        "question": "选择处理方式",
        "options": [{"id": "draft", "label": "草稿"}, {"id": "publish", "label": "发布"}],
        "allow_other": False,
    })
    tool = HumanOnCallTool()
    result = json.loads(tool.format_answers(params, {"choice": "draft"}, {"choice": "补充"}).output)
    assert result["choice"] == {"id": "draft", "label": "草稿", "is_other": False}
    assert result["notes"] == {"choice": "补充"}
    assert result["notes_role"] == "context_only"
    assert "notes" not in json.loads(tool.format_answers(params, None, {"choice": "丢弃"}).output)


@pytest.mark.asyncio
async def test_desktop_human_response_stores_notes_only_after_validation() -> None:
    future = asyncio.get_running_loop().create_future()
    request = SimpleNamespace(request_id="request-1", questions=QUESTIONS, future=future, answer_notes={})
    runtime = SimpleNamespace(human_oncall_request=request)
    accepted = _human_oncall_response_result(runtime, {
        "request_id": "request-1", "answers": {"choice": "draft"}, "notes": {"choice": "补充"},
    })
    assert accepted["ok"] is True
    assert future.result() == {"choice": "draft"}
    assert request.answer_notes == {"choice": "补充"}

    future = asyncio.get_running_loop().create_future()
    request = SimpleNamespace(request_id="request-2", questions=QUESTIONS, future=future, answer_notes={})
    invalid = _human_oncall_response_result(SimpleNamespace(human_oncall_request=request), {
        "request_id": "request-2", "answers": {"choice": "draft"}, "notes": {"other": "越界"},
    })
    assert invalid["ok"] is False and invalid["error_type"] == "invalid_notes"
    assert not future.done() and request.answer_notes == {}


def test_ask_user_notes_round_trip_keeps_legacy_answer_lines() -> None:
    params = AskUserParams.model_validate({"questions": [{
        "type": "radio", "name": "choice", "message": "选择", "options": ["draft", "publish"]
    }]})
    content = format_ask_answers(params, {"choice": "draft"}, {"choice": "补充"}).output
    conversation = ConversationManager()
    conversation.history.extend([
        Message("assistant", "", tool_uses=[ToolUseBlock("q", "AskUserQuestion", params.model_dump())]),
        Message("user", "", tool_results=[ToolResultBlock(
            "q", content, host_observation={"user_answer": {
                "request_id": "q", "questions": [q.model_dump() for q in params.questions],
                "answers": {"choice": "draft"}, "notes": {"choice": "补充"},
                "notes_role": "context_only",
            }}
        )]),
    ])
    rows = recorded_user_answers(conversation)
    assert rows[0]["answers"] == {"choice": "draft"}
    assert rows[0]["notes"] == {"choice": "补充"}
    assert rows[0]["notes_role"] == "context_only"


def test_case_note_is_context_only_and_invalid_note_is_atomic() -> None:
    record = {"node_states": {"work": {"input_revision": "rev", "status": "paused"}},
              "node_ledgers": {}, "locked_flow": {"nodes": [{"node_id": "work"}], "edges": [], "meta": {}}}
    case = open_case(record, "work")
    case = apply_event(record, case["case_id"], {"kind": "wait", "identity": "wait", "pending": {
        "kind": "elevation", "authority": [], "question": "补充材料",
        "options": [{"value": "input"}, {"value": "close"}],
    }})
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match="2000"):
        answer_case(record, case["case_id"], round=0, choice="input", answer="材料", note="x" * 2001)
    assert record == before
    answered = answer_case(record, case["case_id"], round=0, choice="input", answer="材料", note="补充背景")
    assert answered["status"] == "open"
    dossier = build_dossier(record, answered)
    assert dossier["user_inputs"][0]["context_note"] == "补充背景"
    assert dossier["user_inputs"][0]["context_note_role"] == "context_only"
    assert "补充背景" in dossier_summary(dossier)


@pytest.mark.asyncio
async def test_gate_note_is_context_only_and_does_not_change_confirmation_scope(tmp_path) -> None:
    from mewcode.experts.gate_dialogue import await_dialogue
    from tests.test_gate_decision_context import current_gate

    flow, store, run_id, session = current_gate(tmp_path)
    async def emit(event):
        if event["type"] == "human_oncall":
            session._human_oncall_request.answer_notes = {"choice": "后续只核对已选范围"}
            session._human_oncall_request.future.set_result({"choice": "continue"})

    assert await await_dialogue(node=flow.node_map["approval"], flow=flow, session=session,
        store=store, run_id=run_id, emit=emit)
    snapshot = store.load(run_id)
    assert confirmation_valid(flow.node_map["approval"], flow, snapshot)
    assert snapshot["gate_dialogues"]["approval"]["answer_notes"] == {
        "choice": "后续只核对已选范围"
    }
    assert snapshot["gate_confirmations"]["approval"]["answer_notes"]
    assert snapshot["gate_confirmations"]["approval"]["decision"] == "continue"


@pytest.mark.asyncio
async def test_gate_note_reaches_second_proposal_as_context_only(tmp_path) -> None:
    from mewcode.experts.gate_dialogue import await_dialogue
    from tests.test_gate_decision_context import current_gate

    flow, store, run_id, session = current_gate(tmp_path)
    flow.meta["authorization_grants"] = {"approval": [{
        "operation_class": "external_send",
        "scope": {"model_routes": ["test/vision"], "content_classes": ["page_image"],
                  "cost_cap_tokens": 200000},
    }]}
    builder = AsyncMock(side_effect=["首轮方案", "第二轮方案"])
    seen = []

    async def emit(event):
        if event["type"] != "human_oncall":
            return
        seen.append(event)
        assert not store.load(run_id).get("gate_confirmations")
        if len(seen) == 1:
            assert event["questions"][0]["allowOther"]
            session._human_oncall_request.answer_notes = {"choice": "SENTINEL_NOTE"}
            session._human_oncall_request.future.set_result({"choice": "调整样本范围"})
        else:
            session._human_oncall_request.future.set_result({"choice": "continue"})

    assert await await_dialogue(node=flow.node_map["approval"], flow=flow, session=session,
        store=store, run_id=run_id, emit=emit, proposal_builder=builder)
    assert len(seen) == 2 and builder.await_count == 2
    second_prompt = builder.call_args_list[1].args[0]
    assert "SENTINEL_NOTE" in second_prompt and '"answer_notes_role": "context_only"' in second_prompt

    snapshot = store.load(run_id)
    draft = snapshot["gate_dialogues"]["approval"]
    confirmation = snapshot["gate_confirmations"]["approval"]
    assert draft["answer_notes"] == {"choice": "SENTINEL_NOTE"}
    assert "SENTINEL_NOTE" not in draft["proposal"]
    assert confirmation["proposal_hash"] == digest(draft["proposal"])
    assert "SENTINEL_NOTE" not in json.dumps(snapshot.get("authorizations", {}), ensure_ascii=False)


def test_decision_background_renders_stage_notes_without_treating_them_as_upstream(tmp_path) -> None:
    from tests.test_gate_decision_context import current_gate

    flow, store, run_id, _ = current_gate(tmp_path)
    store.mutate(run_id, lambda value: value["node_states"]["approval"].update(
        input_notes={"request-1": {"notes": {"cost": "只用现有额度"}, "notes_role": "context_only"}}))
    rendered = decision_background(flow.node_map["approval"], flow, store.load(run_id))
    assert "只用现有额度" in rendered
