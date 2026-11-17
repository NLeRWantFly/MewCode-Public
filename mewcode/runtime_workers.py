"""Bounded workers: disk scans cannot starve durable run commits."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import weakref
from concurrent.futures import ThreadPoolExecutor

_IO = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mewcode-io")
_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mewcode-commit")
_LIMITS = weakref.WeakKeyDictionary()


async def _run(pool, capacity, function, args, kwargs):
    loop = asyncio.get_running_loop()
    limits = _LIMITS.setdefault(loop, {})
    limit = limits.setdefault(pool, asyncio.Semaphore(capacity))
    await limit.acquire()
    call = functools.partial(function, *args, **kwargs)
    context = contextvars.copy_context()
    try:
        future = loop.run_in_executor(pool, context.run, call)
    except BaseException:
        limit.release()
        raise
    # Keep the slot until the OS worker finishes, including after cancellation.
    future.add_done_callback(lambda _: limit.release())
    return await asyncio.shield(future)


async def io_call(function, /, *args, **kwargs):
    return await _run(_IO, 16, function, args, kwargs)


async def commit_call(function, /, *args, **kwargs):
    return await _run(_WRITER, 16, function, args, kwargs)
