"""Keep a durable worker alive until its transaction finishes, even on cancel."""
import asyncio

from mewcode.runtime_workers import commit_call


async def durable_call(function, /, *args, **kwargs):
    worker = asyncio.create_task(commit_call(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Repeated cancellation must not abandon an in-flight transaction.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        worker.result()
        raise
