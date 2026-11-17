from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from frontend import orch_reports
from frontend.orch_activity import check_scope
from mewcode.orchestration import OrchestrationSpec, to_pipeline_graph
from mewcode.orchestration.activity import ActivityStore
from mewcode.orchestration.work_reports import (
    ReportCache, ReportJobs, digest, eligible_name, fact_text, local_cards,
    model_input, validate_cards,
)
from mewcode.pipeline.store import PipelineStore
from mewcode.tools.report_step import ReportStepParams, ReportStepTool, expert_step_from_report


def event(id="e1", **extra):
    return {"id": id, "run_id": "chat:task", "node_id": "n", "attempt": 1,
            "at": "2026-09-12T10:00:00Z", "revision": 1, "kind": "call", "name": "ReadFile",
            "arguments": {"path": "paper/intro.md"}, "status": "completed", **extra}


@pytest.mark.asyncio
async def test_report_fields_survive_tool_and_expert_activity(tmp_path):
    args = {"label": "核对引言第二段", "status": "running", "node": "n",
            "summary": "我正在核对引言第二段；拆开了两处长句，正在检查条件范围。",
            "work_object": "引言第二段", "purpose": "让句子更容易理解", "findings": "两处句子过长",
            "changes": "已拆开两处长句", "verification": "条件范围尚需审查", "blocker": "",
            "next_step": "核对拆分前后的条件范围", "evidence_refs": ["existing-source"]}
    output = await ReportStepTool().execute(ReportStepParams(**args))
    assert output.output == "STEP\trunning\t核对引言第二段"
    step = expert_step_from_report(args=args, output=output.output, role="executor", node_id="n")
    assert step["summary"] == args["summary"]
    assert step["changes"] == args["changes"]
    store = ActivityStore(tmp_path)
    store.bind("c", "g", "run", nodes=[{"id": "n"}])
    store.expert({**step, "conversation_id": "c", "run_id": "run", "seq": 1, "node_attempt": 2})
    row = store.page("c", "g", "run", node_id="n")["items"][0]
    assert row["role"] == "executor" and row["attempt"] == 2
    assert fact_text(row) == args["summary"]
    card = local_cards([row])[0]
    assert card["evidence_refs"] == [row["id"]]  # Invented/out-of-scope evidence is not exposed as a link.
    assert card["details"]["changes"] == args["changes"]
    assert "verdict" not in card


def test_pipeline_keeps_structured_report_and_excludes_thinking(tmp_path):
    store = ActivityStore(tmp_path)
    store.bind("c", "g", "chat:t", nodes=[{"id": "n"}])
    base = {"conversation_id": "c", "task_id": "t", "phase": "complete", "seq": 1}
    store.pipeline({**base, "item_id": "step", "kind": "call", "name": "report_step",
                    "meta": {"arguments": {"node": "n", "summary": "我已核对第二段，正在返修。", "changes": "拆开两处长句"}}})
    store.pipeline({**base, "item_id": "private", "kind": "thinking", "text": "private deliberation"})
    rows = store.page("c", "g", "chat:t")["items"]
    assert len(rows) == 1
    assert local_cards(rows)[0]["text"] == "我已核对第二段，正在返修。"


@pytest.mark.parametrize("name,status,expected", [
    ("ReadFile", "completed", "已读取"), ("ReadFile", "running", "正在读取"),
    ("WriteFile", "completed", "写入操作"), ("EditFile", "running", "正在修改"),
    ("Bash", "failed", "操作失败"), ("Bash", "waiting", "等待处理"),
    ("Bash", "completed", "尚未记录具体结论"), ("Bash", "paused", "已暂停"),
])
def test_automatic_facts_never_claim_acceptance(name, status, expected):
    text = fact_text(event(name=name, status=status))
    assert expected in text
    assert "验收通过" not in text


def test_adjacent_reads_merge_but_not_parallel_runs_or_attempts():
    rows = [event("read1", revision=1), event("read2", revision=2),
            event("retry", revision=3, attempt=2), event("other", revision=4, run_id="chat:other")]
    cards = local_cards(rows)
    assert len(cards) == 3
    assert cards[-1]["evidence_refs"] == ["read1", "read2"]
    assert cards[-1]["count"] == 2


