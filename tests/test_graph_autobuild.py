"""Bounded construction, coverage repair, and durable-stage resumption."""
import copy
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from frontend import orch_drafts, server
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_completion import protected_view
from mewcode.orchestration.graph_generation import delivery_view
from mewcode.orchestration.workflow_draft import apply_patch, project_graph
from tests.graph_autobuild_fixtures import BuildModel, DEFAULT_SOURCE, RUNTIME_SOURCE
from tests.test_workflow_drafts import api_env, cited, record, write_history

from mewcode.orchestration.graph_autobuild import (
    CALL_LIMIT_REASON, MAX_MODEL_CALLS, MAX_REPAIR_ROUNDS, TIME_LIMIT_REASON,
    new_build_state, run_autobuild,
)


def entry(entry_id, kind="node", data=None):
    return {"id": entry_id, "kind": kind, "title": entry_id, "body": entry_id,
            "state": "active", "data": data or {}, "sources": [], "evidence": "manual"}


def node(entry_id, node_kind):
    return entry(entry_id, "node", {"node_kind": node_kind, "outputs": ["产物"],
                                     "acceptance": ["验收"]})


def edge(entry_id, start, end):
    return entry(entry_id, "edge", {"from_node": start, "to_node": end,
                                     "condition": "always"})


def graph(*, ready=False):
    entries = [node("plan", "plan"), node("audit", "audit"), node("finish", "reduce"),
               edge("first", "plan", "audit")]
    if ready:
        entries.append(edge("last", "audit", "finish"))
    return {"schema_version": 1, "entries": entries, "ignored": []}


def proposal(*upserts):
    return json.dumps({"upserts": list(upserts), "notes": ""}, ensure_ascii=False)


def finish_edge():
    return {"id": "last", "kind": "edge", "title": "进入终验", "body": "复核后终验",
            "data": {"from_node": "audit", "to_node": "finish", "condition": "always"},
            "rationale": "连接终验"}


def pdf_gap():
    return {"source_id": "u", "quote": "最后导出 PDF", "summary": "缺少 PDF 导出节点",
            "suggested_fix": "加导出节点"}


def pdf_proposal():
    pdf = {"id": "pdf", "kind": "node", "title": "导出 PDF", "body": "导出文件",
           "data": {"node_kind": "implement", "outputs": ["PDF"], "acceptance": ["可打开"]},
           "rationale": "补齐用户要求"}
    incoming = {"id": "to_pdf", "kind": "edge", "title": "进入导出", "body": "依赖",
                "data": {"from_node": "audit", "to_node": "pdf", "condition": "always"},
                "rationale": "连接导出"}
    outgoing = {"id": "from_pdf", "kind": "edge", "title": "导出后终验", "body": "依赖",
                "data": {"from_node": "pdf_review", "to_node": "finish", "condition": "always"},
                "rationale": "连接终验"}
    review = {"id": "pdf_review", "kind": "node", "title": "核对 PDF", "body": "验证导出结果",
              "data": {"node_kind": "audit", "outputs": ["核验结论"], "acceptance": ["PDF 可打开"]},
              "rationale": "新增导出产物也需独立核验"}
    review_input = {"id": "to_pdf_review", "kind": "edge", "title": "导出后核验", "body": "依赖",
                    "data": {"from_node": "pdf", "to_node": "pdf_review", "condition": "always"},
                    "rationale": "核对实际导出产物"}
    return proposal(pdf, incoming, review, review_input, outgoing)


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, system, user):
        self.calls.append((system, user))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


async def run(document, model, reviewer, **kwargs):
    return await run_autobuild(document, {"finalizer_node_id": "finish"},
                               [{"id": "u", "role": "user", "text": "最后导出 PDF"}],
                               conversation_id="c", revision=1,
                               ask_model=model, ask_review=reviewer, **kwargs)


@pytest.mark.asyncio
async def test_build_reaches_ready_with_independent_review_and_two_calls():
    document = graph()
    original = copy.deepcopy(document)
    model = FakeModel(proposal(finish_edge()))
    reviewer = FakeModel('{"gaps":[],"notes":"覆盖完整"}')
    result = await run(document, model, reviewer)
    assert result["build"]["status"] == "ready"
    assert result["build"]["phase"] == "done" and result["build"]["remaining"] == []
    assert result["build"]["model_calls"] == len(model.calls) + len(reviewer.calls) == 2
    assert result["build"]["review"]["notes"] == "覆盖完整"
    assert document == original and model is not reviewer


def deferred_capability_graph():
    document = graph(ready=True)
    owner = node("prepare_runtime", "implement")
    owner["data"]["steps"] = ["验证运行前能力，未通过不继续"]
    document["entries"][3]["data"]["to_node"] = "prepare_runtime"
    document["entries"].extend([owner, edge("runtime_to_review", "prepare_runtime", "audit")])
    question = entry("service_probe", "question", {
        "issue_type": "capability", "blocking": False, "node_id": "prepare_runtime"})
    question.update(evidence="ai_completed", sources=[{"source_id": "u", "quote": "最后导出 PDF"}])
    document["entries"].append(question)
    return document, question, owner


