"""Long draft recovery: real stream lifecycle and durable API checkpoints."""
import asyncio
from contextvars import ContextVar
import json

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.orchestration import extract
from mewcode.orchestration.draft_sources import digest, next_chunk, estimate_chunks
from mewcode.orchestration.draft_store import DraftStore
from mewcode.runtime_scheduler import RuntimeScheduler
from mewcode.tools.base import TextDelta, ThinkingDelta, StreamEnd, StreamStatus
from tests.test_workflow_drafts import api_env, poll, write_history, record, entry, cited


@pytest.fixture(autouse=True)
def stream_scheduler(monkeypatch):
    scheduler = RuntimeScheduler(limit=1)
    monkeypatch.setattr("mewcode.client.current_runtime_scheduler", lambda fallback: scheduler)
    yield scheduler
    assert scheduler.snapshot()["running"] == scheduler.snapshot()["queued"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [" \n", "{"])
async def test_current_reasoning_phase_overrides_earlier_text(monkeypatch, prefix):
    waits = []

    async def stream(*args, **kwargs):
        yield TextDelta(prefix)
        yield ThinkingDelta("PRIVATE_REASONING")
        yield StreamStatus("activity", "heartbeat")
        yield TextDelta('{"ok":true}' if prefix.isspace() else '"ok":true}')
        yield StreamEnd("stop")

    async def next_event(iterator, timeout):
        waits.append(timeout)
        return await anext(iterator)

    monkeypatch.setattr(extract, "scheduled_client_stream", stream)
    monkeypatch.setattr(extract, "next_provider_event", next_event)
    events = []
    result = await extract.collect_model_text(object(), system="", user_text="",
        first_event_timeout=20, reasoning_idle_timeout=30, idle_timeout=10,
        progress=events.append)
    assert json.loads(result) == {"ok": True}
    assert waits[1] == (30 if prefix.isspace() else 10)
    assert waits[2:4] == [30, 30]
    assert "PRIVATE_REASONING" not in json.dumps(events)


class ScopedClient:
    def __init__(self, *, block=False, end="stop"):
        self.context = ContextVar("draft-stream", default=None)
        self.tasks = []
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block, self.end = block, end

    async def stream(self, *args, **kwargs):
        token = self.context.set("owned")
        self.tasks.append(asyncio.current_task())
        try:
            yield ThinkingDelta("reasoning")
            self.started.set()
            if self.block:
                await self.release.wait()
            self.tasks.append(asyncio.current_task())
            yield TextDelta('{"ok":true}')
            yield StreamEnd(self.end)
        finally:
            self.tasks.append(asyncio.current_task())
            self.context.reset(token)
            self.closed = True


@pytest.mark.asyncio
async def test_real_scheduled_stream_uses_one_context_and_closes():
    client = ScopedClient()
    assert await extract.collect_model_text(client, system="", user_text="") == '{"ok":true}'
    assert client.closed and len(set(client.tasks)) == 1
    assert client.context.get() is None


