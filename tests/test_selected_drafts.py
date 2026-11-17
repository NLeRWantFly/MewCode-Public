"""Selective inclusion never advances the full-history cursor or authorizes execution."""
import asyncio
import json

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration.draft_sources import read_sources, select_messages
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.workflow_draft import apply_patch, empty_document
from tests.test_workflow_drafts import api_env, cited, entry, poll, record, write_history


def test_selected_message_is_persisted_exact_and_context_is_separate(tmp_path):
    path = tmp_path / "history.jsonl"
    long = "候选建议" * 2000
    write_history(path, [record("user", "不纳入的旧要求"), record("assistant", "旧方案"),
                        record("user", "如何补充验收？"), record("assistant", long),
                        record("user", "无关的下一条")])
    sources, _, _ = read_sources(path)
    selected, context = select_messages(sources, {"texts": [long]})
    assert len(selected) == 2 and {s["line"] for s in selected} == {4}
    assert [s["text"] for s in context] == ["如何补充验收？"]
    for selection in ({"texts": ["伪造建议"]}, {"texts": []}, {"texts": ["不纳入的旧要求"]}):
        with pytest.raises(ValueError):
            select_messages(sources, selection)
    write_history(path, [record("assistant", long)], append=True)
    with pytest.raises(ValueError, match="多个相同"):
        select_messages(read_sources(path)[0], {"texts": [long]})


def test_selected_streamed_reply_can_span_tools_but_not_user_turns(tmp_path):
    path = tmp_path / "history.jsonl"
    write_history(path, [record("assistant", "说明一"), record("tool_result", "不作为指令"),
                        record("assistant", "说明二"), record("user", "新请求"), record("assistant", "说明三")])
    sources = read_sources(path)[0]
    assert len(select_messages(sources, {"texts": ["说明一", "说明二"]})[0]) == 2
    with pytest.raises(ValueError):
        select_messages(sources, {"texts": ["说明二", "说明三"]})


def test_reference_context_cannot_become_selected_authorization():
    src = {"id": "s", "text": "建议改为抽样", "role": "assistant"}
    ctx = {"id": "u", "text": "原来必须全量", "role": "user"}
    old = cited(entry("rule", "constraint", "全量"), ctx)
    old["evidence"] = "user_source"
    patch = {"upserts": [cited(entry("rule", "constraint", "抽样"), src)], "ignored": []}
    patch["upserts"][0]["sources"] += cited(entry(), ctx)["sources"]
    result = apply_patch({**empty_document(), "entries": [old]}, patch, [src], context=[ctx], selected_only=True)
    assert result["entries"][0]["state"] == "conflict"
    assert result["entries"][0]["evidence"] == "proposal"
    with pytest.raises(ValueError, match="只引用了参考上下文"):
        apply_patch(empty_document(), {"upserts": [cited(entry(), ctx)], "ignored": [{"source_id": "s", "reason": "忽略"}]},
                    [src], context=[ctx], selected_only=True)


def test_selected_store_confirm_append_restore_and_cursor_integrity(tmp_path):
    path = tmp_path / "history.jsonl"
    write_history(path, [record("user", "旧要求"), record("assistant", "方案一"), record("assistant", "方案二")])
    sources, warnings, snapshot = read_sources(path)
    store = DraftStore(tmp_path, "paper")
    store.refresh(sources, warnings, snapshot)
    first, second = sources[1]["id"], sources[2]["id"]
    candidate = store.begin(None, selection=[first])
    candidate = store.checkpoint(candidate, candidate["document"], [first], "ready")
    seq = candidate["sequence"]
    assert store.begin(None, selection=[first])["sequence"] == seq
    candidate = store.begin(None, selection=[second])
    assert candidate["source_ids"] == [first, second] and candidate["processed_ids"] == [first]
    assert store.get()["coverage"]["pending"] == 1
    with pytest.raises(ValueError):
        store.checkpoint(candidate, candidate["document"], [sources[0]["id"]])
    candidate = store.checkpoint(candidate, candidate["document"], [first, second], "ready")
    store.confirm(candidate["id"], candidate["sequence"])
    state = store.get()
    assert state["processed_ids"] == [] and state["selected_ids"] == [first, second]
    assert store.begin(None, selection=[first]) is None
    # An explicit full-history scan remains available and preserves its own cursor.
    full = store.begin(None)
    full = store.checkpoint(full, full["document"], [s["id"] for s in sources], "ready")
    store.confirm(full["id"], full["sequence"])
    assert not store.get()["selected_ids"]
    restored = store.restore(1, 2)
    assert restored["selected_ids"] == [first, second] and restored["processed_ids"] == []