@pytest.mark.asyncio
async def test_plan_only_preserves_unresolved_stage_owned_capability_without_claiming_it_resolved():
    from mewcode.orchestration.run_inputs import resolved_question_ids
    document, question, _ = deferred_capability_graph()
    original = copy.deepcopy(document)
    initial = new_build_state(document, now=0)
    initial["phase"] = "review"
    reviewer = FakeModel('{"gaps":[]}')
    result = await run(document, FakeModel(), reviewer, state=initial, clock=lambda: 0, plan_only=True)
    assert result["build"]["status"] == "ready"
    assert result["build"]["deferred_capability_question_ids"] == ["service_probe"]
    assert result["document"] == document == original
    assert question["state"] == "active" and "service_probe" not in resolved_question_ids(result["document"])
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plan", "execute", "default"])
async def test_completed_plan_rechecks_pending_capability_under_current_intent(mode):
    document, _, _ = deferred_capability_graph()
    initial = new_build_state(document, now=0)
    initial["phase"] = "review"
    ready = await run(document, FakeModel(), FakeModel('{"gaps":[]}'),
                      state=initial, clock=lambda: 0, plan_only=True)
    assert ready["build"]["status"] == "ready"
    before = copy.deepcopy(ready)
    model, review = FakeModel(), FakeModel()
    kwargs = {} if mode == "default" else {"plan_only": mode == "plan"}
    result = await run(ready["document"], model, review, state=ready["build"], clock=lambda: 0, **kwargs)
    assert ready == before and result["document"] == document
    assert not model.calls and not review.calls
    assert result["build"]["model_calls"] == ready["build"]["model_calls"]
    assert result["build"]["status"] == ("ready" if mode == "plan" else "needs_review")
    assert result["build"]["deferred_capability_question_ids"] == (["service_probe"] if mode == "plan" else [])
    if mode != "plan":
        assert result["build"]["reason"] == "pending_questions"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["execute", "truthy_plan", "blocking", "technical", "business",
    "input", "user_source", "reviewed", "options", "missing_owner", "nonimplement_owner",
    "missing_steps", "missing_acceptance", "missing_sources"])
async def test_plan_capability_exception_never_drops_other_pending_questions(case):
    document, question, owner = deferred_capability_graph()
    plan_only = False if case == "execute" else "true" if case == "truthy_plan" else True
    if case == "blocking":
        question["data"]["blocking"] = True
    elif case in {"technical", "business", "input"}:
        question["data"]["issue_type"] = case
    elif case == "user_source":
        question["evidence"] = "user_source"
    elif case == "reviewed":
        question["reviewed"] = True
    elif case == "options":
        question["data"]["options"] = [{"value": "paid", "label": "付费服务"}]
    elif case == "missing_owner":
        question["data"]["node_id"] = "absent"
    elif case == "nonimplement_owner":
        owner["data"]["node_kind"] = "plan"
    elif case == "missing_steps":
        owner["data"].pop("steps")
    elif case == "missing_acceptance":
        owner["data"].pop("acceptance")
    elif case == "missing_sources":
        question["sources"] = []
    original = copy.deepcopy(document)
    initial = new_build_state(document, now=0)
    initial["phase"] = "review"
    result = await run(document, FakeModel(), FakeModel('{"gaps":[]}'),
                       state=initial, clock=lambda: 0, plan_only=plan_only)
    assert result["build"]["status"] != "ready"
    assert result["build"]["deferred_capability_question_ids"] == []
    assert result["document"] == document == original


@pytest.mark.asyncio
async def test_coverage_gap_gets_exactly_one_no_regression_completion_round():
    model = FakeModel(proposal(), pdf_proposal())
    reviewer = FakeModel(
        json.dumps({"gaps": [pdf_gap()], "notes": "缺口"}, ensure_ascii=False),
        '{"gaps":[],"notes":"已补齐"}',
    )
    result = await run(graph(ready=True), model, reviewer)
    build = result["build"]
    assert build["status"] == "ready" and build["model_calls"] == 4
    assert len(build["gap_fill"]["rounds"]) == 1
    assert build["gap_fill"]["rounds"][0]["structural"]["reason"] == "no_regression"
    assert len(model.calls) == 2 and len(reviewer.calls) == 2
    assert json.loads(model.calls[1][1])["diagnostics"][-1]["code"] == "coverage_gap"
    assert any(e["id"] == "pdf" for e in result["document"]["entries"])


@pytest.mark.asyncio
async def test_unresolved_gap_is_delivered_once_as_needs_review():
    model = FakeModel(proposal(), proposal())
    gap = json.dumps({"gaps": [pdf_gap()], "notes": ""}, ensure_ascii=False)
    reviewer = FakeModel(gap, gap)
    result = await run(graph(ready=True), model, reviewer)
    assert result["build"]["status"] == "needs_review"
    assert result["build"]["reason"] == "coverage_gaps_remaining"
    assert result["build"]["pending_gaps"] == [pdf_gap()]
    assert len(result["build"]["gap_fill"]["rounds"]) == 1
    assert result["build"]["model_calls"] <= MAX_MODEL_CALLS


