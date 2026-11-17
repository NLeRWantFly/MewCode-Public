"""Real scheduler plus an offline provider with ContextVar-owned cleanup."""
import asyncio
from contextlib import aclosing
from contextvars import ContextVar

import pytest

import mewcode.client as clients
from mewcode.client import ModelRequestTimeout, scheduled_client_stream
from mewcode.conversation import ConversationManager
from mewcode.llm_watchdog import ProviderStreamTimeout, watch_provider_stream
from mewcode.runtime_scheduler import RuntimeScheduler
from mewcode.tools.base import StreamEnd, StreamStatus, TextDelta, ThinkingDelta


class ScopedClient:
    def __init__(self, *, mode="normal", error=None, cleanup_gate=None):
        self.mode, self.error, self.cleanup_gate = mode, error, cleanup_gate
        self.calls = 0
        self.tasks = []
        self.scope = ContextVar("offline-provider-scope", default=None)
        self.release = asyncio.Event()
        self.waiting = asyncio.Event()
        self.third_event = asyncio.Event()
        self.closing = asyncio.Event()
        self.closed = False

    async def stream(self, _conversation, system="", tools=None):
        self.calls += 1
        token = self.scope.set("owned")
        self.tasks.append(asyncio.current_task())
        try:
            if self.mode == "silent":
                self.waiting.set()
                await self.release.wait()
            yield ThinkingDelta("reasoning") if self.mode == "reasoning" else TextDelta("first")
            if self.error is not None:
                raise self.error
            if self.mode in {"hold", "reasoning"}:
                self.waiting.set()
                await self.release.wait()
            if self.mode == "burst":
                for i in range(8):
                    if i == 1:
                        self.third_event.set()
                    self.tasks.append(asyncio.current_task())
                    yield TextDelta(str(i))
            self.tasks.append(asyncio.current_task())
            yield TextDelta("last")
            yield StreamEnd("end")
        finally:
            self.tasks.append(asyncio.current_task())
            self.closing.set()
            if self.cleanup_gate is not None:
                await self.cleanup_gate.wait()
            self.scope.reset(token)
            self.closed = True


@pytest.fixture
def pool(monkeypatch):
    scheduler = RuntimeScheduler(limit=1)
    monkeypatch.setattr(clients, "GLOBAL_RUNTIME_SCHEDULER", scheduler)
    yield scheduler
    assert scheduler.snapshot()["running"] == scheduler.snapshot()["queued"] == 0


def wrapped(client, *, request_timeout=5, **timeouts):
    return watch_provider_stream(
        scheduled_client_stream(client, ConversationManager(), request_timeout=request_timeout),
        first_timeout=timeouts.get("first_timeout", .5),
        reasoning_timeout=timeouts.get("reasoning_timeout", .5),
        idle_timeout=timeouts.get("idle_timeout", .5),
    )


def assert_closed(client):
    assert client.calls == 1 and client.closed
    assert len(set(client.tasks)) == 1
    assert client.scope.get() is None


@pytest.mark.asyncio
async def test_scheduler_stream_finishes_in_one_owner_task(pool):
    client = ScopedClient()
    events = [event async for event in wrapped(client)]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["first", "last"]
    assert isinstance(events[-1], StreamEnd)
    assert_closed(client)


@pytest.mark.asyncio
async def test_first_event_is_delivered_while_owner_is_still_running(pool):
    client = ScopedClient(mode="hold")
    stream = wrapped(client)
    try:
        async with asyncio.timeout(.1):
            event = await stream.__anext__()
        assert event.text == "first" and not client.closed
    finally:
        await stream.aclose()
    assert_closed(client)


@pytest.mark.asyncio
async def test_soft_idle_warning_preserves_original_stream(pool):
    client = ScopedClient(mode="hold")
    stream = wrapped(client, idle_timeout=.02)
    try:
        assert (await stream.__anext__()).text == "first"
        warning = await stream.__anext__()
        assert isinstance(warning, StreamStatus) and warning.stage == "provider_slow"
        assert not client.closed and client.calls == 1
        client.release.set()
        remaining = [event async for event in stream]
        assert [e.text for e in remaining if isinstance(e, TextDelta)] == ["last"]
    finally:
        await stream.aclose()
    assert_closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,phase", [("silent", "first_event"), ("reasoning", "reasoning"), ("hold", "output")])
async def test_watchdog_hard_timeout_closes_owned_scope(pool, mode, phase):
    client = ScopedClient(mode=mode)
    with pytest.raises(ProviderStreamTimeout) as caught:
        async for _ in wrapped(client, first_timeout=.02, reasoning_timeout=.02, idle_timeout=.01):
            pass
    assert caught.value.phase == phase
    assert caught.value.retryable is (phase == "first_event")
    assert_closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("offline provider failed"), TimeoutError("provider-owned timeout")])
async def test_provider_error_identity_is_preserved(pool, error):
    client = ScopedClient(error=error)
    with pytest.raises(type(error)) as caught:
        async for _ in wrapped(client):
            pass
    assert caught.value is error
    assert_closed(client)


@pytest.mark.asyncio
async def test_total_request_timeout_keeps_its_type(pool):
    client = ScopedClient(mode="hold")
    with pytest.raises(ModelRequestTimeout):
        async for _ in wrapped(client, request_timeout=.025):
            pass
    assert_closed(client)


@pytest.mark.asyncio
async def test_request_timeout_while_consumer_is_backpressured(pool):
    client = ScopedClient(mode="burst")
    stream = wrapped(client, request_timeout=.03)
    try:
        assert (await stream.__anext__()).text == "first"
        await asyncio.wait_for(client.third_event.wait(), .5)
        await asyncio.sleep(.06)
        with pytest.raises(ModelRequestTimeout):
            async for _ in stream:
                pass
    finally:
        await stream.aclose()
    assert_closed(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["hold", "burst"])
