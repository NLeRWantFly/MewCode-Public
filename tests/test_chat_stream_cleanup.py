"""Real chat/Agent stream ownership with an offline, ContextVar-scoped client."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import gc

import pytest

from frontend import server
from mewcode.runtime_scheduler import RuntimeScheduler, current_runtime_scheduler
from mewcode.tools.base import TextDelta, StreamEnd
from mewcode.usage.service import current_usage_scope, usage_trace_scope
from tests.test_agent import MockLLMClient
from tests.test_continuation_chat import setup


class ScopedChatClient(MockLLMClient):
    def __init__(self, *, wait_before_output):
        super().__init__([])
        self.wait_before_output = wait_before_output
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.close_count = 0
        self.owner_tasks = []
        self.scope = ContextVar("offline-chat-provider-scope", default=None)

    async def stream(self, *_args, **_kwargs):
        self.calls += 1
        token = self.scope.set("provider-owned")
        entered = asyncio.current_task()
        self.started.set()
        try:
            if self.wait_before_output:
                await self.release.wait()
            yield TextDelta("offline partial response")
            await self.release.wait()
            yield StreamEnd("end_turn", input_tokens=1, output_tokens=1)
        finally:
            self.owner_tasks.append((entered, asyncio.current_task()))
            self.scope.reset(token)
            self.closed = True
            self.close_count += 1


class ConsumerSocket:
    def __init__(self, client, *, failure):
        self.client, self.failure = client, failure
        self.entered = asyncio.Event()
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)
        if payload.get("type") == "delta":
            self.entered.set()
            if self.failure == "consumer_cancel":
                await asyncio.Event().wait()
            if self.failure == "consumer_error":
                raise RuntimeError("offline consumer failed")
        elif payload.get("type") == "done":
            assert self.client.closed, "provider cleanup must finish before chat reports done"
            assert current_usage_scope().session_id == "outer-chat-owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("watchdog", [False, True], ids=["direct-stream", "watchdog-stream"])
@pytest.mark.parametrize("failure", ["provider_cancel", "consumer_cancel", "consumer_error"])
async def test_real_chat_closes_scoped_stream_before_terminal_event(tmp_path, monkeypatch, watchdog, failure):
    _, runtime, _ = setup(tmp_path, [])
    client = ScopedChatClient(wait_before_output=failure == "provider_cancel")
    runtime.client = runtime.agent.client = client
    runtime.agent.provider_watchdog = watchdog
    runtime.agent.session_id = runtime.conversation_id
    scheduler = RuntimeScheduler(limit=1)
    runtime.agent.runtime_scheduler = scheduler
    socket = ConsumerSocket(client, failure=failure)
    monkeypatch.setattr(server.SESSION, "_mcp_init_task", None, raising=False)
    loop = asyncio.get_running_loop()
    prior_handler, cleanup_errors = loop.get_exception_handler(), []
    loop.set_exception_handler(lambda _loop, context: cleanup_errors.append(context))

    async def consume():
        before_scheduler = current_runtime_scheduler(scheduler)
        with usage_trace_scope(session_id="outer-chat-owner"):
            before_usage = current_usage_scope()
            result = await server.run_chat(socket, "解释一个离线变量", runtime=runtime)
            assert current_usage_scope() == before_usage
            assert current_runtime_scheduler(scheduler) is before_scheduler
            return result

    task = asyncio.create_task(consume())
    try:
        entered = client.started if failure == "provider_cancel" else socket.entered
        try:
            await asyncio.wait_for(entered.wait(), 5)
        except TimeoutError:
            pytest.fail(f"chat did not reach {failure}: task_done={task.done()}, "
                        f"calls={client.calls}, messages={socket.messages}, "
                        f"result={task.result() if task.done() else None}")
        if failure != "consumer_error":
            task.cancel()
        outcome = await asyncio.wait_for(task, 5)
        assert outcome["interrupted"] == (failure != "consumer_error")
        assert outcome["error"] == ("offline consumer failed" if failure == "consumer_error" else None)
        assert client.calls == client.close_count == 1
        assert client.closed and client.scope.get() is None
        assert all(entered is closed for entered, closed in client.owner_tasks)
        assert scheduler.snapshot()["running"] == scheduler.snapshot()["queued"] == 0
        assert sum(event.get("type") == "done" for event in socket.messages) == 1
        gc.collect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not cleanup_errors
    finally:
        client.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        loop.set_exception_handler(prior_handler)
        runtime.session.close()