@pytest.mark.asyncio
async def test_fresh_review_gap_does_not_reuse_previous_completed_gap_round():
    document = graph(ready=True)
    initial = new_build_state(document, now=0)
    initial.update(phase="review", model_calls=1, call_attempts=1)
    old_round = {"phase":"completion", "diagnostics":[{"code":"coverage_gap", "message":"old gap"}],
                 "structural":{"reason":"no_regression"}}
    initial["gap_fill"] = {"rounds":[old_round], "model_calls":1}
    model = FakeModel(pdf_proposal())
    reviewer = FakeModel(json.dumps({"gaps":[pdf_gap()]}), '{"gaps":[]}')
    result = await run(document, model, reviewer, state=initial, clock=lambda:0)
    build = result["build"]
    assert build["status"] == "ready" and len(model.calls) == 1
    assert json.loads(model.calls[0][1])["diagnostics"][-1]["code"] == "coverage_gap"
    assert any(e["id"] == "pdf" for e in result["document"]["entries"])
    assert build["gap_fill_history"] == [initial["gap_fill"]]
    assert build["retired_gap_fill_calls"] == 1
    assert build["call_attempts"] == build["model_calls"] == 4


@pytest.mark.asyncio
async def test_retiring_old_gap_round_keeps_the_reserved_call_limit():
    document = graph(ready=True)
    initial = new_build_state(document, now=0)
    initial.update(phase="review", call_attempts=MAX_MODEL_CALLS-1)
    initial["gap_fill"] = {"rounds":[{"phase":"completion"}], "model_calls":1}
    model = FakeModel(pdf_proposal())
    reviewer = FakeModel(json.dumps({"gaps":[pdf_gap()]}))
    result = await run(document, model, reviewer, state=initial, clock=lambda:0)
    assert result["build"]["reason"] == CALL_LIMIT_REASON
    assert result["build"]["call_attempts"] == MAX_MODEL_CALLS
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("at_limit", [False, True])
async def test_resume_manual_edit_rechecks_before_using_stale_gaps(at_limit):
    doc = graph(ready=True)
    initial = new_build_state(doc, now=0)
    old_review = {"gaps": [pdf_gap()], "model_calls": 1, "error": ""}
    initial.update(phase="gap_fill", review=old_review, review_stale=True,
                   manual_edit_pending_review=True,
                   pending_gaps=[pdf_gap()], model_calls=2,
                   call_attempts=MAX_MODEL_CALLS if at_limit else 3)
    initial["autofix"] = {"rounds": [{"phase": "completion"}], "model_calls": 1}
    before = copy.deepcopy(initial)
    model, reviewer = FakeModel(), FakeModel('{"gaps":[],"notes":"edited graph covered"}')
    result = await run(doc, model, reviewer, state=initial, clock=lambda: 0)
    build = result["build"]
    assert model.calls == []  # No automatic repair based on the old gap.
    assert build["manual_edit_review_history"] == [{"review": old_review}]
    assert build["retired_review_calls"] == 1
    assert build["started_at"] == initial["started_at"]
    assert build["deadline_at"] == initial["deadline_at"]
    assert initial == before and result["document"] == doc
    if at_limit:
        assert reviewer.calls == [] and build["reason"] == CALL_LIMIT_REASON
        assert build["call_attempts"] == MAX_MODEL_CALLS and build["review_stale"] is True
    else:
        assert len(reviewer.calls) == 1 and build["status"] == "ready"
        assert build["call_attempts"] == 4 and build["model_calls"] == 3
        assert build["review_stale"] is False and build["pending_gaps"] == []


def test_acceptance_preserves_retired_gap_receipts_and_cost(tmp_path):
    store = DraftStore(tmp_path, "chat")
    candidate = store.begin(None, graph_first=True)
    document = graph(ready=True)
    history = [{"rounds":[{"phase":"completion", "diagnostics":[{"message":"old gap"}]}], "model_calls":1}]
    build = new_build_state(document)
    build.update(phase="done", status="ready", model_calls=4, call_attempts=4,
                 retired_gap_fill_calls=1, gap_fill_history=history)
    candidate = store.checkpoint(candidate, document, [], "ready", autobuild=build)
    saved = store.confirm(candidate["id"], candidate["sequence"])
    receipt = saved["autobuild_result"]
    assert receipt["retired_gap_fill_calls"] == 1 and receipt["gap_fill_history"] == history
    assert receipt["call_attempts"] == receipt["model_calls"] == 4
    with store.db() as db:
        version = json.loads(db.execute("SELECT data FROM version WHERE conversation=? AND name=? AND revision=?",
                                       (*store.scope,saved["revision"])).fetchone()[0])
    assert version["autobuild_result"] == receipt


@pytest.mark.asyncio
async def test_nonconverging_structure_stops_after_three_repairs_without_review():
    model = FakeModel(*(proposal() for _ in range(5)))
    reviewer = FakeModel('{"gaps":[]}')
    result = await run(graph(), model, reviewer)
    assert result["build"]["status"] == "needs_review"
    assert result["build"]["reason"] == "rounds_exhausted"
    assert len(result["build"]["autofix"]["rounds"]) == 1 + MAX_REPAIR_ROUNDS
    assert result["build"]["model_calls"] == len(model.calls) == 4
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_review_error_does_not_become_successful_zero_gaps():
    reviewer = FakeModel(RuntimeError("secret credential"))
    result = await run(graph(ready=True), FakeModel(proposal()), reviewer)
    assert result["build"]["status"] == "needs_review"
    assert result["build"]["reason"] == "review_error"
    assert result["build"]["review"]["error"] == "RuntimeError"
    assert "secret credential" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,reason", [(RuntimeError("private"), "model_error"), ("bad JSON", "parse_failed")])
