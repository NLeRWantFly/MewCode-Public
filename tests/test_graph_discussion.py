"""Real candidate/store/API boundaries with synthetic, explicitly scoped models."""
import asyncio
import copy
import json
import time

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_autobuild import new_build_state, run_autobuild
from mewcode.orchestration.graph_discussion import MAX_DISCUSSION_CALLS, public_discussion
from mewcode.orchestration.graph_review import review_coverage
from tests.test_graph_autobuild import entry, graph, proposal
from tests.test_workflow_drafts import api_env, record, write_history


def discussion_graph():
    document = graph(ready=True)
    document["entries"].append(entry("meaning", "question", {
        "node_id": "plan", "blocking": True, "issue_type": "business", "input_id": "labels"}))
    document["entries"].append(entry("labels", "input", {"name": "labels", "required": True, "default": None}))
    return document


def prepare(store):
    candidate = store.begin(None, graph_first=True)
    document = discussion_graph()
    build = new_build_state(document)
    build.update(phase="waiting_user", status="waiting_user", reason="business_decision",
                 waiting_since=time.time(), resume_phase="review", model_calls=4, call_attempts=4)
    build["autofix"] = {"rounds": [{"phase": "completion", "structural": {"reason": "no_regression"}}], "model_calls": 1}
    build["review"] = {"gaps": [], "error": "", "model_calls": 1}
    build["gap_fill"] = {"rounds": [], "model_calls": 1}
    build["final_review"] = {"gaps": [], "error": "", "model_calls": 1}
    return store.checkpoint(candidate, document, list(store.get()["source_ids"]), "ready", autobuild=build)


def turn(store, text):
    candidate = store.get()["candidate"]
    return store.discuss(None, "begin", candidate_id=candidate["id"], sequence=candidate["sequence"], message=text)


def response(binding, *, tentative=False, changes=None):
    return {"reply": "先比较两种方案。" if tentative else "将层级名称作为示例，下面是待采用改动。",
            "proposal": None if tentative else {"summary": "行测和数量分析作为示例，保留五层结构",
            "answers": [{"question_id": "meaning", "value": "层级名称是示例，五层结构固定", "user_message_id": binding["turn_id"]}],
            "changes": changes if changes is not None else [{"entry_id": "labels", "reason": "同一个决定不在开跑时重复收集",
                "changes": {"data": {"default": "示例名称，固定五层结构"}}}]}}


def complete(store, binding, reply):
    store.discuss(None, "complete", binding=binding, response=reply)


def adopt(store):
    candidate = store.get()["candidate"]
    store.discuss(None, "adopt", candidate_id=candidate["id"], sequence=candidate["sequence"],
                  proposal_id=candidate["autobuild"]["discussion"]["proposal"]["id"])


def test_multiple_turns_reopen_and_explicit_adoption(tmp_path):
    store = DraftStore(tmp_path, "chat")
    candidate = prepare(store)
    original = copy.deepcopy(candidate["document"])
    first, _ = turn(store, "先不决定，比较固定名称和示例名称")
    complete(store, first, response(first, tentative=True))
    assert store.get()["candidate"]["document"] == original
    reopened = DraftStore(tmp_path, "chat")
    second, prompt = turn(reopened, "这些名称是示例，五层结构固定")
    assert len(json.loads(prompt)["messages"]) == 3
    complete(reopened, second, response(second))
    saved = reopened.get()["candidate"]
    assert saved["document"] == original and saved["autobuild"]["call_attempts"] == 4
    public = public_discussion(saved["autobuild"])
    assert "document" not in public["proposal"] and public["calls"] == 2
    adopt(reopened)
    saved = reopened.get()["candidate"]
    answered = next(item for item in saved["document"]["entries"] if item["id"] == "meaning")
    assert answered["kind"] == "decision" and answered["data"]["category"] == "confirmed" and answered["reviewed"]
    assert saved["autobuild"]["phase"] == "autofix" and saved["autobuild"]["review_stale"]
    assert saved["autobuild"]["retired_review_calls"] == 2 and saved["autobuild"]["call_attempts"] == 4
    assert saved["processed_ids"] == candidate["processed_ids"]
    assert store.get()["source_ids"] == [] and store.sources() == []
    assert store.source(second["turn_id"])["text"] == "这些名称是示例，五层结构固定"