def test_history_validation_preserves_time_and_rejects_cross_run_evidence():
    rows = [event("a"), event("b", run_id="chat:another", at="2026-09-12T11:00:00Z")]
    cards = validate_cards({"cards": [{"text": "我已读取引言，具体发现尚未记录。", "evidence_refs": ["a"]}]}, rows)
    historical = next(c for c in cards if c["source"] == "history_summary")
    assert historical["at"] == rows[0]["at"]
    assert historical["evidence_refs"] == ["a"]
    for refs in (["missing"], ["a", "b"], []):
        with pytest.raises(ValueError):
            validate_cards({"cards": [{"text": "汇报", "evidence_refs": refs}]}, rows)
    with pytest.raises(ValueError):
        validate_cards({"cards": [{"text": "汇报", "evidence_refs": ["a"]}] * 2}, rows)


def test_history_input_allowlist_and_name_protection():
    source = model_input([event(secret="not for model", thinking="private", arguments={"command": "secret command", "path": "intro.md"}),
                          event("private", kind="thinking", text="private chain")])
    serialized = json.dumps(source)
    assert "private" not in serialized and "secret" not in serialized
    assert source["events"][0]["target"] == "intro.md"
    assert eligible_name({"label": "len_intro"})
    assert eligible_name({"label": "处理阶段"})
    assert not eligible_name({"label": "len_intro", "label_source": "manual"})
    assert not eligible_name({"label": "核对引言中的条件范围"})


@pytest.mark.asyncio
async def test_cache_singleflight_failure_retry_and_versioned_inputs(tmp_path):
    jobs, store = ReportJobs(), ActivityStore(tmp_path)
    released = asyncio.Event()
    calls = 0

    async def generate():
        nonlocal calls
        calls += 1
        await released.wait()
        return {"cards": []}

    kwargs = dict(activity=store, scope={"conversation_id": "c", "node_id": "n"},
                  inputs=[{"id": "a", "revision": 1}], fallback={"cards": []}, generate=generate)
    replies = await asyncio.gather(*[jobs.request(**kwargs) for _ in range(5)])
    assert all(r["status"] == "pending" for r in replies)
    released.set()
    await asyncio.gather(*jobs.tasks.values())
    assert calls == 1
    assert (await jobs.request(**kwargs))["status"] == "ready" and calls == 1
    assert ReportCache(store).get(replies[0]["cache_key"])["status"] == "ready"
    changed = {**kwargs, "inputs": [{"id": "a", "revision": 2}]}
    assert (await jobs.request(**changed))["status"] == "pending"
    await asyncio.gather(*jobs.tasks.values())
    assert calls == 2

    async def broken():
        raise RuntimeError("provider key must not leak")

    failure = {**kwargs, "scope": {"conversation_id": "other"}, "generate": broken}
    await jobs.request(**failure)
    await asyncio.gather(*jobs.tasks.values())
    response = await jobs.request(**failure)
    assert response["status"] == "failed" and "provider key" not in str(response)
    assert not jobs.tasks
    assert (await jobs.request(**{**failure, "generate": generate}, retry=True))["status"] == "pending"
    await asyncio.gather(*jobs.tasks.values())
    assert calls == 3


@pytest.fixture
def report_env(tmp_path, monkeypatch):
    spec = OrchestrationSpec.from_value({"nodes": [
        {"node_id": "n", "label": "len_intro", "kind": "implement", "goal": "拆分引言中的长句，保留原有条件范围"},
        {"node_id": "other", "label": "核对表格数据", "kind": "implement"},
        {"node_id": "manual", "label": "custom_name", "kind": "implement"}],
        "edges": [], "meta": {"conversation_id": "c", "manual_node_labels": {"manual": "custom_name"}}})
    graph = PipelineStore(tmp_path).create_pipeline(to_pipeline_graph(spec, graph_id="g"))
    store = ActivityStore(tmp_path)
    store.bind("c", "g", "chat:task", nodes=[{"id": "n"}, {"id": "other"}])
    event_id = store.record("c", "chat:task", "read", event())
    state = {"conv": "c", "calls": [], "clients": []}
    srv = SimpleNamespace(SESSION=SimpleNamespace(work_dir=str(tmp_path)), PipelineStore=PipelineStore,
        _active_conversation_id=lambda: state["conv"], is_orchestration_graph=lambda _: True,
        _orchestration_attempt_context=lambda: {"provider_key": "chosen", "model": "selected-model", "requested_effort": "xhigh"},
        _new_orchestration_client=lambda context, effort: state["clients"].append((context, effort)) or object())
    jobs = ReportJobs()
    monkeypatch.setattr(orch_reports, "JOBS", jobs)

    async def complete(client, *, system, user_text, **kwargs):
        inputs = json.loads(user_text)
        state["calls"].append(inputs)
        if "nodes" in inputs:
            return json.dumps({"labels": {n["id"]: "拆分引言中的过长句子" for n in inputs["nodes"]}})
        return json.dumps({"cards": [{"text": "我已读取引言，尚未记录修改结果。", "evidence_refs": [e["id"]]} for e in inputs["events"]]})

    monkeypatch.setattr(orch_reports, "collect_model_text", complete)
    app = FastAPI()
    orch_reports.install(app, srv, check_scope)
    scope = {"project": str(tmp_path), "conversation_id": "c", "graph_id": "g"}
    return app, scope, state, store, event_id, jobs, graph