async def test_failed_classification_on_a_structurally_ready_graph_is_not_success(response, reason):
    reviewer = FakeModel('{"gaps":[]}')
    result = await run(graph(ready=True), FakeModel(response), reviewer)
    assert result["build"]["status"] == "needs_review"
    assert result["build"]["reason"] == reason
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_explicit_goal_blocker_is_delivered_and_survives_projection():
    from mewcode.orchestration.extract import collect_open_questions
    from mewcode.orchestration.workflow_draft import project_graph
    document = graph(ready=True)
    document["entries"].append(entry("goal_question", "question", {"blocking": True, "issue_type": "business"}))
    result = await run(document, FakeModel(proposal()), FakeModel('{"gaps":[]}'))
    assert result["build"]["status"] == "waiting_user" and result["build"]["reason"] == "business_decision"
    spec = project_graph(result["document"], {"finalizer_node_id": "finish"}, conversation_id="c", revision=1)
    assert collect_open_questions(spec) == [{"node_id": "goal", "label": "goal_question", "question": "goal_question"}]


@pytest.mark.asyncio
async def test_expired_deadline_finishes_without_model_calls():
    document = graph()
    state = new_build_state(document, now=0)
    model, reviewer = FakeModel(), FakeModel()
    result = await run(document, model, reviewer, state=state, clock=lambda: 1801)
    assert result["build"]["reason"] == TIME_LIMIT_REASON
    assert result["build"]["status"] == "needs_review"
    assert model.calls == reviewer.calls == []


class Crash(Exception):
    pass


@pytest.mark.asyncio
async def test_resume_after_every_durable_stage_matches_uninterrupted_result():
    document = graph(ready=True)
    initial = new_build_state(document, now=0)
    gap = json.dumps({"gaps": [pdf_gap()], "notes": "缺口"}, ensure_ascii=False)

    def models():
        return FakeModel(proposal(), pdf_proposal()), FakeModel(gap, '{"gaps":[],"notes":""}')

    model, reviewer = models()
    whole = await run(document, model, reviewer, state=initial, clock=lambda: 0)
    assert whole["build"]["status"] == "ready"

    for stop_after in range(1, 11):
        saved = {}
        calls = 0

        def checkpoint(build, current):
            nonlocal calls
            calls += 1
            saved.update(build=copy.deepcopy(build), document=copy.deepcopy(current))
            if calls == stop_after:
                raise Crash()

        model, reviewer = models()
        with pytest.raises(Crash):
            await run(document, model, reviewer, state=initial,
                      on_checkpoint=checkpoint, clock=lambda: 0)
        used_model, used_review = len(model.calls), len(reviewer.calls)
        original_saved = copy.deepcopy(saved)
        resumed = await run(saved["document"],
                            FakeModel(*model.responses), FakeModel(*reviewer.responses),
                            state=saved["build"], clock=lambda: 0)
        # A crash before dispatch conservatively retains its reserved slot;
        # completed work and model outputs must still be identical.
        resumed_attempts = resumed["build"].pop("call_attempts")
        expected = copy.deepcopy(whole)
        expected["build"].pop("call_attempts")
        assert resumed == expected
        assert whole["build"]["call_attempts"] <= resumed_attempts <= MAX_MODEL_CALLS
        assert saved == original_saved
        assert resumed["build"]["model_calls"] == used_model + used_review + (
            whole["build"]["model_calls"] - used_model - used_review)


@pytest.mark.asyncio
async def test_checkpoint_failure_propagates_and_inputs_are_unchanged():
    document = graph()
    state = new_build_state(document, now=0)
    original = copy.deepcopy((document, state))

    def fail(*_):
        raise RuntimeError("disk unavailable")

    with pytest.raises(RuntimeError, match="disk unavailable"):
        await run(document, FakeModel(proposal(finish_edge())),
                  FakeModel('{"gaps":[]}'), state=state, on_checkpoint=fail,
                  clock=lambda: 0)
    assert (document, state) == original


@pytest.mark.asyncio
async def test_interrupted_requests_keep_their_budget_slots_across_restart():
    document = graph()
    saved = {"document": document, "build": new_build_state(document, now=0)}
    calls = 0

    async def cancel(*_):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    def checkpoint(build, current):
        saved.update(build=build, document=current)

    for attempt in range(MAX_MODEL_CALLS):
        with pytest.raises(asyncio.CancelledError):
            await run(saved["document"], cancel, FakeModel(), state=saved["build"],
                      on_checkpoint=checkpoint, clock=lambda: 0)
        assert saved["build"]["call_attempts"] == attempt + 1
    final = await run(saved["document"], cancel, FakeModel(), state=saved["build"],
                      on_checkpoint=checkpoint, clock=lambda: 0)
    assert calls == MAX_MODEL_CALLS
    assert final["build"]["status"] == "needs_review"
    assert final["build"]["reason"] == CALL_LIMIT_REASON
    assert final["build"]["phase"] == "done"


API = "/api/orchestration/graph-autobuild"


def make_app():
    app = FastAPI()
    orch_drafts.install(app, server)
    app.add_api_route("/api/orchestration/open-canvas", server.api_orchestration_open_canvas, methods=["POST"])
    return app