def test_selection_preserves_unconfirmed_full_scan_edits_without_reading_remainder(tmp_path):
    path = tmp_path / "history.jsonl"
    write_history(path, [record("user", "保留旧要求"), record("assistant", "不选择的建议"), record("assistant", "新选择")])
    sources, warnings, snapshot = read_sources(path)
    store = DraftStore(tmp_path, "paper")
    store.refresh(sources, warnings, snapshot)
    full = store.begin(None)
    doc = {**empty_document(), "entries": [cited(entry("old", "constraint", "人工核对过的条款"), sources[0])]}
    full = store.checkpoint(full, doc, [sources[0]["id"]], "paused")
    chosen = store.begin(None, selection=[sources[2]["id"]])
    assert chosen["id"] == full["id"] and chosen["document"] == doc
    assert chosen["source_ids"] == [sources[2]["id"]] and not chosen["processed_ids"]
    chosen = store.checkpoint(chosen, doc, chosen["source_ids"], "ready")
    confirmed = store.confirm(chosen["id"], chosen["sequence"])
    assert confirmed["processed_ids"] == [sources[0]["id"]]
    assert confirmed["selected_ids"] == [sources[2]["id"]]


@pytest.mark.asyncio
async def test_selected_api_runs_only_selected_sources_and_never_applies_graph(api_env, monkeypatch, tmp_path):
    _, plans, path, owner = api_env
    write_history(path, [record("assistant", "建议添加逐页校验"), record("user", "之后的未选中要求")], append=True)
    calls, entered, release = [], asyncio.Event(), asyncio.Event()
    async def model(client, **kwargs):
        inputs = json.loads(kwargs["user_text"])
        calls.append(inputs)
        assert "本次为用户点选" in kwargs["system"]
        entered.set()
        await release.wait()
        return json.dumps({"upserts": [cited(entry("audit", "constraint", "逐页校验"), inputs["sources"][0])], "ignored": []})
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        request = {**owner, "selection": {"texts": ["建议添加逐页校验"]}}
        response = await client.post("/api/orchestration/draft/update", json=request)
        assert response.status_code == 202, response.text
        await asyncio.wait_for(entered.wait(), 2)
        assert (await client.post("/api/orchestration/draft/update", json=request)).status_code == 409
        # New conversation text must not enlarge the selected work snapshot.
        write_history(path, [record("user", "再加一个未选中要求")], append=True)
        release.set()
        state = await poll(client, owner)
        assert state["coverage"]["total"] == 1 and state["coverage"]["pending"] == 0
        assert [s["text"] for s in calls[0]["sources"]] == ["建议添加逐页校验"]
        assert [s["text"] for s in calls[0]["context"]] == ["先分析，再审查，必须保留双稿"]
        c = state["candidate"]
        assert c["status"] == "ready" and c["document"]["entries"][0]["evidence"] == "proposal"
        response = await client.post("/api/orchestration/draft/action", json={**owner, "action": "confirm", "candidate_id": c["id"], "sequence": c["sequence"]})
        assert response.status_code == 200, response.text
        response = await client.post("/api/orchestration/draft/update", json=request)
        assert response.status_code == 200 and response.json()["already_included"]
        assert len(calls) == 1 and plans.get(str(tmp_path)) is None
        assert DraftStore(tmp_path, "paper").get()["processed_ids"] == []
        assert (await client.post("/api/orchestration/draft/update", json={**request, "conversation_id": "foreign"})).status_code == 409
