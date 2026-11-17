"""Provider-stream timeouts only: never time a tool or a human wait."""
from __future__ import annotations

import asyncio

from mewcode.client import LLMError
from mewcode.tools.base import TextDelta, ToolCallStart, ToolCallDelta, ToolCallComplete, ThinkingDelta, ThinkingComplete, StreamStatus

# Allow upstream queues and cold starts without weakening output-idle checks.
FIRST_EVENT_TIMEOUT = 120.0
REASONING_IDLE_TIMEOUT = 360.0
STREAM_IDLE_TIMEOUT = 60.0

class ProviderStreamTimeout(LLMError):
    """Transport interruption, not a failed business acceptance or loop budget."""
    retryable = False
    max_retries = 2
    def __init__(self, phase, seconds):
        self.phase, self.seconds = phase, seconds
        # Only a request that yielded no event can be retried automatically.
        # Partial output/tool-call streams require checkpoint reconciliation.
        self.retryable = phase == "first_event"
        super().__init__(f"provider stalled: no response activity for {seconds:g}s (phase={phase}; resumable stream interruption)")


class ProviderEventTimeout(TimeoutError):
    """Only the local wait for the next event expired."""


async def next_provider_event(iterator, timeout):
    # A scheduled async generator owns ContextVar/timeout scopes across yields.
    # wait_for creates a fresh Task for every pull, breaking those scopes during
    # normal completion and cancellation. Keep the pull in the consumer's Task.
    try:
        async with asyncio.timeout(timeout) as deadline:
            return await iterator.__anext__()
    except TimeoutError as exc:
        if deadline.expired():
            raise ProviderEventTimeout() from exc
        raise


async def watch_provider_stream(stream, *, first_timeout=FIRST_EVENT_TIMEOUT,
                                reasoning_timeout=REASONING_IDLE_TIMEOUT, idle_timeout=STREAM_IDLE_TIMEOUT):
    iterator = stream.__aiter__()
    events = asyncio.Queue(maxsize=1)

    async def produce():
        # Scheduler leases and request deadlines span generator yields. One
        # Task must enter, consume and close those scopes for the entire stream.
        try:
            async for event in iterator:
                try:
                    await events.put(event)
                except asyncio.CancelledError as exc:
                    # A request's total deadline can cancel this Task while it
                    # is publishing an event rather than pulling the generator.
                    # Let the suspended timeout scope interpret that cancellation
                    # before closing; aclose alone would inject GeneratorExit.
                    throw = getattr(iterator, "athrow", None)
                    if throw is not None:
                        await throw(exc)
                    raise
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    owner = asyncio.create_task(produce(), name="provider-stream-owner")
    phase = "first_event"
    pending = None

    async def wait_event(timeout):
        done, _ = await asyncio.wait(
            {pending, owner}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
        if pending.done():
            return pending.result()
        if owner in done:
            if not events.empty():
                return await pending
            owner.result()  # Preserve provider errors and request total limits.
            raise StopAsyncIteration
        raise ProviderEventTimeout()

    try:
        while True:
            soft = first_timeout if phase == "first_event" else reasoning_timeout if phase == "reasoning" else idle_timeout
            # Output silence is first a warning. Preserve the same in-flight
            # request for a bounded grace period; never resubmit it blindly.
            hard = soft if phase in {"first_event", "reasoning"} else soft * 3
            deadline = asyncio.get_running_loop().time() + hard
            # Only the queue read runs in a temporary Task. The model generator
            # stays in its original owner, including during soft idle warnings.
            pending = asyncio.create_task(events.get())
            try:
                try:
                    event = await wait_event(soft)
                except ProviderEventTimeout:
                    if hard <= soft:
                        raise ProviderStreamTimeout(phase, hard) from None
                    yield StreamStatus(stage="provider_slow", detail=f"模型流暂时无新事件，保留原请求等待（最多 {hard:g} 秒）；未重试、未判定验收失败。")
                    try:
                        event = await wait_event(max(0, deadline - asyncio.get_running_loop().time()))
                    except ProviderEventTimeout:
                        raise ProviderStreamTimeout(phase, hard) from None
                pending = None
            except StopAsyncIteration:
                return
            if isinstance(event, (ThinkingDelta, ThinkingComplete)) or isinstance(event, StreamStatus) and "reason" in event.stage:
                phase = "reasoning"
            elif isinstance(event, (TextDelta, ToolCallStart, ToolCallDelta, ToolCallComplete)):
                phase = "output"
            elif phase == "first_event":
                phase = "reasoning"
            yield event
    finally:
        cleanup_tasks = [owner]
        if pending is not None:
            if not pending.done():
                pending.cancel()
            cleanup_tasks.append(pending)
        if not owner.done() and not owner.cancelling():
            owner.cancel()
        # Repeated consumer cancellation must not interrupt the owner's async
        # cleanup or leave it running after this generator has been closed.
        finished = asyncio.gather(*cleanup_tasks, return_exceptions=True)
        cancelled = None
        while not finished.done():
            try:
                await asyncio.shield(finished)
            except asyncio.CancelledError as exc:
                cancelled = exc
        if cancelled is not None:
            raise cancelled