async def poll_auto(client, owner):
    for _ in range(500):
        response = await client.get(API, params=owner)
        assert response.status_code == 200, response.text
        if not response.json()["running"]:
            return response.json()
        await asyncio.sleep(.01)
    pytest.fail("autobuild did not settle")


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["plan", "execute", None])
async def test_http_completed_plan_rechecks_capability_before_reusing_ready(api_env, monkeypatch, intent):
    from mewcode.orchestration.task_design import TaskDesign
    _, _, path, owner = api_env
    write_history(path, [record("user", DEFAULT_SOURCE)])
    model = BuildModel("defaults")
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        assert (await client.post(API + "/update", json=owner)).status_code == 202
        await poll_auto(client, owner)
        store = DraftStore(owner["project"], owner["conversation_id"])
        candidate = store.get()["candidate"]
        document = copy.deepcopy(candidate["document"])
        preparation = next(row for row in document["entries"] if row["id"] == "research")
        preparation["data"]["steps"] = ["运行前核验服务能力，未通过不继续"]
        question = entry("service_probe", "question", {
            "issue_type": "capability", "blocking": False, "node_id": "research"})
        question.update(evidence="ai_completed", sources=copy.deepcopy(preparation["sources"]))
        document["entries"].append(question)
        initial = new_build_state(document)
        initial["phase"] = "review"
        planned = await run_autobuild(document, candidate.get("base_snapshot"), store.sources(),
            conversation_id=owner["conversation_id"], revision=candidate["base_revision"] + 1,
            ask_model=FakeModel(), ask_review=FakeModel('{"gaps":[]}'), state=initial, plan_only=True)
        assert planned["build"]["status"] == "ready"
        store.checkpoint(candidate, document, candidate["processed_ids"], "ready", autobuild=planned["build"])
        if intent:
            TaskDesign(owner["project"], owner["conversation_id"]).start(intent)
        calls = len(model.calls)
        response = await client.post(API + "/update", json=owner)
        assert response.status_code == 200, response.text
        assert response.json()["already_included"]
        build = store.get()["candidate"]["autobuild"]
        assert build["status"] == ("ready" if intent == "plan" else "needs_review")
        assert build["deferred_capability_question_ids"] == (["service_probe"] if intent == "plan" else [])
        assert len(model.calls) == calls
        assert store.get()["candidate"]["document"] == document


@pytest.mark.asyncio
async def test_auto_build_reads_builtin_experience_in_all_model_phases_without_extra_calls(api_env, monkeypatch):
    from mewcode.orchestration_guide import orchestration_build_experience

    _, _, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    experience = orchestration_build_experience()
    seen = []
    async def inspect_request(phase, kwargs):
        assert experience in kwargs["system"], f"{phase} did not receive the packaged skill experience"
        payload = json.loads(kwargs["user_text"])
        # Shared guidance must not become a business source or a user requirement.
        for source in payload.get("sources", []) + payload.get("requirements", []):
            assert experience not in source.get("text", "")
        seen.append(phase)
    model = BuildModel(on_call=inspect_request)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        response = await client.post(API + "/update", json=owner)
        assert response.status_code == 202, response.text
        result = await poll_auto(client, owner)
    assert seen == ["extract", "complete", "review"]
    assert len(model.calls) == 3 and result["delivery"]["call_attempts"] == 3
    assert result["delivery"]["status"] == "ready"
    assert DraftStore.read_state(owner["project"], owner["conversation_id"])["revision"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["runtime", "defaults"])
