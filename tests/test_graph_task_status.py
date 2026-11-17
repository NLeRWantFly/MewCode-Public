"""Task liveness survives failed responses without changing the graph."""
import asyncio
import copy
import json
import threading

import httpx
import pytest

from frontend import orch_drafts
from mewcode.orchestration.draft_sources import read_sources
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.extract import OrchestrationGenerationLimit, OrchestrationStreamTimeout
from mewcode.orchestration.graph_autobuild import TIME_LIMIT_REASON, CALL_LIMIT_REASON
from mewcode.orchestration.graph_generation import graph_task_state
from tests.test_graph_autobuild import make_app
from tests.test_graph_local_repair import API, prepare
from tests.test_workflow_drafts import api_env


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "invalid", "timeout", "limit", "crash", "stop"])
async def test_local_terminal_state_survives_reopen_and_cleans_activity(api_env, monkeypatch, outcome):
    _, _, path, owner = api_env
    store = DraftStore(owner["project"], "paper")
    proposal = prepare(store, read_sources(path)[0])
    before = copy.deepcopy(store.get()["candidate"])
    entered, release = asyncio.Event(), asyncio.Event()
    async def model(client, **kwargs):
        assert kwargs["max_generated_chars"] is None
        kwargs["progress"]({"stage": "streaming", "provider_event": True, "output_chars": 123})
        entered.set()
        await release.wait()
        if outcome == "invalid": return "bad json"
        if outcome == "timeout": raise OrchestrationStreamTimeout(phase="stream_idle", timeout_seconds=360)
        if outcome == "limit": raise OrchestrationGenerationLimit("模型服务输出未完整结束")
        if outcome == "crash": raise RuntimeError("PRIVATE_PROVIDER_SECRET")
        proposal["ticket"] = json.loads(kwargs["user_text"])["ticket"]
        return json.dumps(proposal)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        request = asyncio.create_task(client.post(API + "/repair", json={**owner, "action": "local"}))
        await asyncio.wait_for(entered.wait(), 3)
        live = (await client.get(API + "/progress", params=owner)).json()
        assert live["task"]["status"] == "running" and live["progress"]["model"]["output_chars"] == 123
        if outcome == "stop": await client.post(API + "/action", json={**owner, "action": "stop"})
        release.set()
        assert (await request).status_code == (200 if outcome == "success" else 409)
        for suffix in ("", "/progress"):
            result = (await client.get(API + suffix, params=owner)).json()
            assert not result["running"] and result["progress"] is None
            assert result["task"]["status"] == ("completed" if outcome == "success" else "paused" if outcome == "stop" else "failed")
            assert "PRIVATE_PROVIDER_SECRET" not in json.dumps(result)
            if outcome != "success": assert result["task"]["error"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as reopened:
        restored = (await reopened.get(API + "/progress", params=owner)).json()
        assert restored["task"] == result["task"] and not restored["running"]
    assert store.get()["candidate"] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "paused", "working"])
async def test_auto_terminal_state_agrees_in_both_endpoints_with_invalid_graph(api_env, status):
    _, _, path, owner = api_env
    store = DraftStore(owner["project"], "paper")
    prepare(store, read_sources(path)[0])
    c = store.get()["candidate"]
    store.checkpoint(c, c["document"], c["processed_ids"], status, "测试停止" if status != "working" else "",
                     autobuild={"phase": "autofix", "status": "working", "model_calls": 1, "call_attempts": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        full = (await client.get(API, params=owner)).json()
        cheap = (await client.get(API + "/progress", params=owner)).json()
    expected = "interrupted" if status == "working" else status
    assert full["task"]["status"] == cheap["task"]["status"] == expected
    assert full["delivery"]["status"] == cheap["candidate"]["autobuild"]["status"] == expected
    assert full["delivery"]["phase"] == "autofix"


@pytest.mark.asyncio
async def test_progress_does_not_read_sources_estimate_chunks_or_diff_graph(api_env, monkeypatch):
    _, _, path, owner = api_env
    store = DraftStore(owner["project"], "paper")
    prepare(store, read_sources(path)[0])
    def forbidden(*args, **kwargs): raise AssertionError("progress must not repeat full graph/history work")
    monkeypatch.setattr(DraftStore, "sources", forbidden)
    monkeypatch.setattr(orch_drafts, "estimate_chunks", forbidden)
    monkeypatch.setattr("mewcode.orchestration.draft_store.document_diff", forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        for _ in range(3): assert (await client.get(API + "/progress", params=owner)).status_code == 200


@pytest.mark.asyncio
async def test_unfinished_local_run_is_interrupted_after_restart(api_env):
    _, _, path, owner = api_env
    store = DraftStore(owner["project"], "paper")
    prepare(store, read_sources(path)[0])
    old = store.start_local_repair()
    current = store.start_local_repair()
    store.finish_local_repair(old, "failed", "late failure")
    assert store.get()["local_repair_run"]["id"] == current
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        result = (await client.get(API + "/progress", params=owner)).json()
    assert result["task"]["status"] == "interrupted" and not result["running"]


@pytest.mark.parametrize('reason', [TIME_LIMIT_REASON, CALL_LIMIT_REASON])
def test_budget_termination_is_explicit(reason):
    state = {'candidate':{'status':'ready','autobuild':{'phase':'done','status':'needs_review','reason':reason}}}
    assert graph_task_state(state, False)['status']=='failed'


@pytest.mark.asyncio
async def test_progress_rereads_when_job_finishes_during_snapshot_read(api_env, monkeypatch):
    _, _, path, owner = api_env
    store = DraftStore(owner['project'], 'paper')
    proposal = prepare(store, read_sources(path)[0])
    entered, release = asyncio.Event(), asyncio.Event()
    snap_read, snap_release = threading.Event(), threading.Event()
    async def model(client, **kwargs):
        entered.set()
        await release.wait()
        proposal['ticket'] = json.loads(kwargs['user_text'])['ticket']
        return json.dumps(proposal)
    monkeypatch.setattr(orch_drafts, 'collect_model_text', model)
    original = DraftStore.get
    def blocked_get(self, **kwargs):
        result = original(self, **kwargs)
        if kwargs.get('include_changes') is False and not snap_read.is_set():
            snap_read.set()
            assert snap_release.wait(5)
        return result
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url='http://test') as client:
        job = asyncio.create_task(client.post(API+'/repair', json={**owner,'action':'local'}))
        await asyncio.wait_for(entered.wait(), 3)
        monkeypatch.setattr(DraftStore, 'get', blocked_get)
        poll = asyncio.create_task(client.get(API+'/progress', params=owner))
        try:
            assert await asyncio.to_thread(snap_read.wait, 3)
            release.set()
            assert (await job).status_code==200
        finally:
            snap_release.set()
        result = (await poll).json()
        assert not result['running'] and result['task']['status']=='completed'