def test_revoke_and_late_reply_never_modify_graph(tmp_path):
    store = DraftStore(tmp_path, "chat")
    original = prepare(store)["document"]
    first, _ = turn(store, "暂定固定名字")
    complete(store, first, response(first))
    candidate = store.get()["candidate"]
    old_id = candidate["autobuild"]["discussion"]["proposal"]["id"]
    store.discuss(None, "discard", candidate_id=candidate["id"], sequence=candidate["sequence"])
    with pytest.raises(ValueError, match="失效|变化"):
        store.discuss(None, "adopt", candidate_id=candidate["id"], sequence=candidate["sequence"], proposal_id=old_id)
    second, _ = turn(store, "撤回，先不要确定")
    with pytest.raises(ValueError, match="变化|更新"):
        complete(store, first, response(first))
    complete(store, second, response(second, tentative=True))
    assert store.get()["candidate"]["document"] == original


@pytest.mark.parametrize("changes", [
    [{"entry_id": "plan", "reason": "改验收", "changes": {"data": {"acceptance": []}}}],
    [{"entry_id": "plan", "reason": "删节点", "changes": {"state": "retired"}}],
    [{"entry_id": "plan", "reason": "来源", "changes": {"sources": []}}],
    [{"entry_id": "labels", "reason": "无需再问", "changes": {"data": {"required": False}}}],
])
def test_forbidden_changes_roll_back_and_keep_reserved_call(tmp_path, changes):
    store = DraftStore(tmp_path, "chat")
    original = prepare(store)["document"]
    binding, _ = turn(store, "讨论一下")
    with pytest.raises(ValueError):
        complete(store, binding, response(binding, changes=changes))
    assert store.get()["candidate"]["document"] == original
    assert store.get()["candidate"]["autobuild"]["discussion"]["calls"] == 1


def test_discussion_budget_durable_after_unknown_responses(tmp_path):
    store = DraftStore(tmp_path, "chat")
    prepare(store)
    for i in range(MAX_DISCUSSION_CALLS):
        turn(DraftStore(tmp_path, "chat"), f"第 {i} 轮继续讨论")
    before = store.get()
    with pytest.raises(ValueError, match="上限"):
        turn(store, "再讨论一次")
    assert store.get() == before


@pytest.mark.asyncio
async def test_business_choice_pauses_before_review_and_waiting_is_idempotent():
    doc = discussion_graph()
    calls = []
    async def model(system, prompt):
        calls.append("build")
        return proposal()
    async def review(system, prompt):
        calls.append("review")
        return '{"gaps":[],"notes":""}'
    result = await run_autobuild(doc, None, [], conversation_id="chat", revision=1, ask_model=model, ask_review=review)
    assert calls == ["build"]
    assert result["build"]["phase"] == "waiting_user"
    resumed = await run_autobuild(result["document"], None, [], conversation_id="chat", revision=1,
                                 ask_model=model, ask_review=review, state=result["build"])
    assert calls == ["build"] and resumed == result


@pytest.mark.asyncio
async def test_adopted_decision_coverage_has_exact_active_requirement(tmp_path):
    store = DraftStore(tmp_path, "chat")
    prepare(store)
    binding, _ = turn(store, "也许名字固定，也许示例；最终五层固定，名字按示例")
    complete(store, binding, response(binding))
    adopt(store)
    captured = []
    async def review(system, prompt):
        data = json.loads(prompt); captured.append(data)
        req = data["requirements"][0]
        return json.dumps({"gaps": [{"source_id":req["source_id"],"quote":req["text"],"summary":"测试引用"}],"notes":""})
    result = await review_coverage([], store.get()["candidate"]["document"], ask_review=review)
    assert captured[0]["requirements"] == [{"source_id":"adopted_meaning","text":"层级名称是示例，五层结构固定"}]
    assert len(result["gaps"]) == 1 and not result["dropped"]


