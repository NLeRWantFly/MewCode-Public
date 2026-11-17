"""Exercise the real scheduler/collector without network requests or long waits."""
import asyncio
from contextvars import ContextVar
from contextlib import aclosing

import httpx
import pytest

import mewcode.client as clients
from mewcode.conversation import ConversationManager
from frontend import orch_drafts, server
from mewcode.llm_watchdog import next_provider_event, ProviderEventTimeout
from mewcode.orchestration import extract
from mewcode.orchestration.graph_autobuild import TIME_LIMIT_REASON
from mewcode.runtime_scheduler import RuntimeScheduler, current_runtime_scheduler
from mewcode.tools.base import StreamStatus, TextDelta, StreamEnd
from tests.graph_autobuild_fixtures import RUNTIME_SOURCE
from tests.test_graph_autobuild import API, make_app, poll_auto
from tests.test_workflow_drafts import api_env, record, write_history


class LocalClient:
    def __init__(self, *, delay=.05, heartbeat=False, fail=False, hold=False):
        self.delay, self.heartbeat, self.fail, self.hold = delay, heartbeat, fail, hold
        self.calls = 0
        self.started = asyncio.Event()
        self.closed = False
        self.tasks = []
        self.scope = ContextVar("test-request-deadline", default=None)

    async def stream(self, *args, **kwargs):
        self.calls += 1
        token = self.scope.set("request-owned")
        self.tasks.append(asyncio.current_task())
        try:
            self.started.set()
            yield StreamStatus("accepted", "accepted")
            if self.fail:
                raise TimeoutError("provider-specific private error")
            if self.hold:
                await asyncio.Event().wait()
            if self.heartbeat:
                while True:
                    await asyncio.sleep(.003)
                    yield StreamStatus("activity", "provider heartbeat")
            await asyncio.sleep(self.delay)
            yield TextDelta('{"ok":true}')
            yield StreamEnd("stop")
        finally:
            self.tasks.append(asyncio.current_task())
            self.scope.reset(token)
            self.closed = True


@pytest.fixture
def pool(monkeypatch):
    scheduler = RuntimeScheduler(limit=1)
    monkeypatch.setattr(clients, "GLOBAL_RUNTIME_SCHEDULER", scheduler)
    yield scheduler
    assert scheduler.snapshot()["running"] == scheduler.snapshot()["queued"] == 0


@pytest.mark.asyncio
async def test_silent_reasoning_uses_explicit_budget_and_keeps_same_request(pool, monkeypatch):
    monkeypatch.setattr(clients, "LLM_REQUEST_TIMEOUT", .01)
    model, updates = LocalClient(), []
    result = await extract.collect_model_text(model, system="", user_text="",
        total_timeout=.3, first_event_timeout=.1, idle_timeout=.01,
        soft_idle_warning=.01, progress=updates.append)
    assert result == '{"ok":true}' and model.calls == 1
    assert any(u.get("waiting") and "静默推理" in u["detail"] for u in updates)
    assert not any(u["stage"] in {"stalled", "time_limit"} for u in updates)
    assert updates[-1]["provider_events"] == 3, "local admission is not provider activity"
    assert model.closed and len(set(model.tasks)) == 1 and model.scope.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat", [False, True])
async def test_request_total_is_not_misclassified_as_reasoning_idle(pool, heartbeat):
    model, updates = LocalClient(delay=10, heartbeat=heartbeat), []
    with pytest.raises(extract.OrchestrationGenerationLimit) as caught:
        await extract.collect_model_text(model, system="", user_text="",
            total_timeout=.03, first_event_timeout=.1, progress=updates.append)
    assert caught.value.phase == "request_total" and caught.value.timeout_seconds == .03
    assert updates[-1]["stage"] == "time_limit"
    assert not any(u["stage"] == "stalled" for u in updates)
    assert model.calls == 1 and model.closed and len(set(model.tasks)) == 1