@pytest.mark.parametrize("configured_review", [False, True])
async def test_api_one_click_delivery_save_and_repeat_without_interaction(api_env, monkeypatch, kind, configured_review):
    _, plans, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE if kind == "runtime" else DEFAULT_SOURCE)])
    clients, resolved = [], []

    def new_client(context, effort):
        assert (context["model"], effort) == ("chosen", "high")
        client = SimpleNamespace(route="chosen", effort=effort)
        clients.append(client)
        return client

    review_client = SimpleNamespace(route="configured-review")

    def resolve(ref):
        resolved.append(ref)
        return SimpleNamespace(client=review_client)

    monkeypatch.setattr(server, "_new_orchestration_client", new_client)
    monkeypatch.setattr(server, "_load_review_model", lambda: {"provider_key": "audit-provider", "model_id": "audit-model"} if configured_review else {})
    monkeypatch.setattr(server.MODEL_REGISTRY, "resolve", resolve)
    model = BuildModel(kind)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        response = await client.post(API + "/update", json=owner)
        assert response.status_code == 202, response.text
        state = await poll_auto(client, owner)
        assert state["can_preview"], state
        assert state["delivery"]["status"] == "ready"
        assert state["delivery"]["phase"] == "done"
        assert len(state["view"]["nodes"]) == 5
        assert state["delivery"]["pending"] == []
        assert [call["phase"] for call in model.calls] == ["extract", "complete", "review"]
        assert state["delivery"]["model_calls"] == state["delivery"]["call_attempts"] == 3
        assert clients[0] is not clients[1], "completion is separate from extraction"
        review_call = model.calls[-1]
        if configured_review:
            assert review_call["client"] is review_client
            assert [(ref.provider_key, ref.model_id) for ref in resolved] == [("audit-provider", "audit-model")]
        else:
            assert review_call["client"].route == "chosen"
            assert review_call["client"] is not model.calls[1]["client"]
            assert resolved == []
        assert plans.get(owner["project"]) is None, "delivery must not save or start a task"
        protected = next(e for e in state["elements"] if e["id"] == "write")
        assert protected["data"]["acceptance"] == ["不得丢失任一原始要求"]
        assert protected["evidence"] == "user_source" and protected["sources"]
        attempt = state["delivery"]["attempts"][0]
        assert attempt["diagnostics"] and attempt["proposal"]["upserts"]
        assert any(row["entry_id"] == "write" and row["code"] == "locked_content" for row in attempt["structural"]["rejected"])
        assert all(item["evidence"] == "ai_completed" for item in state["delivery"]["assumptions"])
        if kind == "runtime":
            assert [item["name"] for item in state["delivery"]["run_inputs"]] == ["主题", "图数", "读者"]
        else:
            assert state["delivery"]["run_inputs"] == []
            assert state["delivery"]["assumptions"] == []
            assert any(row["code"] == "structure_not_adopted" for row in attempt["ledger"]["rejected"])
        # Repeated input consumes no further calls and does not grow the ledger.
        for _ in range(3):
            repeated = await client.post(API + "/update", json=owner)
            assert repeated.status_code == 200 and repeated.json()["already_included"]
            assert repeated.json()["delivery"]["pending"] == []
        assert len(model.calls) == 3
        source_id = protected["sources"][0]["source_id"]
        original = await client.get(API + "/source/" + source_id, params=owner)
        assert original.status_code == 200 and "不得丢失任一原始要求" in original.json()["text"]
        preview = await client.post(API + "/graph", json={**owner, "action": "preview"})
        assert preview.status_code == 200, preview.text
        saved = await client.post(API + "/graph", json={**owner, "action": "apply", "preview_id": preview.json()["id"]})
        assert saved.status_code == 200, saved.text
        assert DraftStore(owner["project"], "paper").get()["revision"] == 1
        assert server._conversation_plan_store("paper").get(owner["project"]).status == "drafting"
        reopened = (await client.get(API, params=owner)).json()
        assert reopened["delivery"]["review"] == state["delivery"]["review"]
        assert reopened["delivery"]["attempts"] == state["delivery"]["attempts"]
        assert reopened["delivery"]["assumptions"] == state["delivery"]["assumptions"]
        assert reopened["delivery"]["run_inputs"] == state["delivery"]["run_inputs"]
        assert len(model.calls) == 3
        # A user's saved layout and binding remain authoritative on resave.
        manual = copy.deepcopy(saved.json()["spec"])
        writer = next(n for n in manual["nodes"] if n["node_id"] == "write")
        writer.update(position={"x": 123, "y": 456},
                      model={"provider_key": "bound", "model_id": "chosen", "effort": "high"})
        changed = await client.post("/api/orchestration/open-canvas", json={
            **owner, "spec": manual, "expected_version": manual["meta"]["pipeline_graph_version"]})
        assert changed.status_code == 200, changed.text
        stale = (await client.get(API, params=owner)).json()
        assert stale["delivery"]["review_stale"] and stale["delivery"]["status"] == "needs_review"
        resave_preview = await client.post(API + "/graph", json=owner)
        assert resave_preview.status_code == 200, resave_preview.text
        resaved = await client.post(API + "/graph", json={**owner, "action": "apply",
                                                          "preview_id": resave_preview.json()["id"]})
        assert resaved.status_code == 200, resaved.text
        final_writer = next(n for n in resaved.json()["spec"]["nodes"] if n["node_id"] == "write")
        assert final_writer["position"] == writer["position"] and final_writer["model"] == writer["model"]
        assert resaved.json()["spec"]["meta"]["pipeline_graph_version"] > manual["meta"]["pipeline_graph_version"]
        assert len(model.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["disconnected", "cycle"])