async def finish_request(client, jobs, body):
    first = await client.post("/api/orchestration/readable-report", json=body)
    assert first.status_code == 200, first.text
    if jobs.tasks:
        await asyncio.gather(*jobs.tasks.values())
    return await client.post("/api/orchestration/readable-report", json=body)


@pytest.mark.asyncio
async def test_names_api_is_display_only_and_uses_selected_model(report_env):
    app, scope, state, store, _, jobs, graph = report_env
    before = PipelineStore(scope["project"]).get_pipeline("g").model_dump_json()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await finish_request(client, jobs, {**scope, "mode": "names"})
        assert response.json()["labels"] == {"n": "拆分引言中的过长句子"}
        assert len(state["calls"]) == 1
        assert state["clients"][0][0]["model"] == "selected-model"
        assert state["clients"][0][1] == "xhigh"
    assert PipelineStore(scope["project"]).get_pipeline("g").model_dump_json() == before


@pytest.mark.asyncio
async def test_history_scope_cache_and_manual_correction(report_env):
    app, scope, state, store, event_id, jobs, _ = report_env
    body = {**scope, "mode": "history", "run_id": "chat:task", "node_id": "n", "event_ids": [event_id]}
    original = store.page("c", "g", "chat:task")["items"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await finish_request(client, jobs, body)
        assert response.json()["status"] == "ready"
        card = response.json()["cards"][0]
        assert card["source"] == "history_summary" and card["at"] == original[0]["at"]
        assert len(state["calls"]) == 1
        for change in ({"conversation_id": "foreign"}, {"node_id": "other"}, {"run_id": "chat:other"}, {"event_ids": ["foreign"]}):
            assert (await client.post("/api/orchestration/readable-report", json={**body, **change})).status_code == 409
        store.assign("c", "g", "chat:task", event_id, "other")
        assert (await client.post("/api/orchestration/readable-report", json=body)).status_code == 409
        store.assign("c", "g", "chat:task", event_id, None)
        renewed = await finish_request(client, jobs, body)
        assert renewed.json()["cache_key"] != response.json()["cache_key"]
        assert len(state["calls"]) == 2
    latest = store.page("c", "g", "chat:task")["items"][0]
    assert latest["text"] == original[0].get("text", "") and latest["node_id"] == "n"


@pytest.mark.asyncio
async def test_history_does_not_replace_structured_reports(report_env):
    app, scope, state, store, event_id, jobs, _ = report_env
    reported = store.record("c", "chat:task", "step", event(name="report_step", arguments={
        "summary": "我已改写第二段，正在核对条件范围。", "changes": "拆开两处长句", "next_step": "提交审查"}))
    body = {**scope, "mode": "history", "run_id": "chat:task", "node_id": "n", "event_ids": [event_id, reported]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = (await finish_request(client, jobs, body)).json()
        card = next(c for c in result["cards"] if c["id"] == reported)
        assert card["source"] == "model_report" and card["details"]["changes"] == "拆开两处长句"
        assert len(state["calls"][0]["events"]) == 1
        only_live = await client.post("/api/orchestration/readable-report", json={**body, "event_ids": [reported]})
        assert only_live.json()["status"] == "ready" and len(state["calls"]) == 1


def test_selected_events_still_use_projection_and_attempt_filter(tmp_path):
    store = ActivityStore(tmp_path)
    store.bind("c", "g", "chat:t", nodes=[{"id": "n"}, {"id": "other"}])
    a = store.record("c", "chat:t", "a", event())
    b = store.record("c", "chat:t", "b", event(attempt=2))
    assert len(store.page("c", "g", "chat:t", node_id="n", event_ids=[a, b])["items"]) == 2
    assert [e["id"] for e in store.page("c", "g", "chat:t", attempt=2, event_ids=[a, b])["items"]] == [b]
    store.assign("c", "g", "chat:t", a, "other")
    assert not store.page("c", "g", "chat:t", node_id="n", event_ids=[a])["items"]
