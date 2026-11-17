"""Explicit retries keep the graph and extraction cursor instead of replaying history."""
import copy
import time

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_autobuild import TIME_LIMIT_REASON
from tests.graph_autobuild_fixtures import BuildModel, RUNTIME_SOURCE
from tests.test_graph_autobuild import API, make_app, poll_auto
from tests.test_workflow_drafts import api_env, record, write_history


@pytest.mark.asyncio
async def test_explicit_retry_of_expired_build_reuses_graph_and_preserves_manual_edits(api_env, monkeypatch):
    _, plans, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    model = BuildModel(nonconverging=True)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    monkeypatch.setattr(server, "_load_review_model", lambda: {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        assert (await client.post(API + "/update", json=owner)).status_code == 202
        old = await poll_auto(client, owner)
        store = DraftStore(owner["project"], "paper")
        current = store.get()["candidate"]
        document = copy.deepcopy(current["document"])
        target = next(item for item in document["entries"] if item["id"] == "write")
        target.update(evidence="manual")
        target["data"]["inputs"] = ["最新人工输入"]
        expired = {**current["autobuild"], "deadline_at": 0, "call_attempts": 8, "reason": TIME_LIMIT_REASON}
        store.checkpoint(current, document, current["processed_ids"], "ready", autobuild=expired)
        prior = store.get()["candidate"]
        attempts_before = len(model.calls)
        # Ordinary polling/reopening/update retains the completed attempt.
        repeated = await client.post(API + "/update", json=owner)
        assert repeated.status_code == 200 and repeated.json()["already_included"]
        assert len(model.calls) == attempts_before

        model.nonconverging = False
        started = time.time()
        retry = await client.post(API + "/update", json={**owner, "retry_build": True})
        assert retry.status_code == 202, retry.text
        fixed = await poll_auto(client, owner)
        assert fixed["candidate"]["id"] == old["candidate"]["id"]
        assert fixed["delivery"]["status"] == "ready" and fixed["can_preview"]
        assert [row["phase"] for row in model.calls[attempts_before:]] == ["complete", "review"]
        feedback = model.calls[attempts_before]["inputs"]["previous_feedback"]
        assert len(feedback) == 2 and all(row["structural"]["reason"] == "not_proposed" for row in feedback)
        after = store.get()["candidate"]
        assert after["processed_ids"] == prior["processed_ids"] and after["log"] == prior["log"]
        assert after["autobuild"]["started_at"] >= started
        assert after["autobuild"]["call_attempts"] == 2
        history = fixed["delivery"]["previous_attempts"]
        assert len(history) == 1 and history[0]["reason"] == TIME_LIMIT_REASON
        assert history[0]["call_attempts"] == 8 and history[0]["autofix"]["rounds"]
        kept = next(item for item in fixed["elements"] if item["id"] == "write")
        assert kept["data"]["inputs"] == ["最新人工输入"]
        assert kept["data"]["acceptance"] == ["不得丢失任一原始要求"]
        assert plans.get(owner["project"]) is None
        # A successfully delivered graph cannot silently restart another paid attempt.
        calls_before = len(model.calls)
        invalid = await client.post(API + "/update", json={**owner, "retry_build": True})
        assert invalid.status_code == 409 and len(model.calls) == calls_before


@pytest.mark.asyncio
async def test_retry_rejects_wrong_route_or_unfinished_attempt_without_candidate_mutation(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    model = BuildModel(nonconverging=True)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        assert (await client.post(API + "/update", json=owner)).status_code == 202
        await poll_auto(client, owner)
        store = DraftStore(owner["project"], "paper")
        baseline = store.get()["candidate"]
        calls_before = len(model.calls)
        for route, body in [
            (API, {"retry_build": "true"}), (API, {"retry_build": True, "rebuild": True}),
            (API, {"retry_build": True, "selection": {"texts": [RUNTIME_SOURCE]}}),
            ("/api/orchestration/graph-generation", {"retry_build": True}),
        ]:
            result = await client.post(route + "/update", json={**owner, **body})
            assert result.status_code == 409
            assert store.get()["candidate"] == baseline and len(model.calls) == calls_before
        build = {**baseline["autobuild"], "phase": "review", "status": "working"}
        store.checkpoint(baseline, baseline["document"], baseline["processed_ids"], "paused", autobuild=build)
        paused = store.get()["candidate"]
        result = await client.post(API + "/update", json={**owner, "retry_build": True})
        assert result.status_code == 409 and store.get()["candidate"] == paused
        assert len(model.calls) == calls_before