async def test_api_nonconverging_graph_is_delivered_editable_but_cannot_be_saved(api_env, monkeypatch, damage):
    _, _, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    model = BuildModel(nonconverging=True)
    async def damaged(client, **kwargs):
        raw = await model(client, **kwargs)
        inputs = json.loads(kwargs["user_text"])
        if damage == "cycle" and "sources" in inputs:
            patch = json.loads(raw)
            for edge_id, start, end in (("connect", "research", "write"), ("back", "write", "research")):
                patch["upserts"].append(cited(edge(edge_id, start, end), inputs["sources"][0]))
            raw = json.dumps(patch, ensure_ascii=False)
        return raw
    monkeypatch.setattr(orch_drafts, "collect_model_text", damaged)
    monkeypatch.setattr(server, "_load_review_model", lambda: {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        state = await poll_auto(client, owner)
        assert state["delivery"]["status"] == "needs_review" and state["candidate"]["status"] == "ready"
        assert len(state["delivery"]["attempts"]) == 1
        assert state["delivery"]["model_calls"] == 3
        assert state["delivery"]["reason"] == "model_stopped"
        assert [call["phase"] for call in model.calls] == ["extract", "complete", "control"]
        assert state["readiness"]["ready"] is False and state["delivery"]["pending"]
        assert not state["can_preview"] and state["view"]["nodes"]
        result = await client.post(API + "/graph", json=owner)
        assert result.status_code == 409
        if damage == "cycle":
            with pytest.raises(ValueError) as original_error:
                project_graph({"entries": state["elements"], "ignored": []}, None,
                              conversation_id=owner["conversation_id"], revision=1)
            assert result.json()["error"] == str(original_error.value)
        else:
            assert "结构尚未就绪" in result.json()["error"]
        candidate = state["candidate"]
        changed = await client.post(API + "/action", json={**owner, "action": "edit", "candidate_id": candidate["id"],
            "sequence": candidate["sequence"], "entry_id": "research", "changes": {"body": "用户可继续善后"}})
        assert changed.status_code == 200, changed.text
        assert len(model.calls) == 3


@pytest.mark.asyncio
async def test_api_stop_review_restart_resumes_from_durable_phase(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    entered, release = asyncio.Event(), asyncio.Event()

    async def block_review(phase, _):
        if phase == "review":
            entered.set()
            await release.wait()

    model = BuildModel(on_call=block_review)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    monkeypatch.setattr(server, "_load_review_model", lambda: {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        await asyncio.wait_for(entered.wait(), timeout=5)
        progress = (await client.get(API + "/progress", params=owner)).json()
        assert progress["progress"]["phase"] == "review"
        stopped = await client.post(API + "/action", json={**owner, "action": "stop"})
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["delivery"]["status"] == "paused"
        assert stopped.json()["candidate"]["status"] == "paused"
        snapshot = DraftStore(owner["project"], "paper").get()["candidate"]
        assert (await client.post("/api/orchestration/graph-generation/update", json=owner)).status_code == 409
        assert DraftStore(owner["project"], "paper").get()["candidate"] == snapshot, "rejected legacy resume must not change the paused state"
        c = stopped.json()["candidate"]
        edited = await client.post(API + "/action", json={**owner, "action": "edit", "candidate_id": c["id"], "sequence": c["sequence"],
            "entry_id": "write", "changes": {"body": "用户修改后的写作职责"}})
        assert edited.status_code == 200, edited.text
    store = DraftStore(owner["project"], "paper")
    before = store.get()["candidate"]
    assert before["autobuild"]["phase"] == "review"
    assert before["autobuild"]["locked_baseline"]["write"]["body"] == "用户修改后的写作职责"
    release.set()
    # A new router has no in-memory job/progress state, like a restarted host.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as restarted:
        assert (await restarted.post(API + "/update", json=owner)).status_code == 202
        final = await poll_auto(restarted, owner)
        assert final["delivery"]["status"] == "ready"
        assert [call["phase"] for call in model.calls] == ["extract", "complete", "review", "review"]
        assert final["delivery"]["call_attempts"] == 4
        assert next(e for e in final["elements"] if e["id"] == "write")["body"] == "用户修改后的写作职责"
        assert store.get()["candidate"]["autobuild"]["started_at"] == before["autobuild"]["started_at"]


@pytest.mark.asyncio
async def test_api_legacy_extraction_deadline_does_not_prevent_explicit_resume(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    entered = asyncio.Event()

    async def block_extraction(phase, _):
        if phase == "extract" and len(model.calls) == 1:
            entered.set()
            await asyncio.Event().wait()

    model = BuildModel(on_call=block_extraction)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        await asyncio.wait_for(entered.wait(), timeout=5)
        await client.post(API + "/action", json={**owner, "action": "stop"})
    store = DraftStore(owner["project"], "paper")
    candidate = store.get()["candidate"]
    started = candidate["autobuild"]["started_at"]
    build = {**candidate["autobuild"], "deadline_at": time.time() - 1}
    store.checkpoint(candidate, candidate["document"], candidate["processed_ids"], "paused", autobuild=build)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as restarted:
        await restarted.post(API + "/update", json=owner)
        final = await poll_auto(restarted, owner)
        assert final["delivery"]["status"] == "ready" and final["delivery"]["phase"] == "done"
        assert final["coverage"]["pending"] == 0 and final["can_preview"]
        assert len(model.calls) == 4
        assert store.get()["candidate"]["autobuild"]["started_at"] == started


@pytest.mark.asyncio
async def test_api_soft_expiry_keeps_inflight_extraction_until_manual_stop(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    cancelled = asyncio.Event()
    entered = asyncio.Event()

    async def hold(phase, kwargs):
        assert phase == "extract" and kwargs["total_timeout"] is None
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    model = BuildModel(on_call=hold)
    # Keep setup/file I/O out of this test's deliberately tiny in-flight
    # timeout; asyncio's real timer still has to cancel the model coroutine.
    monkeypatch.setattr(orch_drafts, "time", SimpleNamespace(time=lambda: 0.0, monotonic=time.monotonic))
    monkeypatch.setattr(orch_drafts, "MAX_SECONDS", .15)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        await client.post(API + "/update", json=owner)
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.sleep(.25)
        live = (await client.get(API + "/progress", params=owner)).json()
        assert live["running"] and not cancelled.is_set()
        await client.post(API + "/action", json={**owner, "action": "stop"})
        final = await poll_auto(client, owner)
        assert final["task"]["status"] == "paused"
        assert len(model.calls) == 1 and cancelled.is_set()
        assert final["coverage"]["processed"] == 0


@pytest.mark.asyncio
async def test_runtime_paths_defaults_and_user_confirmation_are_delivered_without_questions():
    document = graph(ready=True)
    document["entries"][-1]["data"]["to_node"] = "confirm"
    document["entries"].extend([
        entry("confirm", "node", {"node_kind": "gate"}),
        edge("confirmed", "confirm", "finish"),
    ])
    document["entries"][-2].update(title="删除文件前由用户确认", body="删除文件前必须我确认")
    for item in document["entries"]:
        item["evidence"] = "user_source"
    original = copy.deepcopy(document)
    declared = [
        {"id": "topic", "kind": "input", "title": "主题", "body": "本次主题",
         "data": {"name": "主题", "required": True, "default": ""}, "rationale": "开跑时集中填写"},
        {"id": "path", "kind": "input", "title": "文件路径", "body": "本次文件路径",
         "data": {"name": "文件路径", "required": True, "default": ""}, "rationale": "开跑时集中填写"},
        {"id": "count", "kind": "decision", "title": "论文篇数", "body": "默认八篇",
         "data": {"category": "defaulted", "value": "8"}, "rationale": "采用可修改的合理默认"},
    ]
    forbidden = [
        {"id": "plan", "state": "retired", "rationale": "尝试删除"},
        {"id": "audit", "data": {"acceptance": ["放宽验收"]}, "rationale": "尝试放宽"},
        {"id": "confirm", "body": "AI 自动通过", "rationale": "尝试绕过确认"},
        {"id": "extra_gate", "kind": "node", "title": "新增签认", "body": "新增签认",
         "data": {"node_kind": "gate"}, "rationale": "尝试加确认点"},
    ]
    model, reviewer = FakeModel(proposal(*declared, *forbidden)), FakeModel('{"gaps":[]}')
    result = await run(document, model, reviewer)
    delivery = delivery_view(result["document"], result["build"])
    assert result["build"]["status"] == "ready" and delivery["pending"] == []
    assert [(item["name"], item["required"]) for item in delivery["run_inputs"]] == [("主题", True), ("文件路径", True)]
    assert delivery["assumptions"] == []
    assert result["build"]["autofix"]["rounds"][0]["ledger"]["rejected"][0]["code"] == "structure_not_adopted"
    after = {item["id"]: item for item in result["document"]["entries"]}
    assert [protected_view(after[item["id"]]) for item in original["entries"]] == [protected_view(item) for item in original["entries"]]
    assert "extra_gate" not in after and after["confirm"] == original["entries"][-2]
    rejected = result["build"]["autofix"]["rounds"][0]["structural"]["rejected"]
    assert {item["entry_id"] for item in rejected} == {item["id"] for item in forbidden}
    assert all(item["message"] for item in rejected)
    assert len(model.calls) == len(reviewer.calls) == 1 and document == original


@pytest.mark.asyncio
async def test_user_conflict_keeps_structural_diagnostics_and_bounded_repair_visible():
    document = graph()
    document["entries"][0].update(body="保留所有原稿", evidence="user_source",
                                  sources=[{"source_id": "u_first", "quote": "保留所有原稿"}])
    later = {"id": "u_later", "role": "user", "text": "禁止保留原稿，与之前的要求相反"}
    document = apply_patch(document, {"upserts": [cited({**document["entries"][0],
        "body": "禁止保留原稿，与之前的要求相反", "state": "conflict"}, later)], "ignored": []}, [later])
    assert len([item for item in document["entries"] if item["state"] == "conflict"]) == 1
    model, reviewer = FakeModel(*[proposal() for _ in range(4)]), FakeModel()
    result = await run(document, model, reviewer)
    assert result["build"]["status"] == "needs_review"
    assert result["document"] == document
    assert len(model.calls) == 4 and reviewer.calls == []
    pending = delivery_view(result["document"], result["build"])["pending"]
    assert any(item.get("code") == "entry_conflict" for item in pending)
    assert any(item.get("code") == "disconnected" for item in pending)
    assert len(result["build"]["autofix"]["rounds"]) == 4


@pytest.mark.asyncio
async def test_missing_irreversible_confirmation_stays_pending_without_an_ai_gate():
    document = graph(ready=True)
    original = copy.deepcopy(document)
    model = FakeModel(proposal(
        {"id": "approval", "kind": "question", "title": "删除前确认", "body": "不可逆删除步骤缺少用户确认",
             "data": {"blocking": False, "issue_type": "business"}, "rationale": "确认只能由用户决定，不能由 AI 新增签认点"},
        {"id": "ai_gate", "kind": "node", "title": "AI 签认", "body": "自动签认",
         "data": {"node_kind": "gate"}, "rationale": "故意尝试新增确认点"}))
    result = await run(document, model, FakeModel('{"gaps":[]}'))
    delivery = delivery_view(result["document"], result["build"])
    assert result["build"]["status"] == "waiting_user" and result["build"]["reason"] == "business_decision"
    assert len(delivery["pending"]) == 1 and delivery["pending"][0]["id"] == "approval"
    assert delivery["assumptions"] == []
    assert not any((item.get("data") or {}).get("node_kind") == "gate" for item in result["document"]["entries"])
    assert result["document"]["entries"][:len(original["entries"])] == original["entries"]
    rejected = result["build"]["autofix"]["rounds"][0]["structural"]["rejected"]
    assert rejected[0]["entry_id"] == "ai_gate" and rejected[0]["code"] == "gate_forbidden"