@pytest.mark.asyncio
async def test_embedded_api_preserves_route_and_never_extracts_discussion(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", "原始业务目标")])
    api = "/api/orchestration/graph-autobuild"
    prompts = []
    async def synthetic(client, **kwargs):
        assert client == ("chosen", "high")
        data = json.loads(kwargs["user_text"]); prompts.append(data)
        return json.dumps(response(data["binding"], tentative=len(prompts) == 1), ensure_ascii=False)
    monkeypatch.setattr(orch_drafts, "collect_model_text", synthetic)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.get(api, params=owner)).status_code == 200
        store = DraftStore(owner["project"], owner["conversation_id"])
        prepare(store)
        before = store.get()
        for text in ("先比较方案", "用示例名字，五层固定"):
            candidate = store.get()["candidate"]
            result = await client.post(api + "/discussion", json={**owner,"action":"send","candidate_id":candidate["id"],
                "sequence":candidate["sequence"],"message":text})
            assert result.status_code == 200, result.text
        after = store.get()
        assert before["document"] == after["document"]
        assert before["candidate"]["document"] == after["candidate"]["document"]
        assert before["source_ids"] == after["source_ids"] and before["candidate"]["processed_ids"] == after["candidate"]["processed_ids"]
        assert len(after["candidate"]["autobuild"]["discussion"]["messages"]) == 4
        denied = await client.post(api + "/discussion", json={**owner,"conversation_id":"someone_else","message":"继续"})
        assert denied.status_code == 409 and len(prompts) == 2


def test_linked_input_sync_and_confirmed_decision_survive_graph_roundtrip(tmp_path):
    from mewcode.orchestration.workflow_draft import project_graph, seed_document
    store = DraftStore(tmp_path, "chat")
    prepare(store)
    binding, _ = turn(store, "名称是示例，保留五层")
    complete(store, binding, response(binding, changes=[]))
    preview = public_discussion(store.get()["candidate"]["autobuild"])["proposal"]
    assert {row["entry_id"] for row in preview["changes"]} == {"meaning", "labels"}
    adopt(store)
    document = store.get()["candidate"]["document"]
    value = next(row for row in document["entries"] if row["id"] == "labels")["data"]["default"]
    assert value == "层级名称是示例，五层结构固定"
    spec = project_graph(document, None, conversation_id="chat", revision=1)
    seeded = seed_document(spec.as_dict(), previous=document)
    decision = next(row for row in seeded["entries"] if row["id"] == "meaning")
    assert decision["kind"] == "decision" and decision["data"]["category"] == "confirmed"
    assert decision["sources"][-1]["source_id"] == binding["turn_id"]


def test_waiting_and_adopted_graph_cannot_be_saved_before_validation(tmp_path):
    from mewcode.orchestration.graph_generation import ready_graph_document
    store = DraftStore(tmp_path, "chat")
    prepare(store)
    def blocked():
        state = store.get(); candidate = state["candidate"]
        with pytest.raises(ValueError, match="讨论|待校验"):
            ready_graph_document(state, candidate, None, conversation_id="chat")
        with pytest.raises(ValueError, match="讨论|待校验"):
            store.confirm(candidate["id"], candidate["sequence"])
    blocked()
    binding, _ = turn(store, "采用示例名称")
    complete(store, binding, response(binding))
    adopt(store)
    blocked()