@pytest.mark.asyncio
async def test_cancel_closes_real_stream_without_retry():
    client = ScopedClient(block=True)
    task = asyncio.create_task(extract.collect_model_text(client, system="", user_text=""))
    await asyncio.wait_for(client.started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed and len(set(client.tasks)) == 1


@pytest.mark.asyncio
async def test_soft_warning_preserves_the_same_request():
    client = ScopedClient(block=True)
    events = []

    def progress(event):
        events.append(event)
        if event.get("waiting"):
            client.release.set()

    result = await extract.collect_model_text(client, system="", user_text="",
        progress=progress, soft_idle_warning=0.01, reasoning_idle_timeout=1)
    assert result == '{"ok":true}'
    assert any(e.get("waiting") for e in events)
    assert not any(e["stage"] == "stalled" for e in events)
    assert client.closed and len(set(client.tasks)) == 1


@pytest.mark.asyncio
async def test_real_stream_timeout_cleans_up_in_its_own_context():
    client = ScopedClient(block=True)
    with pytest.raises(extract.OrchestrationStreamTimeout) as caught:
        await extract.collect_model_text(client, system="", user_text="", reasoning_idle_timeout=0.01)
    assert caught.value.phase == "reasoning_idle"
    assert client.closed and len(set(client.tasks)) == 1


@pytest.mark.asyncio
async def test_incomplete_response_is_not_a_successful_json():
    client = ScopedClient(end="length")
    with pytest.raises(extract.OrchestrationGenerationLimit):
        await extract.collect_model_text(client, system="", user_text="")
    assert client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", ["total", "characters"])
async def test_generation_limits_close_the_request(budget):
    client = ScopedClient(block=True)
    limits = {"total_timeout": 0.02} if budget == "total" else {"max_generated_chars": 2}
    with pytest.raises(extract.OrchestrationGenerationLimit):
        await extract.collect_model_text(client, system="", user_text="", **limits)
    assert client.closed and len(set(client.tasks)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["stop", "max_tokens"])
async def test_unlimited_characters_preserve_full_output_but_reject_provider_truncation(monkeypatch, end):
    text = json.dumps({"result": "x" * 250_000})
    async def stream(*args, **kwargs):
        yield ThinkingDelta("r" * 200_001)
        yield TextDelta(text)
        yield StreamEnd(end)
    monkeypatch.setattr(extract, "scheduled_client_stream", stream)
    if end == "stop":
        assert await extract.collect_model_text(object(), system="", user_text="", max_generated_chars=None) == text
    else:
        with pytest.raises(extract.OrchestrationGenerationLimit):
            await extract.collect_model_text(object(), system="", user_text="", max_generated_chars=None)


def test_source_limit_matches_estimates_without_changing_sources():
    sources = [{"id": str(n), "text": "长文本" * 500} for n in range(9)]
    before, processed, batches = digest(sources), [], []
    while batch := next_chunk(sources, processed, source_limit=2):
        assert len(batch) <= 2
        batches.append(batch)
        processed += [s["id"] for s in batch]
    assert len(batches) == estimate_chunks(sources, [], source_limit=2) == 5
    assert digest(sources) == before and processed == [s["id"] for s in sources]


def patch_for(batch):
    return json.dumps({"upserts": [cited(entry("n_" + batch[0]["id"]), batch[0])],
        "ignored": [{"source_id": s["id"], "reason": "相同要求"} for s in batch[1:]]})


@pytest.mark.asyncio
async def test_timeout_halves_batch_and_covers_all_original_sources(api_env, monkeypatch):
    _, plans, path, owner = api_env
    write_history(path, [record("user", f"要求 {n}：" + "完整保留" * 250) for n in range(9)])
    original = path.read_bytes()
    calls, successful = [], []

    async def model(client, **kwargs):
        assert client == ("chosen", "high")
        batch = json.loads(kwargs["user_text"])["sources"]
        calls.append(len(batch))
        if len(batch) > 2:
            raise extract.OrchestrationStreamTimeout(phase="reasoning_idle", timeout_seconds=360)
        successful.extend(s["id"] for s in batch)
        return patch_for(batch)

    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.post("/api/orchestration/draft/update", json=owner)).status_code == 202
        state = await poll(client, owner)
        assert state["candidate"]["status"] == "ready", state["candidate"].get("error")
        assert (await client.post("/api/orchestration/draft/graph", json=owner)).status_code == 409
    saved = DraftStore(owner["project"], "paper").get()["candidate"]
    assert calls[:3] == [9, 4, 2] and all(n <= 2 for n in calls[2:])
    assert saved["chunk_source_limit"] == 2
    assert saved["processed_ids"] == successful and len(successful) == len(set(successful)) == 9
    assert [e["status"] for e in saved["log"]][:2] == ["retry", "retry"]
    assert state["coverage"]["pending"] == 0 and state["revision"] == 0
    assert path.read_bytes() == original and plans.get(owner["project"]) is None


