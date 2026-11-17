"""Model-authored questions default to choices, with input only where needed."""
import copy
import json
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from frontend import interaction_choices as api
from mewcode.orchestration.interaction_choices import prepare_choices, project_questions, resolve_answers
from tests.test_interaction_choices_api import FakeSrv, _app


def questions():
    return [{"name": "external_processing_authorization", "label": "试点服务与费用授权", "type": "text",
             "description": "与 external_service_authorizations 引用同一授权记录。", "required": True},
            {"name": "route", "label": "处理方式", "type": "text", "required": True}]


def model_questions():
    return [{"name": "external_processing_authorization", "question": "是否仅对各书的 1–3 页开展试点？",
             "description": "选择试点范围，全量处理另行确认。", "input_mode": "choice", "choices": [
                 {"id": "pilot", "label": "仅做 1–3 页试点", "value": "仅限各书的1–3页试点；全量另行确认",
                  "description": "按已配置服务处理，执行前核对页码和费用。", "recommended": True},
                 {"id": "local", "label": "暂不使用外部服务", "value": "仅使用本地处理；不允许外发",
                  "description": "保留当前进度，先核对本地处理能力。"}]},
            {"name": "route", "question": "试点先使用哪种处理方式？", "input_mode": "choice", "choices": [
                 {"id": "local-route", "label": "先使用本地文本", "value": "本地文本"},
                 {"id": "configured-route", "label": "使用已配置识别服务", "value": "已配置服务"}]}]


@pytest.mark.asyncio
async def test_model_authors_question_and_options_without_changing_answer_contract(tmp_path):
    original = questions()
    saved = copy.deepcopy(original)
    calls = []

    async def complete(system, prompt):
        calls.append(system)
        return json.dumps({"searches": []} if len(calls) == 1 else {"questions": model_questions()}, ensure_ascii=False)

    bundle = await prepare_choices(identity={"run_id": "run"}, questions=original, context={},
                                   complete=complete, allowed_roots=[str(tmp_path)])
    assert bundle["status"] == "ready"
    projected = project_questions(original, bundle)
    assert projected[0]["message"] == model_questions()[0]["question"]
    assert projected[0]["description"] == model_questions()[0]["description"]
    assert projected[0]["input_mode"] == "choice" and projected[0]["default"] is None
    assert "优先使用input_mode=choice" in calls[1]
    assert original == saved
    resolved = resolve_answers(bundle, {"external_processing_authorization": "pilot"},
                               bundle_id=bundle["id"], version=bundle["version"])
    assert resolved["values"] == {"external_processing_authorization": model_questions()[0]["choices"][0]["value"]}
    assert not resolved["actions"]


@pytest.mark.asyncio
async def test_text_exception_keeps_custom_value_and_closed_enum_stays_choice(tmp_path):
    source = [{"name": "output", "type": "text"},
              {"name": "scope", "type": "radio", "allow_other": False,
               "options": [{"value": "pilot", "label": "试点"}]}]
    count = 0

    async def complete(system, prompt):
        nonlocal count
        count += 1
        if count == 1:
            return '{"searches":[]}'
        return json.dumps({"questions": [
            {"name": "output", "question": "请提供结果保存位置", "description": "目前没有已知保存路径。",
             "input_mode": "text", "choices": []},
            {"name": "scope", "input_mode": "text", "choices": []}]})

    bundle = await prepare_choices(identity={}, questions=source, context={}, complete=complete,
                                   allowed_roots=[str(tmp_path)])
    projected = project_questions(source, bundle)
    assert [row["input_mode"] for row in projected] == ["text", "choice"]
    assert resolve_answers(bundle, {"output": {"custom": "defer"}})["values"] == {"output": "defer"}
    with pytest.raises(ValueError, match="原参数合同"):
        resolve_answers(bundle, {"scope": {"custom": "full"}})


@pytest.mark.asyncio
async def test_host_prepares_user_owned_input_and_persists_question_metadata(monkeypatch, tmp_path):
    case = {"case_id": "case", "status": "awaiting_user", "epoch": 1, "input_revision": "r1", "rounds": [],
            "node_id": "work", "pending": {"kind": "input", "input_definitions": questions()},
            "handling": {"owner": "user", "handling_state": "needs_user"}}
    record = {"run_id": "run", "conversation_id": "chat", "node_states": {
        "work": {"status": "paused", "epoch": 1, "input_revision": "r1", "recovery_case": case}}}
    srv = FakeSrv(tmp_path, runs={"run": record})
    calls = []

    async def complete(_client, *, system, user_text, **kwargs):
        calls.append(system)
        return json.dumps({"searches": []} if len(calls) == 1 else {"questions": model_questions()})

    monkeypatch.setattr(api, "collect_model_text", complete)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(srv)), base_url="http://test") as client:
        response = await client.post(api._ROUTE, json={"conversation_id": "chat", "run_id": "run", "node_id": "work",
                                                      "request_id": "case", "case_id": "case"})
    assert response.status_code == 200, response.text
    pending = srv.EXPERT_RUNS.load("run")["node_states"]["work"]["recovery_case"]["pending"]
    assert pending["questions"][0]["message"] == model_questions()[0]["question"]
    assert pending["choice_bundle"]["questions"][0]["input_mode"] == "choice"
    assert not srv.EXPERT_RUNS.load("run").get("run_inputs")
    assert not srv.EXPERT_RUNS.load("run").get("authorizations")