@pytest.mark.asyncio
async def test_inner_total_has_its_own_type_even_with_recent_activity(pool):
    model, updates = LocalClient(heartbeat=True), []
    async with aclosing(clients.scheduled_client_stream(
        model, ConversationManager(),
        request_timeout=.025)) as stream:
        with pytest.raises(extract.OrchestrationGenerationLimit) as caught:
            await extract._collect_model_stream(stream.__aiter__(), progress=updates.append,
                first_event_timeout=.1, reasoning_idle_timeout=.2, idle_timeout=.1,
                total_timeout=.3, max_generated_chars=1000, soft_idle_warning=.01, queue_timeout=.1)
    assert caught.value.phase == "request_total"
    assert caught.value.timeout_seconds == .025
    assert updates[-1]["stage"] == "time_limit" and model.closed


@pytest.mark.asyncio
async def test_local_queue_does_not_consume_first_event_or_request_budget(pool):
    holder = await pool.acquire(kind="model")
    model, updates = LocalClient(delay=.012), []
    task = asyncio.create_task(extract.collect_model_text(model, system="", user_text="",
        total_timeout=.05, first_event_timeout=.025, queue_timeout=.5, progress=updates.append))
    try:
        await asyncio.sleep(.08)
        assert not task.done() and model.calls == 0
        assert updates[-1]["stage"] == "queued" and not updates[-1].get("provider_event")
        await pool.release(holder)
        assert await task == '{"ok":true}'
        assert model.calls == 1 and model.closed
    finally:
        await pool.release(holder)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_queue_timeout_never_blames_or_dispatches_provider(pool):
    holder = await pool.acquire(kind="model")
    model, updates = LocalClient(), []
    try:
        with pytest.raises(extract.OrchestrationStreamTimeout) as caught:
            await extract.collect_model_text(model, system="", user_text="",
                queue_timeout=.02, first_event_timeout=.1, progress=updates.append)
        assert caught.value.phase == "queue" and "尚未发送" in str(caught.value)
        assert model.calls == 0 and not model.closed
        assert updates[-1]["provider_events"] == 0 and updates[-1]["stalled_phase"] == "queue"
        assert pool.snapshot()["queued"] == 0
    finally:
        await pool.release(holder)


@pytest.mark.asyncio
async def test_provider_timeout_is_not_a_local_idle_or_total_limit(pool):
    model, updates = LocalClient(fail=True), []
    with pytest.raises(clients.LLMError, match="Provider 连接或请求超时") as caught:
        await extract.collect_model_text(model, system="", user_text="", progress=updates.append)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert not any(u["stage"] in {"stalled", "time_limit"} for u in updates)
    assert model.closed and model.calls == 1


