"""Bounded model waits without real network traffic or long wall-clock sleeps."""
import asyncio
import math
from unittest.mock import Mock

import pytest

import mewcode.client as client_module
from mewcode.agent import STREAM_RETRY_MAX_DELAY, _stream_retry_delay
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.runtime_scheduler import RuntimeScheduler
from mewcode.tools.base import StreamEnd, TextDelta


@pytest.mark.parametrize("retry_after", [None, -1, 0, 2, 60, 86400, "inf", "nan", "bad"])
def test_retry_delay_bounds_local_backoff_but_honors_provider_minimum(retry_after):
    error = RuntimeError("rate limited")
    error.retry_after = retry_after
    for attempt in range(1, 5):
        delay = _stream_retry_delay(attempt, error)
        minimum = retry_after if isinstance(retry_after, (int, float)) and retry_after > 0 else 0
        assert math.isfinite(delay) and max(0, minimum) <= delay <= max(STREAM_RETRY_MAX_DELAY, minimum)


@pytest.mark.parametrize("name,protocol", [("AnthropicClient", "anthropic"),
    ("OpenAIClient", "openai"), ("OpenAICompatClient", "openai-compat")])
def test_sdk_cannot_multiply_visible_retry_budget(monkeypatch, name, protocol):
    factory = Mock()
    monkeypatch.setattr(client_module, "AsyncAnthropic", factory)
    monkeypatch.setattr(client_module, "AsyncOpenAI", factory)
    config = ProviderConfig(name="test", protocol=protocol, base_url="http://localhost/v1", model="test", api_key="test")
    getattr(client_module, name)(config)
    assert factory.call_args.kwargs["max_retries"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stalled_model_releases_stream_and_slot_on_timeout_or_cancel(monkeypatch, cancel):
    scheduler = RuntimeScheduler(limit=1)
    monkeypatch.setattr(client_module, "GLOBAL_RUNTIME_SCHEDULER", scheduler)
    monkeypatch.setattr(client_module, "LLM_REQUEST_TIMEOUT", 10 if cancel else .025)
    closed = asyncio.Event()
    started = asyncio.Event()

    class Client:
        async def stream(self, *_args, **_kwargs):
            try:
                started.set()
                while True:
                    yield TextDelta("keepalive")
                    await asyncio.sleep(.001)
            finally:
                closed.set()

    async def consume():
        async for _ in client_module.scheduled_client_stream(Client(), ConversationManager()):
            pass

    task = asyncio.create_task(consume())
    await started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    assert closed.is_set()
    assert scheduler.snapshot()["running"] == scheduler.snapshot()["queued"] == 0


@pytest.mark.asyncio
async def test_request_deadline_does_not_expire_while_waiting_for_admission(monkeypatch):
    scheduler = RuntimeScheduler(limit=1)
    monkeypatch.setattr(client_module, "GLOBAL_RUNTIME_SCHEDULER", scheduler)
    monkeypatch.setattr(client_module, "LLM_REQUEST_TIMEOUT", .01)
    holder = await scheduler.acquire(kind="tool")

    class Client:
        async def stream(self, *_args, **_kwargs):
            yield StreamEnd(stop_reason="end")

    async def consume():
        return [event async for event in client_module.scheduled_client_stream(Client(), ConversationManager())]

    task = asyncio.create_task(consume())
    await asyncio.sleep(.025)
    assert not task.done()
    await scheduler.release(holder)
    assert len(await task) == 1