async def test_early_close_finishes_owner_cleanup(pool, mode):
    client = ScopedClient(mode=mode)
    stream = wrapped(client)
    assert (await stream.__anext__()).text == "first"
    await asyncio.wait_for((client.waiting if mode == "hold" else client.third_event).wait(), .5)
    await asyncio.wait_for(stream.aclose(), .5)
    assert_closed(client)


@pytest.mark.asyncio
async def test_consumer_cancellation_cleans_pending_read(pool):
    client = ScopedClient(mode="hold")

    async def consume():
        async for _ in wrapped(client):
            pass

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(client.waiting.wait(), .5)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert_closed(client)


@pytest.mark.asyncio
async def test_consumer_cancellation_cleans_full_queue(pool):
    client = ScopedClient(mode="burst")

    async def consume():
        async with aclosing(wrapped(client)) as stream:
            await stream.__anext__()
            await client.third_event.wait()
            await asyncio.Event().wait()

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(client.third_event.wait(), .5)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert_closed(client)


@pytest.mark.asyncio
async def test_consumer_cancellation_removes_queued_admission(pool):
    holder = await pool.acquire(kind="model")
    client = ScopedClient()

    async def consume():
        async for _ in wrapped(client):
            pass

    consumer = asyncio.create_task(consume())
    try:
        for _ in range(20):
            if pool.snapshot()["queued"]:
                break
            await asyncio.sleep(0)
        assert pool.snapshot()["queued"] == 1 and client.calls == 0
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert pool.snapshot()["queued"] == 0 and client.calls == 0
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await pool.release(holder)


@pytest.mark.asyncio
async def test_repeated_cancel_cannot_interrupt_async_owner_cleanup(pool):
    cleanup_gate = asyncio.Event()
    client = ScopedClient(mode="hold", cleanup_gate=cleanup_gate)

    async def consume():
        async for _ in wrapped(client):
            pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(client.waiting.wait(), .5)
        consumer.cancel()
        await asyncio.wait_for(client.closing.wait(), .5)
        consumer.cancel()
        await asyncio.sleep(0)
        assert not client.closed
        cleanup_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert_closed(client)
    finally:
        cleanup_gate.set()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
async def test_agent_watchdog_executes_declared_tool_once(pool, tmp_path):
    from mewcode.agent import Agent, ToolResultEvent
    from mewcode.tools import create_default_registry
    from mewcode.tools.base import ToolCallComplete
    from tests.test_agent import MockLLMClient

    source = tmp_path / "source.txt"
    source.write_text("checked offline source", encoding="utf-8")

    class ToolClient(MockLLMClient):
        calls = 0
        tasks = []
        scope = ContextVar("offline-tool-provider-scope", default=None)

        async def stream(self, *args, **kwargs):
            self.calls += 1
            token = self.scope.set("owned")
            started = asyncio.current_task()
            try:
                if self.calls == 1:
                    yield ToolCallComplete("read-once", "ReadFile", {"file_path": str(source)})
                else:
                    yield TextDelta("done")
                yield StreamEnd("end_turn", input_tokens=1, output_tokens=1)
            finally:
                self.tasks.append((started, asyncio.current_task()))
                self.scope.reset(token)

    client = ToolClient([])
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(tmp_path))
    agent.provider_watchdog = True
    agent.runtime_scheduler = pool
    conversation = ConversationManager()
    conversation.add_user_message("Read source")
    events = []
    async with asyncio.timeout(2):
        async for event in agent.run(conversation):
            events.append(event)
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert client.calls == 2 and len(results) == 1
    assert not results[0].is_error
    assert "checked offline source" in results[0].output
    assert all(started is finished for started, finished in client.tasks)
    assert client.scope.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("watchdog", [False, True])
async def test_agent_early_close_closes_model_in_its_original_owner(pool, tmp_path, watchdog):
    from mewcode.agent import Agent, StreamText
    from mewcode.tools import create_default_registry
    from mewcode.usage.service import current_usage_scope, usage_trace_scope
    from tests.test_agent import MockLLMClient

    class AgentScopedClient(MockLLMClient, ScopedClient):
        stream = ScopedClient.stream

        def __init__(self):
            MockLLMClient.__init__(self, [])
            ScopedClient.__init__(self, mode="hold")

    client = AgentScopedClient()
    agent = Agent(client, create_default_registry(), "anthropic", work_dir=str(tmp_path))
    agent.provider_watchdog = watchdog
    agent.runtime_scheduler = pool
    agent.session_id = "offline-agent-owner"
    conversation = ConversationManager()
    conversation.add_user_message("Explain an offline variable")
    with usage_trace_scope(session_id="outer-agent-owner"):
        before = current_usage_scope()
        async with aclosing(agent.run(conversation)) as stream:
            async for event in stream:
                if isinstance(event, StreamText):
                    break
        assert current_usage_scope() == before
    assert_closed(client)


@pytest.mark.asyncio
async def test_consumer_cancel_during_request_timeout_waits_for_cleanup(pool):
    cleanup_gate = asyncio.Event()
    client = ScopedClient(mode="hold", cleanup_gate=cleanup_gate)

    async def consume():
        async for _ in wrapped(client, request_timeout=.02):
            pass

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(client.closing.wait(), .5)
        consumer.cancel()
        await asyncio.sleep(.01)
        assert not consumer.done(), "consumer must await the timeout's existing cleanup"
        cleanup_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert_closed(client)
    finally:
        cleanup_gate.set()
        await asyncio.gather(consumer, return_exceptions=True)