@pytest.mark.asyncio
async def test_next_event_distinguishes_its_deadline_from_provider_timeout():
    original = TimeoutError("provider timeout")
    async def fail():
        raise original
        yield
    with pytest.raises(TimeoutError) as caught:
        await next_provider_event(fail(), .1)
    assert caught.value is original and not isinstance(caught.value, ProviderEventTimeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_queued", [False, True])
async def test_autobuild_uses_app_pool_and_stop_cleans_queue_or_stream(api_env, monkeypatch, cancel_queued):
    _, _, path, owner = api_env
    write_history(path, [record("user", RUNTIME_SOURCE)])
    app_pool, fallback = RuntimeScheduler(limit=1), RuntimeScheduler(limit=1)
    holder = await app_pool.acquire(kind="model")
    model = LocalClient(hold=True)
    monkeypatch.setattr(server, "APP_RUNTIME_SCHEDULER", app_pool)
    monkeypatch.setattr(clients, "GLOBAL_RUNTIME_SCHEDULER", fallback)
    monkeypatch.setattr(server, "_new_orchestration_client", lambda *a: model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as api:
        try:
            assert (await api.post(API + "/update", json=owner)).status_code == 202
            for _ in range(100):
                if app_pool.snapshot()["queued"]:
                    break
                await asyncio.sleep(.005)
            assert app_pool.snapshot()["queued"] == 1 and model.calls == 0
            assert fallback.snapshot()["running"] == fallback.snapshot()["queued"] == 0
            data = (await api.get(API + "/progress", params=owner)).json()
            assert data["progress"]["model"]["stage"] == "queued"
            assert data["progress"]["model"]["provider_events"] == 0
            if not cancel_queued:
                await app_pool.release(holder)
                await asyncio.wait_for(model.started.wait(), 1)
            stopped = await api.post(API + "/action", json={**owner, "action": "stop"})
            assert stopped.status_code == 200
            final = await poll_auto(api, owner)
            assert not final["running"] and final["revision"] == 0
            assert final["candidate"]["status"] == "paused"
            assert model.calls == (0 if cancel_queued else 1)
            assert app_pool.snapshot()["queued"] == 0
            if not cancel_queued:
                assert model.closed and len(set(model.tasks)) == 1
            assert current_runtime_scheduler(fallback) is fallback, "job binding must not leak into HTTP scope"
        finally:
            await app_pool.release(holder)
    assert app_pool.snapshot()["running"] == app_pool.snapshot()["queued"] == 0


@pytest.mark.asyncio
async def test_queue_failure_does_not_shrink_sources_or_resubmit(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", "业务要求 " + str(i)) for i in range(4)])
    calls = []
    async def queue_error(*args, **kwargs):
        calls.append(kwargs)
        raise extract.OrchestrationStreamTimeout(phase="queue", timeout_seconds=120)
    monkeypatch.setattr(orch_drafts, "collect_model_text", queue_error)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as api:
        await api.post(API + "/update", json=owner)
        result = await poll_auto(api, owner)
    assert len(calls) == 1 and result["candidate"].get("chunk_source_limit") is None
    assert "本地模型调度排队" in result["candidate"]["error"]
    assert [r["status"] for r in result["candidate"]["log"]] == ["failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("auto", [False, True])
async def test_provider_timeout_is_not_the_whole_autobuild_deadline(api_env, monkeypatch, auto):
    calls = []
    async def provider_timeout(*args, **kwargs):
        calls.append(kwargs)
        raise TimeoutError("synthetic provider failure")
    monkeypatch.setattr(orch_drafts, "collect_model_text", provider_timeout)
    owner = api_env[3]
    root = API if auto else "/api/orchestration/draft"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as api:
        await api.post(root + "/update", json=owner)
        result = await poll_auto(api, owner)
    assert len(calls) == 1 and result["candidate"]["status"] == "failed"
    assert "TimeoutError" in result["candidate"]["error"]
    assert (result["candidate"].get("autobuild") or {}).get("reason") != TIME_LIMIT_REASON


@pytest.mark.asyncio
async def test_autobuild_keeps_queued_work_after_old_total_limit_until_user_stop(api_env, monkeypatch):
    owner = api_env[3]
    scheduler = RuntimeScheduler(limit=1)
    holder = await scheduler.acquire(kind="model")
    model = LocalClient()
    monkeypatch.setattr(server, "APP_RUNTIME_SCHEDULER", scheduler)
    monkeypatch.setattr(server, "_new_orchestration_client", lambda *a: model)
    monkeypatch.setattr(orch_drafts, "MAX_SECONDS", .08)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app()), base_url="http://test") as api:
        try:
            await api.post(API + "/update", json=owner)
            await asyncio.sleep(.12)
            current = (await api.get(API, params=owner)).json()
            assert current['running'] and model.calls == 0
            assert scheduler.snapshot()['queued'] == 1
            assert (await api.post(API + '/action', json={**owner, 'action': 'stop'})).status_code == 200
            final = await poll_auto(api, owner)
            assert not final['running'] and final['candidate']['status'] == 'paused'
            assert model.calls == 0 and scheduler.snapshot()["queued"] == 0
        finally:
            await scheduler.release(holder)
    assert scheduler.snapshot()["running"] == 0
