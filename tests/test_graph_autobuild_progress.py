"""Request clocks and live stream activity cover completion and independent review."""
import asyncio
import json
from datetime import datetime

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration.extract import OrchestrationStreamTimeout, OrchestrationGenerationLimit
from tests.graph_autobuild_fixtures import BuildModel, RUNTIME_SOURCE
from tests.test_graph_autobuild import API, make_app, poll_auto
from tests.test_workflow_drafts import api_env, record, write_history


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,thinking,output", [("first_event", 0, 0), ("reasoning_idle", 48, 0), ("stream_idle", 48, 128)])
async def test_timeout_keeps_visible_failure_phase_and_partial_counters(api_env, monkeypatch, phase, thinking, output):
    _, _, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    async def fail(kind, _):
        if kind == "complete":
            raise OrchestrationStreamTimeout(phase=phase, timeout_seconds=360,
                                             thinking_chars=thinking, output_chars=output)
    model = BuildModel(on_call=fail)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        assert (await client.post(API + "/update", json=owner)).status_code == 202
        final = await poll_auto(client, owner)
        assert final["delivery"]["status"] == "needs_review" and final["delivery"]["reason"] == "model_error"
        attempt = final["delivery"]["attempts"][0]
        assert attempt["failure"]["phase"] == phase and attempt["failure"]["timeout_seconds"] == 360
        assert attempt["failure"]["thinking_chars"] == thinking and attempt["failure"]["output_chars"] == output
        assert "360" in attempt["failure"]["detail"]
        assert attempt["diagnostics_before"] == attempt["diagnostics_after"] and not attempt["structural"]["adopted"]
        assert len(model.calls) == 2, "a timeout must not resubmit the same request automatically"


@pytest.mark.asyncio
async def test_completion_and_review_report_distinct_request_clocks_and_live_events(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    entered = {phase: asyncio.Event() for phase in ("complete", "review")}
    release = {phase: asyncio.Event() for phase in entered}
    callbacks = {}

    async def hold(phase, kwargs):
        if phase in entered:
            callbacks[phase] = kwargs["progress"]
            entered[phase].set()
            await release[phase].wait()

    model = BuildModel(on_call=hold)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    monkeypatch.setattr(server, "_load_review_model", lambda: {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as client:
        try:
            assert (await client.post(API + "/update", json=owner)).status_code == 202
            starts = []
            for phase in entered:
                await asyncio.wait_for(entered[phase].wait(), timeout=5)
                response = (await client.get(API + "/progress", params=owner)).json()
                progress = response["progress"]
                assert response["running"]
                assert progress["request_kind"] == ("completion" if phase == "complete" else "review")
                assert datetime.fromisoformat(progress["request_started_at"]) >= datetime.fromisoformat(progress["job_started_at"])
                assert progress["model"]["stage"] == "preparing"
                assert progress["model"]["provider_events"] == progress["model"]["output_chars"] == 0
                assert "last_event_at" not in progress["model"], "a new request must not reuse past activity"
                starts.append(progress["request_started_at"])

                callbacks[phase]({"stage": "thinking", "detail": "正在生成提案", "provider_events": 2,
                    "thinking_chars": 48, "provider_event": True, "thinking_preview": "PRIVATE_REASONING"})
                activity = (await client.get(API + "/progress", params=owner)).json()
                metrics = activity["progress"]["model"]
                assert metrics["provider_events"] == 2 and metrics["thinking_chars"] == 48
                assert metrics["last_event_at"] and not metrics["waiting"]
                assert "PRIVATE_REASONING" not in json.dumps(activity)
                last = metrics["last_event_at"]

                callbacks[phase]({"stage": "thinking", "detail": "暂时无新流事件", "waiting": True})
                waiting = (await client.get(API + "/progress", params=owner)).json()["progress"]["model"]
                assert waiting["waiting"] and waiting["last_event_at"] == last
                callbacks[phase]({"stage": "streaming", "detail": "正在接收提案", "provider_events": 6,
                    "output_chars": 128, "output_chunks": 4, "provider_event": True})
                output = (await client.get(API + "/progress", params=owner)).json()["progress"]["model"]
                assert output["stage"] == "streaming" and output["output_chars"] == 128 and output["output_chunks"] == 4
                assert not output["waiting"] and output["last_event_at"] >= last
                release[phase].set()
            assert starts[1] >= starts[0]
            final = await poll_auto(client, owner)
            assert final["delivery"]["status"] == "ready" and len(model.calls) == 3
        finally:
            for event in release.values():
                event.set()


@pytest.mark.asyncio
async def test_request_total_failure_is_persisted_without_inventing_idle(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    async def fail(phase, _):
        if phase == "complete":
            raise OrchestrationGenerationLimit("模型请求达到总时限 900 秒；已有进度保持不变。",
                phase="request_total", timeout_seconds=900)
    model = BuildModel(on_call=fail)
    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as api:
        await api.post(API + "/update", json=owner)
        final = await poll_auto(api, owner)
    attempt = final["delivery"]["attempts"][0]
    assert attempt["failure"]["phase"] == "request_total"
    assert attempt["failure"]["timeout_seconds"] == 900
    assert attempt["diagnostics_before"] == attempt["diagnostics_after"]
    assert not attempt["structural"]["adopted"] and len(model.calls) == 2