@pytest.mark.asyncio
async def test_repeated_stall_is_bounded_and_resumes_persisted_smaller_batch(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", f"第 {n} 条要求") for n in range(9)])
    failing, calls = True, []

    async def model(client, **kwargs):
        batch = json.loads(kwargs["user_text"])["sources"]
        calls.append(len(batch))
        if failing:
            raise extract.OrchestrationStreamTimeout(phase="stream_idle", timeout_seconds=180)
        return patch_for(batch)

    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post("/api/orchestration/draft/update", json=owner)
        state = await poll(client, owner)
        assert len(calls) == orch_drafts.CHUNK_ATTEMPTS == 3
        assert state["candidate"]["status"] == "failed" and state["coverage"]["processed"] == 0
        assert DraftStore(owner["project"], "paper").get()["candidate"]["chunk_source_limit"] == 2
        failing = False
        resumed = await client.post("/api/orchestration/draft/update", json=owner)
        assert resumed.json()["candidate"]["status"] == "working"
        assert resumed.json()["candidate"]["error"] == ""
        assert any(e["status"] == "failed" for e in resumed.json()["candidate"]["log"])
        state = await poll(client, owner)
    assert calls[3] == 2 and state["candidate"]["status"] == "ready"
    assert state["coverage"]["pending"] == 0


@pytest.mark.asyncio
async def test_real_collector_api_cancel_and_resume_smaller_batch(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", f"要求 {n}：" + "正文" * 600) for n in range(9)])

    class Client:
        def __init__(self):
            self.block = True
            self.started = asyncio.Event()
            self.calls, self.closed = [], []
            self.context = ContextVar("draft-api-stream", default=None)

        async def stream(self, conversation, system="", tools=None):
            assert tools is None
            token = self.context.set("bound")
            batch = json.loads(conversation.history[-1].content)["sources"]
            self.calls.append(len(batch))
            try:
                yield TextDelta(" \n")
                yield ThinkingDelta("PRIVATE_REASONING")
                if len(batch) > 4:
                    yield StreamEnd("length")
                    return
                if self.block:
                    self.started.set()
                    await asyncio.Event().wait()
                yield TextDelta(patch_for(batch))
                yield StreamEnd("stop")
            finally:
                self.context.reset(token)
                self.closed.append(len(batch))

    model = Client()
    monkeypatch.setattr(server, "_new_orchestration_client", lambda c, e: model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        assert (await client.post("/api/orchestration/draft/update", json=owner)).status_code == 202
        await asyncio.wait_for(model.started.wait(), 2)
        assert model.calls == [9, 4] and model.closed == [9]
        response = await client.post("/api/orchestration/draft/action", json={**owner, "action": "stop"})
        assert response.status_code == 200, response.text
        state = response.json()
        assert state["candidate"]["status"] == "paused" and state["running"] is False
        assert state["candidate"]["chunk_source_limit"] == 4
        assert state["coverage"]["processed"] == 0
        assert [e["status"] for e in state["candidate"]["log"]] == ["retry", "paused"]
        assert model.calls == model.closed == [9, 4]
        assert "PRIVATE_REASONING" not in json.dumps(state)
        model.block = False
        await client.post("/api/orchestration/draft/update", json=owner)
        state = await poll(client, owner)
        assert state["candidate"]["status"] == "ready", state["candidate"].get("error")
        assert state["coverage"]["processed"] == 9 and state["coverage"]["pending"] == 0
        assert state["revision"] == 0
        assert (await client.post("/api/orchestration/draft/graph", json=owner)).status_code == 409
    assert model.calls == model.closed == [9, 4, 4, 4, 1]


@pytest.mark.asyncio
async def test_six_figure_history_keeps_full_order_and_earliest_requirement(api_env, monkeypatch):
    _, _, path, owner = api_env
    write_history(path, [record("user", f"原始约束 {n} 不可删除。" + "完整保留" * 4000) for n in range(9)])
    original = path.read_bytes()
    calls, accepted = [], []

    async def model(client, **kwargs):
        batch = json.loads(kwargs["user_text"])["sources"]
        calls.append(len(batch))
        if len(batch) > 1:
            raise extract.OrchestrationGenerationLimit("合成输出上限")
        accepted.extend(s["id"] for s in batch)
        return json.dumps({"upserts": [cited(entry("n_" + batch[0]["id"], body=batch[0]["text"][:24]), batch[0])],
                           "ignored": []})

    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        await client.post("/api/orchestration/draft/update", json=owner)
        state = await poll(client, owner)
    store = DraftStore(owner["project"], "paper")
    sources = store.sources()
    assert sum(len(s["text"]) for s in sources) > 140000
    assert state["candidate"]["status"] == "ready", state["candidate"].get("error")
    assert state["coverage"]["pending"] == 0
    assert accepted == [s["id"] for s in sources]
    assert store.get()["candidate"]["processed_ids"] == accepted
    assert any("原始约束 0 不可删除" in e["body"] for e in state["candidate"]["document"]["entries"])
    assert calls[0] > 1 and all(size == 1 for size in calls[1:])
    assert path.read_bytes() == original