def test_long_discussion_memory_is_source_bound_and_preserves_tentative_preference(tmp_path):
    store = DraftStore(tmp_path, "chat")
    prepare(store)
    first, _ = turn(store, "暂定名称是示例，还需要比较")
    memory = [{"kind":"tentative", "quote":"暂定名称是示例", "user_message_id":first["turn_id"]}]
    complete(store, first, {**response(first, tentative=True), "memory":memory})
    for i in range(5):
        binding, prompt = turn(store, f"继续比较第 {i} 项")
        complete(store, binding, response(binding, tentative=True))
    payload = json.loads(prompt)
    assert first["turn_id"] not in {row["id"] for row in payload["messages"]}
    assert payload["tentative_and_unresolved"] == memory
    binding, _ = turn(store, "还未决定")
    with pytest.raises(ValueError, match="真实用户引文"):
        complete(store, binding, {**response(binding, tentative=True),
            "memory":[{**memory[0], "quote":"我已经批准固定名称"}]})
    assert public_discussion(store.get()["candidate"]["autobuild"])["memory"] == memory


def test_manual_edit_invalidates_proposal_and_continue_requires_resolved_questions(tmp_path):
    store = DraftStore(tmp_path, "chat")
    prepare(store)
    binding, _ = turn(store, "先给方案")
    complete(store, binding, response(binding))
    candidate = store.get()["candidate"]
    edited = store.edit(candidate["id"], candidate["sequence"], "plan", {"body":"补充计划说明"})
    assert not edited["autobuild"]["discussion"]["proposal"]
    with pytest.raises(ValueError, match="待决问题"):
        store.discuss(None, "continue", candidate_id=edited["id"], sequence=edited["sequence"])
    edited = store.edit(edited["id"], edited["sequence"], "meaning", {"state":"retired"})
    store.discuss(None, "continue", candidate_id=edited["id"], sequence=edited["sequence"])
    build = store.get()["candidate"]["autobuild"]
    assert build["phase"] == "autofix" and build["review_stale"] and build["call_attempts"] == 4


@pytest.mark.asyncio
async def test_adoption_excludes_wait_time_and_does_not_reset_exhausted_budget(tmp_path):
    from mewcode.orchestration.graph_autobuild import MAX_MODEL_CALLS, CALL_LIMIT_REASON
    store = DraftStore(tmp_path, "chat")
    candidate = prepare(store)
    build = candidate["autobuild"]
    build.update(call_attempts=MAX_MODEL_CALLS, waiting_since=time.time()-60)
    deadline = build["deadline_at"]
    store.checkpoint(candidate, candidate["document"], candidate["processed_ids"], "ready", autobuild=build)
    binding, _ = turn(store, "名称是示例")
    complete(store, binding, response(binding))
    adopt(store)
    candidate = store.get()["candidate"]
    assert candidate["autobuild"]["deadline_at"] >= deadline + 59
    async def forbidden(*args):
        raise AssertionError("An exhausted build must not dispatch a provider")
    result = await run_autobuild(candidate["document"], None, [], conversation_id="chat", revision=1,
        ask_model=forbidden, ask_review=forbidden, state=candidate["autobuild"])
    assert result["build"]["reason"] == CALL_LIMIT_REASON and result["build"]["call_attempts"] == MAX_MODEL_CALLS


@pytest.mark.parametrize("issue_type", ["technical", "business"])
def test_ai_resolution_only_closes_technical_questions(issue_type):
    from mewcode.orchestration.graph_completion import apply_completion
    from mewcode.orchestration.graph_discussion import waiting_questions
    from mewcode.orchestration.run_inputs import resolved_question_ids
    document=graph(ready=True)
    document["entries"].append(entry("choice", "question", {"node_id":"plan", "issue_type":issue_type, "blocking":True}))
    change={"id":"resolution", "kind":"decision", "title":"采用默认顺序", "body":"先计划后复核",
        "data":{"category":"designed", "value":"先计划后复核", "resolves_question_id":"choice"},
        "rationale":"宿主协议允许自行解决技术顺序"}
    result, applied, rejected=apply_completion(document, {"upserts":[change],"notes":""})
    if issue_type == "technical":
        assert applied and not rejected and resolved_question_ids(result) == {"choice"}
        assert not waiting_questions(result)
    else:
        assert not applied and rejected[0]["code"] == "business_decision_forbidden"
        assert not resolved_question_ids(result) and waiting_questions(result)[0]["id"] == "choice"
